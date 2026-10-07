"""`audit-kit std` 실행: 기준서(rules.toml)로 프로젝트를 검사하고 결과에 조항 번호를 붙인다.

단계: ① ruff(기준서 ruff.toml 강제) ② mypy(없는 모듈·속성만) ③ 직접 구현 AST 검사.
모든 Finding.rule 은 기준서 조항 ID(STD-04, EFF-03 …)이고, 도구 규칙 ID 는 extra["tool_rule"] 에 남는다.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import re
import sys
import tempfile
import time
from pathlib import Path

from audit_kit import tools
from audit_kit.config import AuditConfig, read_pyproject
from audit_kit.gitutil import changed_lines
from audit_kit.models import IGNORE, IMPROVE, Finding, ToolResult
from audit_kit.runner import run_module
from audit_kit.scope import ENV_PATTERNS, list_python_files

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from audit_kit.std import checks
from audit_kit.std.rules import (
    Rule,
    bundled,
    load_registry,
    load_rules,
    ruff_rule_for,
    rule_by_id,
    rule_for_custom,
)
from audit_kit.textio import read_text

SKIP_PATTERNS = ["*/fixtures/*", "fixtures/*", "*/.claude/*", ".claude/*"]
MYPY_RULES = {"import-not-found": "ERR-01", "attr-defined": "ERR-02"}
CLAUDE_MD_LIMIT = 200


def _finding(
    rule: Rule,
    tool: str,
    code: str,
    message: str,
    file: str | None,
    line: int | None,
) -> Finding:
    return Finding(
        tool=tool,
        category=rule.category,
        rule=rule.id,
        message=message,
        file=file,
        line=line,
        severity=rule.severity,
        evidence=f"[{rule.id}] {rule.title} — 근거: {rule.source}",
        extra={"clause": rule.id, "tool_rule": code},
    )


def project_files(cfg: AuditConfig) -> list:
    """검사할 .py 파일(상대 경로). 가상환경·빌드물·fixtures 는 뺀다."""
    files, _ = list_python_files(cfg.root)
    skip = ENV_PATTERNS + SKIP_PATTERNS + list(cfg.exclude_paths)
    return [f for f in files if not any(fnmatch.fnmatch(f, g) for g in skip)]


def parse_files(cfg: AuditConfig, files: list) -> tuple:
    """(rel -> ast, 구문 오류 파일 목록)."""
    parsed, broken = {}, []
    for rel in files:
        try:
            parsed[rel] = ast.parse(read_text(cfg.root / rel))
        except (SyntaxError, ValueError, OSError):
            broken.append(rel)
    return parsed, broken


def local_names(cfg: AuditConfig, files: list) -> set:
    """프로젝트 안에 있는 모든 모듈·폴더 이름 (import 검사에서 외부 도구로 보지 않는다).

    스크립트는 자기 폴더나 sys.path 에 추가한 폴더의 모듈을 import 하므로 깊이와 무관하게 이름만 본다.
    프로젝트 어디에도 없는 이름(깨진 import)만 남는다.
    """
    names = set(cfg.packages)
    for rel in files:
        *folders, filename = rel.split("/")
        names.update(folders)
        names.add(Path(filename).stem)
    return names


_PROJECT_RUFF_CANDIDATES = ("configs/ruff.toml", "ruff.toml", "pyproject.toml")
_IMMUTABLE_CALLS_RE = re.compile(r"(extend-immutable-calls\s*=\s*\[)(.*?)(\])", re.DOTALL)


def _project_extra_immutable_calls(root: Path) -> list[str]:
    """대상 프로젝트 자체 ruff 설정에 선언된 extend-immutable-calls 추가 항목만 가져온다.

    B008(함수 호출을 기본 인자로) 은 FastAPI Depends()/Query() 자체는 표준 번들
    ruff.toml 이 이미 예외 처리하지만, `Depends(require_role(...))` 처럼 프로젝트마다
    다른 이름의 자체 의존성 팩토리 함수가 Depends() 안에 중첩되면 ruff 가 그 안쪽 호출을
    별도로 또 잡는다(2026-09-29 실측 확인: extend-immutable-calls 에 바깥쪽 Depends 만
    있으면 안쪽 팩토리 호출은 여전히 걸림 — 최소 재현으로 직접 확인). 표준 자체에 프로젝트
    고유 함수명을 넣으면 다른 프로젝트에 의미 없는 항목이 쌓이므로, 대상 프로젝트가 자기
    설정에 선언한 것만 이 필드 하나에 한해 병합한다(select/ignore 등 나머지는 표준을 그대로
    강제 — 표준을 우회하는 범용 탈출구가 아니다).
    """
    for rel in _PROJECT_RUFF_CANDIDATES:
        path = root / rel
        if not path.exists():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if rel == "pyproject.toml":
            data = data.get("tool", {}).get("ruff", {})
        calls = data.get("lint", {}).get("flake8-bugbear", {}).get("extend-immutable-calls", [])
        if calls:
            return [str(c) for c in calls]
    return []


def effective_ruff_config(root: Path, bundled_config: Path) -> Path:
    """번들 ruff.toml 에 대상 프로젝트의 extend-immutable-calls 추가 항목만 병합한 임시
    파일 경로를 반환한다. 추가 항목이 없으면 번들 경로를 그대로 돌려준다(임시 파일 없음)."""
    extra = _project_extra_immutable_calls(root)
    if not extra:
        return bundled_config
    text = bundled_config.read_text(encoding="utf-8")
    m = _IMMUTABLE_CALLS_RE.search(text)
    if not m:
        return bundled_config
    existing = [c.strip().strip('"') for c in m.group(2).split(",") if c.strip()]
    merged = list(dict.fromkeys([*existing, *extra]))
    replacement = (
        m.group(1) + "\n    " + ",\n    ".join(f'"{c}"' for c in merged) + ",\n" + m.group(3)
    )
    merged_text = text[: m.start()] + replacement + text[m.end() :]
    with tempfile.NamedTemporaryFile(
        "w", suffix=".toml", delete=False, encoding="utf-8", prefix="audit-kit-ruff-merged-"
    ) as tf:
        tf.write(merged_text)
    return Path(tf.name)


# ---------------------------------------------------------------- ① ruff
def run_std_ruff(cfg: AuditConfig, rules: list, files: list, ruff_config: Path) -> ToolResult:
    t0 = time.perf_counter()
    targets = sorted({f.split("/")[0] for f in files})
    args = [
        "check",
        "--output-format",
        "json",
        "--exit-zero",
        "--no-cache",
        "--config",
        str(ruff_config),
    ]
    p = run_module("ruff", [*args, *targets], cfg.root)
    if p.missing:
        return ToolResult("std-ruff", "skipped", detail=p.stderr)
    if p.returncode != 0 or not p.stdout.strip():
        # --exit-zero 라 위반이 있어도 0 이고, JSON 출력은 0건이어도 "[]" 다. 종료코드가 0 이 아니거나
        # (2=설정·내부 오류, -1=시간 초과) 출력이 비면 검사를 못 돈 것이므로 '0건 통과'가 아니라
        # 오류로 보고한다(ERR-10, 2026-10-08).
        detail = f"ruff 종료코드 {p.returncode}, 출력 없음 또는 비정상: " + p.stderr[-1500:]
        return ToolResult("std-ruff", "error", detail=detail)
    try:
        items = json.loads(p.stdout)
    except json.JSONDecodeError:
        return ToolResult("std-ruff", "error", detail=(p.stderr or p.stdout)[-1500:])
    scanned = set(files)
    out = [
        f for it in items if (f := _ruff_finding(cfg, rules, it)) is not None and f.file in scanned
    ]
    return ToolResult(
        "std-ruff", "findings" if out else "ok", out, duration=time.perf_counter() - t0
    )


def _ruff_finding(cfg: AuditConfig, rules: list, item: dict) -> Finding | None:
    file = cfg.rel(item.get("filename", ""))
    code = item.get("code") or ""
    if not file.endswith(".py"):  # ruff 가 설정 파일(ruff.toml 등)까지 검사하는 경우 제외
        return None
    line = (item.get("location") or {}).get("row")
    rule = ruff_rule_for(rules, code)
    if (
        rule is None
    ):  # 조항이 없는 ruff 규칙(서식·정렬 등)은 저장 시 hook 이 다루므로 보고서에서는 무시 등급
        return Finding(
            tool="std-ruff",
            category="스타일",
            rule=code or "ruff",
            message=item.get("message", ""),
            file=file,
            line=line,
            severity=IGNORE,
            evidence="기준서 조항 없음(ruff.toml 규칙)",
            extra={"tool_rule": code},
        )
    return _finding(rule, "std-ruff", code, f"{code}: {item.get('message', '')}", file, line)


# ---------------------------------------------------------------- ② mypy
def run_std_mypy(cfg: AuditConfig, rules: list) -> ToolResult:
    res = tools.run_mypy(cfg)
    if res.status in {"skipped", "error"}:
        return ToolResult("std-mypy", res.status, detail=res.detail)
    out = []
    for f in res.findings:
        rule = rule_by_id(rules, MYPY_RULES.get(f.rule, ""))
        if rule:
            finding = _finding(rule, "std-mypy", f.rule, f.message, f.file, f.line)
            if f.rule == "attr-defined" and not f.message.startswith("Module "):
                finding.severity = IMPROVE  # 타입 표기가 부족한 경우(예: ast.AST.lineno)는 환각 API 로 단정하지 않는다
            out.append(finding)
    return ToolResult("std-mypy", "findings" if out else "ok", out, duration=res.duration)


# ---------------------------------------------------------------- ③ 직접 구현 검사
def run_std_custom(cfg: AuditConfig, rules: list, registry: list, files: list) -> ToolResult:
    t0 = time.perf_counter()
    parsed, broken = parse_files(cfg, files)
    hits = [h for rel, tree in parsed.items() for h in checks.run_file_checks(tree, rel)]
    hits += checks.check_func_body_dup(parsed)
    hits += checks.check_scattered_parents_root(parsed)
    hits += checks.check_init_subpackage_reexport(parsed)
    hits += checks.check_pyinstaller_entry_relative_import(cfg.root)
    hits += checks.check_copy_filenames(files)
    hits += checks.check_root_tests(files)
    hits += checks.check_claude_md(cfg.root, CLAUDE_MD_LIMIT)
    hits += checks.check_pytest_settings(cfg.root)
    hits += checks.check_secret_scan(cfg.root, files)
    hits += checks.check_gitignore_secrets(cfg.root)
    reg_imports = {i for lib in registry for i in lib.get("imports", [])}
    declared = checks.declared_dependencies(
        read_pyproject(cfg.root)
    ) | checks.declared_requirements(cfg.root)
    hits += checks.check_release_config_present(cfg.root)
    hits += checks.check_error_tracking_sdk_present(cfg.root, declared)
    hits += checks.check_mkdocs_config_present(cfg.root)
    hits += checks.check_sonarqube_config_present(cfg.root)
    hits += checks.check_sbom_present(cfg.root)
    hits += checks.check_dependabot_config_present(cfg.root)
    hits += checks.check_runbook_present(cfg.root)
    hits += checks.check_structured_logging_present(declared)
    hits += checks.check_privacy_policy_present(cfg.root)
    hits += checks.check_apm_sdk_present(declared)
    hits += checks.check_deployment_doc_present(cfg.root)
    hits += checks.check_community_files_present(cfg.root)
    hits += checks.check_adr_dir_present(cfg.root)
    hits += checks.check_db_migration_risk_config_present(cfg.root)
    hits += checks.check_db_schema_drift_config_present(cfg.root)
    hits += checks.check_api_contract_test_config_present(cfg.root, declared)
    missing, gaps = checks.check_imports(parsed, local_names(cfg, files), reg_imports, declared)
    hits += missing + gaps
    out = []
    for h in hits:
        rule = _rule_for_hit(rules, h.check)
        if rule:
            finding = _finding(rule, "std-custom", h.check, h.message, h.file, h.line)
            finding.severity = h.severity or finding.severity
            out.append(finding)
    detail = (
        f"구문 오류로 검사하지 못한 파일 {len(broken)}개: {', '.join(broken[:3])}" if broken else ""
    )
    return ToolResult(
        "std-custom",
        "findings" if out else "ok",
        out,
        detail=detail,
        duration=time.perf_counter() - t0,
    )


def _rule_for_hit(rules: list, check: str) -> Rule | None:
    if check == "IMPORT-RESOLVE":
        return rule_by_id(rules, "ERR-01")
    if check == "REGISTRY-GAP":
        return rule_by_id(rules, "DOC-02")
    return rule_for_custom(rules, check)


# ---------------------------------------------------------------- 전체
def _mark_pre_existing(cfg: AuditConfig, results: list) -> None:
    """각 Finding 에 이번 변경(마지막 커밋 대비 작업 트리)으로 생긴 줄인지 표시한다(Anthropic
    Code Review 의 🟣Pre-existing 태깅과 같은 구분, 2026-09-28). git 정보가 없으면(저장소 아님 등)
    손대지 않는다 — 기본값 None(판정 불가) 그대로 둔다."""
    changed = changed_lines(cfg.root)
    if changed is None:
        return
    for r in results:
        for f in r.findings:
            if f.file and f.line:
                f.pre_existing = f.file not in changed or f.line not in changed[f.file]


def run_std(
    cfg: AuditConfig,
    rules_path: Path | None = None,
    ruff_config: Path | None = None,
    registry_path: Path | None = None,
    with_mypy: bool = True,
) -> list:
    rules = load_rules(rules_path)
    registry = load_registry(registry_path)
    files = project_files(cfg)
    base_ruff_config = ruff_config or bundled("ruff.toml")
    results = [run_std_ruff(cfg, rules, files, effective_ruff_config(cfg.root, base_ruff_config))]
    if with_mypy and cfg.packages:
        results.append(run_std_mypy(cfg, rules))
    results.append(run_std_custom(cfg, rules, registry, files))
    _mark_pre_existing(cfg, results)
    return results

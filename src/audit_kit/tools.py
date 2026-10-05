"""도구별 실행 + 결과 파싱 + 1차 심각도 부여.

각 함수는 ToolResult를 돌려준다. 심각도 규칙은 설계서 3장(치명/개선/무시)을 따른다.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from audit_kit.config import AuditConfig
from audit_kit.models import CRITICAL, IGNORE, IMPROVE, REVIEW, Finding, ToolResult
from audit_kit.runner import module_available, run_module, run_python
from audit_kit.scope import build_project_graph, get_scope
from audit_kit.textio import read_text


def _timed(fn):
    def wrapper(cfg, *a, **kw):
        t0 = time.perf_counter()
        res = fn(cfg, *a, **kw)
        res.duration = time.perf_counter() - t0
        return res

    wrapper.__name__ = fn.__name__
    return wrapper


def _targets(cfg: AuditConfig) -> list:
    """제품 코드 경로."""
    return [cfg.rel(p) for p in cfg.package_paths()]


def _all_targets(cfg: AuditConfig, skip_roles=()) -> list:
    """제품 + 보조 코드 경로 (skip_roles 역할 제외). 보조 코드도 버그·보안 검사 대상이다."""
    out = _targets(cfg)
    for role, tops in get_scope(cfg).support_roots().items():
        if role not in skip_roles:
            out += [t for t in tops if t not in out and (cfg.root / t).exists()]
    return out


def _starts(code: str, prefixes: list) -> bool:
    return any(code.startswith(p) for p in prefixes)


# ---------------------------------------------------------------- 0. 검사 범위
@_timed
def run_scope(cfg: AuditConfig) -> ToolResult:
    """모든 .py 파일을 분류하고, 저장소에 들어간 복사본·백업과 미지정 파일을 보고한다."""
    s = get_scope(cfg)
    out = []
    for why, files in sorted(s.junk.items()):
        tops = sorted({"/".join(f.split("/")[:3]) for f in files})
        out.append(
            Finding(
                tool="scope",
                category="저장소",
                rule="SCOPE-JUNK",
                message=f"{why}: {len(files)}개 파일 ({', '.join(tops[:3])}{' 외' if len(tops) > 3 else ''})",
                file=files[0],
                severity=IMPROVE,
                evidence="코드 사본이 저장소에 있으면 검색·리뷰·분석이 중복되고 옛 코드가 실수로 쓰일 수 있음. "
                "`git rm -r --cached <경로>` 후 .gitignore 에 추가",
                extra={"files": len(files)},
            )
        )
    if not (
        cfg.root / "architecture.toml"
    ).is_file():  # 설계 파일이 있으면 arch 검사가 같은 내용을 보고
        groups: dict = {}
        for f in s.unassigned:
            groups.setdefault(f.split("/")[0] if "/" in f else f, []).append(f)
        for top, files in sorted(groups.items()):
            out.append(
                Finding(
                    tool="scope",
                    category="구조",
                    rule="SCOPE-UNASSIGNED",
                    message=f"{top}: 파일 {len(files)}개가 제품 코드(packages)에도 보조 코드(tests/scripts/plugins/migrations)에도 속하지 않음",
                    file=files[0],
                    severity=REVIEW,
                    evidence="검사 범위 밖에 방치된 코드 — [tool.audit-kit] packages 또는 support 에 역할을 지정하거나 삭제",
                )
            )
    summary = " / ".join(s.summary())
    data = {
        "source": s.source,
        "total": s.total,
        "production": len(s.production),
        "support": {r: len(v) for r, v in s.support.items()},
        "unassigned": len(s.unassigned),
        "excluded": {k: len(v) for k, v in s.excluded.items()},
        "junk": {k: len(v) for k, v in s.junk.items()},
        "summary": summary,
    }
    return ToolResult("scope", "findings" if out else "ok", out, detail=summary, data=data)


# ---------------------------------------------------------------- 1. ruff
def ruff_severity(cfg: AuditConfig, code: str) -> str:
    if _starts(code, cfg.ruff_critical):
        return CRITICAL
    if _starts(code, cfg.ruff_ignore):
        return IGNORE
    return IMPROVE


# 프로젝트에 ruff 설정이 없을 때 쓰는 표준 규칙 (init 템플릿과 동일).
# 없으면 ruff 버전별 기본 규칙이 적용돼 버전에 따라 결과가 달라진다.
RUFF_STANDARD = ["--select", "E,W,F,I,B,UP,SIM,C4", "--ignore", "E501"]


def has_ruff_config(root: Path) -> bool:
    """root 또는 상위 폴더에 ruff 설정이 있는가 (ruff 자체 탐색 규칙과 동일하게 위로 올라감)."""
    for d in [root, *root.parents]:
        if (d / "ruff.toml").is_file() or (d / ".ruff.toml").is_file():
            return True
        pp = d / "pyproject.toml"
        if pp.is_file() and "[tool.ruff" in pp.read_text(encoding="utf-8", errors="ignore"):
            return True
    return False


@_timed
def run_ruff(cfg: AuditConfig, targets=None, fix: bool = False) -> ToolResult:
    targets = targets or _all_targets(cfg)
    args = ["check", "--output-format", "json", "--exit-zero", *targets]
    if not has_ruff_config(cfg.root):
        args[1:1] = RUFF_STANDARD
    if fix:
        args.insert(1, "--fix")
    p = run_module("ruff", args, cfg.root)
    if p.missing:
        return ToolResult("ruff", "skipped", detail=p.stderr)
    try:
        items = json.loads(p.stdout or "[]")
    except json.JSONDecodeError:
        return ToolResult("ruff", "error", detail=(p.stderr or p.stdout)[-2000:])
    out = []
    for it in items:
        code = it.get("code") or "E999"  # 구문 오류는 code가 비어 있음
        sev = ruff_severity(cfg, code)
        if sev == IGNORE:
            category = "스타일"
        elif _starts(code, ["UP", "SIM", "C4", "RET", "PIE", "PERF"]):
            category = "코드품질"
        else:
            category = "버그패턴"
        out.append(
            Finding(
                tool="ruff",
                category=category,
                rule=code,
                message=it.get("message", ""),
                file=cfg.rel(it["filename"]),
                line=(it.get("location") or {}).get("row"),
                severity=sev,
                evidence=f"ruff {code}" + (f" — {it['url']}" if it.get("url") else ""),
                extra={"fixable": bool(it.get("fix"))},
            )
        )
    return ToolResult("ruff", "findings" if out else "ok", out)


# ---------------------------------------------------------------- 2. mypy
MYPY_RE = re.compile(
    r"^(?P<file>.+?\.pyi?):(?P<line>\d+):(?:\d+:)?\s*(?P<kind>error|warning|note):\s*(?P<msg>.*?)(?:\s+\[(?P<code>[\w-]+)\])?$"
)


def parse_mypy(cfg: AuditConfig, text: str, only_file=None) -> list:
    out = []
    for line in text.splitlines():
        m = MYPY_RE.match(line.strip())
        if not m or m["kind"] == "note":
            continue
        f = cfg.rel(m["file"])
        if only_file and f != only_file:
            continue
        code = m["code"] or "mypy"
        # 없는 모듈 속성 임포트(from x import 없는함수)는 실행 시 ImportError/AttributeError
        missing_attr = code == "attr-defined" and m["msg"].startswith("Module ")
        critical = code in cfg.mypy_critical or missing_attr
        out.append(
            Finding(
                tool="mypy",
                category="타입" if not critical else "버그패턴",
                rule=code,
                message=m["msg"],
                file=f,
                line=int(m["line"]),
                severity=CRITICAL if critical else IMPROVE,
                evidence=f"mypy [{code}]"
                + (" — 실행 시 오류가 날 가능성이 높음" if critical else ""),
            )
        )
    return out


@_timed
def run_mypy(cfg: AuditConfig, targets=None) -> ToolResult:
    targets = targets or _targets(cfg)
    args = [
        "--show-error-codes",
        "--no-error-summary",
        "--no-color-output",
        "--no-pretty",
        "--hide-error-context",
        *targets,
    ]
    p = run_module("mypy", args, cfg.root)
    if p.missing:
        return ToolResult("mypy", "skipped", detail=p.stderr)
    out = parse_mypy(cfg, p.stdout)
    if p.returncode not in (0, 1) and not out:
        return ToolResult("mypy", "error", detail=(p.stderr or p.stdout)[-2000:])
    detail = ""
    blockers = [f for f in out if f.rule == "syntax"]
    if blockers:
        b = blockers[0]
        detail = (
            f"⚠ mypy 가 {b.location()} 구문 오류에서 중단 — 나머지 파일은 검사되지 않음. "
            "실제 문법 오류이거나, '# type: ...' 주석을 타입 주석으로 오인한 경우. 고친 뒤 다시 실행"
        )
        for f in blockers:
            f.evidence += " — 이 오류 때문에 mypy 전체 검사가 멈춤"
    return ToolResult("mypy", "findings" if out else "ok", out, detail=detail)


# ---------------------------------------------------------------- 3. import-linter
LINTER_EDGE_RE = re.compile(r"^-?\s*(?P<src>[\w.]+) -> (?P<dst>[\w.]+) \(l\.(?P<line>[\d, ]+)\)")


def _module_file(cfg: AuditConfig, module: str):
    for base in (cfg.root, cfg.root / "src"):
        p = base / module.replace(".", "/")
        if p.with_suffix(".py").is_file():
            return cfg.rel(p.with_suffix(".py"))
        if (p / "__init__.py").is_file():
            return cfg.rel(p / "__init__.py")
    return None


def parse_import_linter(cfg: AuditConfig, text: str) -> list:
    out = []
    lines = text.splitlines()
    in_broken = False
    contract = ""
    rule_line = ""
    for i, line in enumerate(lines):
        s = line.strip()
        if s == "Broken contracts":
            in_broken = True
            continue
        if not in_broken or not s:
            continue
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if nxt and set(nxt) == {"-"} and not set(s) <= {"-"}:
            contract = s
            continue
        if set(s) <= {"-"}:
            continue
        if s.endswith(":") and "->" not in s:
            rule_line = s.rstrip(":")
            continue
        m = LINTER_EDGE_RE.match(s)
        if m and s.startswith("-"):
            first_line = int(m["line"].split(",")[0])
            out.append(
                Finding(
                    tool="import-linter",
                    category="구조",
                    rule="LAYER",
                    message=f"{rule_line or '계약 위반'}: {m['src']} -> {m['dst']}",
                    file=_module_file(cfg, m["src"]),
                    line=first_line,
                    severity=REVIEW,
                    evidence=f"import-linter 계약 '{contract}' 위반 (설계 판단 필요 → AI 리뷰에서 등급 결정)",
                )
            )
    return out


def has_importlinter_config(cfg: AuditConfig) -> bool:
    from audit_kit.config import read_pyproject

    if "importlinter" in read_pyproject(cfg.root).get("tool", {}):
        return True
    return any(
        (cfg.root / n).is_file() for n in (".importlinter", "setup.cfg")
    ) and "[importlinter]" in "".join(
        (cfg.root / n).read_text(encoding="utf-8", errors="ignore")
        for n in (".importlinter", "setup.cfg")
        if (cfg.root / n).is_file()
    )


@_timed
def run_import_linter(cfg: AuditConfig) -> ToolResult:
    if not module_available("importlinter"):
        return ToolResult("import-linter", "skipped", detail="import-linter 미설치")
    if not has_importlinter_config(cfg):
        return ToolResult(
            "import-linter",
            "skipped",
            detail="계약 미정의 — pyproject.toml에 [tool.importlinter] 추가 필요 (audit-kit init 템플릿 참고)",
        )
    code = (
        "import sys; sys.argv=['lint-imports','--no-cache']; "
        "from importlinter.cli import lint_imports_command; lint_imports_command()"
    )
    p = run_python(["-c", code], cfg.root)
    text = p.stdout + "\n" + p.stderr
    out = parse_import_linter(cfg, text)
    if p.returncode != 0 and not out:
        if "BROKEN" in text:
            out.append(
                Finding(
                    tool="import-linter",
                    category="구조",
                    rule="LAYER",
                    message="계약 위반(상세 파싱 실패 — 원문 확인)",
                    severity=REVIEW,
                    evidence="import-linter",
                    extra={"raw": text[-3000:]},
                )
            )
        else:
            return ToolResult("import-linter", "error", detail=text[-2000:])
    return ToolResult("import-linter", "findings" if out else "ok", out)


# ---------------------------------------------------------------- 3'. 설계(architecture.toml) 검사
@_timed
def run_arch(cfg: AuditConfig) -> ToolResult:
    from audit_kit.arch.scan import scan
    from audit_kit.arch.spec import load_spec

    spec = load_spec(cfg.root)
    if spec is None:
        return ToolResult("arch", "skipped", detail="architecture.toml 없음")
    out = [v.to_finding() for v in scan(cfg, spec)]
    return ToolResult(
        "arch", "findings" if out else "ok", out, detail="" if out else "설계 위반 없음"
    )


# ---------------------------------------------------------------- 4. 순환 임포트 + 의존성 그래프
@_timed
def run_cycles(cfg: AuditConfig, report_dir=None, report_findings: bool = True) -> ToolResult:
    graph = build_project_graph(cfg)
    out = []
    for cyc in graph.cycles() if report_findings else []:
        first, second = cyc[0], cyc[1]
        out.append(
            Finding(
                tool="cycles",
                category="구조",
                rule="IMPORT-CYCLE",
                message="순환 임포트: " + " -> ".join(cyc),
                file=cfg.rel(graph.modules[first]),
                line=graph.edges[first].get(second),
                severity=IMPROVE,
                evidence="모듈 최상위 import 간 순환 (AST 분석). 임포트 순서에 따라 ImportError로 이어질 수 있음 — "
                "실제 임포트 실패는 pytest 수집 오류로 치명 처리됨",
            )
        )
    data: dict[str, object] = {
        "modules": len(graph.modules),
        "edges": sum(len(v) for v in graph.edges.values()),
    }
    if report_dir:
        mm = graph.mermaid()
        (Path(report_dir) / "deps.md").write_text(
            "# 모듈 의존성 그래프\n\n빨간 선 = 순환 임포트 경로. 숫자 = import 개수.\n\n```mermaid\n"
            + mm
            + "\n```\n",
            encoding="utf-8",
        )
        data["graph"] = "deps.md"
        svg = _pydeps_svg(cfg, Path(report_dir))
        if svg:
            data["pydeps"] = svg
    return ToolResult("cycles", "findings" if out else "ok", out, data=data)


def _pydeps_svg(cfg: AuditConfig, report_dir: Path):
    """pydeps + graphviz가 있으면 SVG도 만든다(선택)."""
    if not module_available("pydeps") or not shutil.which("dot"):
        return None
    made = []
    for pkg in cfg.package_paths():
        out = report_dir / f"pydeps_{pkg.stem}.svg"
        code = "import sys; from pydeps.pydeps import pydeps; sys.argv[0]='pydeps'; pydeps()"
        p = run_python(
            ["-c", code, cfg.rel(pkg), "--noshow", "--cluster", "--max-bacon", "2", "-o", str(out)],
            cfg.root,
            timeout=300,
        )
        if p.returncode == 0 and out.exists():
            made.append(out.name)
    return made or None


# ---------------------------------------------------------------- 5. vulture
VULTURE_RE = re.compile(
    r"^(?P<file>.+?\.py):(?P<line>\d+): (?P<msg>.+?) \((?P<conf>\d+)% confidence(?:, \d+ lines?)?\)$"
)


def vulture_args(cfg: AuditConfig, targets: list) -> list:
    args = [*targets]
    wl = cfg.root / cfg.vulture_whitelist
    if wl.is_file():
        args.append(cfg.rel(wl))
    if cfg.vulture_ignore_decorators:
        args += ["--ignore-decorators", ",".join(cfg.vulture_ignore_decorators)]
    if cfg.vulture_ignore_names:
        args += ["--ignore-names", ",".join(cfg.vulture_ignore_names)]
    return args


@_timed
def run_vulture(cfg: AuditConfig) -> ToolResult:
    # 테스트·스크립트의 사용처도 '사용'으로 셈하도록 함께 넘기고, 보고는 제품 코드만 한다
    args = vulture_args(cfg, _all_targets(cfg)) + [
        "--min-confidence",
        str(cfg.vulture_min_confidence),
    ]
    production = set(get_scope(cfg).production)
    p = run_module("vulture", args, cfg.root)
    if p.missing:
        return ToolResult("vulture", "skipped", detail=p.stderr)
    if p.returncode not in (0, 1, 3):
        return ToolResult("vulture", "error", detail=(p.stderr or p.stdout)[-2000:])
    out = []
    params_cache: dict = {}
    for line in p.stdout.splitlines():
        m = VULTURE_RE.match(line.strip())
        if not m or (production and cfg.rel(m["file"]) not in production):
            continue
        conf = int(m["conf"])
        name = re.search(r"unused variable '(\w+)'", m["msg"])
        if name and (name[1], int(m["line"])) in _function_params(
            cfg.root / m["file"], params_cache
        ):
            out.append(
                Finding(
                    tool="vulture",
                    category="죽은코드",
                    rule="V-ARG",
                    message=m["msg"] + " (함수 인자)",
                    file=cfg.rel(m["file"]),
                    line=int(m["line"]),
                    severity=IGNORE,
                    evidence="vulture — 함수 인자는 인터페이스 구현·FastAPI 의존성 주입으로 흔히 미사용",
                )
            )
            continue
        out.append(
            Finding(
                tool="vulture",
                category="죽은코드",
                rule=f"V{conf}",
                message=m["msg"],
                file=cfg.rel(m["file"]),
                line=int(m["line"]),
                severity=IMPROVE if conf >= cfg.vulture_improve_confidence else IGNORE,
                evidence=f"vulture 신뢰도 {conf}%"
                + (
                    ""
                    if conf >= cfg.vulture_improve_confidence
                    else " — 낮은 신뢰도, 오탐이면 화이트리스트 등록"
                ),
            )
        )
    return ToolResult("vulture", "findings" if out else "ok", out)


def _function_params(path: Path, cache: dict) -> set:
    """파일 안 모든 함수 매개변수의 (이름, 줄) 집합."""
    if path not in cache:
        import ast

        found = set()
        try:
            tree = ast.parse(read_text(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.arg):
                    found.add((node.arg, node.lineno))
        except (OSError, SyntaxError):
            pass
        cache[path] = found
    return cache[path]


def make_vulture_whitelist(cfg: AuditConfig) -> str:
    args = vulture_args(cfg, _targets(cfg)) + [
        "--make-whitelist",
        "--min-confidence",
        str(cfg.vulture_min_confidence),
    ]
    # 기존 화이트리스트를 입력에 넣으면 결과가 비므로 제외
    wl = cfg.rel(cfg.root / cfg.vulture_whitelist)
    args = [a for a in args if a != wl]
    p = run_module("vulture", args, cfg.root)
    return p.stdout


# ---------------------------------------------------------------- 6. radon
RANKS = "ABCDEF"


@_timed
def run_radon(cfg: AuditConfig) -> ToolResult:
    p = run_module("radon", ["cc", "-j", "-n", cfg.radon_min_rank, *_targets(cfg)], cfg.root)
    if p.missing:
        return ToolResult("radon", "skipped", detail=p.stderr)
    try:
        data = json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        return ToolResult("radon", "error", detail=(p.stderr or p.stdout)[-2000:])
    out = []
    min_idx = RANKS.index(cfg.radon_min_rank.upper())
    for file, blocks in data.items():
        if isinstance(blocks, dict):  # {"error": "..."}
            continue
        for b in blocks:
            if b.get("type") == "class" or RANKS.index(b["rank"]) < min_idx:
                continue
            name = f"{b['classname']}.{b['name']}" if b.get("classname") else b["name"]
            out.append(
                Finding(
                    tool="radon",
                    category="복잡도",
                    rule=f"CC-{b['rank']}",
                    message=f"{name}() 순환복잡도 {b['complexity']} (등급 {b['rank']}) — 함수 분리 검토",
                    file=cfg.rel(file),
                    line=b.get("lineno"),
                    severity=IMPROVE,
                    evidence=f"radon cc 등급 {b['rank']} (A~B 권장, 도메인 계산 로직은 예외 가능)",
                    extra={"complexity": b["complexity"]},
                )
            )
    out.sort(key=lambda f: -f.extra["complexity"])
    return ToolResult("radon", "findings" if out else "ok", out)


# ---------------------------------------------------------------- 7. bandit
def bandit_severity(cfg: AuditConfig, test_id: str, sev: str, conf: str) -> str:
    if test_id in cfg.bandit_critical:
        return CRITICAL
    if sev == "HIGH":
        return CRITICAL if conf in ("HIGH", "MEDIUM") else IMPROVE
    if sev == "MEDIUM" or test_id in cfg.bandit_promote:
        return IMPROVE
    return IGNORE


@_timed
def run_bandit(cfg: AuditConfig, targets=None) -> ToolResult:
    # 테스트는 assert 등으로 잡음이 많아 제외, 스크립트·플러그인은 보안 검사 대상에 포함
    p = run_module(
        "bandit",
        ["-r", *(targets or _all_targets(cfg, skip_roles=("tests",))), "-f", "json", "-q"],
        cfg.root,
    )
    if p.missing:
        return ToolResult("bandit", "skipped", detail=p.stderr)
    try:
        start = p.stdout.index("{")
        data = json.loads(p.stdout[start:])
    except (ValueError, json.JSONDecodeError):
        return ToolResult("bandit", "error", detail=(p.stderr or p.stdout)[-2000:])
    out = []
    for r in data.get("results", []):
        tid, sev, conf = r["test_id"], r["issue_severity"], r["issue_confidence"]
        out.append(
            Finding(
                tool="bandit",
                category="보안",
                rule=tid,
                message=r["issue_text"],
                file=cfg.rel(r["filename"]),
                line=r.get("line_number"),
                severity=bandit_severity(cfg, tid, sev, conf),
                evidence=f"bandit {tid}({r.get('test_name', '')}) 심각도 {sev}/신뢰도 {conf} — {r.get('more_info', '')}",
            )
        )
    return ToolResult("bandit", "findings" if out else "ok", out)


# ---------------------------------------------------------------- 8. pytest + coverage
@_timed
def run_pytest(cfg: AuditConfig) -> ToolResult:
    if not cfg.run_tests:
        return ToolResult("pytest", "skipped", detail="run_tests = false")
    if not module_available("pytest") or not module_available("pytest_cov"):
        return ToolResult("pytest", "skipped", detail="pytest / pytest-cov 미설치")
    with tempfile.TemporaryDirectory() as tmp:
        cov_json = Path(tmp) / "cov.json"
        junit = Path(tmp) / "junit.xml"
        args = [
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit}",
            f"--cov-report=json:{cov_json}",
            "--cov-report=",
            *cfg.pytest_args,
        ]
        args.extend(f"--cov={pkg}" for pkg in cfg.packages)
        p = run_module("pytest", args, cfg.root, timeout=cfg.pytest_timeout)
        out, data = [], {}
        if p.returncode == 5:
            out.append(
                Finding(
                    tool="pytest",
                    category="테스트",
                    rule="NO-TESTS",
                    message="수집된 테스트가 없음",
                    severity=IMPROVE,
                    evidence="pytest exit 5",
                )
            )
        if junit.exists():
            out += _parse_junit(cfg, junit)
        elif p.returncode not in (0, 5):
            return ToolResult("pytest", "error", detail=(p.stdout + p.stderr)[-3000:])
        if cov_json.exists():
            cov = json.loads(cov_json.read_text(encoding="utf-8"))
            pct = round(cov["totals"]["percent_covered"], 1)
            data["coverage"] = pct
            files = sorted(
                (
                    (cfg.rel(f), v["summary"]["percent_covered"], v["summary"]["num_statements"])
                    for f, v in cov.get("files", {}).items()
                ),
                key=lambda x: (x[1], -x[2]),
            )
            data["lowest"] = [
                {"file": f, "percent": round(pc, 1), "statements": n} for f, pc, n in files if n > 0
            ][:10]
            if pct < cfg.coverage_target:
                out.append(
                    Finding(
                        tool="pytest",
                        category="테스트",
                        rule="COVERAGE",
                        message=f"커버리지 {pct}% < 목표 {cfg.coverage_target:g}%",
                        severity=IMPROVE,
                        evidence="pytest-cov (커버리지는 실행 여부만 보며 정확성은 보장하지 않음)",
                    )
                )
    data["coverage_target"] = cfg.coverage_target
    return ToolResult("pytest", "findings" if out else "ok", out, data=data)


def _junit_file(cfg: AuditConfig, classname: str):
    """junit classname(tests.test_x.TestCls) → 존재하는 가장 긴 파일 경로."""
    parts = classname.split(".")
    while parts:
        p = cfg.root / Path(*parts).with_suffix(".py")
        if p.is_file():
            return str(p)
        parts.pop()
    return None


def _parse_junit(cfg: AuditConfig, path: Path) -> list:
    out = []
    root = ET.parse(path).getroot()
    for tc in root.iter("testcase"):
        for tag in ("failure", "error"):
            el = tc.find(tag)
            if el is None:
                continue
            node = ".".join(x for x in (tc.get("classname"), tc.get("name")) if x)
            msg = (el.get("message") or "").splitlines()[0][:300] if el.get("message") else tag
            collection = "collection" in (el.get("message") or "").lower() or not tc.get(
                "classname"
            )
            file = tc.get("file") or _junit_file(cfg, tc.get("classname") or tc.get("name") or "")
            out.append(
                Finding(
                    tool="pytest",
                    category="테스트",
                    rule="COLLECT-ERROR"
                    if collection
                    else ("TEST-ERROR" if tag == "error" else "TEST-FAIL"),
                    message=(
                        f"테스트 수집 실패(임포트 오류 가능): {node} — {msg}"
                        if collection
                        else f"테스트 실패: {node} — {msg}"
                    ),
                    file=cfg.rel(file) if file else None,
                    line=int(tc_line) + 1 if (tc_line := tc.get("line")) else None,
                    severity=CRITICAL,
                    evidence="pytest "
                    + tag
                    + (
                        " — 순환참조 등으로 임포트 자체가 실패하면 여기서 드러남"
                        if collection
                        else ""
                    ),
                    extra={"detail": (el.text or "")[-1500:]},
                )
            )
    return out

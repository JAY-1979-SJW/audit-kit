"""Claude Code PostToolUse hook: Claude가 .py 파일을 저장할 때마다 빠른 검사.

- ruff (자동 수정 안 함: hook이 파일을 바꾸면 Claude가 아는 내용과 달라져 다음 Edit이 실패함)
- mypy (해당 파일 오류만)
- 순환 임포트 (해당 파일이 포함된 순환만, AST라 빠름)

문제가 있으면 hook_mode에 따라
  block: stderr + exit 2 → Claude가 반드시 보고 수정
  warn : additionalContext JSON → Claude에게 경고만 전달
"""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

from audit_kit.arch.scan import scan
from audit_kit.arch.spec import load_spec
from audit_kit.config import load_config
from audit_kit.gitutil import changed_lines_for_file
from audit_kit.models import IGNORE
from audit_kit.scope import build_project_graph
from audit_kit.std import checks
from audit_kit.std.rules import load_rules, rule_for_custom
from audit_kit.textio import read_text
from audit_kit.tools import run_mypy, run_ruff

MAX_LINES = 40


def _resolve(fp: str, data: dict) -> Path:
    p = Path(fp)
    if not p.is_absolute():
        base = data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or str(Path.cwd())
        p = Path(base) / p
    return p


def _file_from_input(data: dict):
    ti = data.get("tool_input") or {}
    fp = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
    return _resolve(fp, data) if fp else None


def _files_from_input(data: dict) -> list[Path]:
    """`tool_input.file_paths`(목록, 커밋 단계 일괄 호출용) 또는 단일 `file_path`.

    Claude Code PostToolUse 이벤트는 항상 파일 하나(`file_path`)지만, 커밋 단계 게이트처럼
    여러 파일을 검사해야 하는 호출 쪽은 `file_paths`로 한 프로세스 안에서 묶어 보낼 수 있다 —
    파일마다 새 프로세스를 띄우면 `build_project_graph`를 매번(파일당 약 8.7초, 2026-10-08
    PR #160 CI 90분 초과 조사) 다시 계산하게 된다."""
    ti = data.get("tool_input") or {}
    multi = ti.get("file_paths")
    if isinstance(multi, list) and multi:
        return [_resolve(fp, data) for fp in multi if fp]
    single = _file_from_input(data)
    return [single] if single else []


LINT_HOOK_MARKERS = ("py_post_edit", "ruff")


def global_lint_hook_present(home: Path | None = None) -> bool:
    """사용자 전역 설정(~/.claude/settings.json)에 저장 시 ruff/mypy 를 돌리는 hook 이 이미 있는가.
    (예: 32. Claude 개발표준 의 py_post_edit.py) — 있으면 audit-kit hook 은 설계 검사만 한다."""
    settings = (home or Path.home()) / ".claude" / "settings.json"
    try:
        data = json.loads(settings.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    for entry in (data.get("hooks") or {}).get("PostToolUse") or []:
        for h in entry.get("hooks", []) if isinstance(entry, dict) else []:
            cmd = str(h.get("command", ""))
            if "audit_kit" not in cmd and any(m in cmd for m in LINT_HOOK_MARKERS):
                return True
    return False


def lint_in_hook(cfg) -> bool:
    if cfg.hook_tools == "design":
        return False
    if cfg.hook_tools == "all":
        return True
    return not global_lint_hook_present()  # auto


def _std_custom_msgs(root: Path, path: Path, rel: str) -> list[str]:
    """파일 하나만으로 완결되는 개발 기준서 커스텀 검사(EFF-02 리스트 멤버십, STD-02 절대경로 등,
    `checks.FILE_CHECKS`)를 저장 시점에 즉시 실행해 보고한다. 프로젝트 전체 상태가 필요한 검사
    (중복 함수 탐지·import 해석 등)는 `run_file_checks`에 애초에 없어 자동으로 빠진다 — 새 검사가
    `FILE_CHECKS`에 추가되면 이 hook 도 손댈 것 없이 그대로 따라간다(2026-09-28).

    저장 시점에도 신규/기존을 구분한다(Anthropic Code Review 의 🟣Pre-existing 과 같은 발상) —
    지금 막 편집하던 파일에 원래 있던 문제면 `(기존)`을 붙여, 이번 편집이 만든 게 아님을 바로
    알려준다. git 정보가 없으면(저장소 아님 등) 태그 없이 그대로 보여준다."""
    try:
        tree = ast.parse(read_text(path))
    except (SyntaxError, ValueError, OSError):
        return []  # 문법 오류는 이미 다른 검사(ruff 등)에서 보고됨
    rules = load_rules()
    changed = changed_lines_for_file(root, rel)
    msgs = []
    for h in checks.run_file_checks(tree, rel):
        rule = rule_for_custom(rules, h.check)
        label = rule.id if rule else h.check
        tag = " (기존)" if changed is not None and h.line not in changed else ""
        msgs.append(f"[표준 {label}] {rel}:{h.line} {h.message}{tag}")
    return msgs


_GRAPH_CACHE: dict[str, tuple] = {}


def _cached_graph_and_spec(cfg):
    """`build_project_graph(cfg)`·`load_spec(cfg.root)`를 프로세스당 저장소(cfg.root)마다 1회만.

    같은 프로세스 안에서 여러 파일을 검사할 때(배치 입력, 또는 테스트에서 `check_file`을 여러
    번 부를 때) 매번 프로젝트 전체 .py 를 다시 읽는 비용(파일당 약 8.7초, 2026-10-08 보고)을
    없앤다. 프로세스가 끝나면 캐시도 사라지므로 파일이 그 사이 바뀌어도 다음 저장(새 프로세스)
    에서는 새로 계산된다."""
    key = str(cfg.root.resolve())
    if key not in _GRAPH_CACHE:
        _GRAPH_CACHE[key] = (build_project_graph(cfg), load_spec(cfg.root))
    return _GRAPH_CACHE[key]


def check_file(path: Path) -> list:
    cfg = load_config(path.parent)
    rel = cfg.rel(path)
    msgs: list[str] = []

    if lint_in_hook(cfg):  # 전역 hook 이 ruff/mypy 를 이미 돌리면 중복 실행하지 않는다
        msgs.extend(
            f"[ruff {f.rule}] {f.location()} {f.message}"
            for f in run_ruff(cfg, targets=[rel]).findings
            if f.severity != IGNORE
        )
        if cfg.hook_mypy:
            msgs.extend(
                f"[mypy {f.rule}] {f.location()} {f.message}"
                for f in run_mypy(cfg, targets=[rel]).findings
                if f.file == rel
            )

    # ruff/mypy 아닌 audit-kit 자체 규칙이라 lint_in_hook 게이트와 무관하게 항상 돈다(전역
    # ruff/mypy hook 이 있어도 이 규칙들은 대신 검사해 주지 않으므로).
    msgs.extend(_std_custom_msgs(cfg.root, path, rel))

    try:
        graph, spec = _cached_graph_and_spec(cfg)
        mod = next((m for m, fp in graph.modules.items() if cfg.rel(fp) == rel), None)
        if mod and spec is not None:
            # 설계 파일이 있으면 설계 기준으로: 이 파일에서 나가는 위반 + 이 파일이 낀 순환
            for v in scan(cfg, spec, graph):
                cyc = v.rule == "ARCH-CYCLE" and mod in v.message.split(": ", 1)[-1].split(" -> ")
                if (v.src == mod and v.rule not in ("ARCH-UNASSIGNED", "ARCH-SPEC")) or cyc:
                    msgs.append(f"[설계 {v.rule}] {v.file}:{v.line} {v.message} → {v.hint}")
        elif mod:
            msgs.extend(
                "[cycle] 순환 임포트: " + " -> ".join(cyc) for cyc in graph.cycles() if mod in cyc
            )
    except Exception as exc:  # ruff: ignore[blind-except] — 구조 검사(그래프·설계 비교)는 어떤 프로젝트
        # 구조에서도 실행되는 보조 기능이라 예외 종류를 예측할 수 없다. 실패해도 hook을
        # 막지는 않되, 조용히 삼키지 않고 stderr 로 남긴다(STD-04: 예외 삼키기 금지).
        print(f"[audit-kit hook] 구조 검사 보조 기능 오류(무시하고 계속): {exc!r}", file=sys.stderr)
    return msgs


def read_input() -> dict:
    """hook 입력(JSON). BOM·인코딩 차이를 흡수하고, 해석 실패는 조용히 넘기지 않고 알린다."""
    raw = (
        sys.stdin.buffer.read()
        if hasattr(sys.stdin, "buffer")
        else sys.stdin.read().encode("utf-8")
    )
    text = raw.decode("utf-8-sig", errors="replace").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"hook 입력을 JSON 으로 해석할 수 없음 ({e.msg}, 위치 {e.pos}): {text[:80]!r}"
        ) from e
    if not isinstance(data, dict):
        raise ValueError(f"hook 입력이 JSON 객체가 아님: {type(data).__name__}")
    return data


def main() -> int:
    try:
        data = read_input()
    except ValueError as e:
        # exit 1 = Claude Code 에서 '막지 않는 오류'로 표시됨 → 검사가 건너뛰어졌음을 사용자가 알 수 있다
        sys.stderr.write(f"audit-kit hook: {e} — 이번 저장은 검사하지 않았습니다\n")
        return 1
    def eligible(p: Path) -> bool:
        return (
            p.suffix == ".py"
            and p.is_file()
            and not any(part in {".venv", "venv", "site-packages", "node_modules"} for part in p.parts)
        )

    paths = [p for p in _files_from_input(data) if eligible(p)]
    if not paths:
        return 0

    block_sections: list[str] = []
    warn_sections: list[str] = []
    for path in paths:
        cfg = load_config(path.parent)
        msgs = check_file(path)
        if not msgs:
            continue
        shown = msgs[:MAX_LINES]
        if len(msgs) > MAX_LINES:
            shown.append(f"... 외 {len(msgs) - MAX_LINES}건")
        body = f"audit-kit: {cfg.rel(path)} 검사에서 {len(msgs)}건 발견\n" + "\n".join(shown)
        (warn_sections if cfg.hook_mode == "warn" else block_sections).append(body)

    if warn_sections:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": "\n\n".join(warn_sections),
                    }
                },
                ensure_ascii=False,
            )
        )
    if block_sections:
        sys.stderr.write(
            "\n\n".join(block_sections)
            + "\n수정 후 다시 저장하세요. (타입 오류가 의도된 것이면 `# type: ignore[코드]` 사유와 함께)\n"
        )
        return 2
    return 0

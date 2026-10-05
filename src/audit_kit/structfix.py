"""구조 규칙 자동 수정: `audit-kit struct check | fix | undo`.

항상 작업공간(임시 복사본)에서 고치고, 건마다 검증을 통과한 것만 남긴다.
  __init__.py 추가     → 그 폴더 아래 제품 모듈 실제 임포트 확인 (수정 전보다 실패가 늘면 취소)
  최상위 이름 임포트 교정 → 문법 확인 + (제품 모듈이면) 실제 임포트 확인
  의존성 선언 추가      → pyproject.toml 문법·반영 확인 (서식·주석 유지)
마지막에 구조 검사를 다시 돌려 고친 규칙 건수가 줄었는지, 다른 규칙이 늘지 않았는지 확인한다.
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from audit_kit.arch.edit import Editor, render_from
from audit_kit.arch.fix import Workspace, import_failures, undo
from audit_kit.cliargs import add_fix_flags, add_path, add_undo
from audit_kit.config import AuditConfig, load_config
from audit_kit.pyproject_edit import add_dependency, replace_dependency
from audit_kit.scope import build_project_graph, get_scope
from audit_kit.structure import RULE_LABEL, check


@dataclass
class StructFixResult:
    workspace: Workspace
    applied: list = field(default_factory=list)  # (rule, 설명)
    failed: list = field(default_factory=list)  # (rule, 설명, 사유)
    before: Counter = field(default_factory=Counter)
    after: Counter = field(default_factory=Counter)
    remaining: list = field(default_factory=list)


class _ImportGuard:
    """수정 전(원본)과 후(작업공간)의 임포트 실패를 비교. 원래 실패하던 모듈은 문제로 보지 않는다."""

    def __init__(self, ws: Workspace, enabled: bool):
        self.ws, self.enabled = ws, enabled
        self.base: dict[str, str | None] = {}

    def new_failures(self, modules: list) -> dict:
        if not self.enabled or not modules:
            return {}
        todo = [m for m in modules if m not in self.base]
        if todo:
            fails = import_failures(self.ws.orig, todo)
            self.base.update({m: fails.get(m) for m in todo})
        now = import_failures(self.ws.tmp, modules)
        return {m: e for m, e in now.items() if not self.base.get(m)}


def _modules_under(graph, cfg, dotted: str, production: set, limit: int = 25) -> list:
    mods = [
        m
        for m, f in graph.modules.items()
        if (m == dotted or m.startswith(dotted + ".")) and cfg.rel(f) in production
    ]
    return sorted(mods)[:limit]


def fix_no_init(ws, cfg, graph, issues, guard, res):
    production = set(get_scope(cfg).production)
    for i in issues:
        rel = i.data["create"]
        dotted = rel[: -len("/__init__.py")].removeprefix("src/").replace("/", ".")
        target = ws.tmp / rel
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")
        bad = guard.new_failures(_modules_under(graph, cfg, dotted, production))
        if bad:
            target.unlink()
            m, e = next(iter(bad.items()))
            res.failed.append((i.rule, rel, f"추가하면 {m} 임포트 실패: {e}"))
        else:
            res.applied.append((i.rule, f"{rel} 생성 ('{dotted}' 를 일반 패키지로)"))


def fix_toplevel(ws, cfg, graph, issues, guard, res):
    production = set(get_scope(cfg).production)
    for i in issues:
        d = i.data
        rel = cfg.rel(graph.modules[d["module"]])
        text = ws.read(rel)
        tree = ast.parse(text)
        node = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, (ast.Import, ast.ImportFrom)) and n.lineno == d["line"]
            ),
            None,
        )
        ed = Editor(text)
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == d["old"]:
            ed.replace_node(node, render_from(d["new"], node.names))
        elif (
            isinstance(node, ast.Import)
            and len(node.names) == 1
            and node.names[0].asname
            and node.names[0].name == d["old"]
        ):
            ed.replace_node(node, f"import {d['new']} as {node.names[0].asname}")
        else:
            res.failed.append((
                i.rule,
                f"{rel}:{d['line']}",
                "임포트 문 형태가 달라 자동 교체 안 함",
            ))
            continue
        new_text = ed.apply()
        try:
            compile(new_text, rel, "exec")
        except SyntaxError as e:
            res.failed.append((i.rule, f"{rel}:{d['line']}", f"문법 오류 {e.msg}"))
            continue
        before = ws.read(rel)
        ws.write(rel, new_text)
        if rel in production:
            bad = guard.new_failures([d["module"]])
            if bad:
                ws.write(rel, before)
                res.failed.append((
                    i.rule,
                    f"{rel}:{d['line']}",
                    f"임포트 실패: {next(iter(bad.values()))}",
                ))
                continue
        res.applied.append((i.rule, f"{rel}:{d['line']} '{d['old']}' → '{d['new']}'"))


def fix_dependencies(ws, issues, res):
    rel = "pyproject.toml"
    if not (ws.tmp / rel).exists():
        return
    for i in issues:
        text = ws.read(rel)
        try:
            if i.rule == "STRUCT-DEP-MISSING":
                new = add_dependency(text, i.data["spec"], i.data["group"])
                desc = f"pyproject.toml [{i.data['group']}] 에 '{i.data['spec']}' 추가"
            else:
                new = replace_dependency(text, i.data["old"], i.data["new"])
                desc = f"pyproject.toml '{i.data['old']}' → '{i.data['new']}'"
        except ValueError as e:
            res.failed.append((i.rule, i.message, str(e)))
            continue
        ws.write(rel, new)
        res.applied.append((i.rule, desc))


def run_struct_fix(
    cfg: AuditConfig,
    pin_major: bool = False,
    import_check: bool = True,
    only=None,
    include_support: bool = False,
) -> StructFixResult:
    issues = check(cfg, pin_major=pin_major)
    ws = Workspace(cfg)
    res = StructFixResult(ws, before=Counter(i.rule for i in issues))
    graph = build_project_graph(cfg)
    guard = _ImportGuard(ws, import_check)
    groups = {
        "init": [i for i in issues if i.rule == "STRUCT-NO-INIT" and i.fixable],
        "toplevel": [
            i
            for i in issues
            if i.rule == "STRUCT-TOPLEVEL-IMPORT"
            and i.fixable
            and (include_support or not i.data.get("support"))
        ],
        "deps": [
            i
            for i in issues
            if i.rule in ("STRUCT-DEP-MISSING", "STRUCT-DEP-UNBOUNDED") and i.fixable
        ],
    }
    if not only or "init" in only:
        fix_no_init(ws, cfg, graph, groups["init"], guard, res)
    if not only or "toplevel" in only:
        fix_toplevel(ws, cfg, graph, groups["toplevel"], guard, res)
    if not only or "deps" in only:
        fix_dependencies(ws, groups["deps"], res)
    if res.applied:
        from audit_kit.scope import _CACHE

        _CACHE.clear()
        res.remaining = check(ws.cfg, pin_major=pin_major)
        res.after = Counter(i.rule for i in res.remaining)
    else:
        res.remaining, res.after = issues, Counter(res.before)
    return res


# ---------------------------------------------------------------- CLI
def _print_issues(issues: list):
    by: dict[str, list] = {}
    for i in issues:
        by.setdefault(i.rule, []).append(i)
    for rule, items in by.items():
        fx = sum(1 for i in items if i.fixable)
        print(
            f"\n[{RULE_LABEL.get(rule, rule)}] {len(items)}건"
            + (f" (자동 수정 가능 {fx})" if fx else "")
        )
        for i in items[:15]:
            loc = f"{i.file}:{i.line}" if i.line else (i.file or "")
            print(f"  {loc} — {i.message}")
            if i.hint:
                print(f"      → {i.hint}")
        if len(items) > 15:
            print(f"  … 외 {len(items) - 15}건")
        print(f"      근거: {items[0].ref}")


def _cmd_check(cfg, args) -> int:
    issues = check(
        cfg, pin_major=args.pin_major, only=set(args.only.split(",")) if args.only else None
    )
    print(f"구조 문제 {len(issues)}건 (자동 수정 가능 {sum(1 for i in issues if i.fixable)}건)")
    _print_issues(issues)
    return 1 if issues else 0


def _cmd_undo(cfg) -> int:
    latest = cfg.root / cfg.report_dir / "STRUCT_FIX_LATEST.txt"
    if not latest.exists():
        print("되돌릴 struct fix 기록이 없습니다.", file=sys.stderr)
        return 1
    files = undo(
        cfg.root, cfg.root / cfg.report_dir / latest.read_text(encoding="utf-8").strip() / "backup"
    )
    latest.unlink()
    print(f"복원: {len(files)}개 파일")
    return 0


def _report_struct_fix(res) -> None:
    for rule in sorted(set(res.before) | set(res.after)):
        b, a = res.before.get(rule, 0), res.after.get(rule, 0)
        mark = " ⚠ 증가" if a > b else ""
        print(f"  {RULE_LABEL.get(rule, rule):32} {b} → {a}{mark}")
    for _rule, desc in res.applied[:40]:
        print(f"  ✔ {desc}")
    if len(res.applied) > 40:
        print(f"  … 외 {len(res.applied) - 40}건")
    for _rule, what, why in res.failed:
        print(f"  ✘ {what}: {why}")


def _write_struct_fix_report(out_dir: Path, res, diffs: str) -> None:
    from audit_kit.arch.commands import render_fix_report

    (out_dir / "struct-fix.md").write_text(
        render_fix_report("구조 자동 수정", res, diffs), encoding="utf-8"
    )


def _apply_struct_fix(cfg, args, res, out_dir: Path, changed: list, diffs: str) -> int:
    """미리보기/적용 반영은 `arch fix`·`tangle --fix`와 완전히 같은 절차라 공용 헬퍼를 쓴다
    (2026-09-28 Stop hook 중복 코드 지적으로 `_apply_or_preview()`에 합침)."""
    from audit_kit.arch.commands import ApplyLabels, FixOutput, _apply_or_preview

    return _apply_or_preview(
        cfg,
        res,
        FixOutput(out_dir, changed, diffs),
        args,
        ApplyLabels(
            preview_cmd="audit-kit struct fix --apply  (새로 만든 파일 포함 되돌리기: struct undo)",
            marker_file="STRUCT_FIX_LATEST.txt",
            undo_cmd="audit-kit struct undo",
        ),
    )


def _cmd_fix(cfg, args) -> int:
    print("작업공간(임시 복사본)에서 구조 수정·검증 중...")
    res = run_struct_fix(
        cfg,
        pin_major=args.pin_major,
        import_check=not args.no_import_check,
        only=set(args.only.split(",")) if args.only else None,
        include_support=args.include_support,
    )
    ws = res.workspace
    try:
        changed = ws.changed()
        print(
            f"\n자동 수정 {len(res.applied)}건, 실패 {len(res.failed)}건, 바뀐 파일 {len(changed)}개"
        )
        _report_struct_fix(res)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = cfg.root / cfg.report_dir / f"struct-fix_{ts}"
        out_dir.mkdir(parents=True, exist_ok=True)
        diffs = "".join(ws.diff(rel) for rel in changed)
        _write_struct_fix_report(out_dir, res, diffs)
        print(f"\n리포트: {out_dir / 'struct-fix.md'}")
        if any(res.after.get(r, 0) > res.before.get(r, 0) for r in res.after):
            print(
                "⚠ 수정 후 늘어난 구조 문제가 있어 적용하지 않습니다. 리포트를 확인하세요.",
                file=sys.stderr,
            )
            return 1
        if not changed:
            return 0
        return _apply_struct_fix(cfg, args, res, out_dir, changed, diffs)
    finally:
        ws.cleanup()


def cmd(args) -> int:
    """`audit-kit struct check|fix|undo` 진입점. 하위 명령마다 `_cmd_*()`로 나눠뒀다(STD-08:
    원래 이 함수 하나가 복잡도 14·분기 14·문장 53이었다, 2026-09-28)."""
    cfg = load_config(args.path)
    if args.struct_cmd == "check":
        return _cmd_check(cfg, args)
    if args.struct_cmd == "undo":
        return _cmd_undo(cfg)
    return _cmd_fix(cfg, args)


def register(sub):
    p = sub.add_parser("struct", help="프로그램 구조 규칙 검사·자동 수정 (공식 문서 근거)")
    s = p.add_subparsers(dest="struct_cmd", required=True)
    q = s.add_parser("check", help="구조 규칙 검사")
    add_path(q)
    q.add_argument("--only", help="init,toplevel,deps,body,pyver")
    q.add_argument("--pin-major", action="store_true", help="상한 없는 의존성에 '<다음메이저' 제안")
    q.set_defaults(func=cmd)
    q = s.add_parser("fix", help="자동 수정 (기본 미리보기)")
    add_path(q)
    add_fix_flags(q)
    q.add_argument("--pin-major", action="store_true", help="상한 없는 의존성에 '<다음메이저' 추가")
    q.add_argument("--only", help="init,toplevel,deps")
    q.add_argument(
        "--include-support",
        action="store_true",
        help="스크립트·테스트의 최상위 이름 임포트도 교체 (실행 방식이 프로젝트 루트 기준일 때만)",
    )
    q.add_argument("--no-import-check", action="store_true")
    q.set_defaults(func=cmd)
    add_undo(s, target="struct", func=cmd, extra_help=" (새로 만든 파일은 삭제)")

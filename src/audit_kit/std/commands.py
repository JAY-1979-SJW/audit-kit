"""`audit-kit std` 명령: 개발 기준서로 프로젝트를 검사·자동 수정한다."""

from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from audit_kit.config import load_config
from audit_kit.models import CRITICAL, IMPROVE
from audit_kit.report import write_reports
from audit_kit.std.run import run_std


def _opt(value):
    return Path(value) if value else None


def cmd_std(args) -> int:
    cfg = load_config(args.path)
    if args.undo:
        return _cmd_std_undo(cfg)
    if args.fix:
        return _cmd_std_fix(cfg, args)
    return _cmd_std_check(cfg, args)


def _cmd_std_check(cfg, args) -> int:
    when = datetime.now()
    out_dir = cfg.root / cfg.report_dir / f"std_{when:%Y%m%d_%H%M%S}"
    print(f"audit-kit std — {cfg.root}")
    results = run_std(
        cfg,
        rules_path=_opt(args.rules),
        ruff_config=_opt(args.ruff_config),
        registry_path=_opt(args.registry),
        with_mypy=not args.no_mypy,
    )
    for r in results:
        note = f" — {r.detail[:160]}" if r.detail else ""
        print(f"  {r.tool}: {r.status} ({len(r.findings)}건, {r.duration:.1f}s){note}")
    info = write_reports(cfg, results, out_dir, when, [])
    counts = info["counts"]
    _print_by_clause(results)
    print(
        f"\n치명 {counts.get('critical', 0)} / 개선 {counts.get('improve', 0)} / "
        f"참고 {counts.get('review', 0)} / 무시 {counts.get('ignore', 0)}"
    )
    print(f"리포트: {out_dir / 'report.md'}")
    std_exit = 0
    if args.fail_on == "critical" and counts.get(CRITICAL):
        std_exit = 1
    if args.fail_on == "improve" and (counts.get(CRITICAL) or counts.get(IMPROVE)):
        std_exit = 1

    if getattr(args, "solo", False):
        return std_exit
    print("\n[세트] std 뒤에 run 을 이어서 실행합니다 (--solo 로 끄기)")
    run_exit = _run_paired_run(args)
    return max(std_exit, run_exit)


def _run_paired_run(args) -> int:
    """`std`가 끝난 뒤 `run`을 이어서 실행한다(2026-09-29: 사용자 지시로 run/std는 항상 세트).
    무한 재귀 방지: 이어서 실행하는 run 쪽은 반드시 solo=True 로 넘긴다.
    감사할 패키지를 못 찾으면(run 자체 사양) 그 사실만 알리고 std 결과로 판정한다."""
    import argparse

    from audit_kit.cli import cmd_run

    run_args = argparse.Namespace(
        path=args.path,
        only=None,
        no_tests=False,
        fix=False,
        fail_on=args.fail_on,
        solo=True,
    )
    exit_code = cmd_run(run_args)
    if exit_code == 2:
        print("(run 은 감사할 패키지를 찾지 못해 생략됨 — std 결과만 반영)")
        return 0
    return exit_code


def _print_by_clause(results: list) -> None:
    by_clause = Counter(f.rule for r in results for f in r.findings if f.severity != "ignore")
    if by_clause:
        print("\n조항별 위반:")
        for clause, n in sorted(by_clause.items()):
            print(f"  {clause}: {n}건")


# ---------------------------------------------------------------- --fix / --undo
def _report_std_fix(res) -> None:
    mark = " ⚠ 증가" if res.after > res.before else ""
    print(f"  EFF-02: {res.before} → {res.after}{mark}")
    for _rule, desc in res.applied[:40]:
        print(f"  ✔ {desc}")
    if len(res.applied) > 40:
        print(f"  … 외 {len(res.applied) - 40}건")
    for _rule, what, why in res.failed:
        print(f"  ✘ {what}: {why}")


def _write_std_fix_report(out_dir: Path, res, diffs: str) -> None:
    from audit_kit.arch.commands import render_fix_report

    (out_dir / "std-fix.md").write_text(
        render_fix_report("개발 기준서 자동 수정 (EFF-02)", res, diffs), encoding="utf-8"
    )


def _apply_std_fix(cfg, args, res, out_dir: Path, changed: list, diffs: str) -> int:
    """미리보기/적용 반영은 `arch fix`·`struct fix`와 완전히 같은 절차라 공용 헬퍼를 쓴다."""
    from audit_kit.arch.commands import ApplyLabels, FixOutput, _apply_or_preview

    return _apply_or_preview(
        cfg,
        res,
        FixOutput(out_dir, changed, diffs),
        args,
        ApplyLabels(
            preview_cmd="audit-kit std --fix --apply",
            marker_file="STD_FIX_LATEST.txt",
            undo_cmd="audit-kit std --undo",
        ),
    )


def _cmd_std_fix(cfg, args) -> int:
    from audit_kit.std.fix import run_std_fix

    print("작업공간(임시 복사본)에서 EFF-02 자동 수정·검증 중...")
    res = run_std_fix(cfg)
    ws = res.workspace
    try:
        changed = ws.changed()
        print(
            f"\n자동 수정 {len(res.applied)}건, 실패 {len(res.failed)}건, 바뀐 파일 {len(changed)}개"
        )
        _report_std_fix(res)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = cfg.root / cfg.report_dir / f"std-fix_{ts}"
        out_dir.mkdir(parents=True, exist_ok=True)
        diffs = "".join(ws.diff(rel) for rel in changed)
        _write_std_fix_report(out_dir, res, diffs)
        print(f"\n리포트: {out_dir / 'std-fix.md'}")
        if res.after > res.before:
            print(
                "⚠ 수정 후 EFF-02 가 늘어나 적용하지 않습니다. 리포트를 확인하세요.",
                file=sys.stderr,
            )
            return 1
        if not changed:
            return 0
        return _apply_std_fix(cfg, args, res, out_dir, changed, diffs)
    finally:
        ws.cleanup()


def _cmd_std_undo(cfg) -> int:
    from audit_kit.arch.fix import undo

    latest = cfg.root / cfg.report_dir / "STD_FIX_LATEST.txt"
    if not latest.exists():
        print("되돌릴 std fix 기록이 없습니다.", file=sys.stderr)
        return 1
    files = undo(
        cfg.root, cfg.root / cfg.report_dir / latest.read_text(encoding="utf-8").strip() / "backup"
    )
    latest.unlink()
    print(f"복원: {len(files)}개 파일")
    return 0


def register(sub) -> None:
    p = sub.add_parser("std", help="개발 기준서(조항 ID)로 검사·자동 수정: 표준·중복·효율·오류")
    p.add_argument("--path", default=None, help="프로젝트 경로(기본: 현재 폴더)")
    p.add_argument("--rules", help="규칙 원장(rules.toml). 기본: 번들된 기준서")
    p.add_argument("--ruff-config", help="ruff 설정. 기본: 번들된 기준서 ruff.toml")
    p.add_argument("--registry", help="개발자 센터 등록부(docs_registry.toml)")
    p.add_argument("--no-mypy", action="store_true", help="mypy 생략(없는 모듈·속성 검사 제외)")
    p.add_argument(
        "--fail-on",
        choices=["critical", "improve", "never"],
        default="critical",
        help="종료코드 기준 (기본: 치명이 있으면 1)",
    )
    p.add_argument(
        "--fix", action="store_true", help="EFF-02(리스트 멤버십) 자동 수정(기본 미리보기)"
    )
    p.add_argument("--apply", action="store_true", help="--fix 와 함께: 미리보기 대신 실제 반영")
    p.add_argument("--verify", action="store_true", help="--fix --apply 전 pytest 결과 비교")
    p.add_argument(
        "--force", action="store_true", help="--fix --apply: 커밋 안 된 변경 있어도 진행"
    )
    p.add_argument("--quiet", action="store_true", help="--fix 미리보기 diff 출력 생략")
    p.add_argument("--undo", action="store_true", help="마지막 std --fix --apply 되돌리기")
    p.add_argument(
        "--solo",
        action="store_true",
        help="run 을 이어서 실행하지 않는다 (기본: std+run 세트로 실행, 2026-09-29 사용자 지시)",
    )
    p.set_defaults(func=cmd_std)

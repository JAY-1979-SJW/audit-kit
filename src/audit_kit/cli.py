"""audit-kit 명령줄.

audit-kit init       대상 프로젝트에 설정·hook·/audit 스킬 설치
audit-kit run        전체 감사(1~8단계) + 리포트 생성
audit-kit check F…   파일 단위 빠른 검사(pre-commit용)
audit-kit hook       Claude Code PostToolUse hook (stdin JSON)
audit-kit whitelist  vulture 화이트리스트 생성
audit-kit doctor     도구 설치·설정 상태 점검
audit-kit arch …     프로그램 구조 설계(architecture.toml) → 스캔 → 자동 수정
audit-kit fix …      감사 결과 수정 모드: 계획 → 브랜치 → 건별 재검사·커밋 → 보고
audit-kit std        개발 기준서(조항 ID)로 검사: 표준·중복·효율·오류
audit-kit new        확정된 설계로 새 프로젝트 골격 생성 (2단계)
audit-kit pre-push   git push 전 로컬 게이트(pytest 전체 + std --fail-on critical)
audit-kit gh-check   push 후 GitHub Actions 결과를 폴링해서 실제로 확인
audit-kit baseline … 테스트 실패 기준선 저장·비교(내 변경이 만든 새 실패만 가려냄)
audit-kit ci-local   GitHub Actions 워크플로의 run 단계를 이 PC 에서 실행(Actions 를 못 쓸 때)
audit-kit deps       코드의 import 를 조사해 의존성 선언(requirements.txt) 초안 생성
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from datetime import datetime
from pathlib import Path

from audit_kit import __version__

STEPS = [
    "ruff",
    "mypy",
    "import-linter",
    "struct",
    "cycles",
    "vulture",
    "radon",
    "bandit",
    "pytest",
]


def _utf8_stdout():
    # stdout/stderr 가 없거나(pythonw) reconfigure 를 지원하지 않는 스트림(pytest 캡처 등)이면
    # AttributeError, 이미 닫혔거나 재구성할 수 없으면 ValueError — 둘 다 UTF-8 강제는 best-effort 다.
    for s in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            s.reconfigure(encoding="utf-8", errors="replace")


def cmd_run(args) -> int:
    from audit_kit import tools
    from audit_kit.config import load_config
    from audit_kit.heuristics import router_files, run_heuristics
    from audit_kit.models import CRITICAL, IMPROVE
    from audit_kit.report import dedupe, write_reports
    from audit_kit.score import compute_health_score
    from audit_kit.structure import run_struct

    cfg = load_config(args.path)
    if args.no_tests:
        cfg.run_tests = False
    if not cfg.packages:
        print(
            "감사할 패키지를 찾지 못했습니다. pyproject.toml [tool.audit-kit] packages 를 지정하세요.",
            file=sys.stderr,
        )
        return 2
    only = set(args.only.split(",")) if args.only else set(STEPS)
    when = datetime.now()
    out_dir = cfg.root / cfg.report_dir / when.strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    # architecture.toml 이 있으면 3단계는 설계 검사가 대신한다 (계층·순환·외부 라이브러리 포함)
    has_spec = (cfg.root / "architecture.toml").is_file()
    runners = {
        "ruff": lambda: tools.run_ruff(cfg, fix=args.fix),
        "mypy": lambda: tools.run_mypy(cfg),
        "import-linter": (lambda: tools.run_arch(cfg))
        if has_spec
        else (lambda: tools.run_import_linter(cfg)),
        "struct": lambda: run_struct(cfg),
        "cycles": lambda: tools.run_cycles(cfg, out_dir, report_findings=not has_spec),
        "vulture": lambda: tools.run_vulture(cfg),
        "radon": lambda: tools.run_radon(cfg),
        "bandit": lambda: tools.run_bandit(cfg),
        "pytest": lambda: tools.run_pytest(cfg),
    }
    print(f"audit-kit {__version__} — {cfg.root} (패키지: {', '.join(cfg.packages)})")
    scope = tools.run_scope(cfg)  # 항상 실행: 무엇을 검사하고 무엇이 빠졌는지 먼저 보고
    print(f"  [0/{len(STEPS)}] 검사 범위 ... {scope.detail}")
    results = [scope]
    for i, name in enumerate(STEPS, 1):
        if name not in only:
            continue
        label = "arch(설계)" if name == "import-linter" and has_spec else name
        print(f"  [{i}/{len(STEPS)}] {label} ...", end="", flush=True)
        r = runners[name]()
        results.append(r)
        show = r.detail and (r.status != "findings" or r.detail.startswith("⚠"))
        print(
            f" {r.status} ({len(r.findings)}건, {r.duration:.1f}s)"
            + (f" — {r.detail[:200]}" if show else "")
        )
    h = run_heuristics(cfg)
    results.append(h)
    dedupe(results)
    info = write_reports(cfg, results, out_dir, when, router_files(cfg))
    c = info["counts"]
    print(
        f"\n치명 {c.get('critical', 0)} / 개선 {c.get('improve', 0)} / AI 리뷰 필요 {c.get('review', 0)} / 무시 {c.get('ignore', 0)}"
    )
    health = compute_health_score(cfg, c)
    print(f"코드 건강도: {health.score}/100 ({health.grade}등급, {health.loc}줄 기준)")
    print(f"리포트: {out_dir / 'report.md'}")
    print(f"AI 리뷰 입력: {out_dir / 'ai-review.md'}  (Claude Code에서 /audit 로 9단계 진행)")
    run_exit = 0
    if args.fail_on == "critical" and c.get(CRITICAL):
        run_exit = 1
    if args.fail_on == "improve" and (c.get(CRITICAL) or c.get(IMPROVE)):
        run_exit = 1

    if getattr(args, "solo", False) or getattr(args, "only", None):
        return run_exit
    print("\n[세트] run 뒤에 std 를 이어서 실행합니다 (--solo 로 끄기)")
    std_exit = _run_paired_std(args)
    return max(run_exit, std_exit)


def _run_paired_std(args) -> int:
    """`run`이 끝난 뒤 `std`를 이어서 실행한다(2026-09-29: 사용자 지시로 run/std는 항상 세트).
    무한 재귀 방지: 이어서 실행하는 std 쪽은 반드시 solo=True 로 넘긴다."""
    import argparse

    from audit_kit.std.commands import cmd_std

    std_args = argparse.Namespace(
        path=args.path,
        rules=None,
        ruff_config=None,
        registry=None,
        no_mypy=False,
        fail_on=args.fail_on,
        fix=False,
        apply=False,
        verify=False,
        force=False,
        quiet=False,
        undo=False,
        solo=True,
    )
    return cmd_std(std_args)


def cmd_check(args) -> int:
    from audit_kit.hook import check_file

    failed = 0
    for f in args.files:
        p = Path(f).resolve()
        if p.suffix != ".py" or not p.is_file():
            continue
        msgs = check_file(p)
        if msgs:
            failed += 1
            print(f"{f}:")
            for m in msgs:
                print(f"  {m}")
    return 1 if failed else 0


def cmd_hook(_args) -> int:  # args.func(args) 로 균일하게 호출되는 인터페이스라 안 쓰여도 유지
    from audit_kit.hook import main

    return main()


def cmd_init(args) -> int:
    from audit_kit.init_project import init_project

    root = Path(args.path or ".")
    pkgs = args.packages.split(",") if args.packages else None
    for line in init_project(
        root, pkgs, with_hook=not args.no_hook, with_precommit=not args.no_precommit
    ):
        print(line)
    print(
        "\n다음 단계:\n  1) audit-kit arch init   구조 설계 초안(architecture.toml) → 검토 → arch check / arch fix\n"
        "                          (Claude Code 에서는 /arch)\n"
        "  2) audit-kit whitelist   (vulture 오탐 화이트리스트, 결과 검토 후 커밋)\n"
        "  3) audit-kit run         또는 Claude Code에서 /audit"
    )
    return 0


def cmd_whitelist(args) -> int:
    from audit_kit.config import load_config
    from audit_kit.tools import make_vulture_whitelist

    cfg = load_config(args.path)
    text = make_vulture_whitelist(cfg)
    wl = cfg.root / cfg.vulture_whitelist
    if wl.exists() and not args.force:
        print(f"{wl.name} 이 이미 있습니다. --force 로 덮어쓰거나 아래 내용을 수동 병합하세요.\n")
        print(text)
        return 1
    header = (
        "# vulture 화이트리스트 — audit-kit whitelist 로 생성.\n"
        "# 프레임워크가 호출하는(오탐) 항목만 남기고, 진짜 죽은 코드는 지우고 코드에서 삭제하세요.\n"
    )
    wl.write_text(header + text, encoding="utf-8")
    print(f"{wl} 생성 ({len(text.splitlines())}줄). 검토 후 커밋하세요.")
    return 0


def cmd_doctor(args) -> int:
    import shutil

    from audit_kit.config import load_config
    from audit_kit.runner import module_available
    from audit_kit.tools import has_importlinter_config

    cfg = load_config(args.path)
    print(f"python   : {sys.executable}")
    print(f"프로젝트 : {cfg.root}")
    print(
        f"패키지   : {', '.join(cfg.packages) or '(없음!)'} → {[cfg.rel(p) for p in cfg.package_paths()]}"
    )
    ok = True
    for mod in [
        "ruff",
        "mypy",
        "importlinter",
        "vulture",
        "radon",
        "bandit",
        "pytest",
        "pytest_cov",
    ]:
        has = module_available(mod)
        ok &= has
        print(f"  {'OK ' if has else 'NO '} {mod}")
    print(
        f"  {'OK ' if module_available('pydeps') and shutil.which('dot') else '-- '} pydeps+graphviz (선택: SVG 그래프)"
    )
    has_spec = (cfg.root / "architecture.toml").is_file()
    print(
        f"  {'OK ' if has_spec else '-- '} architecture.toml 구조 설계"
        + ("" if has_spec else " (audit-kit arch init)")
    )
    print(
        f"  {'OK ' if has_importlinter_config(cfg) or has_spec else 'NO '} import-linter 계약 설정"
        + (" (설계 파일이 대신함)" if has_spec and not has_importlinter_config(cfg) else "")
    )
    settings = cfg.root / ".claude" / "settings.json"
    hook = settings.is_file() and "audit_kit hook" in settings.read_text(encoding="utf-8")
    print(f"  {'OK ' if hook else 'NO '} Claude Code hook")
    print(
        f"  {'OK ' if (cfg.root / '.claude/skills/audit/SKILL.md').is_file() else 'NO '} /audit 스킬"
    )
    return 0 if ok and cfg.packages else 1


def cmd_pre_push(args) -> int:
    """git push 전 로컬 게이트: ci.yml 과 같은 무거운 검사(pytest 전체 + audit-kit std)를
    미리 돌려 실패하면 push 자체를 막는다.

    GitHub 개인 계정 + 비공개 저장소는 서버 쪽 branch protection/required status check API 가
    막혀 있다(GitHub Pro 필요 — 2026-09-27 실측 확인: 403 "Upgrade to GitHub Pro or make this
    repository public"). 그래서 서버가 아니라 여기서(`.pre-commit-config.yaml`의 pre-push 단계,
    `pre-commit install --hook-type pre-push`) 같은 효과를 낸다 — 실패하는 커밋은 애초에
    원격에 못 올라간다.
    """
    from audit_kit.config import load_config
    from audit_kit.runner import run_python

    cfg = load_config(args.path)
    if not (cfg.root / "tests").is_dir():
        print("tests/ 없음 — pre-push 검사 생략")
        return 0

    print(f"[pre-push] {cfg.root} — pytest 실행 중...")
    r = run_python(["-m", "pytest", "-q"], cfg.root, timeout=cfg.pytest_timeout)
    print((r.stdout + r.stderr).strip())
    if r.returncode != 0:
        print("[pre-push] pytest 실패 — push 를 막습니다.", file=sys.stderr)
        return 1

    print("[pre-push] audit-kit std --fail-on critical 실행 중...")
    # --solo: 위에서 이미 pytest 전체를 돌렸으므로 std 가 이어서 run(pytest 포함)을
    # 또 돌리면 중복이다 — pre-push 는 빠르게 유지한다.
    r = run_python(
        ["-m", "audit_kit", "std", "--path", str(cfg.root), "--fail-on", "critical", "--solo"],
        cfg.root,
        timeout=cfg.pytest_timeout,
    )
    print((r.stdout + r.stderr).strip())
    if r.returncode != 0:
        print("[pre-push] audit-kit std 치명 위반 — push 를 막습니다.", file=sys.stderr)
        return 1

    print("[pre-push] 통과 — push 계속 진행")
    return 0


def main(argv=None) -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(prog="audit-kit", description="Python 프로젝트 표준 감사 도구")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="전체 감사 + 리포트")
    p.add_argument(
        "--path", default=None, help="프로젝트 경로(기본: 현재 폴더에서 pyproject.toml 탐색)"
    )
    p.add_argument("--only", help=f"일부 단계만: {','.join(STEPS)}")
    p.add_argument("--no-tests", action="store_true", help="pytest 생략")
    p.add_argument("--fix", action="store_true", help="ruff --fix 적용")
    p.add_argument(
        "--fail-on",
        choices=["critical", "improve", "never"],
        default="critical",
        help="CI 종료코드 기준 (기본: 치명이 있으면 1)",
    )
    p.add_argument(
        "--solo",
        action="store_true",
        help="std 를 이어서 실행하지 않는다 (기본: run+std 세트로 실행, 2026-09-29 사용자 지시)",
    )
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("check", help="파일 단위 빠른 검사 (pre-commit)")
    p.add_argument("files", nargs="*")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("hook", help="Claude Code PostToolUse hook")
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("init", help="프로젝트에 설정·hook·스킬 설치")
    p.add_argument("--path", default=None)
    p.add_argument("--packages", help="감사 대상 패키지(쉼표 구분). 생략 시 자동 탐지")
    p.add_argument("--no-hook", action="store_true")
    p.add_argument("--no-precommit", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("whitelist", help="vulture 화이트리스트 생성")
    p.add_argument("--path", default=None)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_whitelist)

    p = sub.add_parser("doctor", help="설치·설정 점검")
    p.add_argument("--path", default=None)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser(
        "pre-push", help="git push 전 로컬 게이트 (pytest 전체 + std --fail-on critical)"
    )
    p.add_argument("--path", default=None)
    p.set_defaults(func=cmd_pre_push)

    from audit_kit.arch.commands import register
    from audit_kit.arch.tangle import register as register_tangle
    from audit_kit.baseline import register as register_baseline
    from audit_kit.cilocal import register as register_ci_local
    from audit_kit.deps import register as register_deps
    from audit_kit.fixflow import register as register_fix
    from audit_kit.ghcheck import register as register_gh_check
    from audit_kit.newproj import register as register_new
    from audit_kit.std.commands import register as register_std
    from audit_kit.structfix import register as register_struct

    # main() 문장 수(PLR0915)를 늘리지 않고 서브커맨드를 더하려고 한 줄 반복으로 등록한다(순서 유지).
    for register_command in (
        register,
        register_fix,
        register_struct,
        register_std,
        register_new,
        register_gh_check,
        register_tangle,
        register_baseline,
        register_ci_local,
        register_deps,
    ):
        register_command(sub)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

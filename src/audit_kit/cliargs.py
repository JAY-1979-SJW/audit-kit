"""`register()` 함수들이 공유하는 argparse 서브커맨드 조각.

`arch`와 `struct`의 fix/undo 서브커맨드가 거의 같은 --path/--apply/--verify/--force/
--quiet 골격을 반복해 썼다(dupscan 실측, 2026-09-28 — `audit_kit.arch.commands.register`와
`audit_kit.structfix.register`가 유사도 1.0으로 잡힘). 공통 뼈대만 여기로 옮기고,
서브커맨드마다 다른 도움말·전략 옵션(--only, --allow-lazy 등)은 그대로 각 register()에 둔다.
"""

from __future__ import annotations

import argparse


def add_path(p: argparse.ArgumentParser) -> None:
    p.add_argument("--path")


def add_fix_flags(
    p: argparse.ArgumentParser,
    *,
    apply_help: str | None = None,
    verify_help: str = "적용 전 pytest 결과 비교",
    force_help: str | None = None,
) -> None:
    """--fix 계열(기본 미리보기) 서브커맨드가 공통으로 쓰는 --apply/--verify/--force/--quiet."""
    p.add_argument("--apply", action="store_true", help=apply_help)
    p.add_argument("--verify", action="store_true", help=verify_help)
    p.add_argument("--force", action="store_true", help=force_help)
    p.add_argument("--quiet", action="store_true", help="미리보기 diff 출력 생략")


def add_undo(sub, *, target: str, func, extra_help: str = "") -> argparse.ArgumentParser:
    """`마지막 <target> fix --apply 되돌리기` 서브커맨드(--path만 받음)."""
    q = sub.add_parser("undo", help=f"마지막 {target} fix --apply 되돌리기{extra_help}")
    add_path(q)
    q.set_defaults(func=func)
    return q

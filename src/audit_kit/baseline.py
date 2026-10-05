"""`audit-kit baseline`: 테스트 실패 목록(기준선)을 저장하고, 변경 뒤 새로 생긴 실패만 가려낸다.

배경(2026-09-30): 수정 전후 테스트를 비교하려고 `git stash` 로 수정을 걷어내고 실패 목록을 손으로
비교하는 일을 반복했다. `fix start`/`fix report` 에도 기준선 비교가 있지만 브랜치 생성·깨끗한
작업트리에 묶여 있어 단독으로 쓸 수 없다. 여기서는 같은 테스트 실행(`_failing_tests`)을 재사용해
"저장 → 비교"만 분리한다.

- 기준선은 `<report_dir>/baseline_<이름>.json` 에 저장한다(추적 파일을 새로 만들지 않도록 리포트 폴더 사용).
- 테스트가 추적 파일(예: data/reports/*.json)을 바꾸면 경고한다. 자동으로 되돌리지는 않는다.
- pytest 자체가 실행되지 않은 경우(수집 오류 등)는 "실패 0건"이 아니라 측정 실패로 취급해
  기준선을 저장하지 않는다(오류와 빈 결과는 다른 값이다).

종료코드: 0 새 실패 없음 / 1 새 실패 있음 / 2 실행할 수 없음(기준선 없음, pytest 실행 실패 등)
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from audit_kit.arch.commands import _failing_tests
from audit_kit.config import AuditConfig, load_config
from audit_kit.gitutil import run_git

NOT_RUN = "<pytest 실행 실패>"  # arch.commands._failing_tests 가 pytest 미실행 시 돌려주는 값
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_HASH_BYTES = 5 * 1024 * 1024
SHOW_LIMIT = 20

Snapshot = dict[str, tuple[str, str | None]]  # 경로 -> (porcelain 상태, 내용 해시 또는 None)
Effects = dict[str, list[str]]  # {"modified": [...], "created": [...]}
Runner = Callable[[AuditConfig], tuple[set[str], Effects]]


def baseline_path(cfg: AuditConfig, name: str) -> Path:
    return cfg.root / cfg.report_dir / f"baseline_{name}.json"


def _digest(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_HASH_BYTES:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def git_snapshot(root: Path) -> Snapshot | None:
    """git status 로 본 작업트리 상태. 수정된 추적 파일은 내용 해시까지 기록한다.
    git 저장소가 아니거나 git 이 없으면 None(판정 불가 — "변경 없음"으로 단정하지 않는다)."""
    try:
        p = run_git(
            root,
            "-c",
            "core.quotepath=false",
            "status",
            "--porcelain",
            "--untracked-files=all",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    snap: Snapshot = {}
    for line in p.stdout.splitlines():
        if len(line) < 4:
            continue
        status, path = line[:2], line[3:].strip()
        if " -> " in path:  # 이름 바꾸기: 새 경로만
            path = path.split(" -> ")[-1]
        digest = None if status == "??" else _digest(root / path)
        snap[path] = (status, digest)
    return snap


def diff_snapshots(before: Snapshot | None, after: Snapshot | None) -> Effects:
    """테스트 실행 전후 비교: 테스트가 바꾼 추적 파일(modified)과 새로 만든 파일(created)."""
    out: Effects = {"modified": [], "created": []}
    if before is None or after is None:
        return out
    for path, (status, digest) in sorted(after.items()):
        prev = before.get(path)
        if prev == (status, digest):
            continue
        if status == "??":
            if prev is None:
                out["created"].append(path)
        else:
            out["modified"].append(path)
    return out


def run_tests(cfg: AuditConfig) -> tuple[set[str], Effects]:
    before = git_snapshot(cfg.root)
    failing = _failing_tests(cfg.root, cfg.pytest_timeout)
    return failing, diff_snapshots(before, git_snapshot(cfg.root))


def head_sha(root: Path) -> str | None:
    try:
        p = run_git(root, "rev-parse", "HEAD", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def _print_effects(effects: Effects) -> None:
    if effects["modified"]:
        print(
            f"\n[주의] 테스트 실행이 git 추적 파일 {len(effects['modified'])}개를 바꿨습니다"
            " (생성 시각 갱신 등). 커밋에 섞이지 않게 확인하세요:"
        )
        for p in effects["modified"][:SHOW_LIMIT]:
            print(f"  - {p}")
        print(
            "  내가 고친 파일이 아니라면: git checkout -- <파일> 로 되돌리세요(자동으로 되돌리지 않습니다)."
        )
    if effects["created"]:
        print(f"[참고] 테스트 실행이 새 파일 {len(effects['created'])}개를 만들었습니다.")


def _print_list(title: str, names: list[str]) -> None:
    print(f"\n{title}: {len(names)}건")
    for n in names[:SHOW_LIMIT]:
        print(f"  - {n}")
    if len(names) > SHOW_LIMIT:
        print(f"  ... 외 {len(names) - SHOW_LIMIT}건")


def _precheck(cfg: AuditConfig, name: str) -> int | None:
    if not NAME_RE.match(name):
        print("기준선 이름은 영문·숫자·_·.·- 만 쓸 수 있습니다(최대 64자).", file=sys.stderr)
        return 2
    if not (cfg.root / "tests").is_dir():
        print(f"{cfg.root} 에 tests/ 폴더가 없어 기준선을 만들 수 없습니다.", file=sys.stderr)
        return 2
    return None


def _not_run_message() -> None:
    print(
        "pytest 가 실행되지 않았습니다(수집 오류·설치 문제 등). '실패 0건'이 아니라 측정 실패입니다.\n"
        "다음에 할 일: `python -m pytest -q` 를 직접 실행해 오류 메시지를 확인하세요.",
        file=sys.stderr,
    )


def save(cfg: AuditConfig, name: str = "default", runner: Runner = run_tests) -> int:
    """지금 작업트리의 테스트 실패 목록을 기준선으로 저장한다."""
    bad = _precheck(cfg, name)
    if bad is not None:
        return bad
    print(f"[baseline] {cfg.root} — 테스트 실행 중(현재 작업트리 기준)...")
    failing, effects = runner(cfg)
    if NOT_RUN in failing:
        _not_run_message()
        return 2
    path = baseline_path(cfg, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema": 1,
        "name": name,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": head_sha(cfg.root),
        "python": sys.version.split()[0],
        "count": len(failing),
        "failures": sorted(failing),
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"기준선 저장: 실패 {len(failing)}건 → {path}")
    _print_effects(effects)
    print(
        "\n다음에 할 일: 코드를 수정한 뒤 `audit-kit baseline compare` 로 새로 생긴 실패만 확인하세요."
    )
    return 0


def compare(cfg: AuditConfig, name: str = "default", runner: Runner = run_tests) -> int:
    """기준선과 지금의 실패 목록을 비교해 '내 변경이 만든 새 실패'만 알려준다."""
    bad = _precheck(cfg, name)
    if bad is not None:
        return bad
    path = baseline_path(cfg, name)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        base = set(saved["failures"])
    except (OSError, ValueError, KeyError, TypeError):
        print(
            f"기준선을 읽을 수 없습니다: {path}\n다음에 할 일: 수정 전에 `audit-kit baseline save` 를 먼저 실행하세요.",
            file=sys.stderr,
        )
        return 2
    print(
        f"[baseline] 기준선 '{name}' (실패 {len(base)}건, 커밋 {str(saved.get('commit'))[:7]}) 와 비교 중..."
    )
    failing, effects = runner(cfg)
    if NOT_RUN in failing:
        _not_run_message()
        return 2
    new = sorted(failing - base)
    fixed = sorted(base - failing)
    still = sorted(base & failing)
    _print_list("새로 생긴 실패", new)
    _print_list("고쳐진 테스트(기준선에는 실패)", fixed)
    print(
        f"\n기준선에도 있던 실패: {len(still)}건 (이번 변경과 무관 — 데이터 파일 누락 등 원래 문제일 수 있음)"
    )
    _print_effects(effects)
    if new:
        print(
            f"\n결론: 새 실패 {len(new)}건 — 이번 변경이 원인일 수 있습니다. 위 테스트를 먼저 확인하세요."
        )
        return 1
    print("\n결론: 새 실패 없음 — 기준선과 같거나 더 좋아졌습니다.")
    return 0


def cmd_baseline(args) -> int:
    cfg = load_config(args.path)
    if args.action == "save":
        return save(cfg, args.name)
    return compare(cfg, args.name)


def register(sub) -> None:
    p = sub.add_parser(
        "baseline", help="테스트 실패 기준선 저장·비교 (내 변경이 만든 새 실패만 가려냄)"
    )
    actions = p.add_subparsers(dest="action", required=True)
    for action, helptext in (
        ("save", "지금 작업트리의 테스트 실패 목록을 기준선으로 저장 (코드를 고치기 전에 실행)"),
        ("compare", "기준선과 비교해 새로 생긴 실패만 표시 (새 실패가 있으면 종료코드 1)"),
    ):
        q = actions.add_parser(action, help=helptext)
        q.add_argument("--path", default=None)
        q.add_argument("--name", default="default", help="기준선 이름(기본 default)")
        q.set_defaults(func=cmd_baseline)

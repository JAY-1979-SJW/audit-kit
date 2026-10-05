"""`audit-kit gh-check`: 방금 push한 커밋의 GitHub Actions 결과를 실제로 확인한다.

로컬 pre-push(cli.py 의 `pre-push`)가 통과해도 원격 CI 는 환경이 달라(OS, 캐시된 의존성,
matrix 의 다른 Python 버전 등) 따로 실패할 수 있다 — 로컬 통과와 원격 통과는 별개 사실이라
push 뒤에 실제로 원격 결과를 확인해야 한다.

2026-09-27 결정(사용자 확인): 이 확인은 **audit-kit 전용 명령으로만** 만든다. `py_stop_check.py`
같은 전역 Stop hook 은 모든 프로젝트·세션에서 매 턴 돈다 — 거기에 네트워크 호출(GitHub API 폴링)을
강제로 추가하는 건 범위가 과하다고 판단해, `git push` 뒤 사람이나 스킬이 명시적으로 호출하는
audit-kit 명령으로 좁혔다. 완료될 때까지 짧게 폴링하되(기본 최대 300초) 무한 대기는 하지 않는다.

`gh` 공식 CLI 를 그대로 쓴다(GitHub REST API 인증을 새로 구현하지 않음 — audit-kit 의 기존
방침과 동일하게 이미 있는 신뢰된 도구를 재사용).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_TIMEOUT = 300
DEFAULT_INTERVAL = 10
TERMINAL_OK = {"success", "neutral", "skipped"}


class GhCheckError(Exception):
    """gh CLI 호출 실패 등, 더 진행할 수 없는 상태."""


def _run_gh(args: list, cwd: Path, timeout: int = 30) -> str:
    try:
        p = subprocess.run(
            ["gh", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GhCheckError(str(e)) from e
    if p.returncode != 0:
        raise GhCheckError((p.stderr or p.stdout).strip())
    return p.stdout


def head_sha(root: Path) -> str:
    p = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if p.returncode != 0 or not p.stdout.strip():
        raise GhCheckError("git 저장소가 아니거나 커밋이 없습니다")
    return p.stdout.strip()


def repo_slug(root: Path) -> str:
    out = _run_gh(["repo", "view", "--json", "nameWithOwner"], root)
    return json.loads(out)["nameWithOwner"]


def fetch_check_runs(root: Path, repo: str, sha: str) -> list:
    out = _run_gh(["api", f"repos/{repo}/commits/{sha}/check-runs"], root)
    return json.loads(out).get("check_runs", [])


def poll(
    root: Path,
    repo: str,
    sha: str,
    timeout: int = DEFAULT_TIMEOUT,
    interval: int = DEFAULT_INTERVAL,
    sleep=time.sleep,
    now=time.monotonic,
) -> tuple[str, list]:
    """`("ok"|"failed"|"pending", check_runs)`. "pending"은 제한 시간 안에 안 끝난 상태.

    `sleep`/`now` 를 주입 가능하게 한 건 실제 시간을 기다리지 않고 폴링 로직(반복·제한시간)만
    테스트하기 위함 — 실제 CLI 호출은 언제나 `_run_gh`(실제 gh 프로세스)를 그대로 쓴다.

    STD-08(PLR0913, 인자 7>6): `sleep`/`now` 를 객체로 묶으면 tests/test_ghcheck.py 의 모든
    호출부가 `Clock(sleep=..., now=...)` 래핑을 추가로 해야 해서 테스트 가독성이 떨어진다.
    표준 DI-for-테스트 패턴이라 그대로 두기로 함(2026-09-28)."""
    deadline = now() + timeout
    while True:
        runs = fetch_check_runs(root, repo, sha)
        incomplete = [r for r in runs if r.get("status") != "completed"]
        if runs and not incomplete:
            failed = [r for r in runs if r.get("conclusion") not in TERMINAL_OK]
            return ("failed" if failed else "ok"), runs
        if now() >= deadline:
            return "pending", runs
        sleep(interval)


def cmd_gh_check(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve() if args.path else Path.cwd()
    if not shutil.which("gh"):
        print("gh CLI 가 없습니다 — https://cli.github.com 설치 필요", file=sys.stderr)
        return 2
    try:
        sha = args.sha or head_sha(root)
        repo = repo_slug(root)
        print(f"{repo} @ {sha[:8]} 의 GitHub Actions 결과 확인 중 (최대 {args.timeout}s)...")
        status, runs = poll(root, repo, sha, timeout=args.timeout, interval=args.interval)
    except GhCheckError as e:
        print(f"확인 실패: {e}", file=sys.stderr)
        return 2
    if not runs:
        print(
            "체크가 하나도 없습니다 — 이 커밋에 CI 워크플로가 걸려 있는지 확인하세요",
            file=sys.stderr,
        )
        return 2
    for r in runs:
        if r.get("status") != "completed":
            mark = "… "
        else:
            mark = "✔" if r.get("conclusion") in TERMINAL_OK else "✘"
        print(f"  {mark} {r.get('name')}: {r.get('status')}/{r.get('conclusion')}")
    if status == "pending":
        print(
            f"\n제한 시간({args.timeout}s) 안에 끝나지 않았습니다(아직 진행 중) — "
            "나중에 다시 확인하세요",
            file=sys.stderr,
        )
        return 3
    if status == "failed":
        print("\nGitHub Actions 실패", file=sys.stderr)
        return 1
    print("\nGitHub Actions 전부 통과")
    return 0


def register(sub) -> None:
    p = sub.add_parser("gh-check", help="방금 push한 커밋의 GitHub Actions 결과를 폴링해서 확인")
    p.add_argument("--path", default=None)
    p.add_argument("--sha", default=None, help="확인할 커밋(기본: HEAD)")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="최대 대기 초 (기본 300)")
    p.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="폴링 간격 초 (기본 10)")
    p.set_defaults(func=cmd_gh_check)

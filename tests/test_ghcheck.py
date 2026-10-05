"""`audit-kit gh-check` 시험.

`poll()`은 `sleep`/`now`를 주입받아, 실제 시간을 기다리지 않고도 "완료까지 반복", "제한 시간
초과" 로직을 검증한다(ghcheck.py 의 docstring 참고). `gh` CLI 호출 자체(`fetch_check_runs`)는
monkeypatch 로 가짜 응답을 준다 — 실제 네트워크는 아래 `test_cmd_gh_check_against_real_repo`
하나에서만 쓴다(이미 push·CI green 이 확인된 이 저장소 자신의 HEAD 를 실제로 조회).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from audit_kit import ghcheck


def _runs(*conclusions_and_status: tuple[str, str, str]) -> list:
    return [
        {"name": name, "status": status, "conclusion": conclusion}
        for name, status, conclusion in conclusions_and_status
    ]


# ------------------------------------------------------------ poll 함수
def test_poll_returns_ok_when_all_succeed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ghcheck,
        "fetch_check_runs",
        lambda root, repo, sha: (calls.append(1), _runs(("test (3.11)", "completed", "success")))[
            1
        ],
    )
    status, runs = ghcheck.poll(Path(), "o/r", "sha", timeout=100, interval=1, sleep=lambda s: None)
    assert status == "ok" and len(runs) == 1 and len(calls) == 1  # 첫 조회에서 바로 끝남


def test_poll_returns_failed_when_any_check_fails(monkeypatch):
    monkeypatch.setattr(
        ghcheck,
        "fetch_check_runs",
        lambda root, repo, sha: _runs(
            ("test (3.11)", "completed", "success"), ("test (3.14)", "completed", "failure")
        ),
    )
    status, runs = ghcheck.poll(Path(), "o/r", "sha", timeout=100, interval=1, sleep=lambda s: None)
    assert status == "failed" and len(runs) == 2


def test_poll_retries_while_pending_then_succeeds(monkeypatch):
    """실제 CI 는 몇 분 걸리므로 처음엔 in_progress, 그다음 조회에서 completed 가 되는 상황."""
    responses = iter([
        _runs(("test (3.11)", "in_progress", None)),
        _runs(("test (3.11)", "completed", "success")),
    ])
    slept = []
    monkeypatch.setattr(ghcheck, "fetch_check_runs", lambda root, repo, sha: next(responses))
    status, _ = ghcheck.poll(Path(), "o/r", "sha", timeout=100, interval=5, sleep=slept.append)
    assert status == "ok"
    assert slept == [5]  # 한 번만 대기하고 두 번째 조회에서 끝남


def test_poll_times_out_while_still_pending():
    """시간을 실제로 흐르게 하지 않고, `now`를 직접 주입해 제한 시간 초과 경로를 확인한다."""
    clock = iter([0, 0, 100, 200])  # deadline=0+100=100; 두 번째 확인(200)에서 초과
    always_pending = lambda root, repo, sha: _runs(("test", "in_progress", None))  # ruff: ignore[lambda-assignment]
    import audit_kit.ghcheck as gc

    orig = gc.fetch_check_runs
    gc.fetch_check_runs = always_pending
    try:
        status, _ = gc.poll(
            Path(),
            "o/r",
            "sha",
            timeout=100,
            interval=1,
            sleep=lambda s: None,
            now=lambda: next(clock),
        )
    finally:
        gc.fetch_check_runs = orig
    assert status == "pending"


# ------------------------------------------------------------ cmd_gh_check (CLI 출력·종료코드)
class _Args:
    def __init__(self, **kw):
        self.path = None
        self.sha = None
        self.timeout = 100
        self.interval = 1
        self.__dict__.update(kw)


def test_cmd_gh_check_reports_success(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(ghcheck.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(ghcheck, "head_sha", lambda root: "abc123")
    monkeypatch.setattr(ghcheck, "repo_slug", lambda root: "o/r")
    monkeypatch.setattr(
        ghcheck, "poll", lambda *a, **kw: ("ok", _runs(("test (3.11)", "completed", "success")))
    )
    rc = ghcheck.cmd_gh_check(_Args(path=str(tmp_path)))
    out = capsys.readouterr()
    assert rc == 0 and "전부 통과" in out.out


def test_cmd_gh_check_reports_failure(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(ghcheck.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(ghcheck, "head_sha", lambda root: "abc123")
    monkeypatch.setattr(ghcheck, "repo_slug", lambda root: "o/r")
    monkeypatch.setattr(
        ghcheck,
        "poll",
        lambda *a, **kw: ("failed", _runs(("test (3.14)", "completed", "failure"))),
    )
    rc = ghcheck.cmd_gh_check(_Args(path=str(tmp_path)))
    out = capsys.readouterr()
    assert rc == 1 and "실패" in out.err


def test_cmd_gh_check_reports_pending_as_distinct_exit_code(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(ghcheck.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(ghcheck, "head_sha", lambda root: "abc123")
    monkeypatch.setattr(ghcheck, "repo_slug", lambda root: "o/r")
    monkeypatch.setattr(
        ghcheck, "poll", lambda *a, **kw: ("pending", _runs(("test (3.11)", "in_progress", None)))
    )
    rc = ghcheck.cmd_gh_check(_Args(path=str(tmp_path)))
    out = capsys.readouterr()
    assert rc == 3 and "진행 중" in out.err


def test_cmd_gh_check_without_gh_cli(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(ghcheck.shutil, "which", lambda name: None)
    rc = ghcheck.cmd_gh_check(_Args(path=str(tmp_path)))
    assert rc == 2 and "gh CLI" in capsys.readouterr().err


def test_cmd_gh_check_no_checks_at_all(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(ghcheck.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(ghcheck, "head_sha", lambda root: "abc123")
    monkeypatch.setattr(ghcheck, "repo_slug", lambda root: "o/r")
    monkeypatch.setattr(ghcheck, "poll", lambda *a, **kw: ("ok", []))
    rc = ghcheck.cmd_gh_check(_Args(path=str(tmp_path)))
    assert rc == 2 and "체크가 하나도 없습니다" in capsys.readouterr().err


# ------------------------------------------------------------ 실기 검증 (실제 gh, 실제 네트워크)
def _gh_available() -> bool:
    from shutil import which

    return which("gh") is not None


def _skip_real_gh_check_reason() -> str:
    """실기(진짜 gh api 호출) 시험을 건너뛸 이유. 없으면 빈 문자열.

    2026-09-27 실측으로 발견한 CI 회귀: 이 시험이 GitHub Actions 러너 안에서 그대로 돌면
    `gh api`가 인증 실패한다("GH_TOKEN 환경변수 설정 필요") — 러너의 `gh` 는 기본적으로
    로그인돼 있지 않고, 이 시험이 확인하려는 대상(방금 그 런의 체크런)은 애초에 그 런이
    끝나기 전이라 "완료" 상태일 수도 없다(자기 자신을 기다리는 모순). `GITHUB_ACTIONS`는
    GitHub 공식 문서가 모든 Actions 런에서 항상 "true"로 설정된다고 보장하는 환경변수라
    이걸로 판별한다 — 이 실기 시험은 사람이 로컬에서(이미 `gh auth login` 된 상태로) 돌릴
    때만 의미가 있다."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return "GitHub Actions 러너 안 — gh 인증(GH_TOKEN) 없음, 로컬 전용 시험이라 생략"
    if not _gh_available():
        return "gh CLI 미설치 환경 — 실기 검증 생략"
    return ""


def _remote_head_sha(root: Path) -> str | None:
    """실제 GitHub 원격의 기본 브랜치 최신 SHA. 로컬 HEAD 를 쓰지 않는 이유(2026-09-27 실측으로
    발견한 버그): 이 저장소는 내가 커밋하고 다른 세션이 push 를 대신하는 워크플로라, 로컬 HEAD 가
    아직 원격에 없을 수 있다 — 실제로 `test_cmd_gh_check_against_real_repo`가 막 커밋한 로컬
    HEAD 로 돌다가 "gh: No commit found for SHA" (422)로 실패했다. 로컬 상태에 기대지 않고
    원격에 실제로 있는 커밋을 gh api 로 직접 물어봐야 이 테스트가 안정적이다(ghcheck 의 기존
    `_run_gh`/`repo_slug` 를 그대로 재사용 — subprocess 호출을 또 새로 안 만듦)."""
    try:
        repo = ghcheck.repo_slug(root)
        out = ghcheck._run_gh(["api", f"repos/{repo}/commits/HEAD", "--jq", ".sha"], root)
    except ghcheck.GhCheckError:
        return None
    return out.strip() or None


@pytest.mark.skipif(bool(_skip_real_gh_check_reason()), reason=_skip_real_gh_check_reason())
def test_cmd_gh_check_against_real_repo():
    """[실기] 이 저장소의 실제 원격(GitHub) 최신 커밋에 대해 진짜 gh api 를 호출한다.

    2026-09-27 실측으로 발견한 두 번째 설계 결함: 이 시험은 "대상 커밋의 CI 가 통과했을
    것"이라고 가정하면 안 된다 — 실제로 이 파일 자신의 첫 번째 버그(위 두 문단 참고) 때문에
    특정 커밋이 진짜로 CI 에 실패한 적이 있어, 그 커밋이 원격 최신이던 동안은 "성공을 가정하는"
    검증이 스스로 깨졌다. 그래서 "CI 가 통과했는가"가 아니라 "`gh-check`가 같은 원격 데이터를
    보고 정확한 판정을 내리는가"만 확인한다 — 같은 `fetch_check_runs` 결과로 직접 계산한
    기대값과 `cmd_gh_check`의 실제 반환값을 비교한다(로컬 HEAD 대신 원격 SHA 를 쓰는 이유는
    `_remote_head_sha` 참고)."""
    root = Path(__file__).resolve().parents[1]
    sha = _remote_head_sha(root)
    if sha is None:
        pytest.skip("원격 HEAD SHA 를 못 가져옴 — 네트워크/gh 인증 문제로 추정, 실기 검증 생략")
    repo = ghcheck.repo_slug(root)
    runs = ghcheck.fetch_check_runs(root, repo, sha)
    if not runs:
        pytest.skip(f"{sha[:8]} 에 체크런이 없음 — 실기 검증 대상이 아님")
    incomplete = [r for r in runs if r.get("status") != "completed"]
    expected_rc = None
    if not incomplete:
        failed = [r for r in runs if r.get("conclusion") not in ghcheck.TERMINAL_OK]
        expected_rc = 1 if failed else 0
    rc = ghcheck.cmd_gh_check(_Args(path=str(root), sha=sha, timeout=30, interval=5))
    if expected_rc is not None:
        assert rc == expected_rc, f"{sha[:8]} 의 실제 체크런과 gh-check 반환값({rc})이 안 맞음"
    else:
        assert rc in (0, 1, 3)  # 폴링하는 동안 진행 중이던 게 끝났을 수도 있어 셋 다 정상

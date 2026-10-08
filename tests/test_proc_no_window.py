"""audit_kit._proc 시험 — Windows 콘솔 깜빡임 방지 + 자손 프로세스 정리 헬퍼."""

from __future__ import annotations

import subprocess
import sys
import time
from unittest import mock

import pytest

from audit_kit._proc import kill_tree, no_window_kwargs


def test_returns_create_no_window_flag_on_windows():
    with mock.patch("audit_kit._proc.sys.platform", "win32"):
        assert no_window_kwargs() == {"creationflags": subprocess.CREATE_NO_WINDOW}


def test_returns_empty_dict_on_non_windows():
    with mock.patch("audit_kit._proc.sys.platform", "linux"):
        assert no_window_kwargs() == {}


def test_returns_empty_dict_on_darwin():
    with mock.patch("audit_kit._proc.sys.platform", "darwin"):
        assert no_window_kwargs() == {}


def test_new_group_adds_new_process_group_flag_on_windows():
    with mock.patch("audit_kit._proc.sys.platform", "win32"):
        flags = no_window_kwargs(new_group=True)["creationflags"]
        assert flags & subprocess.CREATE_NO_WINDOW
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP


def test_new_group_starts_new_session_on_posix():
    with mock.patch("audit_kit._proc.sys.platform", "linux"):
        assert no_window_kwargs(new_group=True) == {"start_new_session": True}
    with mock.patch("audit_kit._proc.sys.platform", "linux"):
        assert no_window_kwargs(new_group=False) == {}


def test_kill_tree_on_already_dead_pid_does_not_raise():
    """이미 끝난 프로세스(그룹)를 다시 죽이려 해도 호출 쪽이 멈추지 않아야 한다."""
    dead = subprocess.Popen(
        [sys.executable, "-c", "pass"], **no_window_kwargs(new_group=True)
    )
    dead.wait(timeout=10)
    kill_tree(dead.pid)  # 예외 없이 끝나면 통과


@pytest.mark.skipif(sys.platform != "win32", reason="taskkill /T 는 Windows 전용 경로")
def test_kill_tree_terminates_grandchild_process():
    """부모뿐 아니라 자손(그랜드차일드)까지 종료되어야 한다 — 부모만 죽이면 자손이 고아로
    남아 파이프를 쥐고 있을 수 있던 문제(2026-10-08, PR #160 CI verify 90분 초과 조사)."""
    marker_script = (
        "import subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "print(p.pid, flush=True); "
        "time.sleep(60)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", marker_script],
        stdout=subprocess.PIPE,
        text=True,
        **no_window_kwargs(new_group=True),
    )
    grandchild_pid = int(parent.stdout.readline().strip())
    time.sleep(0.5)  # 그랜드차일드가 실제로 뜰 시간
    kill_tree(parent.pid)
    parent.wait(timeout=10)
    # tasklist 에 더는 그랜드차일드 pid 가 보이지 않아야 한다
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {grandchild_pid}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert str(grandchild_pid) not in out, out

"""audit_kit.runner.run_python 시험 — 시간 초과 때 자손 프로세스까지 정리하는지."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from audit_kit.runner import run_python


def test_run_python_returns_timeout_proc(tmp_path: Path):
    p = run_python(["-c", "import time; time.sleep(5)"], tmp_path, timeout=1)
    assert p.returncode == -1
    assert "시간 초과" in p.stderr


def test_run_python_normal_call_still_works(tmp_path: Path):
    p = run_python(["-c", "print('ok')"], tmp_path, timeout=10)
    assert p.returncode == 0
    assert p.stdout.strip() == "ok"


@pytest.mark.skipif(sys.platform != "win32", reason="tasklist 로 생존 확인 — Windows 전용")
def test_run_python_timeout_kills_grandchild_process(tmp_path: Path):
    """시간 초과로 끝낸 호출의 자손(그랜드차일드)도 함께 종료되어야 한다 — 부모만 죽이면
    자손이 남아 파이프를 쥔 채 멈춰 있던 문제(2026-10-08, PR #160 CI verify 90분 초과)."""
    marker = tmp_path / "grandchild_pid.txt"
    script = (
        "import subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open(r'{marker}', 'w').write(str(p.pid)); "
        "time.sleep(60)"
    )
    run_python(["-c", script], tmp_path, timeout=2)
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    pid = int(marker.read_text().strip())
    import subprocess

    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, check=False
    ).stdout
    assert str(pid) not in out, out

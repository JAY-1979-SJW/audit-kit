"""audit_kit.runner.run_python 시험 — 시간 초과 때 자손 프로세스까지 정리하는지."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

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
    자손이 남아 파이프를 쥔 채 멈춰 있던 문제(대형 저장소 CI 에서 관찰, 2026-10-08)."""
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
        ["tasklist", "/FI", f"PID eq {pid}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",  # tasklist 는 로케일(예: cp949)로 출력 — pid 숫자만 보면 되니 깨져도 무방
        check=False,
    ).stdout
    assert str(pid) not in out, out


def test_run_python_does_not_hang_when_grandchild_still_holds_pipe(tmp_path: Path, monkeypatch):
    """`kill_tree` 뒤에도 자손(그랜드차일드)이 파이프 쓰기 쪽을 쥐고 있어 두 번째
    `communicate(timeout=5)`마저 시간 초과하면, 무한정 기다리지 말고(원래 증상 재현을
    피해) 파이프를 닫고 빈 출력으로 즉시 돌아와야 한다."""
    real_popen = subprocess.Popen

    class _HangingProc:
        def __init__(self, *a, **kw):
            self._inner = real_popen(*a, **kw)
            self.pid = self._inner.pid
            self.stdout = self._inner.stdout
            self.stderr = self._inner.stderr
            self._calls = 0

        def communicate(self, timeout=None):
            self._calls += 1
            if self._calls >= 2:  # kill_tree 뒤 재시도도 자손이 파이프를 쥐고 있어 또 시간 초과
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout or 0)
            return self._inner.communicate(timeout=timeout)

    created: list[_HangingProc] = []
    original_init = _HangingProc.__init__

    def _tracked_init(self, *a, **kw):
        original_init(self, *a, **kw)
        created.append(self)

    _HangingProc.__init__ = _tracked_init
    monkeypatch.setattr("audit_kit.runner.subprocess.Popen", _HangingProc)
    try:
        with mock.patch("audit_kit.runner.kill_tree") as killed:
            start = time.monotonic()
            p = run_python(["-c", "import time; time.sleep(60)"], tmp_path, timeout=1)
            elapsed = time.monotonic() - start
        assert killed.called
        assert elapsed < 15, f"무한 대기 없이 빠르게 돌아와야 한다 (걸린 시간 {elapsed:.1f}s)"
        assert p.returncode == -1
        assert p.stdout == "" and "시간 초과" in p.stderr
    finally:
        _HangingProc.__init__ = original_init
        for proc in created:  # kill_tree 를 mock 했으니 실제 하위 프로세스는 직접 정리
            proc._inner.kill()
            proc._inner.wait(timeout=10)

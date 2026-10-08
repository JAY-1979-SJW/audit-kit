"""외부 도구 실행 공통 함수."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from audit_kit._proc import kill_tree, no_window_kwargs


@dataclass
class Proc:
    returncode: int
    stdout: str
    stderr: str
    missing: bool = False


def module_available(module: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(module) is not None


def run_python(args: list, cwd: Path, timeout: int = 600) -> Proc:
    """현재 파이썬 인터프리터로 `python <args>` 실행. 대상 프로젝트 venv 안에서 돌아야 mypy/pytest가 의존성을 찾는다.

    시간 초과 때는 부모만 죽이지 않고 프로세스 트리(그룹) 전체를 종료한다 — 부모만 죽이면
    자손(예: mypy/ruff 가 내부적으로 띄운 프로세스)이 고아로 남아 파이프를 쥔 채 멈춰 있을 수
    있다(2026-10-08, PR #160 CI verify 90분 초과 조사에서 관찰).
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.setdefault("NO_COLOR", "1")
    proc = subprocess.Popen(
        [sys.executable, *args],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        **no_window_kwargs(new_group=True),
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        return Proc(-1, _decode(out), f"시간 초과({timeout}s) — 프로세스 트리를 정리했습니다")
    return Proc(proc.returncode, _decode(out), _decode(err))


def run_module(module: str, args: list, cwd: Path, timeout: int = 600) -> Proc:
    if not module_available(module.split(".")[0]):
        return Proc(-1, "", f"{module} 미설치 (pip install audit-kit 로 함께 설치됨)", missing=True)
    return run_python(["-m", module, *args], cwd, timeout)


def _decode(b) -> str:
    if b is None:
        return ""
    if isinstance(b, str):
        return b
    return b.decode("utf-8", errors="replace")

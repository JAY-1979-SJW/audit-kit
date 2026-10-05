"""외부 도구 실행 공통 함수."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


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
    """현재 파이썬 인터프리터로 `python <args>` 실행. 대상 프로젝트 venv 안에서 돌아야 mypy/pytest가 의존성을 찾는다."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.setdefault("NO_COLOR", "1")
    try:
        cp = subprocess.run(
            [sys.executable, *args],
            cwd=str(cwd),
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        return Proc(-1, _decode(e.stdout), f"시간 초과({timeout}s)")
    return Proc(cp.returncode, _decode(cp.stdout), _decode(cp.stderr))


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

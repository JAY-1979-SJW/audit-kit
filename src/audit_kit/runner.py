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
    있다(대형 저장소 CI 에서 관찰, 2026-10-08). `kill_tree` 뒤에도 자손이 파이프 쓰기 쪽을
    쥐고 있으면 재시도 `communicate()`도 다시 시간 초과할 수 있다 — 그때는 `proc.stdout`/
    `proc.stderr`를 닫지 않고(내부 reader 스레드가 같은 파일 객체의 읽기 잠금을 쥐고 있으면
    `close()`가 그 스레드가 풀릴 때까지 똑같이 멈춘다 — 실측으로 확인, 2026-10-08) 그대로
    버려두고 빈 출력으로 즉시 돌아온다 — 핸들은 누수되지만(프로세스 종료 때 OS 가 정리) 호출
    쪽을 무한정 멈추지 않는 것이 우선이다.
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
            # 핸들을 일부러 닫지 않는다 — close()가 내부 reader 스레드의 읽기 잠금을 기다려
            # 똑같이 멈출 수 있다(위 docstring). 버려두고 바로 돌아온다.
            out, err = b"", b""
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

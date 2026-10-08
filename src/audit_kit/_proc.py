"""Windows 콘솔 창 깜빡임 방지 + 자손 프로세스 정리 공용 헬퍼.

커밋 훅 체인(호출 저장소의 git hook → python → audit-kit.exe hook → 이 패키지의
subprocess.run(["git", ...]))에서 자손 프로세스가 콘솔 없이 실행되면 Windows의
git.exe가 매번 새(보이는) conhost를 띄운다(실측: 2026-10-07, 다른 프로젝트
저장소에서 커밋마다 git 프로세스 각자 conhost 생성 — audit-kit.exe 자신에도 재현:
내부 git 호출이 끝나면 고아가 된 conhost가 audit-kit.exe의 자식으로 재부모화되어
관찰됨). subprocess.run(..., **no_window_kwargs())로 CREATE_NO_WINDOW 플래그를
주면 숨겨진다.

`run_python`(runner.py)이 시간 초과로 멈춘 호출을 끝낼 때, 부모(mypy/ruff 같은
파이썬 하위 명령)만 죽이면 그 자손(ruff/mypy 가 내부적으로 띄운 프로세스)이
고아로 남아 파이프를 쥐고 있을 수 있다(2026-10-08, PR #160 CI verify 90분
초과 조사에서 관찰). `no_window_kwargs(new_group=True)`로 자식을 새 프로세스
그룹(Windows)/세션(POSIX)에서 띄우고, `kill_tree()`로 통째로 종료한다.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys


def no_window_kwargs(new_group: bool = False) -> dict[str, int | bool]:
    """Windows면 {'creationflags': CREATE_NO_WINDOW}(+ new_group 이면 CREATE_NEW_PROCESS_GROUP 도 더함).

    그 외 플랫폼은 new_group 이면 {'start_new_session': True}(새 프로세스 그룹 — kill_tree 가
    자손까지 통째로 종료하려면 필요), new_group 이 아니면 빈 dict.
    """
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW
        if new_group:
            flags |= subprocess.CREATE_NEW_PROCESS_GROUP
        return {"creationflags": flags}
    return {"start_new_session": True} if new_group else {}


def kill_tree(pid: int) -> None:
    """시간 초과 등으로 멈춘 프로세스를 자손까지 통째로 종료한다.

    Windows: `taskkill /T /F`(프로세스 트리). POSIX: `no_window_kwargs(new_group=True)`로
    만든 프로세스 그룹 전체에 SIGKILL. 이미 끝난 프로세스(그룹)를 다시 죽이려 해도 조용히
    넘긴다 — 호출 쪽(runner.run_python)이 정리 실패로 멈추지 않게.
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pid, signal.SIGKILL)

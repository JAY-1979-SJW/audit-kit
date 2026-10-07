"""Windows 콘솔 창 깜빡임 방지 공용 헬퍼.

커밋 훅 체인(호출 저장소의 git hook → python → audit-kit.exe hook → 이 패키지의
subprocess.run(["git", ...]))에서 자손 프로세스가 콘솔 없이 실행되면 Windows의
git.exe가 매번 새(보이는) conhost를 띄운다(실측: 2026-10-07, 다른 프로젝트
저장소에서 커밋마다 git 프로세스 각자 conhost 생성 — audit-kit.exe 자신에도 재현:
내부 git 호출이 끝나면 고아가 된 conhost가 audit-kit.exe의 자식으로 재부모화되어
관찰됨). subprocess.run(..., **no_window_kwargs())로 CREATE_NO_WINDOW 플래그를
주면 숨겨진다.
"""

from __future__ import annotations

import subprocess
import sys


def no_window_kwargs() -> dict[str, int]:
    """Windows면 {'creationflags': subprocess.CREATE_NO_WINDOW}, 그 외 플랫폼은 빈 dict."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}

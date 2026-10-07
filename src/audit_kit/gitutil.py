"""git diff 기반 "이번 변경으로 생긴 줄 vs 원래 있던 줄" 판정.

Anthropic Code Review의 🟣Pre-existing(이번 PR이 원인 아닌 기존 버그) 태깅과 같은 구분을 결정적으로
(LLM 호출 없이) audit-kit 자체에 추가하기 위함(2026-09-28). `scope.py`의 `_git()`/`arch/commands.py`의
`_git_dirty()`와 같은 관례를 따른다: git 이 없거나 저장소가 아니면 예외를 올리지 않고 `None`(판정
불가)을 반환한다 — "전부 기존"으로 잘못 단정하지 않는다.

반환값은 항상 "그 파일 안에서 바뀐(새로 생긴) 줄 번호 집합"이다(추적 안 된 새 파일도 실제 줄 수를
읽어 1..N 전체를 채운다) — 호출부가 `줄 not in 집합` 하나로만 판정하면 되게, "새 파일" 전용
별도 값(sentinel)을 두지 않는다(2026-09-28, sentinel 을 뒀다가 `줄 not in None`으로 죽는 걸 실측으로
발견해 이 방식으로 바꿈).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from audit_kit._proc import no_window_kwargs

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def run_git(
    root: Path, *args: str, check: bool = False, timeout: int = 60
) -> subprocess.CompletedProcess:
    """git 서브프로세스를 그대로 실행한다(실패해도 예외를 삼키지 않음 — 실패 시 조용히 넘기고
    싶은 호출부는 직접 try/except 로 감싼다, 이 모듈의 `_git()`처럼). `fixflow.py`와 공유하는
    최소 공통부(2026-09-28 Stop hook 중복 코드 지적으로 분리)."""
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=check,
        **no_window_kwargs(),
    )


def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return run_git(root, *args)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _parse_diff_hunks(diff_text: str) -> dict:
    """`git diff --unified=0` 출력을 {파일 상대경로: 바뀐(새로 생긴) 줄 번호 집합}으로.
    순수 삭제(새 파일 쪽에 줄이 안 남는) hunk 는 제외한다."""
    result: dict = {}
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            current = None if path == "/dev/null" else path.removeprefix("b/")
            if current is not None:
                result.setdefault(current, set())
            continue
        m = _HUNK_RE.match(line)
        if m and current is not None:
            start = int(m.group(1))
            count = 1 if m.group(2) is None else int(m.group(2))
            if count:
                result[current].update(range(start, start + count))
    return result


def _whole_file_lines(root: Path, rel: str) -> set:
    """추적 안 된(untracked) 새 파일 — 실제 줄 수를 읽어 1..N 전체를 "새로 생긴 줄"로 본다."""
    try:
        n = len((root / rel).read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return set()
    return set(range(1, n + 1))


def changed_lines(root: Path) -> dict | None:
    """작업 트리를 마지막 커밋(HEAD)과 비교해 {파일 상대경로: 바뀐 줄 번호 집합}을 만든다. 아직
    커밋 안 된 새 파일(추적 여부 무관)도 실제 줄 수 기준 전체가 채워진다. 안 건드린 파일은 dict에
    아예 없다(= 전부 기존이라는 뜻). git 저장소가 아니거나 명령이 실패하면 `None`을 반환한다
    (판정 불가 — 빈 dict 와는 다른 의미)."""
    p = _git(root, "diff", "--unified=0", "HEAD")
    if p is None or p.returncode != 0:
        return None
    result: dict = _parse_diff_hunks(p.stdout)
    u = _git(root, "ls-files", "--others", "--exclude-standard")
    if u is not None and u.returncode == 0:
        for line in u.stdout.splitlines():
            rel = line.strip().strip('"')
            if rel:
                result[rel] = _whole_file_lines(root, rel)
    return result


def changed_lines_for_file(root: Path, rel: str) -> set | None:
    """`changed_lines`와 같은 판정을 파일 하나로 좁힌 버전(저장 시점 hook 용). git 자체를 못 쓰면
    `None`(판정 불가), 쓸 수 있으면 항상 집합(변경 없으면 빈 집합)."""
    p = _git(root, "diff", "--unified=0", "HEAD", "--", rel)
    if p is None or p.returncode != 0:
        return None
    hunks = _parse_diff_hunks(p.stdout)
    if rel in hunks:
        return hunks[rel]
    u = _git(root, "ls-files", "--others", "--exclude-standard", "--", rel)
    if u is not None and u.returncode == 0 and u.stdout.strip():
        return _whole_file_lines(root, rel)  # 추적 안 된 새 파일
    return set()  # 변경 없음(전부 기존)

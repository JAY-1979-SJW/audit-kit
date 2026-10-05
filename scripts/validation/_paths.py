"""corpus.py/mutate.py 가 공유하는 "이 PC 전용 경로 목록 읽기" 헬퍼.

이 모듈은 부작용이 없다(둘 다 각자 무거운 검사 루프를 최상위에 갖고 있어 직접 서로를
import 하면 그 루프가 같이 실행된다 — 그래서 이 얇은 헬퍼만 따로 뺐다)."""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).parent


def load_paths(name: str) -> list:
    """`name` 파일(이 폴더 기준)에서 경로 목록을 읽는다. 한 줄에 하나, '#' 로 시작하면 주석
    (STD-02: 소스 코드에 절대경로 하드코딩 금지 — 예전엔 corpus.py/mutate.py 안에 리스트로
    박혀 있었다)."""
    lines = (_HERE / name).read_text(encoding="utf-8").splitlines()
    stem, dot, suffix = name.rpartition(".")
    local = _HERE / f"{stem}.local{dot}{suffix}"  # PC 전용 목록(.gitignore 대상) — 있으면 이어서 읽는다
    if local.is_file():
        lines += local.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]

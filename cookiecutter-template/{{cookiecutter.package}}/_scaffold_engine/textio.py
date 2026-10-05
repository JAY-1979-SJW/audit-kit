# pylint: disable=duplicate-code
"""원본 형식을 보존하는 소스 파일 읽기/쓰기.

audit-kit 이 사용자 코드를 고칠 때 바뀐 줄 외에는 한 바이트도 바꾸지 않기 위한 규칙:
- 줄바꿈: 파일의 기존 방식(LF / CRLF)을 유지. (Windows 에서 write_text 는 LF 를 CRLF 로 바꿔 버린다)
- 인코딩: UTF-8 이 아니면 cp949(한글 Windows 구형 파일)로 읽고, 같은 인코딩으로 다시 쓴다.
- BOM: 있던 파일만 유지.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

BOM = b"\xef\xbb\xbf"
FALLBACK_ENCODINGS = ("cp949",)


@dataclass(frozen=True)
class TextFormat:
    encoding: str = "utf-8"
    bom: bool = False
    newline: str = "\n"


def detect_newline(raw: bytes) -> str:
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def decode(raw: bytes) -> tuple:
    """(텍스트(\\n 로 정규화), TextFormat). 디코딩 불가면 UnicodeDecodeError."""
    bom = raw.startswith(BOM)
    body = raw[len(BOM):] if bom else raw
    newline = detect_newline(body)
    for enc in ("utf-8", *FALLBACK_ENCODINGS):
        try:
            text = body.decode(enc)
        except UnicodeDecodeError:
            continue
        return text.replace("\r\n", "\n"), TextFormat(enc, bom, newline)
    raise UnicodeDecodeError("utf-8", body, 0, 1, "UTF-8/cp949 어느 쪽으로도 읽을 수 없음")


def read_source(path: Path) -> tuple:
    return decode(Path(path).read_bytes())


def read_text(path: Path) -> str:
    """분석용 읽기: 형식 정보가 필요 없을 때. 읽을 수 없는 바이트는 대체 문자로."""
    try:
        return read_source(path)[0]
    except UnicodeDecodeError:
        return Path(path).read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")


def encode(text: str, fmt: TextFormat) -> bytes:
    body = text.replace("\r\n", "\n")
    if fmt.newline != "\n":
        body = body.replace("\n", fmt.newline)
    data = body.encode(fmt.encoding)  # 인코딩 불가 문자가 들어가면 UnicodeEncodeError → 호출자가 수정 포기
    return (BOM if fmt.bom else b"") + data


def write_source(path: Path, text: str, fmt: TextFormat):
    Path(path).write_bytes(encode(text, fmt))


def append_text(path: Path, text: str):
    """설정 파일 끝에 덧붙이기 — 기존 줄바꿈·인코딩을 따른다. 파일이 없으면 UTF-8/LF 로 만든다."""
    p = Path(path)
    if p.exists():
        old, fmt = read_source(p)
        if old and not old.endswith("\n"):
            old += "\n"
        write_source(p, old + text, fmt)
    else:
        write_source(p, text, TextFormat())

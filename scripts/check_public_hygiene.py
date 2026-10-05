"""공개 저장소 위생 검사: git 추적 파일에서 유출되면 안 되는 문자열을 '검출'만 한다(변환하지 않는다).

사용법: python scripts/check_public_hygiene.py [--root <저장소>] [--patterns-file <저장소 밖 경로>]

검사 규칙
- 일반 규칙(이 파일에 내장): 윈도우 절대경로, 이메일 주소, 사설 키 헤더, 토큰 접두, 홈 디렉터리 경로.
- 추가 금지어: 환경변수 HYGIENE_PATTERNS(줄바꿈 구분 정규식) 또는 --patterns-file 로 외부에서 주입한다.
  금지어 자체가 정보이므로 이 저장소에는 목록을 두지 않는다. 패턴 파일은 저장소 밖 경로여야 한다.
- 허용 목록: scripts/hygiene_allowlist.txt (줄마다 `경로:줄번호` 또는 `경로:정규식`, `#` 주석).
  경로는 저장소 기준 상대 경로이며 fnmatch 와일드카드를 쓸 수 있다.

출력은 `파일:줄: 규칙ID` 뿐이다. 매치된 줄 내용·패턴은 출력하지 않는다.
종료코드: 0 위반 없음, 1 위반 있음, 2 사용 오류(패턴 파일 없음·정규식 오류·패턴 파일이 저장소 안 등).
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

GENERAL_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("PH-WINPATH", re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:\\{1,2}[A-Za-z0-9_.$ -]")),
    ("PH-EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")),
    ("PH-PRIVKEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("PH-TOKEN", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|AKIA[0-9A-Z]{16})\b")),
    ("PH-HOMEDIR", re.compile(r"(?<![A-Za-z0-9_.-])/(?:home|Users)/[A-Za-z0-9._-]+/")),
]
ALLOWLIST_REL = "scripts/hygiene_allowlist.txt"


class UsageError(Exception):
    pass


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True
    ).stdout
    return [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]


def load_custom_patterns(patterns_file: str | None, root: Path) -> list[re.Pattern[str]]:
    raw = ""
    if patterns_file:
        pf = Path(patterns_file).resolve()
        if not pf.is_file():
            raise UsageError("패턴 파일을 찾을 수 없다")
        if pf.is_relative_to(root.resolve()):
            raise UsageError("패턴 파일은 저장소 밖 경로여야 한다")
        raw += pf.read_text(encoding="utf-8") + "\n"
    raw += os.environ.get("HYGIENE_PATTERNS", "")
    compiled: list[re.Pattern[str]] = []
    for n, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            compiled.append(re.compile(line, re.IGNORECASE))
        except re.error:
            raise UsageError(f"정규식 오류: 패턴 {len(compiled) + 1}번째 항목") from None
    return compiled


def load_allowlist(root: Path) -> list[tuple[str, str]]:
    p = root / ALLOWLIST_REL
    if not p.is_file():
        return []
    entries = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        path, spec = line.split(":", 1)
        entries.append((path.strip(), spec.strip()))
    return entries


def allowed(entries: list[tuple[str, str]], rel: str, lineno: int, text: str) -> bool:
    for path, spec in entries:
        if not fnmatch.fnmatchcase(rel, path):
            continue
        if spec.isdigit():
            if int(spec) == lineno:
                return True
        else:
            try:
                if re.search(spec, text):
                    return True
            except re.error:
                continue
    return False


def scan(root: Path, custom: list[re.Pattern[str]]) -> list[tuple[str, int, str]]:
    allow = load_allowlist(root)
    hits: list[tuple[str, int, str]] = []
    for rel in tracked_files(root):
        for n, pat in enumerate(custom, 1):
            if pat.search(rel):
                hits.append((rel, 0, f"PH-CUSTOM-{n}"))
        path = root / rel
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        text = data.decode("utf-8", "replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            rules = [(rid, rx) for rid, rx in GENERAL_RULES]
            rules += [(f"PH-CUSTOM-{n}", pat) for n, pat in enumerate(custom, 1)]
            for rid, rx in rules:
                if rx.search(line) and not allowed(allow, rel, lineno, line):
                    hits.append((rel, lineno, rid))
    return hits


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--patterns-file", default=None, help="저장소 밖 경로의 추가 금지어 정규식 파일")
    args = ap.parse_args(argv)
    root = Path(args.root)
    try:
        custom = load_custom_patterns(args.patterns_file, root)
    except UsageError as exc:
        print(f"사용 오류: {exc}", file=sys.stderr)
        return 2
    if not custom:
        print("패턴 미설정: 일반 규칙만 검사한다 (HYGIENE_PATTERNS 또는 --patterns-file)")
    hits = scan(root, custom)
    for rel, lineno, rid in hits:
        print(f"{rel}:{lineno}: {rid}")
    print(f"위생 검사: 위반 {len(hits)}건")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())

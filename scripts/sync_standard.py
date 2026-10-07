"""'32. Claude 개발표준' 의 기준서 원본을 audit-kit 패키지(std/data)로 복사한다 — 비공개 이름을 지우는 스크러브 단계를 거쳐서.

사용법: python scripts/sync_standard.py [원본_폴더] [--scrub-map 경로] [--forbidden 경로]
원본 기본값: 이 저장소의 형제 폴더 '32. Claude 개발표준'. 원본을 고친 뒤에는 반드시 다시 실행한다.

이 저장소는 공개 저장소이고 원본에는 사설 프로젝트 이름·실측 출처가 들어 있다. 그래서 그대로 복사하지 않고 아래 순서로 처리한다.
  1. 치환표(scripts/validation/scrub_map.local.txt)를 읽는다. 없거나 비어 있으면 **동기화를 거부한다(fail-closed)**.
  2. 원본 텍스트에 치환표를 적용한 결과를 std/data 에 쓴다.
  3. 쓴 파일과 git 추적 파일을 금지어로 다시 검사한다 — 치환표의 패턴 + scrub_forbidden.local.txt 의 추가 금지어. 하나라도 걸리면 실패한다.
  4. scripts/check_public_hygiene.py(일반 규칙)를 실행한다. 위반이 있으면 실패한다.
  5. 회귀 가드: 공개 번들에 **이미 있던 줄이 바뀌거나 사라지면**(예: 공개용 일반 문구가 원본의 사설 문구로 되돌아감) 그 diff 를 출력하고
     아무것도 쓰지 않은 채 종료코드 3 으로 멈춘다. 사람이 diff 를 확인한 뒤 의도한 변경이면 `--accept-changed` 로 다시 실행한다.
치환표·금지어 목록은 사설 이름 자체가 정보이므로 *.local.txt(.gitignore 대상)에만 둔다. 출력에는 걸린 줄의 내용·패턴을 찍지 않고 `파일:줄` 만 찍는다.

치환표 형식(줄마다 하나): `정규식 => 치환문자열`  (`#` 으로 시작하는 줄과 빈 줄은 무시, 위에서 아래로 차례로 적용)
금지어 형식: 줄마다 정규식 하나.
(tests/test_std.py 가 번들이 '원본을 스크러브한 결과'와 같은지 검사한다.)
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT.parent / "32. Claude 개발표준"
TARGET = ROOT / "src" / "audit_kit" / "std" / "data"
SCRUB_MAP = ROOT / "scripts" / "validation" / "scrub_map.local.txt"
FORBIDDEN = ROOT / "scripts" / "validation" / "scrub_forbidden.local.txt"
HYGIENE = ROOT / "scripts" / "check_public_hygiene.py"
# 번들 이름 <- 원본 상대 경로
FILES = {
    "rules.toml": "standard/rules.toml",
    "docs_registry.toml": "standard/docs_registry.toml",
    "ruff.toml": "project/ruff.toml",
}
SEP = " => "


class ScrubConfigError(Exception):
    """치환표가 없거나 비었거나 잘못되어 안전하게 동기화할 수 없다."""


def load_scrub_rules(path: Path) -> list[tuple[re.Pattern[str], str]]:
    """치환표를 읽는다. 파일이 없거나 규칙이 0개이거나 형식·정규식이 틀리면 ScrubConfigError (fail-closed)."""
    if not path.is_file():
        raise ScrubConfigError(f"치환표가 없습니다: {path.name} (사설 이름 목록은 저장소 밖에서 관리하는 로컬 전용 파일입니다)")
    rules: list[tuple[re.Pattern[str], str]] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if SEP not in line:
            raise ScrubConfigError(f"{path.name}:{number} 형식 오류 — `정규식{SEP}치환문자열` 이어야 합니다")
        pattern, replacement = line.split(SEP, 1)
        try:
            rules.append((re.compile(pattern), replacement))
        except re.error as exc:
            raise ScrubConfigError(f"{path.name}:{number} 정규식 오류: {exc}") from exc
    if not rules:
        raise ScrubConfigError(f"치환표에 규칙이 없습니다: {path.name}")
    return rules


def load_forbidden(path: Path, rules: list[tuple[re.Pattern[str], str]]) -> list[re.Pattern[str]]:
    """금지어 = 치환표의 패턴(치환 뒤에도 남으면 안 된다) + 추가 금지어 파일(선택).

    자기 치환 결과에도 맞는 패턴(예: 줄 전체를 통일 문구로 바꾸는 규칙)은 금지어에서 뺀다 — 안 그러면 정상 결과가 항상 걸린다.
    """
    patterns = [pattern for pattern, replacement in rules if not pattern.search(replacement)]
    if path.is_file():
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                patterns.append(re.compile(line))
            except re.error as exc:
                raise ScrubConfigError(f"{path.name}:{number} 정규식 오류: {exc}") from exc
    return patterns


def scrub_text(text: str, rules: list[tuple[re.Pattern[str], str]]) -> str:
    for pattern, replacement in rules:
        text = pattern.sub(replacement, text)
    return text


def forbidden_lines(text: str, patterns: list[re.Pattern[str]]) -> list[int]:
    """금지어가 걸린 줄 번호(1부터). 줄 내용·패턴은 돌려주지 않는다."""
    return [number for number, line in enumerate(text.splitlines(), 1) if any(p.search(line) for p in patterns)]


def source_commit(source: Path) -> str | None:
    """source 저장소의 현재 HEAD 커밋 SHA (git 저장소가 아니거나 git 이 없으면 None)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip()


def write_version_stamp(source: Path, target: Path) -> None:
    """다른 PC(번들만 배포받은 경우)에서도 이 규칙이 언제 것인지 알 수 있게 커밋을 기록한다."""
    commit = source_commit(source)
    stamp = {
        "source_repo": "claude-python-standard",
        "commit": commit,
        "synced_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (target / "SOURCE_VERSION.json").write_text(json.dumps(stamp, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"버전 고정: SOURCE_VERSION.json (commit={commit or '알 수 없음(git 저장소 아님)'})")


def scrubbed_source(source: Path, rules: list[tuple[re.Pattern[str], str]]) -> dict[str, bytes]:
    """번들 이름 → 스크러브된 원본 바이트(줄바꿈은 원본 그대로 보존)."""
    out: dict[str, bytes] = {}
    for name, rel in FILES.items():
        raw = (source / rel).read_bytes().decode("utf-8")
        out[name] = scrub_text(raw, rules).encode("utf-8")
    return out


def tracked_text_files(root: Path) -> list[Path]:
    proc = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=False)
    if proc.returncode != 0:
        return []
    return [root / rel for rel in proc.stdout.decode("utf-8", errors="replace").split("\0") if rel]


def check_forbidden_content(content: dict[str, bytes], patterns: list[re.Pattern[str]]) -> list[str]:
    """아직 쓰지 않은 스크러브 결과(메모리)에서 금지어를 찾는다. 결과는 `std/data/<이름>:줄` 목록."""
    hits: list[str] = []
    for name, data in content.items():
        hits += [f"std/data/{name}:{n}" for n in forbidden_lines(data.decode("utf-8", errors="replace"), patterns)]
    return hits


def check_forbidden(root: Path, target: Path, patterns: list[re.Pattern[str]], *, skip_bundle: bool = False) -> list[str]:
    """git 추적 파일에서 금지어를 찾는다(skip_bundle 이면 번들 3파일은 건너뜀 — 새 결과는 메모리에서 따로 검사). 결과는 `경로:줄` 목록."""
    hits: list[str] = []
    seen: set[Path] = set()
    bundle = {target / n for n in FILES}
    for path in [*(() if skip_bundle else bundle), *tracked_text_files(root)]:
        if path in seen or not path.is_file() or (skip_bundle and path in bundle):
            continue
        seen.add(path)
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue  # 바이너리 파일
        rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.name
        hits += [f"{rel}:{n}" for n in forbidden_lines(text, patterns)]
    return hits


def changed_existing_lines(old: bytes, new: bytes) -> list[str]:
    """공개 번들에 이미 있던 줄 중 새 결과에 없는 줄이 있으면 그 unified diff(줄 단위)를, 없으면 빈 목록을 돌려준다. 순수 추가는 변경으로 보지 않는다."""
    old_lines = old.decode("utf-8", errors="replace").splitlines()
    new_lines = new.decode("utf-8", errors="replace").splitlines()
    if not (set(old_lines) - set(new_lines)):
        return []
    return list(difflib.unified_diff(old_lines, new_lines, "공개 번들(기존)", "스크러브 결과(신규)", lineterm="", n=0))


def regression_report(target: Path, content: dict[str, bytes]) -> list[str]:
    """기존 번들 파일이 있는 것만 대조한다. 반환은 사람이 볼 diff 출력 줄들(변경 없으면 빈 목록)."""
    out: list[str] = []
    for name, data in content.items():
        existing = target / name
        if not existing.is_file():
            continue
        diff = changed_existing_lines(existing.read_bytes(), data)
        if diff:
            out += [f"=== std/data/{name}: 공개 번들에 있던 줄이 바뀌거나 사라짐 ===", *diff]
    return out


def run_hygiene(root: Path) -> int:
    proc = subprocess.run(
        [sys.executable, str(HYGIENE), "--root", str(root)], capture_output=True, text=True, encoding="utf-8", check=False
    )
    print((proc.stdout or "").strip())
    if proc.returncode != 0 and proc.stderr:
        print(proc.stderr.strip(), file=sys.stderr)
    return proc.returncode


def sync(
    source: Path,
    *,
    target: Path = TARGET,
    scrub_map: Path = SCRUB_MAP,
    forbidden: Path = FORBIDDEN,
    root: Path = ROOT,
    hygiene: bool = True,
    accept_changed: bool = False,
) -> int:
    missing = [rel for rel in FILES.values() if not (source / rel).is_file()]
    if missing:
        print(f"원본을 찾을 수 없습니다: {source} ({', '.join(missing)})", file=sys.stderr)
        return 1
    try:
        rules = load_scrub_rules(scrub_map)
        patterns = load_forbidden(forbidden, rules)
    except ScrubConfigError as exc:
        print(f"동기화를 거부합니다(fail-closed): {exc}", file=sys.stderr)
        return 1
    content = scrubbed_source(source, rules)
    hits = check_forbidden_content(content, patterns) + check_forbidden(root, target, patterns, skip_bundle=True)
    if hits:
        print(f"금지어 검사 실패 — {len(hits)}곳 (줄 내용은 출력하지 않음, 아무것도 쓰지 않았습니다):", file=sys.stderr)
        for hit in hits[:50]:
            print(f"  {hit}", file=sys.stderr)
        return 1
    print("금지어 검사: 0곳")
    report = regression_report(target, content)
    if report and not accept_changed:
        print("회귀 가드: 공개 번들에 이미 있던 줄이 바뀝니다. diff 를 확인하세요(아무것도 쓰지 않았습니다):", file=sys.stderr)
        for line in report:
            print(line, file=sys.stderr)
        print("의도한 변경이면 --accept-changed 로 다시 실행하세요.", file=sys.stderr)
        return 3
    target.mkdir(parents=True, exist_ok=True)
    for name, data in content.items():
        (target / name).write_bytes(data)
        print(f"복사(스크러브): {FILES[name]} -> std/data/{name}")
    write_version_stamp(source, target)
    if hygiene and run_hygiene(root) != 0:
        print("위생 검사 실패", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined,union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined,union-attr]  # STD-14
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", type=Path, default=DEFAULT_SOURCE)
    ap.add_argument("--scrub-map", type=Path, default=SCRUB_MAP)
    ap.add_argument("--forbidden", type=Path, default=FORBIDDEN)
    ap.add_argument("--accept-changed", action="store_true", help="공개 번들에 있던 줄이 바뀌는 diff 를 확인했고 의도한 변경이다")
    args = ap.parse_args(argv[1:])
    return sync(args.source, scrub_map=args.scrub_map, forbidden=args.forbidden, accept_changed=args.accept_changed)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

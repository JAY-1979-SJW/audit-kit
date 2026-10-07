"""'32. Claude 개발표준' 의 기준서 원본을 audit-kit 패키지(std/data)로 복사한다.

사용법: python scripts/sync_standard.py [원본_폴더]
원본 기본값: 이 저장소의 형제 폴더 '32. Claude 개발표준'. 원본을 고친 뒤에는 반드시 다시 실행한다.
(tests/test_std_bundle.py 가 두 곳이 같은지 검사한다.)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT.parent / "32. Claude 개발표준"
TARGET = ROOT / "src" / "audit_kit" / "std" / "data"
# 번들 이름 <- 원본 상대 경로
FILES = {
    "rules.toml": "standard/rules.toml",
    "docs_registry.toml": "standard/docs_registry.toml",
    "ruff.toml": "project/ruff.toml",
}


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


def write_version_stamp(source: Path) -> None:
    """다른 PC(번들만 배포받은 경우)에서도 이 규칙이 언제 것인지 알 수 있게 커밋을 기록한다."""
    commit = source_commit(source)
    stamp = {
        "source_repo": "claude-python-standard",
        "commit": commit,
        "synced_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (TARGET / "SOURCE_VERSION.json").write_text(
        json.dumps(stamp, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"버전 고정: SOURCE_VERSION.json (commit={commit or '알 수 없음(git 저장소 아님)'})")


def main(argv: list) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined,union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined,union-attr]  # STD-14
    source = Path(argv[1]) if len(argv) > 1 else DEFAULT_SOURCE
    missing = [rel for rel in FILES.values() if not (source / rel).is_file()]
    if missing:
        print(f"원본을 찾을 수 없습니다: {source} ({', '.join(missing)})", file=sys.stderr)
        return 1
    TARGET.mkdir(parents=True, exist_ok=True)
    for name, rel in FILES.items():
        shutil.copyfile(source / rel, TARGET / name)
        print(f"복사: {rel} -> std/data/{name}")
    write_version_stamp(source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

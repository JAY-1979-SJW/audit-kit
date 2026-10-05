"""cookiecutter-template 안의 벤더링된 생성 엔진이 원본(src/audit_kit/arch/*.py)과
어긋나지 않았는지 확인한다. 어긋나면 `python scripts/sync_scaffold_engine.py`를 다시
실행해야 한다는 뜻이다(사유는 scripts/sync_scaffold_engine.py 상단 주석 참고)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_scaffold_engine_matches_source():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "sync_scaffold_engine.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr

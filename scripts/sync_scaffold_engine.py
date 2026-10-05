"""`audit-kit new`의 실제 골격 생성 로직 원본(`src/audit_kit/arch/*.py`, `textio.py`)을
cookiecutter 템플릿 안의 벤더링된 사본(`cookiecutter-template/{{cookiecutter.package}}/
_scaffold_engine/`)으로 복사한다.

**왜 필요한가**: `cruft update`는 템플릿을 옛 커밋/새 커밋에서 각각 렌더링해 diff를 만드는데,
이 훅이 설치된 audit_kit 패키지를 그대로 import하면 두 렌더가 항상 같아져 diff가 안 생긴다
(cookiecutter-template/hooks/post_gen_project.py 상단 주석 참고, 2026-09-28 실측). 그래서
생성 로직을 템플릿 자신의 폴더 안에 벤더링해 커밋마다 함께 버전 관리한다 — 원본을 고치면
반드시 이 스크립트를 다시 실행해야 한다(tests/test_scaffold_engine_sync.py 가 동기화 여부를
검사한다, `32. Claude 개발표준`의 `scripts/sync_standard.py`와 같은 패턴).

사용법:
    python scripts/sync_scaffold_engine.py          # 복사·갱신
    python scripts/sync_scaffold_engine.py --check   # 갱신 없이 어긋난 파일만 보고 (exit 1)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "audit_kit"
TARGET = ROOT / "cookiecutter-template" / "{{cookiecutter.package}}" / "_scaffold_engine"

# 번들 파일명 <- 원본 상대 경로 (SRC 기준)
FILES = {
    "textio.py": "textio.py",
    "spec.py": "arch/spec.py",
    "templates.py": "arch/templates.py",
    "layout.py": "arch/layout.py",
    "scaffold.py": "arch/scaffold.py",
    "concerns_scaffold.py": "arch/concerns_scaffold.py",
    "pyproject_gen.py": "arch/pyproject_gen.py",
    "ci_gen.py": "arch/ci_gen.py",
    "db_api_fitness.py": "arch/db_api_fitness.py",
}

# 벤더링된 사본끼리 서로 참조하도록 import 경로만 바꾼다. `audit_kit.init_project`(pyproject_gen.py
# 가 쓰는 pyproject_snippet)는 의도적으로 그대로 둔다 — 설치된 툴 버전을 반영해야 하는 부분이라
# 템플릿 커밋에 못박지 않는다(cookiecutter-template/hooks/post_gen_project.py 상단 주석 참고).
_REWRITES = [
    (re.compile(r"from audit_kit\.arch\.(\w+) import"), r"from _scaffold_engine.\1 import"),
    (re.compile(r"from audit_kit\.textio import"), "from _scaffold_engine.textio import"),
]


def transform(text: str) -> str:
    for pattern, repl in _REWRITES:
        text = pattern.sub(repl, text)
    # 원본과 내용이 같은 게 의도(vendored 사본이라야 cruft update 가 diff 를 만든다) —
    # pylint duplicate-code(R0801)가 매번 이 쌍을 오탐하지 않도록 알린다(공식 문서: 인라인
    # 비활성화 주석이 duplicate-code 에도 그대로 적용됨, 2026-09-28 확인).
    return "# pylint: disable=duplicate-code\n" + text


def compute() -> dict:
    """{번들 파일명: 있어야 할 텍스트}. 원본이 없으면 KeyError 대신 예외로 알린다."""
    out = {}
    for name, rel in FILES.items():
        src_path = SRC / rel
        out[name] = transform(src_path.read_text(encoding="utf-8"))
    return out


def main(argv: list) -> int:
    check_only = "--check" in argv
    missing = [rel for rel in FILES.values() if not (SRC / rel).is_file()]
    if missing:
        print(f"원본을 찾을 수 없습니다: {', '.join(missing)}", file=sys.stderr)
        return 1

    wanted = compute()
    if check_only:
        drifted = [
            name
            for name, text in wanted.items()
            if not (TARGET / name).is_file() or (TARGET / name).read_text(encoding="utf-8") != text
        ]
        if drifted:
            print(
                "벤더링된 사본이 원본과 다릅니다(다시 동기화 필요): "
                + ", ".join(drifted)
                + "\n  python scripts/sync_scaffold_engine.py 를 실행하세요.",
                file=sys.stderr,
            )
            return 1
        print("_scaffold_engine 이 원본과 일치합니다.")
        return 0

    TARGET.mkdir(parents=True, exist_ok=True)
    for name, text in wanted.items():
        (TARGET / name).write_text(text, encoding="utf-8")
        print(f"복사: {FILES[name]} -> _scaffold_engine/{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

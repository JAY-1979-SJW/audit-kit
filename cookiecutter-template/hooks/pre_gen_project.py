"""cookiecutter/cruft 가 파일을 만들기 전에 실행. 유형·관심사 검증만 한다.

이 파일 전체가 먼저 Jinja2 로 렌더링된 뒤 실행된다(cookiecutter.hooks.run_script_with_context).
`{{ cookiecutter | jsonify }}` 는 cookiecutter.json 의 답변 전체를 JSON으로 안전하게 담는다
(따옴표·특수문자 이스케이프를 jsonify 확장이 처리 — 필드별 문자열 보간보다 안전).
"""

from __future__ import annotations

import json
import sys

from audit_kit.arch.templates import CONCERNS, TYPES

CONTEXT = json.loads(r"""{{ cookiecutter | jsonify }}""")


def main() -> int:
    types = [t.strip() for t in CONTEXT["types"].split(",") if t.strip()]
    unknown_types = [t for t in types if t not in TYPES]
    if unknown_types:
        print(
            f"알 수 없는 유형: {', '.join(unknown_types)} (가능: {', '.join(TYPES)})",
            file=sys.stderr,
        )
        return 1

    concerns_raw = CONTEXT.get("concerns") or ""
    concerns = [c.strip() for c in concerns_raw.split(",") if c.strip()]
    unknown_concerns = [c for c in concerns if c not in CONCERNS]
    if unknown_concerns:
        print(
            f"알 수 없는 관심사: {', '.join(unknown_concerns)} (가능: {', '.join(CONCERNS)})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

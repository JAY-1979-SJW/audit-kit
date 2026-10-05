"""git diff 로 바뀐 함수만 골라 mutmut run 에 넘길 이름 패턴을 만든다.

mutmut 는 `mutmut run "모듈.함수*"` 형식의 와일드카드를 받는다(공식 README 예시,
2026-09-27 확인: github.com/boxed/mutmut README.rst). 전체 저장소를 매번 변이
검사하면 시간이 오래 걸려(GitHub Actions 무료 실행 시간 한도), 이번에 바뀐 함수만
골라 검사한다. 모듈 최상위 코드(함수 밖)의 변경은 이 방식으로 잡지 못한다(알려진 한계).

사용법: python scripts/changed_functions.py <base_ref> [<head_ref>]
출력: 한 줄에 패턴 하나(표준 출력). 바뀐 함수가 없으면 아무것도 출력하지 않는다.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

SRC_ROOT = Path("src")


def changed_line_ranges(base: str, head: str) -> dict[str, set[int]]:
    """git diff -U0 으로 (파일 -> 새로 추가된 줄 번호 집합)."""
    out = subprocess.run(
        ["git", "diff", "-U0", "--diff-filter=ACMR", base, head, "--", str(SRC_ROOT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    files: dict[str, set[int]] = {}
    current: str | None = None
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("@@") and current:
            files.setdefault(current, set()).update(_added_lines(line))
    return files


def _added_lines(hunk_header: str) -> range:
    """`@@ -a,b +c,d @@` 에서 새로 추가된 줄 범위. 삭제만 있으면 빈 range."""
    plus = next(p for p in hunk_header.split("@@")[1].split() if p.startswith("+"))
    start_str, *count_str = plus[1:].split(",")
    start = int(start_str)
    count = int(count_str[0]) if count_str else 1
    return range(start, start + count) if count else range(0)


def module_dotted_path(rel_path: Path) -> str | None:
    if rel_path.suffix != ".py" or not rel_path.is_relative_to(SRC_ROOT):
        return None
    parts = rel_path.relative_to(SRC_ROOT).with_suffix("").parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def functions_touching(tree: ast.AST, lines: set[int]) -> set[str]:
    """바뀐 줄과 겹치는 함수의 점 표기 이름들 (중첩 클래스.메서드 포함)."""
    found: set[str] = set()

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = f"{scope}.{child.name}" if scope else child.name
                span = range(child.lineno, (child.end_lineno or child.lineno) + 1)
                if lines & set(span):
                    found.add(name)
                visit(child, name)
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{scope}.{child.name}" if scope else child.name)
            else:
                visit(child, scope)

    visit(tree, "")
    return found


def patterns_for_diff(base: str, head: str) -> list[str]:
    patterns: set[str] = set()
    for rel, lines in changed_line_ranges(base, head).items():
        path = Path(rel)
        dotted = module_dotted_path(path)
        if dotted is None or not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        # 앞에도 * 를 붙인다: mutmut 의 실제 변이체 이름은 mutants/ 사본 안에서의 상대 경로
        # 기준이라(2026-09-27 실측: 예상한 "모듈.함수*" 만으로는 매치가 하나도 안 됐다)
        # 정확한 접두어를 예측하지 않고 끝부분만 맞춰 찾는다.
        patterns.update(f"*{dotted}.{fn}*" for fn in functions_touching(tree, lines))
    return sorted(patterns)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("사용법: changed_functions.py <base_ref> [<head_ref>]", file=sys.stderr)
        return 1
    base = argv[1]
    head = argv[2] if len(argv) > 2 else "HEAD"
    for pattern in patterns_for_diff(base, head):
        print(pattern)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

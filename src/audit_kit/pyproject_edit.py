"""pyproject.toml 의 의존성 목록을 서식·주석을 유지한 채 고친다 (TOML 전체를 다시 쓰지 않는다)."""

from __future__ import annotations

import re
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

HEADER = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


def _table_span(lines: list, name: str):
    """[name] 표의 (헤더 줄, 끝 줄(다음 표 헤더 직전)) — 없으면 None."""
    start = None
    for i, line in enumerate(lines):
        m = HEADER.match(line)
        if not m:
            continue
        if start is not None:
            return start, i
        if m.group(1).strip() == name and not line.lstrip().startswith("[["):
            start = i
    return (start, len(lines)) if start is not None else None


def _array_insert(lines: list, span: tuple, key: str, item: str) -> bool:
    """표 안의 `key = [...]` 배열 끝에 item(따옴표 포함 문자열)을 넣는다. 키가 없으면 False."""
    start, end = span
    key_re = re.compile(rf"^(\s*){re.escape(key)}\s*=\s*\[")
    for i in range(start + 1, end):
        m = key_re.match(lines[i])
        if not m:
            continue
        line = lines[i]
        if line.rstrip().endswith("]") and line.count("[") == line.count("]"):  # 한 줄 배열
            body = line[line.index("[") + 1 : line.rindex("]")].strip().rstrip(",")
            lines[i] = line[: line.index("[") + 1] + (f"{body}, {item}" if body else item) + "]"
            return True
        # 여러 줄 배열: 닫는 ']' 줄 찾기
        for j in range(i + 1, end + 1):
            if j < len(lines) and re.match(r"^\s*\]", lines[j]):
                # 직전 실제 원소 줄에 쉼표 보장
                k = j - 1
                while k > i and (not lines[k].strip() or lines[k].strip().startswith("#")):
                    k -= 1
                if k > i:
                    code, sep, comment = lines[k].partition("#")
                    if code.strip() and not code.rstrip().endswith(","):
                        lines[k] = code.rstrip() + "," + ((" " + sep + comment) if sep else "")
                indent_m = re.match(r"^(\s*)", lines[k] if k > i else "    ")
                assert indent_m  # \s* 는 빈 문자열에도 항상 매치된다
                indent = indent_m.group(1) or "    "
                lines.insert(j, f"{indent}{item},")
                return True
        return False
    return False


def add_dependency(text: str, spec: str, group: str = "dependencies") -> str:
    """spec 을 [project].dependencies 또는 [project.optional-dependencies].<group> 에 추가한 새 텍스트."""
    item = f'"{spec}"'
    lines = text.split("\n")
    if group == "dependencies":
        span = _table_span(lines, "project")
        if span is None:
            raise ValueError("pyproject.toml 에 [project] 표가 없음")
        if not _array_insert(lines, span, "dependencies", item):
            # 키가 없으면 [project] 표의 마지막 비어 있지 않은 줄 뒤에 추가
            k = span[1] - 1
            while k > span[0] and not lines[k].strip():
                k -= 1
            lines[k + 1 : k + 1] = ["dependencies = [", f"    {item},", "]"]
    else:
        span = _table_span(lines, "project.optional-dependencies")
        if span is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines += ["[project.optional-dependencies]", f"{group} = [", f"    {item},", "]", ""]
        elif not _array_insert(lines, span, group, item):
            lines[span[0] + 1 : span[0] + 1] = [f"{group} = [", f"    {item},", "]"]
    new = "\n".join(lines)
    verify(new, spec, group)
    return new


def replace_dependency(text: str, old: str, new_spec: str) -> str:
    for q in ('"', "'"):
        if f"{q}{old}{q}" in text:
            out = text.replace(f"{q}{old}{q}", f"{q}{new_spec}{q}", 1)
            tomllib.loads(out)
            return out
    raise ValueError(f"'{old}' 를 pyproject.toml 에서 찾지 못함")


def verify(text: str, spec: str, group: str):
    data = tomllib.loads(text)  # 문법이 깨졌으면 여기서 예외
    proj = data.get("project", {})
    arr = (
        proj.get("dependencies", [])
        if group == "dependencies"
        else proj.get("optional-dependencies", {}).get(group, [])
    )
    if spec not in arr:
        raise ValueError(f"추가 확인 실패: {spec} 가 {group} 에 없음")

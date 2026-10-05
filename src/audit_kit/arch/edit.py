"""AST 위치 기반 소스 텍스트 편집 (주석·서식 보존)."""

from __future__ import annotations

import ast
from collections.abc import Sequence


class Editor:
    """여러 편집을 모아 한 번에 적용. 위치는 (줄 1-base, 열 = AST col_offset(UTF-8 바이트))."""

    def __init__(self, text: str):
        self.text = text
        self.lines = text.splitlines(keepends=True)
        self.edits: list = []  # (start_abs, end_abs, new_text)

    def _abs(self, line: int, byte_col: int) -> int:
        if line > len(self.lines):
            return len(self.text)
        base = sum(len(x) for x in self.lines[: line - 1])
        raw = self.lines[line - 1].encode("utf-8")
        return base + len(raw[:byte_col].decode("utf-8", errors="ignore"))

    def replace_node(self, node: ast.stmt | ast.expr, new: str):
        # stmt·expr 는(AST 기반 클래스와 달리) 실제 소스를 파싱하면 항상 4개 위치 속성을 가진다.
        assert node.end_lineno is not None and node.end_col_offset is not None
        self.edits.append((
            self._abs(node.lineno, node.col_offset),
            self._abs(node.end_lineno, node.end_col_offset),
            new,
        ))

    def delete_lines(self, first: int, last: int):
        """first..last 줄 전체 삭제 (1-base, 포함)."""
        self.edits.append((
            self._abs(first, 0),
            self._abs(last + 1, 0) if last < len(self.lines) else len(self.text),
            "",
        ))

    def insert_after_line(self, line: int, new: str):
        """line 줄 뒤에 삽입 (0이면 파일 맨 앞). new 는 줄바꿈으로 끝나야 함."""
        pos = self._abs(line + 1, 0) if line < len(self.lines) else len(self.text)
        prefix = "" if pos == 0 or self.text[pos - 1] == "\n" else "\n"
        self.edits.append((pos, pos, prefix + new))

    def segment(self, first: int, last: int) -> str:
        return "".join(self.lines[first - 1 : last])

    def apply(self) -> str:
        text = self.text
        for start, end, new in sorted(self.edits, key=lambda e: (e[0], e[1]), reverse=True):
            text = text[:start] + new + text[end:]
        if not self.text.startswith("\n"):
            text = text.lstrip("\n")
        return text


def parents(tree: ast.AST) -> dict:
    p = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            p[child] = node
    return p


def header_end(tree: ast.Module) -> int:
    """모듈 머리(독스트링·import·TYPE_CHECKING 블록·__future__)의 마지막 줄. 없으면 0."""
    end = 0
    for i, node in enumerate(tree.body):
        if (
            i == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            end = node.end_lineno or end
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            end = node.end_lineno or end
            continue
        if isinstance(node, ast.If) and _is_tc(node.test):
            end = node.end_lineno or end
            continue
        if isinstance(node, ast.Try) and all(
            isinstance(s, (ast.Import, ast.ImportFrom)) for s in node.body
        ):
            end = node.end_lineno or end
            continue
        break
    return end


def _is_tc(test) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def find_type_checking_block(tree: ast.Module):
    for node in tree.body:
        if isinstance(node, ast.If) and _is_tc(node.test):
            return node
    return None


def has_future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(n, ast.ImportFrom)
        and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    )


def _import_binding_names(node) -> dict:
    """Import/ImportFrom 노드 하나가 묶는 이름들 → (종류, 노드, alias)."""
    out = {}
    for a in node.names:
        bound = a.asname or (a.name.split(".")[0] if isinstance(node, ast.Import) else a.name)
        out[bound] = ("import", node, a)
    return out


def _conditional_import_bindings(node: ast.If) -> dict:
    """TYPE_CHECKING / 조건부 정의 블록 안의 import 만 추적한다."""
    out: dict = {}
    for sub in node.body:
        if isinstance(sub, (ast.Import, ast.ImportFrom)):
            out.update(_import_binding_names(sub))
    return out


def top_bindings(tree: ast.Module) -> dict:
    """모듈 최상위 이름 → (종류, 노드, alias). (STD-08: 원래 이 함수 하나가 복잡도 13이었다 —
    import 노드에서 이름을 뽑는, 최상위/If 안에서 두 번 반복되던 로직을 `_import_binding_names()`
    /`_conditional_import_bindings()`로 나눴다, 2026-09-28)"""
    out: dict[str, tuple[str, ast.stmt, ast.alias | None]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[node.name] = ("def", node, None)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = ("assign", node, None)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out[node.target.id] = ("assign", node, None)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            out.update(_import_binding_names(node))
        elif isinstance(node, ast.If):
            for bound, val in _conditional_import_bindings(node).items():
                out.setdefault(bound, val)
    return out


def render_from(module: str, aliases: Sequence) -> str:
    """import 대상 alias 들을 읽기만 한다 — Sequence 로 좁혀서 호출부가 넘긴 list 를 여기서
    바꿀 수 없다고 mypy 가 보장하게 함(2026-09-28, EFF-02 자동 수정에서 '다른 함수로 넘어가
    안전을 증명 못 함'으로 걸렸던 호출부들을 이 타입힌트로 해소)."""
    names = ", ".join(f"{a.name} as {a.asname}" if a.asname else a.name for a in aliases)
    return f"from {module} import {names}"


def import_module_text(node: ast.ImportFrom) -> str:
    return "." * node.level + (node.module or "")


def line_indent(editor: Editor, line: int) -> str:
    s = editor.lines[line - 1]
    return s[: len(s) - len(s.lstrip())]

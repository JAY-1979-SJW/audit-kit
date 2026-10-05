"""공통 관심사(설정·로깅·DB·COM·HTTP)를 실제로 수행하는 코드 위치 찾기."""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass

from audit_kit.arch.templates import CONCERNS


@dataclass(frozen=True)
class ConcernUse:
    concern: str
    module: str
    line: int
    text: str


def _aliases(tree: ast.Module) -> dict:
    """로컬 이름 → 전체 점 경로 (import x as y, from a.b import c as d)."""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                out[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            for a in n.names:
                out[a.asname or a.name] = f"{n.module}.{a.name}"
    return out


def _dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _resolve(dotted: str, aliases: dict) -> str:
    if not dotted:
        return ""
    head, _, rest = dotted.partition(".")
    base = aliases.get(head, head)
    return f"{base}.{rest}" if rest else base


def _match(full: str, patterns: list) -> bool:
    for mod, fn in patterns:
        if mod is None and (full == fn or full.endswith("." + fn)):
            return True
        if fn is None and (full == mod or full.startswith(mod + ".")):
            return True
        if mod and fn and full == f"{mod}.{fn}":
            return True
    return False


def find_uses(graph, modules=None) -> list:
    out = []
    for m, tree in graph.trees.items():
        if tree is None or (modules is not None and m not in modules):
            continue
        al = _aliases(tree)
        for n in ast.walk(tree):
            full, kind = "", ""
            node: ast.Call | ast.Subscript | None = None
            if isinstance(n, ast.Call):
                full, kind, node = _resolve(_dotted(n.func), al), "call", n
            elif isinstance(n, ast.Subscript):
                full, kind, node = _resolve(_dotted(n.value), al), "sub", n
            if not full or node is None:
                continue
            for name, c in CONCERNS.items():
                hit = _match(full, c["calls"]) if kind == "call" else full in c["subscripts"]
                if hit:
                    out.append(ConcernUse(name, m, node.lineno, full))
                    break
    return out


def dominant_locations(uses: list) -> dict:
    """관심사별로 가장 많이 수행하는 모듈 (설계 초안에서 '지정 위치' 제안용)."""
    by: dict = {}
    for u in uses:
        by.setdefault(u.concern, Counter())[u.module] += 1
    return {c: cnt.most_common(1)[0][0] for c, cnt in by.items()}

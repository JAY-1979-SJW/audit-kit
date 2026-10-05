"""설계 위반 자동 수정.

항상 프로젝트의 임시 복사본(작업공간)에서 수정하고, 한 건 고칠 때마다
  1) 문법 컴파일  2) 설계 재검사(해당 위반 감소 + 새 위반 없음)  3) 변경 모듈 임포트 확인
을 통과해야 채택한다. 실패하면 그 수정만 되돌린다. 원본은 --apply 때만 바뀐다.

전략(안전한 순서):
  reexport  상위 모듈을 거쳐 가져오던 이름을 실제 정의된 하위 모듈에서 직접 임포트
  unused    쓰지 않는 위반 임포트 제거
  type_only 타입 힌트에만 쓰는 임포트를 `if TYPE_CHECKING:` 로 이동 (필요 시 어노테이션 문자열화)
  move      상위 모듈의 함수/클래스/상수를 임포트하는 하위 모듈로 이동 (+ 다른 사용처 임포트 갱신)
  lazy      (--allow-lazy, 순환 전용) 함수 안 지연 임포트로 전환 — 설계 개선이 아니라 임시 처방
"""

from __future__ import annotations

import ast
import builtins
import copy
import difflib
import json
import shutil
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from audit_kit.arch.edit import (
    Editor,
    find_type_checking_block,
    has_future_annotations,
    header_end,
    import_module_text,
    line_indent,
    parents,
    render_from,
    top_bindings,
)
from audit_kit.arch.scan import Violation, scan
from audit_kit.arch.spec import ArchSpec
from audit_kit.config import AuditConfig
from audit_kit.importgraph import TOP, ImportGraph
from audit_kit.runner import run_python
from audit_kit.scope import build_project_graph, get_scope
from audit_kit.textio import TextFormat, read_source, read_text, write_source

FIXABLE = {
    "ARCH-LAYER",
    "ARCH-CYCLE",
    "ARCH-FORBIDDEN",
    "ARCH-INDEPENDENT",
    "ARCH-SIBLING",
    "ARCH-SKIP",
}
MOVABLE_RULES = {"ARCH-LAYER", "ARCH-CYCLE"}
BUILTINS = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}
COPY_DIRS_EXTRA = ["tests", "test"]
COPY_SUFFIXES = {".py", ".toml", ".cfg", ".ini"}


@dataclass
class FixAction:
    kind: str
    violation: Violation
    description: str
    texts: dict  # rel path -> new text


@dataclass
class FixResult:
    workspace: Workspace
    applied: list = field(default_factory=list)
    remaining: list = field(default_factory=list)
    unresolved: dict = field(default_factory=dict)  # key -> [사유]
    before: int = 0


def k2(v: Violation):
    return (v.rule, v.src, v.target, tuple(sorted(v.names)))


# ---------------------------------------------------------------- 작업공간
class Workspace:
    def __init__(self, cfg: AuditConfig):
        self.orig = cfg.root
        self.tmp = Path(tempfile.mkdtemp(prefix="audit-kit-arch-"))
        for p in cfg.package_paths():
            self._copy(p)
        # 보조 코드(테스트·스크립트·플러그인 등)도 복사 — 제품 코드 이동 시 그쪽 임포트도 함께 고치고 검증하려고
        for tops in get_scope(cfg).support_roots().values():
            for t in tops:
                if (cfg.root / t).exists():
                    self._copy(cfg.root / t)
        for d in COPY_DIRS_EXTRA:
            if (cfg.root / d).is_dir() and not (self.tmp / d).exists():
                self._copy(cfg.root / d)
        for f in cfg.root.iterdir():
            if f.is_file() and f.suffix in COPY_SUFFIXES:
                shutil.copy2(f, self.tmp / f.name)
        self.cfg = copy.copy(cfg)
        self.cfg.root = self.tmp

    def _copy(self, p: Path):
        dst = self.tmp / p.relative_to(self.orig)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if p.is_dir():
            shutil.copytree(
                p, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"), dirs_exist_ok=True
            )
        else:
            shutil.copy2(p, dst)

    def read(self, rel: str) -> str:
        """\\n 으로 정규화된 텍스트 (편집은 이 텍스트 기준)."""
        return read_source(self.tmp / rel)[0]

    def write(self, rel: str, text: str):
        """원래 파일의 줄바꿈·인코딩·BOM 을 유지해서 쓴다. 인코딩 불가면 UnicodeEncodeError."""
        p = self.tmp / rel
        fmt = read_source(p)[1] if p.exists() else TextFormat()
        write_source(p, text, fmt)

    def original(self, rel: str) -> str:
        p = self.orig / rel
        return read_text(p) if p.is_file() else ""

    def changed(self) -> list:
        """바뀌었거나 새로 생긴 .py/.toml (작업공간 기준 상대 경로)."""
        out = []
        for f in sorted([*self.tmp.rglob("*.py"), *self.tmp.rglob("*.toml")]):
            rel = f.relative_to(self.tmp).as_posix()
            if "__pycache__" in rel:
                continue
            o = self.orig / rel
            if not o.is_file() or f.read_bytes() != o.read_bytes():
                out.append(rel)
        return out

    def diff(self, rel: str) -> str:
        return "".join(
            difflib.unified_diff(
                self.original(rel).splitlines(keepends=True),
                read_text(self.tmp / rel).splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- 공통 도우미
class Ctx:
    """전략 함수가 쓰는 현재 작업공간 상태."""

    def __init__(self, ws: Workspace, spec: ArchSpec, graph: ImportGraph):
        self.ws, self.spec, self.graph = ws, spec, graph
        self.rel = {m: ws.cfg.rel(f) for m, f in graph.modules.items()}

    def text(self, mod: str) -> str:
        return self.ws.read(self.rel[mod])

    def tree(self, mod: str) -> ast.Module:
        return ast.parse(self.text(mod))

    def is_pkg(self, mod: str) -> bool:
        return self.graph.modules[mod].name == "__init__.py"

    def abs_base(self, mod: str, node: ast.ImportFrom) -> str | None:
        return ImportGraph.absolute_base(mod, self.is_pkg(mod), node)

    def allowed(self, importer: str, dep: str | None) -> bool:
        """importer 가 내부 모듈 dep 를 임포트해도 설계상 괜찮은가 (dep 가 같거나 아래 계층)."""
        if dep is None or dep == importer:
            return dep is None
        si, _ = self.spec.layer_of(importer)
        di, _ = self.spec.layer_of(dep)
        if si is None or di is None:
            return True
        return di >= si


def find_import(ctx: Ctx, v: Violation):
    """위반 줄의 from-import 문과, target 에서 가져온 alias 들."""
    assert v.src is not None  # FIXABLE 위반은 scan.py 가 항상 src 를 채운다
    tree = ctx.tree(v.src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.lineno == v.line:
            base = ctx.abs_base(v.src, node)
            if base is None:
                continue
            aliases = [
                a
                for a in node.names
                if f"{base}.{a.name}" not in ctx.graph.modules
                and ctx.graph._resolve(base) == v.target
            ]
            if aliases:
                return tree, node, aliases
    return tree, None, []


def remove_aliases(ed: Editor, stmt: ast.ImportFrom, remove: list):
    remove_set = frozenset(remove)
    remaining = [a for a in stmt.names if a not in remove_set]
    if not remaining:
        assert stmt.end_lineno is not None  # 실제 소스를 파싱한 노드라 항상 있다
        ed.delete_lines(stmt.lineno, stmt.end_lineno)
    else:
        ed.replace_node(stmt, render_from(import_module_text(stmt), remaining))


def name_uses(tree: ast.AST, name: str) -> list:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load)
    ]


def _enclosing(par: dict, node, types):
    cur = par.get(node)
    while cur is not None and not isinstance(cur, types):
        cur = par.get(cur)
    return cur


# ---------------------------------------------------------------- 전략: reexport
def strat_reexport(ctx: Ctx, v: Violation):
    if not v.names or v.target not in ctx.graph.modules:
        return "이름 임포트가 아님"
    assert v.src is not None and v.target is not None  # FIXABLE 위반은 둘 다 항상 있다
    _, stmt, aliases = find_import(ctx, v)
    if stmt is None:
        return "임포트 문을 찾지 못함"
    tb = top_bindings(ctx.tree(v.target))
    new_lines = []
    for a in aliases:
        kind, node, orig_alias = tb.get(a.name, (None, None, None))
        if kind != "import" or not isinstance(node, ast.ImportFrom):
            return f"'{a.name}' 는 {v.target} 에 정의됨(재수출 아님)"
        base = ctx.abs_base(v.target, node)
        if base is None:
            return "상대 임포트 해석 실패"
        origin = (
            base + "." + orig_alias.name
            if f"{base}.{orig_alias.name}" in ctx.graph.modules
            else ctx.graph._resolve(base)
        )
        if origin is not None and not ctx.allowed(v.src, origin):
            return f"원 정의 모듈 {origin} 도 상위 계층"
        final = a.asname or a.name
        as_part = f" as {final}" if final != orig_alias.name else ""
        new_lines.append(f"from {base} import {orig_alias.name}{as_part}")
    text = ctx.text(v.src)
    ed = Editor(text)
    remove_aliases(ed, stmt, aliases)
    indent = line_indent(ed, stmt.lineno)
    ed.insert_after_line(stmt.end_lineno, "".join(indent + ln + "\n" for ln in new_lines))
    return FixAction(
        "reexport",
        v,
        f"{v.src}: {', '.join(a.name for a in aliases)} 를 {v.target} 대신 원 정의 위치에서 임포트",
        {ctx.rel[v.src]: ed.apply()},
    )


# ---------------------------------------------------------------- 전략: unused / type_only
def _annotation_root(par: dict, node):
    """node 가 '안전한' 어노테이션 안에 있으면 (어노테이션 루트, 소유 함수), 아니면 None.
    안전 = 데코레이터 없는 함수의 인자/반환 어노테이션, 또는 함수 본문 지역변수 어노테이션.
    (클래스 필드·모듈 변수·데코레이터 달린 함수(FastAPI 라우트, validator)는 런타임에 평가되므로 제외)"""
    cur = node
    while True:
        p = par.get(cur)
        if p is None or (
            isinstance(p, ast.stmt)
            and not isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.AnnAssign))
        ):
            return None
        if isinstance(p, ast.arg) and p.annotation is cur:
            fn = par.get(par.get(p))
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and not fn.decorator_list:
                return cur, fn
            return None
        if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)) and p.returns is cur:
            return (cur, p) if not p.decorator_list else None
        if isinstance(p, ast.AnnAssign) and p.annotation is cur:
            fn = par.get(p)
            return (cur, fn) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
        if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.AnnAssign)):
            return None
        cur = p


def _type_only_roots(tree: ast.Module, aliases: list, par: dict):
    """별칭들이 어노테이션에서만 쓰이는지 확인. 반환: 실행 코드에서 쓰면 그 사유(str),
    아니면 (어노테이션 루트 노드들, 하나라도 쓰였는가) 쌍."""
    roots, used = {}, False
    for a in aliases:
        bound = a.asname or a.name
        for use in name_uses(tree, bound):
            used = True
            r = _annotation_root(par, use)
            if r is None:
                return f"'{bound}' 를 실행 코드에서 사용 (줄 {use.lineno})"
            roots[id(r[0])] = r[0]
    return roots, used


def _quote_annotation_roots(text: str, ed: Editor, roots: dict):
    """future annotations 가 없을 때 어노테이션에 쓰인 이름을 문자열로 감싼다(지연 평가).
    실패 사유(str)를 반환하거나, 성공하면 None."""
    for root in roots.values():
        seg = ast.get_source_segment(text, root) or ""
        if "\n" in seg or ('"' in seg and "'" in seg):
            return "여러 줄/따옴표 섞인 어노테이션"
        if isinstance(root, ast.Constant):
            continue
        q = "'" if '"' in seg else '"'
        ed.replace_node(root, q + seg + q)
    return None


def strat_type_only(ctx: Ctx, v: Violation):
    if not v.names or v.context != TOP:
        return "최상위 이름 임포트가 아님"
    assert v.src is not None  # FIXABLE 위반은 항상 src 가 있다
    tree, stmt, aliases = find_import(ctx, v)
    if stmt is None:
        return "임포트 문을 찾지 못함"
    par = parents(tree)
    result = _type_only_roots(tree, aliases, par)
    if isinstance(result, str):
        return result
    roots, used = result
    text = ctx.text(v.src)
    ed = Editor(text)
    remove_aliases(ed, stmt, aliases)
    if not used:
        return FixAction(
            "unused",
            v,
            f"{v.src}: 쓰지 않는 임포트 {', '.join(a.name for a in aliases)} 제거",
            {ctx.rel[v.src]: ed.apply()},
        )
    if not has_future_annotations(tree):
        err = _quote_annotation_roots(text, ed, roots)
        if err:
            return err
    line = render_from(import_module_text(stmt), aliases)
    tc = find_type_checking_block(tree)
    if tc is not None:
        ed.insert_after_line(tc.end_lineno, line_indent(ed, tc.body[0].lineno) + line + "\n")
    else:
        need_tc = "TYPE_CHECKING" not in top_bindings(tree)
        block = (
            ("from typing import TYPE_CHECKING\n" if need_tc else "")
            + "\nif TYPE_CHECKING:\n    "
            + line
            + "\n"
        )
        ed.insert_after_line(max(header_end(tree), stmt.end_lineno), block)
    return FixAction(
        "type_only",
        v,
        f"{v.src}: 타입 전용 임포트 {', '.join(a.name for a in aliases)} → TYPE_CHECKING",
        {ctx.rel[v.src]: ed.apply()},
    )


# ---------------------------------------------------------------- 전략: move
def _local_bound(node: ast.AST) -> set:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not node:
            out.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            out |= {a.asname or a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
    return out


def _free_names(node: ast.AST) -> set:
    loaded = {
        n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    return loaded - _local_bound(node) - BUILTINS


def _span(ed: Editor, node) -> tuple:
    first = min([d.lineno for d in getattr(node, "decorator_list", [])] + [node.lineno])
    while first > 1 and ed.lines[first - 2].lstrip().startswith("#"):
        first -= 1
    return first, node.end_lineno


def _module_aliases(ctx: Ctx, mod: str, tree: ast.Module, target: str) -> set:
    """mod 안에서 target 모듈 객체를 가리키는 이름들."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == target:
                    out.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            base = ctx.abs_base(mod, n)
            for a in n.names:
                if base and f"{base}.{a.name}" == target:
                    out.add(a.asname or a.name)
    return out


@dataclass
class MoveEndpoints:
    """이름을 옮기는 방향(target 에서 src 로). 여러 헬퍼가 함께 받으므로 묶었다(PLR0913, 2026-09-28)."""

    tgt: str
    src: str


def _move_collect_nodes(aliases: list, tb: dict, sb: dict, stmt, ep: MoveEndpoints):
    """옮길 이름들이 target 에 정의된 함수/클래스/상수이고 src 에 이미 있지 않은지 확인.
    반환: 실패 사유(str) 또는 (names, {이름: AST 노드}) 쌍."""
    if any(a.asname for a in aliases):
        return "as 별칭 임포트는 자동 이동하지 않음"
    names = [a.name for a in aliases]
    nodes = {}
    for n in names:
        kind, node, _ = tb.get(n, (None, None, None))
        if kind not in ("def", "assign"):
            return f"'{n}' 는 {ep.tgt} 에 정의된 함수/클래스/상수가 아님"
        if isinstance(node, ast.Assign) and len(node.targets) != 1:
            return f"'{n}' 다중 대입"
        if n in sb and sb[n][1] is not stmt:
            return f"{ep.src} 에 이미 '{n}' 이 있음"
        nodes[n] = node
    return names, nodes


def _move_dep_resolution(ctx: Ctx, n: str, dep: str, tb: dict, sb: dict, ep: MoveEndpoints):
    """의존 이름 하나(`dep`)를 검사해 같이 옮길 임포트 줄을 만든다.
    반환: ("error", 사유) | ("skip", None) | ("line", 임포트 줄)."""
    kind, dnode, alias = tb.get(dep, (None, None, None))
    if kind in ("def", "assign"):
        return "error", f"'{n}' 가 {ep.tgt} 의 '{dep}' 에 의존 — '{dep}' 도 함께/먼저 이동 필요"
    if kind != "import" or dep in sb:
        return "skip", None
    if isinstance(dnode, ast.ImportFrom):
        base = ctx.abs_base(ep.tgt, dnode)
        if base is None:
            return "error", "상대 임포트 해석 실패"
        origin = (
            f"{base}.{alias.name}"
            if f"{base}.{alias.name}" in ctx.graph.modules
            else ctx.graph._resolve(base)
        )
        line = f"from {base} import {alias.name}" + (f" as {alias.asname}" if alias.asname else "")
    else:
        origin = ctx.graph._resolve(alias.name)
        line = f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else "")
    if origin == ep.src:
        return "skip", None
    if origin is not None and not ctx.allowed(ep.src, origin):
        return "error", f"'{n}' 가 쓰는 '{dep}'({origin}) 가 {ep.src} 보다 상위 계층"
    return "line", line


def _move_dep_lines(ctx: Ctx, nodes: dict, names: list, sb: dict, tb: dict, ep: MoveEndpoints):
    """옮길 노드들이 target 안의 다른 이름에 의존하면, 같이 옮겨야 할 임포트 줄을 모은다.
    반환: 실패 사유(str) 또는 임포트 줄 목록(list[str])."""
    names_set = frozenset(names)
    dep_lines = []
    for n, node in nodes.items():
        for dep in sorted(_free_names(node)):
            if dep in names_set or (dep in sb and sb[dep][0] != "import"):
                continue
            kind, value = _move_dep_resolution(ctx, n, dep, tb, sb, ep)
            if kind == "error":
                return value
            if kind == "line" and value not in dep_lines:
                dep_lines.append(value)
    return dep_lines


def _move_strip_from_target(
    ctx: Ctx, t_ed: Editor, t_tree: ast.Module, nodes: dict, names: Sequence, ep: MoveEndpoints
) -> list:
    """target 파일에서 옮길 노드들을 지우고, 여전히(속성 접근 포함) 쓰이면 재수출 임포트를
    target 에 남긴다. t_ed 를 직접 수정하고, src 에 붙일 원본 코드 조각들을 반환한다.
    (`names`는 Sequence 로 받는다 — EFF-02 자동 수정이 "다른 함수로 그대로 넘어가 변형 여부를
    증명 못 함"이라며 건너뛴 걸, mypy 가 이미 변형을 막아주는 타입으로 직접 좁혀 해소함,
    2026-09-28.)"""
    removed = []
    seg_texts = []
    for _n, node in sorted(nodes.items(), key=lambda kv: kv[1].lineno):
        first, last = _span(t_ed, node)
        seg_texts.append(t_ed.segment(first, last).rstrip("\n") + "\n")
        while (
            last < len(t_ed.lines) and not t_ed.lines[last].strip() and last - node.end_lineno < 2
        ):
            last += 1
        t_ed.delete_lines(first, last)
        removed.append((first, last))

    def in_removed(ln: int) -> bool:
        return any(a <= ln <= b for a, b in removed)

    still_used = any(not in_removed(u.lineno) for n in names for u in name_uses(t_tree, n))
    # 다른 모듈이 `import tgt` 후 `tgt.name` 형태로 쓰면 재수출이 필요
    names_set = frozenset(names)
    attr_used = False
    for mod, t in ctx.graph.trees.items():
        if t is None or mod == ep.tgt:
            continue
        attrs = [
            x
            for x in ast.walk(t)
            if isinstance(x, ast.Attribute)
            and x.attr in names_set
            and isinstance(x.value, ast.Name)
        ]
        if attrs and any(
            isinstance(x.value, ast.Name) and x.value.id in _module_aliases(ctx, mod, t, ep.tgt)
            for x in attrs
        ):
            attr_used = True
            break
    if still_used or attr_used:
        he = header_end(t_tree)
        t_ed.insert_after_line(
            he,
            f"from {ep.src} import {', '.join(names)}  # audit-kit: {ep.src} 로 이동\n"
            + ("\n\n" if he == 0 else ""),
        )
    return seg_texts


def _move_update_other_importers(ctx: Ctx, names: list, ep: MoveEndpoints, texts: dict) -> None:
    """target 에서 옮긴 이름들을 import 하던 다른 모듈들의 임포트 문을 src 기준으로 갱신한다.
    바뀐 파일은 texts 에 직접 채운다(반환값 없음)."""
    names_set = frozenset(names)
    for mod in ctx.graph.modules:
        if mod in (ep.src, ep.tgt):
            continue
        mtext = ctx.text(mod)
        if not any(n in mtext for n in names):
            continue
        m_tree = ast.parse(mtext)
        m_ed = Editor(mtext)
        touched = False
        for node in ast.walk(m_tree):
            if not isinstance(node, ast.ImportFrom) or ctx.abs_base(mod, node) != ep.tgt:
                continue
            moved = [a for a in node.names if a.name in names_set]
            if not moved:
                continue
            moved_set = frozenset(
                moved
            )  # in 검사만: render_from 은 순서가 필요해 moved 자체는 유지
            rest = [a for a in node.names if a not in moved_set]
            new = render_from(ep.src, moved)
            if rest:
                new = (
                    render_from(import_module_text(node), rest)
                    + "\n"
                    + line_indent(m_ed, node.lineno)
                    + new
                )
            m_ed.replace_node(node, new)
            touched = True
        if touched:
            texts[ctx.rel[mod]] = m_ed.apply()


def strat_move(ctx: Ctx, v: Violation):
    if v.rule not in MOVABLE_RULES or not v.names or v.target not in ctx.graph.modules:
        return "이동 대상 아님"
    assert v.src is not None and v.target is not None  # FIXABLE 위반은 둘 다 항상 있다
    src, tgt = v.src, v.target
    if ctx.spec.is_entrypoint(tgt):
        return "진입점의 심볼은 자동 이동하지 않음"
    t_tree = ctx.tree(tgt)
    tb = top_bindings(t_tree)
    s_tree, stmt, aliases = find_import(ctx, v)
    if stmt is None:
        return "임포트 문을 찾지 못함"
    sb = top_bindings(s_tree)
    ep = MoveEndpoints(tgt, src)
    result = _move_collect_nodes(aliases, tb, sb, stmt, ep)
    if isinstance(result, str):
        return result
    names, nodes = result

    dep_lines = _move_dep_lines(ctx, nodes, names, sb, tb, ep)
    if isinstance(dep_lines, str):
        return dep_lines

    t_ed = Editor(ctx.text(tgt))
    seg_texts = _move_strip_from_target(ctx, t_ed, t_tree, nodes, names, ep)

    # src 에 추가
    s_ed = Editor(ctx.text(src))
    remove_aliases(s_ed, stmt, aliases)
    block = "".join(ln + "\n" for ln in dep_lines) + "\n\n" + "\n\n".join(seg_texts)
    s_ed.insert_after_line(
        max(header_end(s_tree), stmt.end_lineno if stmt in s_tree.body else 0), block
    )
    texts = {ctx.rel[tgt]: t_ed.apply(), ctx.rel[src]: s_ed.apply()}

    _move_update_other_importers(ctx, names, ep, texts)
    return FixAction(
        "move", v, f"{', '.join(names)} 를 {tgt} → {src} 로 이동 (하위 계층으로 내림)", texts
    )


# ---------------------------------------------------------------- 전략: lazy
def strat_lazy(ctx: Ctx, v: Violation):
    if v.rule != "ARCH-CYCLE" or not v.names:
        return "순환이 아님"
    assert v.src is not None  # FIXABLE 위반은 항상 src 가 있다
    tree, stmt, aliases = find_import(ctx, v)
    if stmt is None:
        return "임포트 문을 찾지 못함"
    par = parents(tree)
    funcs = {}
    for a in aliases:
        for use in name_uses(tree, a.asname or a.name):
            fn = None
            cur = use
            while cur is not None:
                p = par.get(cur)
                if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)) and cur in p.body:
                    fn = p
                cur = p
            if fn is None:
                return f"'{a.name}' 를 모듈/클래스 수준에서 사용 (줄 {use.lineno})"
            funcs[id(fn)] = fn
    text = ctx.text(v.src)
    ed = Editor(text)
    remove_aliases(ed, stmt, aliases)
    line = render_from(import_module_text(stmt), aliases) + "  # audit-kit: 지연 임포트(순환 회피)"
    for fn in funcs.values():
        body = fn.body
        k = (
            1
            if (
                isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
                and len(body) > 1
            )
            else 0
        )
        first = body[k]
        start = min([d.lineno for d in getattr(first, "decorator_list", [])] + [first.lineno])
        ed.insert_after_line(start - 1, line_indent(ed, first.lineno) + line + "\n")
    return FixAction(
        "lazy",
        v,
        f"{v.src}: {', '.join(a.name for a in aliases)} 를 함수 안 지연 임포트로 (순환 회피)",
        {ctx.rel[v.src]: ed.apply()},
    )


STRATEGIES = [("reexport", strat_reexport), ("type_only", strat_type_only), ("move", strat_move)]


# ---------------------------------------------------------------- 검증
_IMPORT_CODE = (
    "import importlib, json, sys\n"
    "sys.path[:0] = ['.', 'src']\n"
    "r = {}\n"
    "for m in json.loads(sys.argv[1]):\n"
    "    try:\n"
    "        importlib.import_module(m)\n"
    "    except BaseException as e:\n"
    "        r[m] = type(e).__name__ + ': ' + str(e)[:200]\n"
    "print(json.dumps(r))\n"
)


def import_failures(root: Path, modules: list) -> dict:
    if not modules:
        return {}
    p = run_python(["-c", _IMPORT_CODE, json.dumps(sorted(modules))], root, timeout=180)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return dict.fromkeys(modules, "임포트 검사 실패: " + (p.stderr or p.stdout)[-200:])


class Verifier:
    def __init__(self, ws: Workspace, spec: ArchSpec, import_check: bool):
        self.ws, self.spec, self.import_check = ws, spec, import_check
        self.baseline: dict = {}

    def _baseline(self, modules: list) -> dict:
        todo = [m for m in modules if m not in self.baseline]
        if todo:
            fails = import_failures(self.ws.orig, todo)
            for m in todo:
                self.baseline[m] = fails.get(m)
        return {m: self.baseline[m] for m in modules if self.baseline.get(m)}

    def _violations_improved(self, action: FixAction, before: list, g2) -> str | None:
        """위반이 실제로 줄었는지(같은 종류가 새로 생기지 않았는지) 확인.
        실패 사유(str)를 반환하거나, 문제 없으면 None."""
        after = scan(self.ws.cfg, self.spec, g2)
        old, new = Counter(map(k2, before)), Counter(map(k2, after))
        if new[k2(action.violation)] >= old[k2(action.violation)]:
            return "위반이 사라지지 않음"
        appeared = [k for k in new if k not in old]
        if appeared:
            return "새 위반 발생: " + "; ".join(f"{r} {s}->{t}" for r, s, t, _ in appeared[:3])
        return None

    def _imports_still_ok(self, action: FixAction, g2) -> str | None:
        """`--verify-import`(self.import_check)일 때만: 이 수정으로 새로 깨진 임포트가 있는지.
        실패 사유(str)를 반환하거나, 검사 대상이 아니거나 문제 없으면 None."""
        if not self.import_check:
            return None
        mods = [m for m, f in g2.modules.items() if self.ws.cfg.rel(f) in action.texts]
        fails = import_failures(self.ws.tmp, mods)
        base = self._baseline(mods)
        new_fail = {m: e for m, e in fails.items() if m not in base}
        if new_fail:
            mod, err = next(iter(new_fail.items()))
            return f"임포트 실패 {mod}: {err}"
        return None

    def try_apply(self, action: FixAction, before: list):
        saved = {rel: (self.ws.tmp / rel).read_bytes() for rel in action.texts}

        def revert(why):
            for rel, raw in saved.items():
                (self.ws.tmp / rel).write_bytes(raw)
            return False, why

        try:
            for rel, text in action.texts.items():
                self.ws.write(rel, text)
        except UnicodeEncodeError as e:  # cp949 파일에 표현 못 하는 문자가 들어가는 수정
            return revert(f"원본 인코딩으로 저장 불가: {e.reason}")

        for rel, text in action.texts.items():
            try:
                compile(text, rel, "exec")
            except SyntaxError as e:
                return revert(f"문법 오류 {rel}:{e.lineno}")
        g2 = build_project_graph(self.ws.cfg)
        err = self._violations_improved(action, before, g2) or self._imports_still_ok(action, g2)
        if err:
            return revert(err)
        return True, ""


# ---------------------------------------------------------------- 실행
def run_fix(
    cfg: AuditConfig,
    spec: ArchSpec,
    allow_lazy: bool = False,
    import_check: bool = True,
    only: set | None = None,
    max_iter: int = 200,
) -> FixResult:
    ws = Workspace(cfg)
    verifier = Verifier(ws, spec, import_check)
    strategies = [
        s
        for s in STRATEGIES
        if not only or s[0] in only or (s[0] == "type_only" and "unused" in only)
    ]
    if allow_lazy:
        strategies.append(("lazy", strat_lazy))
    res = FixResult(ws)
    unresolved: dict = {}
    first = True
    for _ in range(max_iter):
        graph = build_project_graph(ws.cfg)
        vs = scan(ws.cfg, spec, graph)
        if first:
            res.before, first = len(vs), False
        todo = [
            v
            for v in vs
            if v.rule in FIXABLE and k2(v) not in unresolved and v.src in graph.modules
        ]
        if not todo:
            break
        v = todo[0]
        ctx = Ctx(ws, spec, graph)
        reasons = []
        done = False
        for name, strat in strategies:
            try:
                out = strat(ctx, v)
            except Exception as e:  # ruff: ignore[blind-except] — strat 은 여러 수정 전략 중 하나(플러그인
                # 성격)라 어떤 예외를 던질지 미리 알 수 없다. 오류 내용은 그대로 사유에 남긴다.
                out = f"내부 오류 {type(e).__name__}: {e}"
            if isinstance(out, FixAction):
                ok, why = verifier.try_apply(out, vs)
                if ok:
                    res.applied.append(out)
                    done = True
                    break
                reasons.append(f"{out.kind}: 검증 실패 — {why}")
            else:
                reasons.append(f"{name}: {out}")
        if not done:
            unresolved[k2(v)] = reasons
    graph = build_project_graph(ws.cfg)
    res.remaining = scan(ws.cfg, spec, graph)
    res.unresolved = {k: r for k, r in unresolved.items() if any(k2(v) == k for v in res.remaining)}
    return res


def write_back(res, backup_dir: Path) -> list:
    """작업공간 변경을 원본에 반영. 기존 파일은 백업, 새 파일은 목록에 기록(되돌리기 때 삭제)."""
    ws = res.workspace
    changed = ws.changed()
    backup_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for rel in changed:
        src = ws.orig / rel
        created = not src.is_file()
        if not created:
            (backup_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, backup_dir / rel)
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes((ws.tmp / rel).read_bytes())  # 작업공간 파일을 바이트 그대로 (형식 보존)
        manifest.append({"path": rel, "created": created})
    (backup_dir / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return changed


def undo(root: Path, backup_dir: Path) -> list:
    manifest = json.loads((backup_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    out = []
    for entry in manifest:
        rel, created = (
            (entry, False) if isinstance(entry, str) else (entry["path"], entry["created"])
        )
        if created:
            (root / rel).unlink(missing_ok=True)  # 수정 때 새로 만든 파일은 지운다
        elif (backup_dir / rel).is_file():
            shutil.copy2(backup_dir / rel, root / rel)
        out.append(rel)
    return out

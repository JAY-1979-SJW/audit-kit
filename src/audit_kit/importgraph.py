"""AST 기반 모듈 임포트 그래프: 순환 임포트 탐지 + Mermaid 의존성 그래프.

모듈 최상위(임포트 시점에 실행되는) import만 간선으로 본다.
함수 안의 지연 임포트와 `if TYPE_CHECKING:` 블록은 순환을 일으키지 않으므로 제외한다.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from audit_kit.textio import read_text


def iter_py_files(path: Path):
    if path.is_file():
        yield path
        return
    for f in sorted(path.rglob("*.py")):
        if any(
            part.startswith(".") or part in {"__pycache__", "venv", ".venv"} for part in f.parts
        ):
            continue
        yield f


def module_name(file: Path, pkg_path: Path) -> str:
    """pkg_path(최상위 패키지 디렉터리) 기준으로 file의 점 표기 모듈명."""
    rel = file.relative_to(pkg_path.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


TOP = "top"  # 임포트 시점에 실행 (모듈 최상위, if/try/with/클래스 본문 포함)
LAZY = "lazy"  # 함수 안 지연 임포트
TYPE_ONLY = "type_checking"  # if TYPE_CHECKING: 블록


def iter_imports(body, ctx: str = TOP):
    """(import 노드, 컨텍스트) 전부."""
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node, ctx
        elif isinstance(node, ast.If):
            yield from iter_imports(node.body, TYPE_ONLY if _is_type_checking(node.test) else ctx)
            yield from iter_imports(node.orelse, ctx)
        elif isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
            for block in (node.body, node.orelse, node.finalbody):
                yield from iter_imports(block, ctx)
            for h in node.handlers:
                yield from iter_imports(h.body, ctx)
        elif isinstance(node, (ast.With, ast.AsyncWith, ast.ClassDef)):
            yield from iter_imports(node.body, ctx)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from iter_imports(node.body, TYPE_ONLY if ctx == TYPE_ONLY else LAZY)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            yield from iter_imports(node.body, ctx)
            yield from iter_imports(node.orelse, ctx)


def _toplevel_imports(body):
    """모듈 최상위에서 실행되는 import 문(함수 안·TYPE_CHECKING 제외)."""
    for node, ctx in iter_imports(body):
        if ctx == TOP:
            yield node


try:
    import sys as _sys

    STDLIB = set(_sys.stdlib_module_names)
except AttributeError:  # pragma: no cover  (3.9)
    STDLIB = {
        "os",
        "sys",
        "re",
        "json",
        "typing",
        "pathlib",
        "subprocess",
        "collections",
        "datetime",
        "math",
        "logging",
        "itertools",
        "functools",
        "dataclasses",
        "abc",
        "enum",
        "io",
        "time",
        "uuid",
        "__future__",
    }


@dataclass
class ImportRecord:
    src: str  # 임포트하는 모듈
    line: int
    context: str  # TOP / LAZY / TYPE_ONLY
    target: str | None = None  # 내부 모듈
    external: str | None = None  # 외부 최상위 패키지 (표준 라이브러리 제외)
    names: list = field(default_factory=list)  # from-import 한 이름 (내부 모듈 대상일 때)
    stmt: str = ""  # 원문 한 줄 요약


@dataclass
class _TarjanState:
    """Tarjan SCC 순회 중 여러 헬퍼가 함께 갱신하는 상태(PLR0913 로 낱개 인자 대신 묶음, 2026-09-28).
    `counter`는 정수 하나를 클로저처럼 공유하려고 리스트로 감쌌다(구현 자체는 그대로)."""

    index: dict = field(default_factory=dict)
    low: dict = field(default_factory=dict)
    stack: list = field(default_factory=list)
    on_stack: set = field(default_factory=set)
    sccs: list = field(default_factory=list)
    counter: list = field(default_factory=lambda: [0])


class ImportGraph:
    def __init__(self):
        self.modules: dict = {}  # module -> file(Path)
        self.edges: dict = {}  # module -> {target: line}  (TOP 임포트만, 순환 판단용)
        self.records: list = []  # 모든 임포트 (설계 검사용)
        self.trees: dict = {}

    @staticmethod
    def _collect_modules(pkg_paths: list, allow) -> tuple[dict, dict]:
        """pkg_paths 아래 .py 파일을 모듈 이름으로 모으고 각각 AST 로 파싱(구문 오류는 None)."""
        modules: dict = {}
        trees: dict = {}
        for pkg in pkg_paths:
            for f in iter_py_files(pkg):
                if allow is not None and f.resolve() not in allow:
                    continue
                mod = module_name(f, pkg) if pkg.is_dir() else pkg.stem
                modules[mod] = f
                try:
                    trees[mod] = ast.parse(read_text(f), filename=str(f))
                except SyntaxError:
                    trees[mod] = None
        return modules, trees

    def _record_node(self, mod: str, is_pkg: bool, node, ctx: str, roots: set) -> None:
        """import 문 하나를 분석해 self.edges/records 를 채운다(내부 대상이면 간선, 아니면
        외부 패키지 기록만 남긴다)."""
        stmt = ast.unparse(node) if hasattr(ast, "unparse") else ""
        by_target: dict = {}
        for target, name in self._targets_named(mod, is_pkg, node):
            if target != mod:
                by_target.setdefault(target, []).append(name)
        for target, names in by_target.items():
            self.records.append(
                ImportRecord(
                    mod, node.lineno, ctx, target=target, names=[n for n in names if n], stmt=stmt
                )
            )
            if ctx == TOP:
                self.edges[mod].setdefault(target, node.lineno)
        if not by_target and not getattr(node, "level", 0):
            for ext in self._externals(node):
                if ext not in roots and ext not in STDLIB:
                    self.records.append(
                        ImportRecord(mod, node.lineno, ctx, external=ext, stmt=stmt)
                    )

    @classmethod
    def build(cls, pkg_paths: list, allow=None) -> ImportGraph:
        """pkg_paths: 패키지/폴더/루트 파일. allow: 포함할 파일 절대경로 집합(없으면 전부)."""
        g = cls()
        g.modules, g.trees = g._collect_modules(pkg_paths, allow)
        roots = {m.split(".")[0] for m in g.modules}
        for mod, tree in g.trees.items():
            g.edges[mod] = {}
            if tree is None:
                continue
            is_pkg = g.modules[mod].name == "__init__.py"
            for node, ctx in iter_imports(tree.body):
                g._record_node(mod, is_pkg, node, ctx, roots)
        return g

    @staticmethod
    def _externals(node) -> set:
        if isinstance(node, ast.Import):
            return {a.name.split(".")[0] for a in node.names}
        return {node.module.split(".")[0]} if node.module else set()

    def _targets_named(self, mod: str, is_pkg: bool, node):
        """(내부 대상 모듈, from-import 한 이름 또는 None)."""
        if isinstance(node, ast.Import):
            for alias in node.names:
                t = self._resolve(alias.name)
                if t:
                    yield t, None
            return
        base = self.absolute_base(mod, is_pkg, node)
        if base is None:
            return
        for alias in node.names:
            sub = f"{base}.{alias.name}" if base else alias.name
            if sub in self.modules:  # from pkg import submodule
                yield sub, None
            else:
                t = self._resolve(base)
                if t:
                    yield t, alias.name

    @staticmethod
    def absolute_base(mod: str, is_pkg: bool, node: ast.ImportFrom):
        base = node.module or ""
        if node.level:
            pkg_parts = mod.split(".") if is_pkg else mod.split(".")[:-1]
            up = node.level - 1
            if up > len(pkg_parts):
                return None
            pkg_parts = pkg_parts[: len(pkg_parts) - up] if up else pkg_parts
            base = ".".join([*pkg_parts, base] if base else pkg_parts)
        return base

    def _resolve(self, name: str):
        """이름의 가장 긴 접두어 중 알려진 모듈."""
        parts = name.split(".")
        while parts:
            cand = ".".join(parts)
            if cand in self.modules:
                return cand
            parts.pop()
        return None

    # ---- 순환 탐지 (Tarjan SCC) ----
    @staticmethod
    def _pop_scc_if_root(node, state: _TarjanState) -> None:
        """node 가 SCC 뿌리(low==index)면 스택에서 그 성분을 꺼내 크기 2 이상만 기록한다."""
        if state.low[node] != state.index[node]:
            return
        comp = []
        while True:
            w = state.stack.pop()
            state.on_stack.discard(w)
            comp.append(w)
            if w == node:
                break
        if len(comp) > 1:
            state.sccs.append(sorted(comp))

    def _strongconnect(self, v, state: _TarjanState) -> None:
        """Tarjan SCC 를 정점 v 에서 시작해 진행(재귀 한도를 피하려고 스택 기반 반복형)."""
        index, low, stack, on_stack = state.index, state.low, state.stack, state.on_stack
        work = [(v, iter(self.edges.get(v, {})))]
        index[v] = low[v] = state.counter[0]
        state.counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        while work:
            node, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = state.counter[0]
                    state.counter[0] += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(self.edges.get(w, {}))))
                    advanced = True
                    break
                if w in on_stack:
                    low[node] = min(low[node], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            self._pop_scc_if_root(node, state)

    def _sccs(self) -> list:
        """강결합요소(크기 2 이상, 즉 진짜 순환에 낀 것만). `cycles()`·`nominate_cycle_breakers()`·
        `topological_layers()`가 모두 이걸 쓴다(같은 계산을 두 번 만들지 않으려고 분리)."""
        state = _TarjanState()
        for v in sorted(self.modules):
            if v not in state.index:
                self._strongconnect(v, state)
        return state.sccs

    def cycles(self) -> list:
        return [self._example_cycle(c) for c in self._sccs()]

    def _example_cycle(self, comp: list) -> list:
        """SCC 안에서 시작 모듈로 돌아오는 경로 하나(BFS 최단)."""
        members = set(comp)
        start = comp[0]
        prev = {start: None}
        queue = [start]
        while queue:
            cur = queue.pop(0)
            for nxt in self.edges.get(cur, {}):
                if nxt not in members:
                    continue
                if nxt == start:
                    path = [cur]
                    while prev[path[-1]] is not None:
                        path.append(prev[path[-1]])
                    return list(reversed(path)) + [start]
                if nxt not in prev:
                    prev[nxt] = cur
                    queue.append(nxt)
        return comp + [start]

    # ---- Mermaid 그래프 ----
    def mermaid(self, depth: int = 2) -> str:
        def collapse(m):
            return ".".join(m.split(".")[:depth])

        agg: dict[tuple[str, str], int] = {}
        for src, targets in self.edges.items():
            for t in targets:
                a, b = collapse(src), collapse(t)
                if a != b:
                    agg[a, b] = agg.get((a, b), 0) + 1
        nodes = sorted({collapse(m) for m in self.modules})
        ids = {n: f"n{i}" for i, n in enumerate(nodes)}
        cyc_pairs = set()
        for cyc in self.cycles():
            for a, b in zip(cyc, cyc[1:], strict=False):
                cyc_pairs.add((collapse(a), collapse(b)))
        lines = ["graph LR"]
        lines.extend(f'  {ids[n]}["{n}"]' for n in nodes)
        link_idx = 0
        red = []
        for (a, b), cnt in sorted(agg.items()):
            lines.append(f"  {ids[a]} -->|{cnt}| {ids[b]}")
            if (a, b) in cyc_pairs:
                red.append(str(link_idx))
            link_idx += 1
        if red:
            lines.append(f"  linkStyle {','.join(red)} stroke:#d33,stroke-width:2px")
        return "\n".join(lines)

    # ---- 결합도 지표 (Robert C. Martin, "OO Design Quality Metrics") ----
    def coupling_metrics(self) -> dict:
        """모듈별 Ca(구심 결합)·Ce(원심 결합)·Instability(I=Ce/(Ce+Ca)).

        정의는 Martin의 1994년 백서를 그대로 인용하는 두 출처로 교차 확인했다(2026-09-27):
        en.wikipedia.org/wiki/Software_package_metrics, pdepend.org 공식 문서. 두 출처 수치 일치.
        "God module" 판정용 절대 수치 임계값은 문헌에 없어(atlasarc.io: 정성적 서술만 확인) 여기서는
        만들지 않는다 — 호출한 쪽이 상대 순위(예: 상위 10%)로만 써야 한다.
        """
        ce = {m: len(targets) for m, targets in self.edges.items()}
        ca: dict[str, int] = dict.fromkeys(self.modules, 0)
        for targets in self.edges.values():
            for t in targets:
                if t in ca:
                    ca[t] += 1
        out = {}
        for m in self.modules:
            c_e, c_a = ce.get(m, 0), ca.get(m, 0)
            out[m] = {
                "ca": c_a,
                "ce": c_e,
                "instability": (c_e / (c_e + c_a)) if (c_e + c_a) else 0.0,
            }
        return out

    # ---- 순환을 끊을 간선 추천 (Eades, Lin, Smyth 의 그리디 휴리스틱) ----
    def nominate_cycle_breakers(self) -> list:
        """순환(SCC)마다, 없애면 그 순환을 깨는 근사 최소 간선 집합을 고른다.

        grimp 라이브러리의 `nominate_cycle_breakers()`가 "the greedy cycle-breaking heuristic of
        Eades, Lin and Smyth"를 쓴다는 공식 문서를 확인했다(2026-09-27, grimp.readthedocs.io).
        같은(공개된) 알고리즘을 새 의존성 없이 여기 직접 구현한다: SCC 를 노드 순서로 정렬해
        "뒤로 가는" 간선(순서를 어기는 간선)이 최소가 되게 하고, 그 간선들을 후보로 돌려준다.
        정확한 최솟값은 아니다(Feedback Arc Set 은 NP-hard) — 실전에서 잘 작동한다고 알려진 근사다.
        """
        breakers: list = []
        for comp in self._sccs():
            breakers.extend(self._break_component(comp))
        return breakers

    @staticmethod
    def _peel_one(remaining: set, adj: dict, remove) -> str | None:
        """adj[n] 이 빈(나가거나 들어오는 간선이 없는) 첫 노드를 정렬 순서로 찾아 remove 하고
        반환한다. 없으면 None(더 뗄 게 없음)."""
        for node in sorted(remaining):
            if not adj[node]:
                remove(node)
                return node
        return None

    @staticmethod
    def _succs_preds(comp: list, edges: dict, members: set) -> tuple[dict, dict]:
        """comp 성분 안에서만 보는 나가는/들어오는 인접 집합(succs/preds)을 만든다."""
        succs: dict[str, set] = {m: {t for t in edges.get(m, {}) if t in members} for m in comp}
        preds: dict[str, set] = {m: set() for m in comp}
        for m, ts in succs.items():
            for t in ts:
                preds[t].add(m)
        return succs, preds

    def _break_component(self, comp: list) -> list:
        members = set(comp)
        succs, preds = self._succs_preds(comp, self.edges, members)
        remaining = set(comp)
        s1: list[str] = []
        s2: list[str] = []

        def remove(node: str) -> None:
            remaining.discard(node)
            for p in preds[node]:
                succs[p].discard(node)
            for s in succs[node]:
                preds[s].discard(node)

        while remaining:
            node = self._peel_one(remaining, succs, remove)  # sink(나가는 간선 없음)부터 뒤에서
            while node is not None:
                s2.insert(0, node)
                node = self._peel_one(remaining, succs, remove)
            if not remaining:
                break
            node = self._peel_one(remaining, preds, remove)  # source(들어오는 간선 없음)는 앞에서
            while node is not None:
                s1.append(node)
                node = self._peel_one(remaining, preds, remove)
            if not remaining:
                break
            node = max(
                remaining, key=lambda n: (len(succs[n]) - len(preds[n]), n)
            )  # 나머지 중 가장 "나가는 쪽이 많은" 노드
            s1.append(node)
            remove(node)

        order = s1 + s2
        pos = {m: i for i, m in enumerate(order)}
        result: list[tuple[str, str, int]] = []
        for m in comp:
            result.extend(
                (m, t, self.edges[m][t])
                for t in self.edges.get(m, {})
                if t in members and pos[m] > pos[t]  # 정해진 순서를 거스르는(뒤로 가는) 간선
            )
        return result

    # ---- 이름 없이, 구조로만 본 계층 (SCC 응축 → 위상 정렬) ----
    def _condensation(self) -> tuple[list, dict, list]:
        """SCC 를 하나로 묶은 응축그래프(항상 비순환). 반환: (성분별 모듈 목록,
        모듈->성분번호, 성분별 나가는 간선(다른 성분 번호) 집합)."""
        comp_members: list = [list(c) for c in self._sccs()]
        comp_of: dict[str, int] = {}
        for i, comp in enumerate(comp_members):
            for m in comp:
                comp_of[m] = i
        for m in sorted(self.modules):
            if m not in comp_of:
                comp_of[m] = len(comp_members)
                comp_members.append([m])
        n = len(comp_members)
        comp_edges: list[set] = [set() for _ in range(n)]
        for m, targets in self.edges.items():
            cm = comp_of[m]
            for t in targets:
                ct = comp_of.get(t)
                if ct is not None and ct != cm:
                    comp_edges[cm].add(ct)
        return comp_members, comp_of, comp_edges

    @staticmethod
    def _topo_depths(comp_edges: list) -> list:
        """응축그래프(비순환)를 위상 정렬하며 각 성분의 깊이(가장 먼 하류까지 거리)를 구한다
        (Kahn 알고리즘: 나가는 간선이 없는 노드부터 뒤에서 채워 나간다)."""
        n = len(comp_edges)
        out_remaining = [len(comp_edges[c]) for c in range(n)]
        rev: list[set] = [set() for _ in range(n)]
        for c, outs in enumerate(comp_edges):
            for t in outs:
                rev[t].add(c)
        depth = [0] * n
        order = [c for c in range(n) if out_remaining[c] == 0]
        i = 0
        while i < len(order):
            c = order[i]
            i += 1
            for p in rev[c]:
                out_remaining[p] -= 1
                depth[p] = max(depth[p], depth[c] + 1)
                if out_remaining[p] == 0:
                    order.append(p)
        return depth

    def topological_layers(self) -> list:
        """SCC 를 하나로 묶은 응축그래프는 항상 비순환이다(cp-algorithms.com 확인, 2026-09-27:
        "The most important property of the condensation graph is that it is acyclic"). 이걸
        위상 정렬해 계층으로 나눈다 — 폴더 이름을 전혀 보지 않고 실제 임포트 방향만 본다.
        반환값: [0번째(아무것도 내부 임포트 안 함, 가장 기초) 계층, 1번째, ...]. 이름 기반 추정
        (`arch/spec.py`의 `_infer_layers`)과는 독립적인 참고용이라, 다른 순서가 나올 수 있다.
        """
        _comp_members, comp_of, comp_edges = self._condensation()
        depth = self._topo_depths(comp_edges)
        layers: dict[int, list] = {}
        for m, c in comp_of.items():
            layers.setdefault(depth[c], []).append(m)
        return [sorted(layers[d]) for d in sorted(layers)]

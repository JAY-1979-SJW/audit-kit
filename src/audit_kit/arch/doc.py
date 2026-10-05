"""ARCHITECTURE.md 생성 + import-linter 계약 내보내기."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import cast

from audit_kit.arch.scan import RULE_LABEL
from audit_kit.arch.spec import ArchSpec, matches
from audit_kit.importgraph import TYPE_ONLY, ImportGraph


def _owner(spec: ArchSpec, module: str) -> str:
    """다이어그램 노드: 계층에 배정된 모듈 접두어, 없으면 최상위 하위패키지."""
    best = None
    for lay in spec.layers:
        for p in lay.modules:
            if matches(module, [p]) and (best is None or len(p) > len(best)):
                best = p
    if best:
        return best
    for e in spec.entrypoints:
        if matches(module, [e]):
            return e
    return ".".join(module.split(".")[:2])


def _layer_subgraphs(spec: ArchSpec, node, placed: set) -> list:
    L = []
    for i, lay in enumerate(spec.layers):
        L.append(f'  subgraph L{i}["{i + 1}. {lay.name}"]')
        for p in lay.modules:
            L.append(f'    {node(p)}["{p}"]')
            placed.add(p)
        L.append("  end")
    if spec.entrypoints:
        L.append('  subgraph EP["진입점"]')
        for e in spec.entrypoints:
            L.append(f'    {node(e)}["{e}"]')
            placed.add(e)
        L.append("  end")
    return L


def mermaid(spec: ArchSpec, graph: ImportGraph, violations: list) -> str:
    """의존성 다이어그램(mermaid). 계층/진입점 서브그래프 부분은 `_layer_subgraphs()`로 뽑아냈다
    (STD-08: 원래 이 함수 하나가 복잡도 13이었다, 2026-09-28)."""
    nid: dict[str, str] = {}

    def node(name):
        if name not in nid:
            nid[name] = f"m{len(nid)}"
        return nid[name]

    placed: set = set()
    L = ["flowchart TB", *_layer_subgraphs(spec, node, placed)]
    bad_pairs = {
        (_owner(spec, v.src), _owner(spec, v.target))
        for v in violations
        if v.src and v.target and v.target in graph.modules
    }
    edges: Counter[tuple[str, str]] = Counter()
    for r in graph.records:
        if r.target and r.context != TYPE_ONLY:
            a, b = _owner(spec, r.src), _owner(spec, r.target)
            if a != b:
                edges[a, b] += 1
    L.extend(
        f'  {node(name)}["{name} (미배정)"]:::unassigned'
        for name in sorted({x for pair in edges for x in pair} - placed)
    )
    red, idx = [], 0
    for (a, b), n in sorted(edges.items()):
        L.append(f"  {node(a)} -->|{n}| {node(b)}")
        if (a, b) in bad_pairs:
            red.append(str(idx))
        idx += 1
    if red:
        L.append(f"  linkStyle {','.join(red)} stroke:#d33,stroke-width:2px")
    L.append("  classDef unassigned stroke-dasharray: 4 3")
    return "\n".join(L)


def _type_sections(spec: ArchSpec) -> list:
    """프로젝트 유형이 있을 때만 나오는 "계층별 책임"·"공통 관심사 위치" 섹션."""
    if not spec.types:
        return []
    from audit_kit.arch.templates import CONCERNS, TYPES

    L = [
        f"**프로젝트 유형**: {', '.join(cast(str, TYPES[t]['title']) for t in spec.types if t in TYPES)}",
        "",
    ]
    if any(lay.responsibility or lay.must_not for lay in spec.layers):
        L += ["### 계층별 책임", "", "| 계층 | 하는 일 | 하면 안 되는 일 |", "|---|---|---|"]
        L += [
            f"| **{lay.name}** | {lay.responsibility} | {', '.join(lay.must_not) or '-'} |"
            for lay in spec.layers
        ]
        L.append("")
    if spec.concerns:
        L += [
            "### 공통 관심사 위치",
            "",
            "이 작업은 지정 위치(+진입점)에서만 한다.",
            "",
            "| 관심사 | 지정 위치 | 이유 |",
            "|---|---|---|",
        ]
        L += [
            f"| {cast(dict, CONCERNS[c])['title']} | {', '.join(f'`{p}`' for p in v)} | "
            f"{cast(dict, CONCERNS[c])['why']} |"
            for c, v in spec.concerns.items()
            if c in CONCERNS
        ]
        L.append("")
    return L


def render_markdown(root_name: str, spec: ArchSpec, graph: ImportGraph, violations: list) -> str:
    """ARCHITECTURE.md 텍스트. 프로젝트 유형 관련 섹션은 `_type_sections()`로 뽑아냈다
    (STD-08: 원래 이 함수 하나가 복잡도 11이었다, 2026-09-28)."""
    L = [
        f"# {root_name} 프로그램 구조",
        "",
        f"> `architecture.toml` 에서 생성됨 ({datetime.now():%Y-%m-%d %H:%M}). 직접 고치지 말고 설계 파일을 수정한 뒤 "
        "`audit-kit arch doc` 로 다시 생성하세요.",
        "",
        "## 계층",
        "",
        "위 계층은 아래 계층만 임포트할 수 있다.",
        "",
        "| # | 계층 | 모듈 | 역할 | 임포트 가능 |",
        "|---|---|---|---|---|",
    ]
    n = len(spec.layers)
    for i, lay in enumerate(spec.layers):
        if i == n - 1:
            can = "(외부 라이브러리만)"
        elif spec.allow_skip_layers:
            can = ", ".join(x.name for x in spec.layers[i + 1 :])
        else:
            can = spec.layers[i + 1].name
        if lay.siblings_independent:
            can += " — 같은 계층 형제끼리 금지"
        L.append(
            f"| {i + 1} | **{lay.name}** | {'<br>'.join(f'`{m}`' for m in lay.modules)} | {lay.description} | {can} |"
        )
    L += [
        "",
        f"**진입점** (모든 계층 조립, 검사 제외): {', '.join(f'`{e}`' for e in spec.entrypoints) or '없음'}",
        "",
    ]
    L += _type_sections(spec)
    L += [
        "## 규칙",
        "",
        f"- 계층 건너뛰기: {'허용' if spec.allow_skip_layers else '금지'}",
        f"- `if TYPE_CHECKING:` 임포트: {'검사 제외' if spec.ignore_type_checking else '검사'}",
        f"- 함수 안 지연 임포트: {'위반으로 봄' if spec.lazy_imports == 'violation' else '허용'}",
        f"- 순환 임포트: {'금지' if spec.cycles == 'forbid' else '허용'}",
        "",
    ]
    if spec.external:
        L += ["## 외부 라이브러리 사용 위치", "", "| 패키지 | 허용 위치 | 이유 |", "|---|---|---|"]
        L += [f"| `{e.package}` | {', '.join(e.allowed_in)} | {e.reason} |" for e in spec.external]
        L.append("")
    if spec.forbidden or spec.independent:
        L += ["## 금지·독립 규칙", ""]
        L += [
            f"- **{f.name}**: {', '.join(f.source)} ↛ {', '.join(f.forbidden)}"
            for f in spec.forbidden
        ]
        L += [f"- **{d.name}**: {' / '.join(d.modules)} 서로 임포트 금지" for d in spec.independent]
        L.append("")
    L += [
        "## 의존성 (실제 코드)",
        "",
        "빨간 선 = 설계 위반. 숫자 = import 개수. 점선 테두리 = 미배정 모듈.",
        "",
        "```mermaid",
        mermaid(spec, graph, violations),
        "```",
        "",
    ]
    L += ["## 현재 위반", ""]
    if violations:
        cnt = Counter(v.rule for v in violations)
        L += ["| 규칙 | 건수 |", "|---|---|"] + [
            f"| {RULE_LABEL.get(r, r)} | {c} |" for r, c in cnt.most_common()
        ]
        L += ["", "상세: `audit-kit arch check`, 자동 수정: `audit-kit arch fix`"]
    else:
        L.append("없음 ✅")
    L.append("")
    return "\n".join(L)


def importlinter_contracts(spec: ArchSpec) -> str:
    """CI 에서 import-linter 를 쓰고 싶을 때 붙여 넣을 계약."""
    L = [
        "[tool.importlinter]",
        f"root_packages = [{', '.join(repr(p) for p in spec.root_packages)}]".replace("'", '"'),
    ]
    if spec.ignore_type_checking:
        L.append("exclude_type_checking_imports = true")
    L += [
        "",
        "[[tool.importlinter.contracts]]",
        'name = "계층 (architecture.toml)"',
        'type = "layers"',
        "layers = [",
    ]
    L += [f'    "{" | ".join(lay.modules)}",' for lay in spec.layers]
    L.append("]")
    for f in spec.forbidden:
        L += [
            "",
            "[[tool.importlinter.contracts]]",
            f'name = "{f.name}"',
            'type = "forbidden"',
            "source_modules = [" + ", ".join(f'"{x}"' for x in f.source) + "]",
            "forbidden_modules = [" + ", ".join(f'"{x}"' for x in f.forbidden) + "]",
        ]
    for d in spec.independent:
        L += [
            "",
            "[[tool.importlinter.contracts]]",
            f'name = "{d.name}"',
            'type = "independence"',
            "modules = [" + ", ".join(f'"{x}"' for x in d.modules) + "]",
        ]
    return "\n".join(L) + "\n"

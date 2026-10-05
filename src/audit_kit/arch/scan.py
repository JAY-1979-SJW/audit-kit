"""설계(architecture.toml) 대비 코드 위반 검사."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from audit_kit.arch.spec import ArchSpec, matches, validate_spec
from audit_kit.config import AuditConfig
from audit_kit.importgraph import LAZY, TOP, TYPE_ONLY, ImportGraph
from audit_kit.models import CRITICAL, IMPROVE, REVIEW, Finding

RULE_LABEL = {
    "ARCH-LAYER": "계층 역방향 임포트",
    "ARCH-SKIP": "계층 건너뛰기",
    "ARCH-SIBLING": "같은 계층 독립 위반",
    "ARCH-ENTRY": "진입점 임포트",
    "ARCH-FORBIDDEN": "금지 규칙",
    "ARCH-EXTERNAL": "외부 라이브러리 사용 위치",
    "ARCH-INDEPENDENT": "독립 모듈 간 임포트",
    "ARCH-CYCLE": "순환 임포트",
    "ARCH-UNASSIGNED": "미배정 모듈",
    "ARCH-SPEC": "설계 파일 불일치",
    "ARCH-SUPPORT-REVERSE": "제품 코드가 보조 코드(테스트·스크립트·플러그인·마이그레이션)를 임포트",
    "ARCH-PRIVATE": "보조 코드가 제품의 비공개(_) 함수 사용",
    "ARCH-CONCERN": "공통 관심사(설정·로깅·DB·COM·HTTP)를 지정 위치 밖에서 수행",
}


@dataclass
class Violation:
    rule: str
    message: str
    src: str | None = None
    file: str | None = None
    line: int | None = None
    target: str | None = None
    names: list = field(default_factory=list)
    context: str = TOP
    hint: str = ""
    severity: str = ""  # 비우면 규칙 기본값

    def key(self):
        return (self.rule, self.src, self.target, self.line)

    def to_finding(self) -> Finding:
        sev = self.severity or (
            REVIEW if self.rule in ("ARCH-UNASSIGNED", "ARCH-SPEC") else IMPROVE
        )
        ctx = {LAZY: " (함수 안 지연 임포트)", TYPE_ONLY: " (TYPE_CHECKING)"}.get(self.context, "")
        return Finding(
            tool="arch",
            category="구조",
            rule=self.rule,
            message=self.message + ctx,
            file=self.file,
            line=self.line,
            severity=sev,
            evidence=f"architecture.toml 설계 — {RULE_LABEL.get(self.rule, self.rule)}"
            + (f". 수정: {self.hint}" if self.hint else ""),
        )


def _skip_ctx(spec: ArchSpec, ctx: str) -> bool:
    return (ctx == TYPE_ONLY and spec.ignore_type_checking) or (
        ctx == LAZY and spec.lazy_imports == "allow"
    )


def _names(r) -> str:
    return f" ({', '.join(r.names)})" if r.names else ""


def _spec_violations(spec: ArchSpec, graph: ImportGraph) -> list:
    return [
        Violation(
            "ARCH-SPEC",
            problem,
            file="architecture.toml",
            hint="architecture.toml 을 코드 구조에 맞게 고치거나 코드를 설계대로 옮기세요",
        )
        for problem in validate_spec(spec, set(graph.modules))
    ]


def _support_role_violations(r, spec: ArchSpec, s_role: str, base: dict) -> list:
    """보조 코드(테스트·스크립트·플러그인·마이그레이션)에서 나가는 임포트: 계층 규칙 대신 비공개 사용만 본다."""
    if not (
        r.target
        and s_role != "tests"
        and spec.private_use == "warn"
        and spec.is_production(r.target)
    ):
        return []
    priv = [n for n in r.names if n.startswith("_") and not n.startswith("__")]
    if not priv:
        return []
    return [
        Violation(
            "ARCH-PRIVATE",
            f"{s_role} {r.src} 가 {r.target} 의 비공개 함수 사용 ({', '.join(priv)})",
            hint="제품 코드에 공개 함수를 만들어 그것을 쓰게 한다 (내부 구현을 바꾸면 조용히 깨짐)",
            **base,
        )
    ]


def _external_violations(r, spec: ArchSpec, base: dict) -> list:
    out: list = []
    for e in spec.external:
        if r.external != e.package:
            continue
        si, slay = spec.layer_of(r.src)
        ok = (
            ("entrypoints" in e.allowed_in and spec.is_entrypoint(r.src))
            or (slay is not None and slay.name in e.allowed_in)
            or matches(r.src, [p for p in e.allowed_in if "." in p or p in spec.root_packages])
        )
        if not ok:
            out.append(
                Violation(
                    "ARCH-EXTERNAL",
                    f"{r.src} 가 {r.external} 사용 — 허용 위치: {', '.join(e.allowed_in)}",
                    hint=f"{r.external} 사용 코드를 허용된 계층으로 옮기고 결과만 전달"
                    + (f" ({e.reason})" if e.reason else ""),
                    **base,
                )
            )
    out.extend(
        Violation(
            "ARCH-FORBIDDEN",
            f"[{fb.name}] {r.src} -> {r.external}",
            hint="금지된 의존 제거",
            **base,
        )
        for fb in spec.forbidden
        if matches(r.src, fb.source) and r.external in fb.forbidden
    )
    return out


def _layer_violations(src: str, tgt: str, spec: ArchSpec, r, base: dict) -> list:
    if spec.is_entrypoint(src):
        return []
    si, slay = spec.layer_of(src)
    ti, tlay = spec.layer_of(tgt)
    if si is None or ti is None:
        return []
    if si > ti:
        return [
            Violation(
                "ARCH-LAYER",
                f"{slay.name} 계층 {src} 가 상위 {tlay.name} 계층 {tgt} 를 임포트{_names(r)}",
                hint="임포트한 코드를 하위 계층으로 이동하거나(arch fix), 타입 전용이면 TYPE_CHECKING 으로",
                **base,
            )
        ]
    if ti - si > 1 and not spec.allow_skip_layers:
        return [
            Violation(
                "ARCH-SKIP",
                f"{src} ({slay.name}) 가 {tgt} ({tlay.name}) 를 건너뛰어 임포트",
                hint="중간 계층을 통해 호출",
                **base,
            )
        ]
    if si == ti and slay.siblings_independent:
        a = next(p for p in slay.modules if matches(src, [p]))
        b = next(p for p in slay.modules if matches(tgt, [p]))
        if a != b:
            return [
                Violation(
                    "ARCH-SIBLING",
                    f"{slay.name} 계층 형제 {src} -> {tgt}{_names(r)}",
                    hint="공통 부분을 하위 계층으로 추출",
                    **base,
                )
            ]
    return []


def _record_violations(r, spec: ArchSpec, base: dict) -> list:
    """레코드 하나(임포트 한 줄)가 낼 수 있는 모든 종류의 위반. `scan()`의 본체 루프를 뽑아낸
    것 — 종류별로 먼저 걸러내는 순서(보조 코드 → 보조코드 역참조 → 외부 라이브러리 → 나머지)는
    그대로다(원래 코드의 `continue` 체인과 같은 순서·의미)."""
    s_role = spec.support_role(r.src)
    if s_role:
        return _support_role_violations(r, spec, s_role, base)

    t_role = spec.support_role(r.target) if r.target else None
    if t_role:
        return [
            Violation(
                "ARCH-SUPPORT-REVERSE",
                f"제품 코드 {r.src} 가 {t_role} 코드 {r.target} 를 임포트{_names(r)}",
                hint=f"필요한 코드를 제품 패키지로 옮기고 {t_role} 쪽이 그것을 임포트하게 한다",
                severity=CRITICAL if t_role in ("tests", "scripts") else "",
                **base,
            )
        ]

    if r.external:
        return _external_violations(r, spec, base)

    src, tgt = r.src, r.target
    out: list = []
    if spec.is_entrypoint(tgt) and not spec.is_entrypoint(src) and tgt not in spec.root_packages:
        out.append(
            Violation(
                "ARCH-ENTRY",
                f"{src} 가 진입점 {tgt} 를 임포트{_names(r)}",
                hint="진입점은 조립만 한다. 필요한 것을 하위 계층으로 옮기세요",
                **base,
            )
        )
    out.extend(
        Violation(
            "ARCH-FORBIDDEN",
            f"[{fb.name}] {src} -> {tgt}{_names(r)}",
            hint="금지된 의존 제거",
            **base,
        )
        for fb in spec.forbidden
        if matches(src, fb.source) and matches(tgt, fb.forbidden)
    )
    for ind in spec.independent:
        a = next((m for m in ind.modules if matches(src, [m])), None)
        b = next((m for m in ind.modules if matches(tgt, [m])), None)
        if a and b and a != b:
            out.append(
                Violation(
                    "ARCH-INDEPENDENT",
                    f"[{ind.name}] {src} -> {tgt}{_names(r)}",
                    hint="공통 부분을 하위 계층으로 추출",
                    **base,
                )
            )
    out.extend(_layer_violations(src, tgt, spec, r, base))
    return out


def _concern_violations(spec: ArchSpec, graph: ImportGraph, rel: dict) -> list:
    from audit_kit.arch.concerns import find_uses
    from audit_kit.arch.templates import CONCERNS

    out: list = []
    prod_mods = {m for m in graph.modules if spec.is_production(m) and not spec.support_role(m)}
    for u in find_uses(graph, prod_mods):
        places = spec.concerns.get(u.concern)
        if (
            not places
            or spec.is_entrypoint(u.module)
            or matches(u.module, [p for p in places if p != "entrypoints"])
        ):
            continue
        info = cast(dict, CONCERNS[u.concern])
        out.append(
            Violation(
                "ARCH-CONCERN",
                f"{u.module} 에서 {info['title']} 직접 수행 ({u.text}) — 지정 위치: {', '.join(places)}",
                src=u.module,
                file=rel.get(u.module),
                line=u.line,
                target=u.concern,
                hint=f"{places[0]} 에 함수를 두고 그것을 호출. 이유: {info['why']}",
            )
        )
    return out


def _cycle_violations(graph: ImportGraph, rel: dict) -> list:
    out: list = []
    for cyc in graph.cycles():
        first, second = cyc[0], cyc[1]
        out.append(
            Violation(
                "ARCH-CYCLE",
                "순환 임포트: " + " -> ".join(cyc),
                src=first,
                file=rel.get(first),
                line=graph.edges[first].get(second),
                target=second,
                context=TOP,
                names=_edge_names(graph, first, second),
                hint="한쪽이 쓰는 심볼을 하위로 이동, 타입 전용이면 TYPE_CHECKING, 최후 수단은 지연 임포트",
            )
        )
    return out


def _unassigned_violations(spec: ArchSpec, graph: ImportGraph, scope, rel: dict) -> list:
    out: list = []
    seen: set = set()
    for m in sorted(graph.modules):
        if (
            m in spec.root_packages
            or spec.is_entrypoint(m)
            or spec.layer_of(m)[0] is not None
            or spec.support_role(m)
        ):
            continue
        top = ".".join(m.split(".")[:2])
        if top in seen:
            continue
        seen.add(top)
        out.append(
            Violation(
                "ARCH-UNASSIGNED",
                f"{top} 가 어느 계층에도 속하지 않음",
                src=m,
                file=rel.get(m),
                hint="architecture.toml 의 계층 modules 또는 entrypoints 에 추가",
            )
        )
    # 제품도 보조도 아닌 폴더·파일 (검사 범위 밖에 방치된 코드)
    groups: dict = {}
    for f in scope.unassigned:
        groups.setdefault(
            "/".join(f.split("/")[:2]) if f.count("/") >= 2 else f.split("/")[0], []
        ).append(f)
    for top, files in sorted(groups.items()):
        out.append(
            Violation(
                "ARCH-UNASSIGNED",
                f"{top}: 파일 {len(files)}개가 제품 코드에도 보조 코드에도 속하지 않음",
                src=top,
                file=files[0],
                hint="packages(제품) 또는 [support] 역할에 넣거나, 필요 없으면 삭제·exclude_paths 로 사유와 함께 제외",
            )
        )
    return out


def scan(cfg: AuditConfig, spec: ArchSpec, graph: ImportGraph | None = None, scope=None) -> list:
    """설계 위반 전체를 모은다. 종류별 검사는 각 `_*_violations()` 헬퍼로 나눠뒀다(STD-08:
    원래 이 함수 하나가 복잡도 35(허용 10)·분기 34(허용 12)·문장 82(허용 50)이었다 — 로직은
    그대로 두고 종류별로만 뽑아냈다, 2026-09-28)."""
    from audit_kit.scope import build_project_graph, get_scope, support_module_prefixes

    scope = scope or get_scope(cfg)
    # [support] 에 없거나 비어 있는 역할은 폴더 이름 자동 분류로 채운다
    for role, prefixes in support_module_prefixes(scope).items():
        if not spec.support.get(role):
            spec.support[role] = prefixes
    graph = graph or build_project_graph(cfg)
    rel = {m: cfg.rel(f) for m, f in graph.modules.items()}

    out: list = list(_spec_violations(spec, graph))
    for r in graph.records:
        if _skip_ctx(spec, r.context):
            continue
        base = dict(
            src=r.src,
            file=rel.get(r.src),
            line=r.line,
            target=r.target or r.external,
            names=list(r.names),
            context=r.context,
        )
        out.extend(_record_violations(r, spec, base))

    if spec.concerns:
        out.extend(_concern_violations(spec, graph, rel))

    if spec.cycles == "forbid":
        out.extend(_cycle_violations(graph, rel))

    if spec.unassigned == "warn":
        out.extend(_unassigned_violations(spec, graph, scope, rel))

    uniq, keys = [], set()
    for v in out:
        if v.key() not in keys:
            keys.add(v.key())
            uniq.append(v)
    return uniq


def _edge_names(graph: ImportGraph, src: str, tgt: str) -> list:
    return sorted({
        n
        for r in graph.records
        if r.src == src and r.target == tgt and r.context == TOP
        for n in r.names
    })

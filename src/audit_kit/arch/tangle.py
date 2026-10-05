"""architecture.toml(설계 파일) 없이도 도는 "엉킨 의존성(거미줄)" 진단 + 자동 복구.

새로 만든 것은 진단(지표·간선 추천·구조 기반 계층) 뿐이다(importgraph.py 참고,
2026-09-27 조사 근거: Martin 의 결합도 지표, grimp 공식 문서가 밝힌 Eades-Lin-Smyth 순환
간선 추천 알고리즘, SCC 응축그래프는 항상 비순환이라는 그래프 이론).

자동 복구는 **새로 만들지 않고** `arch/fix.py` 의 기존 안전장치(Workspace 임시 작업공간,
Verifier 의 3단계 검증, reexport/type_only/move/lazy 전략)를 그대로 재사용한다 — 계층이
없는(layers=[]) ArchSpec 을 주면 ARCH-LAYER/SIBLING/SKIP/INDEPENDENT/FORBIDDEN 은 애초에
안 걸리고 ARCH-CYCLE 만 남으므로, `run_fix()`를 고치지 않고 그대로 호출할 수 있다(2026-09-27
실측으로 확인: 계층 없는 spec 으로 기존 test_arch.py 의 move/lazy 순환 픽스처가 그대로 통과).

**한계(실측으로 확인, 문서에 남김)**: reexport/type_only/move/lazy 는 전부 "이름 있는 심볼을
옮긴다"는 전제라 `v.names`가 있어야 동작한다. `from pkg import 다른서브모듈`처럼 서브모듈
자체를 주고받는 순환(이름이 없음)은 네 전략 다 적용 못 하고 사람이 봐야 한다 — 그런 순환은
리포트에 "자동 복구 불가"로 따로 표시한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from audit_kit.arch.fix import FixResult, run_fix
from audit_kit.arch.scan import _edge_names
from audit_kit.arch.spec import ArchSpec
from audit_kit.config import AuditConfig, load_config
from audit_kit.importgraph import ImportGraph
from audit_kit.scope import build_project_graph

TOP_DEFAULT = 15


def bare_spec(cfg: AuditConfig) -> ArchSpec:
    """계층·금지·독립 그룹이 전부 비어 있는 ArchSpec. `layer_of()`가 항상 (None, None)을
    돌려줘 `Ctx.allowed()`가 항상 True 가 되므로, architecture.toml 없이도 `arch/fix.py`의
    기존 전략들을 그대로 쓸 수 있다(ARCH-CYCLE 외의 조항은 이 spec 으로는 걸리지 않는다)."""
    return ArchSpec(root_packages=list(cfg.packages))


@dataclass
class TangleReport:
    modules: int
    cycles: list = field(default_factory=list)  # [[모듈, ...], ...] (SCC 크기순 정렬은 호출부에서)
    fixable_cycles: list = field(default_factory=list)  # 이름 있는 간선이 하나라도 있는 순환
    breakers: list = field(default_factory=list)  # (from, to, line) — 끊으면 그 순환이 깨지는 간선
    coupling: dict = field(default_factory=dict)  # 모듈 -> {ca, ce, instability} (상위 N개만)
    layers: list = field(default_factory=list)  # 구조(이름 무관) 기반 계층, [0]=가장 기초
    mermaid_text: str = ""


def _has_named_edge(graph: ImportGraph, cycle: list) -> bool:
    """이 순환의 간선 중 하나라도 이름 있는 심볼 임포트(from x import 이름)인가 —
    그래야 reexport/type_only/move/lazy 중 하나라도 시도할 대상이 생긴다."""
    for a, b in zip(cycle, cycle[1:], strict=False):
        if _edge_names(graph, a, b):
            return True
    return False


def diagnose(cfg: AuditConfig, top: int = TOP_DEFAULT) -> TangleReport:
    graph = build_project_graph(cfg)
    cycles = sorted(graph.cycles(), key=len, reverse=True)
    coupling = graph.coupling_metrics()
    ranked = dict(sorted(coupling.items(), key=lambda kv: -(kv[1]["ca"] + kv[1]["ce"]))[:top])
    return TangleReport(
        modules=len(graph.modules),
        cycles=cycles,
        fixable_cycles=[c for c in cycles if _has_named_edge(graph, c)],
        breakers=graph.nominate_cycle_breakers(),
        coupling=ranked,
        layers=graph.topological_layers(),
        mermaid_text=graph.mermaid(),
    )


def render_report(report: TangleReport) -> str:
    lines = [f"모듈 {report.modules}개 검사"]
    if not report.cycles:
        lines.append("순환 임포트 없음")
    else:
        lines.append(f"순환 임포트 {len(report.cycles)}개 (강결합요소 크기 큰 순):")
        for cyc in report.cycles:
            tag = (
                ""
                if cyc in report.fixable_cycles
                else " [자동 복구 불가 — 서브모듈 자체를 주고받음]"
            )
            lines.append(f"  {' -> '.join(cyc)}{tag}")
        if report.breakers:
            lines.append(
                "끊으면 순환이 깨지는 간선 후보(Eades-Lin-Smyth 근사, 정확한 최솟값은 아님):"
            )
            for src, tgt, line in report.breakers:
                lines.append(f"  {src}:{line} 의 '{tgt}' 임포트")
    lines += [
        "",
        (
            f"결합도 상위 {len(report.coupling)}개 "
            "(수치 임계값은 근거 문헌이 없어 상대 순위로만 표시, Ca=참조받음 Ce=참조함):"
        ),
    ]
    for name, v in report.coupling.items():
        lines.append(f"  {name:42} Ca={v['ca']:4} Ce={v['ce']:4} I={v['instability']:.2f}")
    lines += [
        "",
        f"구조(이름 무관) 기반 계층 {len(report.layers)}단(0=가장 기초, 이름 기반 추정과 다를 수 있음):",
    ]
    lines += [f"  {i}: {', '.join(layer)}" for i, layer in enumerate(report.layers)]
    return "\n".join(lines)


def fix(
    cfg: AuditConfig,
    allow_lazy: bool = False,
    import_check: bool = True,
    max_iter: int = 200,
) -> FixResult:
    """순환 임포트를 `arch/fix.py`의 기존 전략(reexport/type_only/move, 필요 시 lazy)으로
    고친다. `architecture.toml` 이 없어도 되게 계층 없는 spec 을 준다(`bare_spec`)."""
    return run_fix(
        cfg, bare_spec(cfg), allow_lazy=allow_lazy, import_check=import_check, max_iter=max_iter
    )


# ---------------------------------------------------------------- CLI (`audit-kit tangle`)
def cmd_tangle(args) -> int:
    from audit_kit.arch.commands import ApplyLabels, FixOutput, _apply_or_preview, _write_report

    cfg = load_config(args.path)
    if not args.fix:
        report = diagnose(cfg, top=args.top)
        print(render_report(report))
        return 1 if report.cycles else 0

    print("작업공간(임시 복사본)에서 수정·검증 중...")
    res = fix(cfg, allow_lazy=args.allow_lazy, import_check=not args.no_import_check)
    ws = res.workspace
    try:
        changed = ws.changed()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = cfg.root / cfg.report_dir / f"tangle-fix_{ts}"
        out_dir.mkdir(parents=True, exist_ok=True)
        print(
            f"\n순환 관련 위반 {res.before}건 → {len(res.remaining)}건 "
            f"(자동 수정 {len(res.applied)}건, 파일 {len(changed)}개)"
        )
        for a in res.applied:
            print(f"  ✔ [{a.kind}] {a.description}")
        if res.unresolved:
            print("\n자동 수정 불가(사람 판단 필요 — 이름 없는 서브모듈 순환이거나 의존이 복잡함):")
            for (_rule, src, tgt, names), reasons in res.unresolved.items():
                print(f"  ✘ {src} -> {tgt} {list(names) or ''}")
                for r in reasons:
                    print(f"      - {r}")
        diffs = "".join(ws.diff(rel) for rel in changed)
        _write_report(out_dir, res, changed, diffs)
        print(f"\n리포트: {out_dir / 'tangle-fix.md'}")
        # 반영 절차(커밋 상태 확인 → 검증 → write_back)는 arch fix 와 완전히 같아 공용 헬퍼를 쓴다
        # (표식 파일도 같은 이름을 써서, 되돌리기는 기존 `audit-kit arch undo` 를 그대로 쓴다).
        return _apply_or_preview(
            cfg,
            res,
            FixOutput(out_dir, changed, diffs),
            args,
            ApplyLabels(preview_cmd="audit-kit tangle --fix --apply"),
        )
    finally:
        ws.cleanup()


def register(sub) -> None:
    p = sub.add_parser(
        "tangle", help="architecture.toml 없이도: 엉킨 의존성(순환 임포트) 진단 + 복구"
    )
    p.add_argument("--path")
    p.add_argument("--top", type=int, default=TOP_DEFAULT, help="결합도 랭킹 상위 몇 개(기본 15)")
    p.add_argument("--fix", action="store_true", help="순환을 자동으로 고쳐본다(기본은 진단만)")
    p.add_argument("--apply", action="store_true", help="원본에 적용(백업은 항상 만들어짐)")
    p.add_argument(
        "--verify", action="store_true", help="적용 전 pytest 결과 비교, 새로 실패하면 중단"
    )
    p.add_argument(
        "--allow-lazy",
        action="store_true",
        help="순환을 함수 안 지연 임포트로 끊는 것 허용(임시 처방, arch fix 와 동일 의미)",
    )
    p.add_argument("--no-import-check", action="store_true", help="변경 모듈 임포트 확인 생략")
    p.add_argument("--force", action="store_true", help="커밋 안 된 파일이어도 적용")
    p.add_argument("--quiet", action="store_true", help="미리보기 diff 출력 생략")
    p.set_defaults(func=cmd_tangle)

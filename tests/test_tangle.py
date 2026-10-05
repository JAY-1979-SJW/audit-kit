"""importgraph 의 결합도 지표(Ca/Ce/Instability)·순환 간선 추천·구조 기반 계층 추정 시험.

지표 정의 출처(2026-09-27 확인): en.wikipedia.org/wiki/Software_package_metrics,
pdepend.org 공식 문서(두 출처 수치 일치). 순환 간선 추천은 grimp 공식 문서가 쓴다고 밝힌
"Eades, Lin and Smyth 의 그리디 휴리스틱"을 새 의존성 없이 직접 구현한 것.
tests/test_audit_kit.py 의 write()/cfg_for() 헬퍼를 그대로 쓴다(순환 픽스처 패턴도 같은 파일 참고).
"""

from __future__ import annotations

import shutil

from audit_kit.arch import tangle
from audit_kit.importgraph import ImportGraph

from test_arch import FIXTURE, _run
from test_audit_kit import cfg_for, write


def build(tmp_path, files: dict) -> ImportGraph:
    write(tmp_path, files)
    return ImportGraph.build(cfg_for(tmp_path).package_paths())


# ------------------------------------------------------------ coupling_metrics
def test_coupling_metrics_ca_ce_instability(tmp_path):
    g = build(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/core.py": "",  # 아무것도 임포트 안 함
            "pkg/util.py": "from pkg import core\n",
            "pkg/api.py": "from pkg import core\nfrom pkg import util\n",
        },
    )
    m = g.coupling_metrics()
    assert m["pkg.core"] == {"ca": 2, "ce": 0, "instability": 0.0}  # util·api 가 참조
    assert m["pkg.api"] == {"ca": 0, "ce": 2, "instability": 1.0}
    assert m["pkg.util"] == {"ca": 1, "ce": 1, "instability": 0.5}


def test_coupling_metrics_isolated_module_is_zero(tmp_path):
    g = build(tmp_path, {"pkg/__init__.py": "", "pkg/lonely.py": ""})
    assert g.coupling_metrics()["pkg.lonely"] == {"ca": 0, "ce": 0, "instability": 0.0}


# ------------------------------------------------------------ nominate_cycle_breakers 관련
def test_cycle_breaker_actually_breaks_the_cycle(tmp_path):
    g = build(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "from pkg import b\n",
            "pkg/b.py": "from pkg import c\n",
            "pkg/c.py": "from pkg import a\n",
        },
    )
    breakers = {(m, t) for m, t, _ in g.nominate_cycle_breakers()}
    assert breakers  # 순환이 있으니 적어도 하나는 골라야 한다
    trimmed = ImportGraph()
    trimmed.modules = g.modules
    trimmed.edges = {
        m: {t: ln for t, ln in targets.items() if (m, t) not in breakers}
        for m, targets in g.edges.items()
    }
    assert trimmed.cycles() == []  # 추천된 간선을 지우면 실제로 순환이 없어져야 한다


def test_cycle_breaker_returns_real_line_numbers(tmp_path):
    g = build(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "x = 1\nfrom pkg import b\n",
            "pkg/b.py": "from pkg import a\n",
        },
    )
    breakers = g.nominate_cycle_breakers()
    for m, t, line in breakers:
        assert g.edges[m][t] == line


def test_no_cycle_no_breakers(tmp_path):
    g = build(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "from pkg import b\n", "pkg/b.py": ""})
    assert g.nominate_cycle_breakers() == []


# ------------------------------------------------------------ topological_layers
def test_topological_layers_orders_by_real_dependency_not_name(tmp_path):
    """폴더·파일 이름은 계층과 무관하게 지었다(z_ 접두어가 실제로는 가장 아래) — 이름이 아니라
    실제 임포트 방향만 본다는 것을 확인하기 위함(arch/spec.py 의 이름 매칭 추정과 대비되는 지점)."""
    g = build(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/z_top.py": "from pkg import a_mid\n",
            "pkg/a_mid.py": "from pkg import zz_bottom\n",
            "pkg/zz_bottom.py": "",
        },
    )
    layers = g.topological_layers()
    # "pkg"(빈 __init__.py)도 내부 의존이 없는 모듈이라 zz_bottom 과 같은 0번째 계층에 낀다.
    assert layers == [["pkg", "pkg.zz_bottom"], ["pkg.a_mid"], ["pkg.z_top"]]


def test_topological_layers_groups_cycle_together(tmp_path):
    g = build(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "from pkg import b\n",
            "pkg/b.py": "from pkg import a\n",
            "pkg/c.py": "from pkg import a\n",
        },
    )
    layers = g.topological_layers()
    # "pkg"(빈 __init__.py)도 내부 의존이 없어 순환(a,b)과 같은 0번째 계층에 낀다.
    assert layers[0] == ["pkg", "pkg.a", "pkg.b"]  # 순환에 낀 것들은 같은 계층(응축 후 한 노드)
    assert layers[1] == ["pkg.c"]


def test_topological_layers_covers_every_module_exactly_once(tmp_path):
    g = build(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "from pkg import b\n",
            "pkg/b.py": "from pkg import c\n",
            "pkg/c.py": "",
            "pkg/isolated.py": "",
        },
    )
    layers = g.topological_layers()
    seen = [m for layer in layers for m in layer]
    assert sorted(seen) == sorted(g.modules)
    assert len(seen) == len(set(seen))


# ------------------------------------------------------------ arch/tangle.py: 진단(리포트)
def cfg_at(tmp_path, files: dict):
    write(tmp_path, files)
    return cfg_for(tmp_path)


def test_diagnose_reports_no_cycle_cleanly(tmp_path):
    cfg = cfg_at(
        tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "from pkg import b\n", "pkg/b.py": ""}
    )
    report = tangle.diagnose(cfg)
    assert report.cycles == []
    assert report.breakers == []
    assert "순환 임포트 없음" in tangle.render_report(report)


def test_diagnose_flags_unfixable_submodule_cycle(tmp_path):
    """`from pkg import 다른서브모듈`(이름 없는 서브모듈 임포트) 끼리의 순환은 이름이 없어
    reexport/type_only/move/lazy 어느 것도 시도할 수 없다 — 실측으로 확인한 진짜 한계."""
    cfg = cfg_at(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "from pkg import b\n",
            "pkg/b.py": "from pkg import a\n",
        },
    )
    report = tangle.diagnose(cfg)
    assert report.cycles and report.fixable_cycles == []
    assert "자동 복구 불가" in tangle.render_report(report)


def test_diagnose_marks_named_symbol_cycle_as_fixable(tmp_path):
    cfg = cfg_at(
        tmp_path,
        {
            "pkg/services/__init__.py": "",
            "pkg/services/x.py": "from pkg.services.y import yfunc\n\n\ndef xfunc():\n    return yfunc()\n",
            "pkg/services/y.py": (
                "from pkg.services.x import xfunc\n\n\n"
                "def yfunc():\n    return 1\n\n\ndef ycall():\n    return xfunc()\n"
            ),
        },
    )
    report = tangle.diagnose(cfg)
    assert report.fixable_cycles == report.cycles
    assert "자동 복구 불가" not in tangle.render_report(report)


# ------------------------------------------------------------ arch/tangle.py: 자동 복구
def test_fix_resolves_named_symbol_cycle_without_architecture_toml(tmp_path):
    """실측(2026-09-27): 계층 없는 ArchSpec 으로도 기존 test_arch.py 의 move 픽스처가 그대로
    통과함을 확인했다 — architecture.toml 없이 arch/fix.py 의 안전장치를 그대로 재사용."""
    cfg = cfg_at(
        tmp_path,
        {
            "pkg/services/__init__.py": "",
            "pkg/services/x.py": "from pkg.services.y import yfunc\n\n\ndef xfunc():\n    return yfunc()\n",
            "pkg/services/y.py": (
                "from pkg.services.x import xfunc\n\n\n"
                "def yfunc():\n    return 1\n\n\ndef ycall():\n    return xfunc()\n"
            ),
        },
    )
    res = tangle.fix(cfg)
    try:
        assert [a.kind for a in res.applied] == ["move"]
        assert all(v.rule != "ARCH-CYCLE" for v in res.remaining)
    finally:
        res.workspace.cleanup()


# ------------------------------------------------------------ CLI e2e (`audit-kit tangle`)
def test_cli_tangle_fix_apply_and_undo(tmp_path):
    """`tangle --fix --apply`가 실제 CLI(서브프로세스)로 반영·되돌리기까지 되는지 확인.
    test_arch.py 의 `arch fix --apply` 대상과 같은 sample_proj 픽스처(models<->services 실제
    순환 임포트, architecture.toml 없이도 존재)를 그대로 써서 `_apply_or_preview` 공용 헬퍼가
    tangle.py 쪽 호출에서도 실제로 실행됨을 증명한다(2026-09-27 실측: preview/apply/undo 모두
    수동 실행으로 먼저 확인 후 이 테스트로 고정)."""
    root = tmp_path / "sample"
    shutil.copytree(
        FIXTURE, root, ignore=shutil.ignore_patterns("__pycache__", "audit-reports", ".claude")
    )
    original = {p: p.read_text(encoding="utf-8") for p in root.rglob("*.py")}

    p = _run(["tangle"], root)  # architecture.toml 없이 진단만
    assert p.returncode == 1 and "순환 임포트 1개" in p.stdout

    p = _run(["tangle", "--fix"], root)  # 미리보기 — 원본 안 바뀜
    assert p.returncode == 0 and "미리보기" in p.stdout
    assert all(f.read_text(encoding="utf-8") == t for f, t in original.items())

    p = _run(["tangle", "--fix", "--apply", "--verify", "--force"], root)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "적용 완료" in p.stdout
    assert _run(["tangle"], root).returncode == 0  # 이제 순환 없음
    assert "def unit_price" in (root / "app/models/wall.py").read_text(encoding="utf-8")

    assert _run(["arch", "undo"], root).returncode == 0  # tangle 도 같은 undo 표식을 씀
    assert all(f.read_text(encoding="utf-8") == t for f, t in original.items())


def test_fix_leaves_unfixable_submodule_cycle_alone(tmp_path):
    cfg = cfg_at(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "from pkg import b\n",
            "pkg/b.py": "from pkg import a\n",
        },
    )
    res = tangle.fix(cfg, allow_lazy=True)
    try:
        assert any(v.rule == "ARCH-CYCLE" for v in res.remaining)
        assert not res.applied
    finally:
        res.workspace.cleanup()

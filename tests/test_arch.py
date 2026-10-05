import ast
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

from audit_kit.arch.fix import run_fix
from audit_kit.arch.scan import scan
from audit_kit.arch.spec import infer_spec, load_spec, render_spec
from audit_kit.config import AuditConfig
from audit_kit.importgraph import ImportGraph

FIXTURE = Path(__file__).parent / "fixtures" / "sample_proj"

SPEC = """
[project]
root_packages = ["pkg"]
entrypoints = ["pkg.main"]

[rules]
allow_skip_layers = true
ignore_type_checking = true
lazy_imports = "violation"
cycles = "forbid"
unassigned = "warn"

[[layers]]
name = "interface"
modules = ["pkg.api"]

[[layers]]
name = "service"
modules = ["pkg.services"]

[[layers]]
name = "domain"
modules = ["pkg.models"]

[[layers]]
name = "infra"
modules = ["pkg.core"]

[[external]]
package = "fastapi"
allowed_in = ["interface", "entrypoints"]
"""


def write(root: Path, files: dict):
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")


def make(tmp_path, files: dict, spec: str = SPEC):
    base = {
        "pkg/__init__.py": "",
        "pkg/api/__init__.py": "",
        "pkg/services/__init__.py": "",
        "pkg/models/__init__.py": "",
        "pkg/core/__init__.py": "",
        "architecture.toml": spec,
    }
    base.update(files)
    write(tmp_path, base)
    cfg = AuditConfig(packages=["pkg"])
    cfg.root = tmp_path
    return cfg, load_spec(tmp_path)


def rules(vs):
    return sorted(v.rule for v in vs)


# ---------------------------------------------------------------- scan
def test_scan_rules(tmp_path):
    cfg, spec = make(
        tmp_path,
        {
            "pkg/main.py": "import fastapi\nfrom pkg.api import r\n",
            "pkg/api/r.py": "import fastapi\nfrom pkg.core import db\n",
            "pkg/services/s.py": "import fastapi\nfrom pkg import main\n",
            "pkg/models/m.py": """
            from typing import TYPE_CHECKING
            if TYPE_CHECKING:
                from pkg.services.s import X
            def f():
                from pkg.api.r import y
        """,
            "pkg/core/db.py": "",
            "pkg/misc/__init__.py": "",
        },
    )
    vs = scan(cfg, spec)
    got = {(v.rule, v.src) for v in vs}
    assert ("ARCH-EXTERNAL", "pkg.services.s") in got  # fastapi 는 interface 만
    assert ("ARCH-EXTERNAL", "pkg.api.r") not in got and ("ARCH-EXTERNAL", "pkg.main") not in got
    assert ("ARCH-ENTRY", "pkg.services.s") in got
    assert ("ARCH-LAYER", "pkg.models.m") in got  # 지연 임포트도 위반
    assert not any(v.context == "type_checking" for v in vs)  # TYPE_CHECKING 은 제외
    assert ("ARCH-UNASSIGNED", "pkg.misc") in got
    assert not any(r == "ARCH-LAYER" and s == "pkg.api.r" for r, s in got)  # 건너뛰기 허용


def test_spec_problems(tmp_path):
    cfg, spec = make(
        tmp_path, {}, SPEC.replace('modules = ["pkg.core"]', 'modules = ["pkg.nothere"]')
    )
    assert "ARCH-SPEC" in rules(scan(cfg, spec))


def test_infer_and_render_roundtrip(tmp_path):
    cfg, _ = make(
        tmp_path,
        {
            "pkg/main.py": "",
            "pkg/api/r.py": "import fastapi\n",
            "pkg/dxf/__init__.py": "",
            "pkg/weird/__init__.py": "",
        },
    )
    graph = ImportGraph.build(cfg.package_paths())
    spec, unassigned = infer_spec(graph, ["pkg"])
    assert [lay.name for lay in spec.layers] == ["interface", "service", "domain", "infra"]
    assert "pkg.dxf" in spec.layers[2].modules
    assert spec.entrypoints == ["pkg.main"]
    assert unassigned == ["pkg.weird"]
    (tmp_path / "architecture.toml").write_text(render_spec(spec, unassigned), encoding="utf-8")
    again = load_spec(tmp_path)
    assert [lay.modules for lay in again.layers] == [lay.modules for lay in spec.layers]
    assert again.external[0].package == "fastapi"


# ---------------------------------------------------------------- fix
FIX_FILES = {
    "pkg/core/util.py": "HELPER = 3\n",
    "pkg/services/s.py": """
        import math

        from pkg.core.util import HELPER


        class Svc:
            pass


        class Other:
            pass


        def calc(x):
            # 면적 계산
            return math.sqrt(x) * HELPER


        def _private():
            return 1


        def svc_run():
            return _private()
    """,
    "pkg/models/a.py": """
        from pkg.services.s import HELPER

        VALUE = HELPER + 1
    """,
    "pkg/models/b.py": """
        from pkg.services.s import Svc


        def use(x: Svc) -> list[Svc]:
            return [x]
    """,
    "pkg/models/c.py": """
        from pkg.services.s import Other

        Z = 1
    """,
    "pkg/models/d.py": """
        from pkg.services.s import calc


        def area():
            return calc(4)
    """,
    "pkg/models/e.py": """
        from pkg.services.s import svc_run

        R = svc_run
    """,
    "pkg/api/r.py": """
        from pkg.services.s import calc, svc_run


        def handler():
            return calc(1), svc_run()
    """,
}


def test_fix_strategies(tmp_path):
    cfg, spec = make(tmp_path, FIX_FILES)
    before = scan(cfg, spec)
    assert rules(before).count("ARCH-LAYER") == 5
    res = run_fix(cfg, spec)
    try:
        kinds = sorted(a.kind for a in res.applied)
        assert kinds == ["move", "reexport", "type_only", "unused"], kinds
        remaining = {(v.rule, v.src) for v in res.remaining}
        assert remaining == {("ARCH-LAYER", "pkg.models.e")}  # _private 의존으로 이동 불가
        reasons = " ".join(r for rs in res.unresolved.values() for r in rs)
        assert "_private" in reasons

        ws = res.workspace
        a = ws.read("pkg/models/a.py")
        assert "from pkg.core.util import HELPER" in a and "services" not in a
        b = ws.read("pkg/models/b.py")
        assert "if TYPE_CHECKING:\n    from pkg.services.s import Svc" in b
        assert 'def use(x: "Svc") -> "list[Svc]":' in b
        assert "Other" not in ws.read("pkg/models/c.py")
        d = ws.read("pkg/models/d.py")
        assert "import math" in d and "from pkg.core.util import HELPER" in d and "# 면적 계산" in d
        assert "def calc(x):" in d and "pkg.services" not in d
        s = ws.read("pkg/services/s.py")
        assert "def calc" not in s
        r = ws.read("pkg/api/r.py")
        assert "from pkg.services.s import svc_run" in r and "from pkg.models.d import calc" in r
        for rel in ws.changed():
            ast.parse(ws.read(rel))
        # 원본은 그대로
        assert "def calc" in (tmp_path / "pkg/services/s.py").read_text(encoding="utf-8")
    finally:
        res.workspace.cleanup()


def test_fix_keeps_reexport_when_target_still_uses(tmp_path):
    cfg, spec = make(
        tmp_path,
        {
            "pkg/services/s.py": """
            def price():
                return 2


            def total(n):
                return n * price()
        """,
            "pkg/models/m.py": "from pkg.services.s import price\n\nP = price()\n",
        },
    )
    res = run_fix(cfg, spec)
    try:
        s = res.workspace.read("pkg/services/s.py")
        assert "from pkg.models.m import price" in s and "def price" not in s
        assert not res.remaining
    finally:
        res.workspace.cleanup()


def test_cycle_move_then_lazy_only_when_allowed(tmp_path):
    files = {
        "pkg/services/x.py": """
            from pkg.services.y import yfunc


            def xfunc():
                return yfunc()
        """,
        "pkg/services/y.py": """
            from pkg.services.x import xfunc


            def yfunc():
                return 1


            def ycall():
                return xfunc()
        """,
    }
    cfg, spec = make(tmp_path, files)
    res = run_fix(cfg, spec)
    try:
        # yfunc 는 의존이 없어 x 로 이동 가능 → 순환 해소 (move 가 lazy 보다 우선)
        assert not res.remaining
        assert [a.kind for a in res.applied] == ["move"]
    finally:
        res.workspace.cleanup()

    # yfunc 가 y 내부 함수에 의존 → 이동 불가 → lazy 는 명시적으로 허용할 때만
    files["pkg/services/y.py"] = """
        from pkg.services.x import xfunc


        def _helper():
            return 1


        def yfunc():
            return _helper()


        def ycall():
            return xfunc()
    """
    cfg, spec = make(tmp_path, files)
    res = run_fix(cfg, spec)
    try:
        assert [v.rule for v in res.remaining] == ["ARCH-CYCLE"]
    finally:
        res.workspace.cleanup()
    res = run_fix(cfg, spec, allow_lazy=True)
    try:
        assert not res.remaining
        assert res.applied[0].kind == "lazy"
        assert "# audit-kit: 지연 임포트" in "".join(
            res.workspace.read(r) for r in res.workspace.changed()
        )
    finally:
        res.workspace.cleanup()


def test_move_blocked_by_module_state(tmp_path):
    cfg, spec = make(
        tmp_path,
        {
            "pkg/services/s.py": """
            REG = []


            def register(f):
                REG.append(f)
                return f
        """,
            "pkg/models/m.py": """
            from pkg.services.s import register


            @register
            def hook():
                return 1
        """,
        },
    )
    res = run_fix(cfg, spec)
    try:
        # register 는 REG(같은 모듈 상수)에 의존 → 이동 불가로 남아야 함
        assert [v.rule for v in res.remaining] == ["ARCH-LAYER"]
        assert not res.workspace.changed()
    finally:
        res.workspace.cleanup()


# ---------------------------------------------------------------- CLI e2e
def _run(args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "audit_kit", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_cli_init_fix_apply_undo(tmp_path):
    root = tmp_path / "sample"
    shutil.copytree(
        FIXTURE, root, ignore=shutil.ignore_patterns("__pycache__", "audit-reports", ".claude")
    )
    original = {p: p.read_text(encoding="utf-8") for p in root.rglob("*.py")}

    assert _run(["arch", "init"], root).returncode == 0
    assert (root / "architecture.toml").is_file() and (root / "ARCHITECTURE.md").is_file()
    assert "```mermaid" in (root / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert _run(["arch", "check"], root).returncode == 1

    p = _run(["arch", "fix"], root)  # 미리보기
    assert p.returncode == 0 and "미리보기" in p.stdout
    assert all(f.read_text(encoding="utf-8") == t for f, t in original.items())

    p = _run(["arch", "fix", "--apply", "--verify", "--force"], root)
    assert p.returncode == 0, p.stdout + p.stderr
    assert _run(["arch", "check"], root).returncode == 0
    assert "def unit_price" in (root / "app/models/wall.py").read_text(encoding="utf-8")

    assert _run(["arch", "undo"], root).returncode == 0
    assert all(f.read_text(encoding="utf-8") == t for f, t in original.items())

    exp = _run(["arch", "export-importlinter"], root)
    assert '"app.api",' in exp.stdout and 'type = "layers"' in exp.stdout


def test_audit_and_hook_use_spec(tmp_path):
    root = tmp_path / "sample"
    shutil.copytree(
        FIXTURE, root, ignore=shutil.ignore_patterns("__pycache__", "audit-reports", ".claude")
    )
    assert _run(["arch", "init"], root).returncode == 0

    p = _run(["run", "--no-tests", "--fail-on", "never", "--solo"], root)
    assert "arch(설계)" in p.stdout
    latest = (root / "audit-reports/LATEST.txt").read_text(encoding="utf-8")
    data = json.loads(
        (root / "audit-reports" / latest / "findings.json").read_text(encoding="utf-8")
    )
    rules_found = [f["rule"] for f in data["findings"]]
    assert "ARCH-LAYER" in rules_found and "ARCH-CYCLE" in rules_found
    assert "IMPORT-CYCLE" not in rules_found  # 설계 검사와 중복 보고 안 함

    stdin = json.dumps({"tool_input": {"file_path": str(root / "app/models/wall.py")}})
    h = subprocess.run(
        [sys.executable, "-m", "audit_kit", "hook"],
        cwd=root,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert h.returncode == 2 and "[설계 ARCH-LAYER]" in h.stderr

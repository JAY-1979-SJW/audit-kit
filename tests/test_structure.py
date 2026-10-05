"""1단계(설계 구조): 공식 문서 근거 구조 규칙 검사와 자동 수정."""

import subprocess
import sys
from pathlib import Path

import pytest

from audit_kit.config import AuditConfig
from audit_kit.models import CRITICAL
from audit_kit.pyproject_edit import add_dependency, replace_dependency
from audit_kit.scope import _CACHE
from audit_kit.structfix import run_struct_fix
from audit_kit.structure import check, find_interpreter

PYPROJECT = """\
[project]
name = "demo"
requires-python = ">=3.11"
dependencies = [
    "openpyxl>=3.1",  # 엑셀
    "radon>=5.0"
]

[tool.audit-kit]
packages = ["app"]
"""

FILES = {
    "app/__init__.py": "",
    "app/core/__init__.py": "",
    "app/core/util.py": "def helper():\n    return 1\n",
    # sys.path 조작 + 내부 폴더를 최상위 이름으로 임포트
    "app/core/loader.py": ("import sys\nfrom pathlib import Path\n"
                           "sys.path.insert(0, str(Path(__file__).parent))\n"
                           "from util import helper as h\n\n\ndef run():\n    return h()\n"),
    # __init__.py 없는 하위 폴더 (namespace 로 동작 중)
    "app/services/calc.py": "import radon\nimport vulture\nfrom app.core.util import helper\n\nprint('loaded')\n",
    "app/entry.py": "from app.services.calc import helper\n",
    "scripts/tool.py": "import bandit\nfrom app.services import calc\n",
    "scripts/types.py": "X = 1\n",
}


def make(root: Path, extra=None):
    for rel, body in {**FILES, **(extra or {})}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    (root / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    _CACHE.clear()
    cfg = AuditConfig(packages=["app"], max_module_lines=5)
    cfg.root = root
    return cfg


def by_rule(issues):
    out = {}
    for i in issues:
        out.setdefault(i.rule, []).append(i)
    return out


def test_detects_structure_rules(tmp_path):
    issues = by_rule(check(make(tmp_path)))
    assert any(i.file == "app/services/" and i.fixable for i in issues["STRUCT-NO-INIT"])
    top = issues["STRUCT-TOPLEVEL-IMPORT"]
    assert top[0].data["new"] == "app.core.util" and top[0].fixable
    missing = {i.message.split("'")[1]: i for i in issues["STRUCT-DEP-MISSING"]}
    assert missing["vulture"].data["group"] == "dependencies"  # 제품 코드가 씀
    assert missing["bandit"].data["group"] == "dev"  # 스크립트만 씀
    assert "radon" not in missing and "openpyxl" not in missing  # 선언됨
    assert any("app.core.loader" in i.message for i in issues["STRUCT-SYSPATH"])
    assert any("print('loaded')" in i.message for i in issues["STRUCT-SIDE-EFFECT"])
    assert any(i.file == "scripts/types.py" for i in issues["STRUCT-SHADOW"])  # 패키지 밖 types.py
    assert issues["STRUCT-DEP-UNBOUNDED"][0].data["old"] == "radon>=5.0"  # 설치된 6.x
    for rule, items in issues.items():
        assert items[0].ref, rule  # 모든 규칙에 근거가 있다


def test_python_version_check_uses_real_interpreter(tmp_path):
    if not find_interpreter((3, 11)):
        pytest.skip("Python 3.11 없음")
    issues = by_rule(check(make(tmp_path, {"app/core/fs.py": 'x = "a"\ny = f"{x.strip(\'\\\\\')}"\n'}), only={"pyver"}))
    [i] = issues["STRUCT-PYVER"]
    assert i.file == "app/core/fs.py" and i.severity == CRITICAL and "3.11" in i.hint


def test_struct_fix_end_to_end(tmp_path):
    cfg = make(tmp_path)
    res = run_struct_fix(cfg, pin_major=True)
    try:
        ws = res.workspace
        kinds = [r for r, _ in res.applied]
        assert "STRUCT-NO-INIT" in kinds and "STRUCT-TOPLEVEL-IMPORT" in kinds and "STRUCT-DEP-MISSING" in kinds
        assert (ws.tmp / "app/services/__init__.py").exists()
        assert "from app.core.util import helper as h" in ws.read("app/core/loader.py")
        pp = ws.read("pyproject.toml")
        assert '"openpyxl>=3.1",  # 엑셀' in pp  # 기존 주석 유지
        assert "vulture>=" in pp and "radon>=5.0,<7" in pp
        assert "[project.optional-dependencies]" in pp and "bandit>=" in pp
        assert res.after["STRUCT-NO-INIT"] < res.before["STRUCT-NO-INIT"]
        assert all(res.after.get(r, 0) <= n for r, n in res.before.items())
    finally:
        res.workspace.cleanup()


def test_struct_fix_apply_and_undo_removes_created_files(tmp_path):
    make(tmp_path)
    before = {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    def ak(*args):
        return subprocess.run([sys.executable, "-m", "audit_kit", "struct", *args], cwd=tmp_path,
                              capture_output=True, text=True, encoding="utf-8")

    p = ak("fix", "--apply", "--force")
    assert p.returncode == 0, p.stdout + p.stderr
    assert (tmp_path / "app/services/__init__.py").exists()
    assert ak("undo").returncode == 0
    assert not (tmp_path / "app/services/__init__.py").exists()  # 새로 만든 파일은 삭제
    after = {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
             and "audit-reports" not in p.as_posix() and "__pycache__" not in p.as_posix()}
    assert after == before


@pytest.mark.parametrize("src,group", [
    ('[project]\nname = "x"\ndependencies = ["a>=1"]\n', "dependencies"),
    ('[project]\nname = "x"\ndependencies = []\n', "dependencies"),
    ('[project]\nname = "x"\n\n[tool.x]\ny = 1\n', "dependencies"),
    ('[project]\nname = "x"\ndependencies = [\n  "a>=1"  # 주석\n]\n', "dependencies"),
    ('[project]\nname = "x"\n', "dev"),
    ('[project]\nname = "x"\n\n[project.optional-dependencies]\ntest = ["pytest"]\n', "dev"),
])
def test_add_dependency_keeps_toml_valid(src, group):
    out = add_dependency(src, "newlib>=2.0,<3", group)  # 내부에서 tomllib 로 검증
    assert "newlib>=2.0,<3" in out
    if "# 주석" in src:
        assert "# 주석" in out


def test_replace_dependency():
    out = replace_dependency('[project]\nname="x"\ndependencies = ["mcp>=1.20"]\n', "mcp>=1.20", "mcp>=1.20,<2")
    assert '"mcp>=1.20,<2"' in out

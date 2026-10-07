"""2026-10-07/08 실측 교훈 조항(STD-13~16, ERR-10~15, FE-10/11, OPS-18)과 그 직접 구현 검사."""

from __future__ import annotations

import ast
from pathlib import Path

from audit_kit.config import load_config
from audit_kit.runner import Proc
from audit_kit.std import checks, run
from audit_kit.std.rules import bundled, load_rules, ruff_rule_for, rule_by_id

NEW_RULES = {
    "STD-13": "ruff",
    "STD-14": "custom",
    "STD-15": "custom",
    "STD-16": "custom",
    "ERR-10": "custom",
    "ERR-11": "manual",
    "ERR-12": "custom",
    "ERR-13": "custom",
    "ERR-14": "manual",
    "ERR-15": "manual",
    "FE-10": "manual",
    "FE-11": "manual",
    "OPS-18": "manual",
}


def hits(check, src: str, rel: str = "app/mod.py") -> list:
    return check(ast.parse(src), rel)


def write(root: Path, files: dict) -> None:
    for rel, text in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- 원장
def test_new_rules_have_kind_and_source():
    rules = load_rules()
    for rid, kind in NEW_RULES.items():
        rule = rule_by_id(rules, rid)
        assert rule is not None, rid
        assert rule.kind == kind, rid
        assert rule.source.startswith("https://"), rid


def test_a005_enabled_and_mapped():
    text = bundled("ruff.toml").read_text(encoding="utf-8")
    assert '"A005"' in text
    assert "strict-checking = true" in text
    assert ruff_rule_for(load_rules(), "A005").id == "STD-13"


# ---------------------------------------------------------------- STD-14
def test_stdout_only_reconfigure_is_flagged():
    src = 'import sys\nsys.stdout.reconfigure(encoding="utf-8")\nprint("한글")\n'
    found = hits(checks.check_stdio_reconfigure_partial, src)
    assert [h.check for h in found] == ["STDIO-RECONFIGURE-PARTIAL"]
    assert "stderr" in found[0].message


def test_stderr_only_reconfigure_is_flagged():
    src = 'import sys\nsys.stderr.reconfigure(encoding="utf-8")\n'
    assert len(hits(checks.check_stdio_reconfigure_partial, src)) == 1


def test_both_streams_reconfigured_is_clean():
    src = (
        "import sys\n"
        'sys.stdout.reconfigure(encoding="utf-8")\n'
        'sys.stderr.reconfigure(encoding="utf-8")\n'
    )
    assert hits(checks.check_stdio_reconfigure_partial, src) == []


def test_loop_reconfigure_is_clean():
    src = 'import sys\nfor s in (sys.stdout, sys.stderr):\n    s.reconfigure(encoding="utf-8")\n'
    assert hits(checks.check_stdio_reconfigure_partial, src) == []


# ---------------------------------------------------------------- ERR-10
GUARDED_GATE = (
    "import subprocess\n"
    "def main(p):\n"
    "    if p.exists():\n"
    "        subprocess.run(['python', str(p)], check=True)\n"
    "    return 0\n"
)


def test_gate_run_only_if_target_exists_is_flagged():
    found = hits(checks.check_silent_gate_pass, GUARDED_GATE, "scripts/quality_gate.py")
    assert [h.line for h in found] == [3]


def test_gate_with_else_failure_is_clean():
    src = GUARDED_GATE.replace("    return 0\n", "    else:\n        return 1\n    return 0\n")
    assert hits(checks.check_silent_gate_pass, src, "scripts/quality_gate.py") == []


def test_not_exists_early_failure_is_clean():
    src = (
        "import subprocess\n"
        "def main(p):\n"
        "    if not p.exists():\n"
        "        subprocess.run(['echo', 'missing'])\n"
        "        return 1\n"
    )
    assert hits(checks.check_silent_gate_pass, src, "scripts/check_x.py") == []


def test_except_returning_empty_around_subprocess_is_flagged():
    src = (
        "import subprocess\n"
        "def findings():\n"
        "    try:\n"
        "        out = subprocess.run(['ruff', 'check'], capture_output=True)\n"
        "    except FileNotFoundError:\n"
        "        return []\n"
        "    return out\n"
    )
    found = hits(checks.check_silent_gate_pass, src, "tools/lint_check.py")
    assert [h.check for h in found] == ["SILENT-GATE-PASS"]


def test_except_that_raises_is_clean():
    src = (
        "import subprocess\n"
        "def findings():\n"
        "    try:\n"
        "        return subprocess.run(['ruff'], capture_output=True)\n"
        "    except FileNotFoundError as exc:\n"
        "        raise SystemExit(2) from exc\n"
    )
    assert hits(checks.check_silent_gate_pass, src, "tools/lint_check.py") == []


def test_except_returning_failure_code_is_clean():
    src = (
        "import subprocess\n"
        "def main():\n"
        "    try:\n"
        "        subprocess.run(['ruff'], check=True)\n"
        "    except FileNotFoundError:\n"
        "        return 1\n"
        "    return 0\n"
    )
    assert hits(checks.check_silent_gate_pass, src, "scripts/gate.py") == []


def test_empty_stdout_defaulting_to_empty_list_is_flagged():
    src = "import json\ndef parse(p):\n    return json.loads(p.stdout or '[]')\n"
    assert len(hits(checks.check_silent_gate_pass, src, "ci/gate.py")) == 1


def test_silent_gate_only_applies_to_gate_or_check_files():
    assert hits(checks.check_silent_gate_pass, GUARDED_GATE, "app/service.py") == []
    assert hits(checks.check_silent_gate_pass, GUARDED_GATE, "tests/test_gate.py") == []


def test_std_ruff_abnormal_exit_is_error_not_ok(tmp_path, monkeypatch):
    """ERR-10 을 audit-kit 자신에게 적용: ruff 가 비정상 종료(2)하거나 출력이 비면 '0건 통과'가 아니다."""
    write(tmp_path, {"pyproject.toml": '[project]\nname = "s"\nversion = "0"\n', "app/a.py": "X = 1\n"})
    cfg = load_config(tmp_path)
    rules = load_rules()
    files = ["app/a.py"]
    for proc in (Proc(2, "", "config error"), Proc(0, "", ""), Proc(-1, "[", "timeout")):
        monkeypatch.setattr(run, "run_module", lambda *_a, _p=proc, **_k: _p)
        res = run.run_std_ruff(cfg, rules, files, bundled("ruff.toml"))
        assert res.status == "error", proc


# ---------------------------------------------------------------- ERR-12
def test_pyinstaller_entry_relative_import_is_flagged(tmp_path):
    write(
        tmp_path,
        {
            "app.spec": "a = Analysis(['src/myapp/main.py'], hiddenimports=[])\n",
            "src/myapp/__init__.py": "",
            "src/myapp/main.py": "from .core import run\nrun()\n",
        },
    )
    found = checks.check_pyinstaller_entry_relative_import(tmp_path)
    assert [(h.check, h.file, h.line) for h in found] == [
        ("PYINSTALLER-ENTRY-RELATIVE-IMPORT", "src/myapp/main.py", 1)
    ]


def test_pyinstaller_absolute_entry_is_clean(tmp_path):
    write(
        tmp_path,
        {
            "app.spec": "a = Analysis(scripts=['run_app.py'])\n",
            "run_app.py": "from myapp.main import run\nrun()\n",
        },
    )
    assert checks.check_pyinstaller_entry_relative_import(tmp_path) == []


def test_pyinstaller_hand_listed_own_hiddenimports_is_review(tmp_path):
    write(
        tmp_path,
        {
            "app.spec": (
                "a = Analysis(['run_app.py'],\n"
                "    hiddenimports=['myapp.a', 'myapp.b', 'requests'])\n"
            ),
            "run_app.py": "import myapp\n",
            "myapp/__init__.py": "",
        },
    )
    found = checks.check_pyinstaller_entry_relative_import(tmp_path)
    assert len(found) == 1
    assert found[0].severity == "review"
    assert "collect_submodules" in found[0].message


def test_pyinstaller_specs_in_build_dirs_are_ignored(tmp_path):
    write(
        tmp_path,
        {
            "build/old.spec": "a = Analysis(['m.py'])\n",
            "build/m.py": "from . import x\n",
        },
    )
    assert checks.check_pyinstaller_entry_relative_import(tmp_path) == []


# ---------------------------------------------------------------- ERR-13
def test_excl_lock_catching_only_file_exists_is_flagged():
    src = (
        "import os\n"
        "def acquire(p):\n"
        "    try:\n"
        "        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)\n"
        "    except FileExistsError:\n"
        "        return False\n"
        "    return fd\n"
    )
    found = hits(checks.check_excl_lock_permission_error, src)
    assert [(h.check, h.line) for h in found] == [("EXCL-LOCK-NO-PERMISSION-ERROR", 4)]


def test_excl_lock_also_catching_permission_error_is_clean():
    src = (
        "import os\n"
        "def acquire(p):\n"
        "    try:\n"
        "        return os.open(p, os.O_CREAT | os.O_EXCL)\n"
        "    except (FileExistsError, PermissionError):\n"
        "        return None\n"
    )
    assert hits(checks.check_excl_lock_permission_error, src) == []


def test_open_x_mode_lock_is_checked_too():
    src = (
        "def acquire(p):\n"
        "    try:\n"
        "        return open(p, 'x', encoding='utf-8')\n"
        "    except FileExistsError:\n"
        "        return None\n"
    )
    assert len(hits(checks.check_excl_lock_permission_error, src)) == 1


def test_plain_open_is_not_a_lock():
    src = "try:\n    f = open('a', 'w')\nexcept FileExistsError:\n    pass\n"
    assert hits(checks.check_excl_lock_permission_error, src) == []


# ---------------------------------------------------------------- STD-15
def parsed_of(files: dict) -> dict:
    return {rel: ast.parse(src) for rel, src in files.items()}


ROOT_CALC = "from pathlib import Path\nROOT = Path(__file__).resolve().parents[2]\n"


def test_scattered_parents_root_in_three_files_is_flagged():
    parsed = parsed_of({
        "app/a.py": ROOT_CALC,
        "app/b/c.py": ROOT_CALC,
        "scripts/d.py": "from pathlib import Path\nR = Path(__file__).parent.parent\n",
    })
    found = checks.check_scattered_parents_root(parsed)
    assert sorted(h.file for h in found) == ["app/a.py", "app/b/c.py", "scripts/d.py"]


def test_single_root_helper_and_tests_are_clean():
    parsed = parsed_of({
        "app/paths.py": ROOT_CALC,
        "tests/test_a.py": ROOT_CALC,
        "tests/test_b.py": ROOT_CALC,
        "app/x.py": "from pathlib import Path\nHERE = Path(__file__).parent\n",
    })
    assert checks.check_scattered_parents_root(parsed) == []


# ---------------------------------------------------------------- STD-16
def test_init_reexporting_subpackage_is_flagged():
    parsed = parsed_of({
        "pkg/__init__.py": "from .sub import thing\nfrom .models import Model\n",
        "pkg/sub/__init__.py": "thing = 1\n",
        "pkg/models.py": "class Model: ...\n",
    })
    found = checks.check_init_subpackage_reexport(parsed)
    assert [(h.file, h.line) for h in found] == [("pkg/__init__.py", 1)]


def test_init_from_dot_import_subpackage_is_flagged():
    parsed = parsed_of({
        "pkg/__init__.py": "from . import sub\n",
        "pkg/sub/__init__.py": "",
    })
    assert len(checks.check_init_subpackage_reexport(parsed)) == 1


def test_init_reexporting_sibling_module_is_clean():
    parsed = parsed_of({
        "pkg/__init__.py": "from .models import Model\n__all__ = ['Model']\n",
        "pkg/models.py": "class Model: ...\n",
    })
    assert checks.check_init_subpackage_reexport(parsed) == []


# ---------------------------------------------------------------- 전체 연결
def test_new_custom_checks_are_reported_by_std(tmp_path):
    write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "s"\nversion = "0"\n',
            "app/__init__.py": "from .sub import x\n",
            "app/sub/__init__.py": "x = 1\n",
            "app/out.py": 'import sys\nsys.stdout.reconfigure(encoding="utf-8")\n',
            "app/release_gate.py": GUARDED_GATE,
            "app/lock.py": (
                "import os\n"
                "def acquire(p):\n"
                "    try:\n"
                "        return os.open(p, os.O_CREAT | os.O_EXCL)\n"
                "    except FileExistsError:\n"
                "        return None\n"
            ),
            "app/main.py": "from .sub import x\n",
            "app.spec": "a = Analysis(['app/main.py'])\n",
        },
    )
    cfg = load_config(tmp_path)
    found = {f.rule for r in run.run_std(cfg, with_mypy=False) for f in r.findings}
    assert {"STD-14", "STD-16", "ERR-10", "ERR-12", "ERR-13"} <= found

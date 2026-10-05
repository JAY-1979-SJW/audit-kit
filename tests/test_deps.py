"""audit-kit deps: 코드의 import 를 조사해 의존성 선언(requirements.txt) 초안을 만든다."""

from pathlib import Path

import pytest
from audit_kit import cli, deps
from audit_kit.config import load_config

FAKE_DISTS = {
    "yaml": ["PyYAML"],
    "win32com": ["pywin32"],
    "pythoncom": ["pywin32"],
    "PIL": ["pillow"],
}
FAKE_VERSIONS = {"PyYAML": "6.0.3", "pywin32": "312", "pillow": "12.3.0"}


@pytest.fixture(autouse=True)
def fake_env(monkeypatch):
    monkeypatch.setattr(deps, "distributions_by_import", lambda: FAKE_DISTS)
    monkeypatch.setattr(deps, "dist_version", lambda name: FAKE_VERSIONS.get(name))


def _proj(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return tmp_path


def _scan(root: Path):
    cfg = load_config(root)
    declared = deps.checks.declared_dependencies(
        deps.read_pyproject(cfg.root)
    ) | deps.checks.declared_requirements(cfg.root)
    usage, broken = deps.collect(cfg, deps.project_files(cfg))
    return usage, broken, declared


# ------------------------------------------------------------ 수집·분류
def test_collect_separates_required_optional_stdlib_and_local(tmp_path):
    root = _proj(
        tmp_path,
        {
            "app.py": "import os\nimport yaml\nfrom helper import x\n",
            "helper.py": "x = 1\n",
            "opt.py": "try:\n    import ujson\nexcept ImportError:\n    ujson = None\n",
            "tc.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import numpy\n",
        },
    )
    usage, broken, _ = _scan(root)
    assert broken == []
    assert set(usage) == {"yaml", "ujson", "numpy"}  # os(표준), helper(로컬), typing(표준) 제외
    assert usage["yaml"][0] == {"app.py"} and usage["yaml"][1] == set()  # 필수
    assert (
        usage["ujson"][1] == {"opt.py"} and usage["ujson"][0] == set()
    )  # try/except ImportError = 선택
    assert usage["numpy"][1] == {"tc.py"}  # TYPE_CHECKING = 선택


def test_collect_reports_files_with_syntax_errors_and_skips_report_dir(tmp_path):
    root = _proj(
        tmp_path,
        {
            "ok.py": "import yaml\n",
            "bad.py": "def (:\n",
            "audit-reports/x/gen.py": "import secretlib\n",
            "node_modules/pkg/tool.py": "import hiddenlib\n",
        },
    )
    usage, broken, _ = _scan(root)
    assert broken == ["bad.py"]
    assert set(usage) == {"yaml"}


# ------------------------------------------------------------ 배포 이름 해석
def test_resolve_groups_imports_by_distribution_and_marks_unknown(tmp_path):
    usage = {
        "win32com": ({"a.py"}, set()),
        "pythoncom": (set(), {"b.py"}),
        "PIL": ({"c.py"}, set()),
        "nothere": ({"d.py"}, set()),
    }
    result = {d.key: d for d in deps.resolve(usage, set())}
    assert set(result) == {"pywin32", "pillow", "?nothere"}
    win = result["pywin32"]
    assert win.imports == ["pythoncom", "win32com"] and win.version == "312"
    assert win.required_in == {"a.py"} and win.optional_in == {
        "b.py"
    }  # 한 배포의 두 import 를 합침
    assert result["pillow"].dist == "pillow"  # PIL -> pillow (ERR-01 이 못 하던 구분)
    assert result["?nothere"].dist is None and result["?nothere"].version is None


def test_declared_matches_by_distribution_or_import_name(tmp_path):
    usage = {"PIL": ({"c.py"}, set()), "yaml": ({"a.py"}, set())}
    by_key = {d.key: d for d in deps.resolve(usage, {"pillow"})}
    assert by_key["pillow"].declared is True and by_key["pyyaml"].declared is False
    by_key = {d.key: d for d in deps.resolve({"yaml": ({"a.py"}, set())}, {"yaml"})}
    assert by_key["pyyaml"].declared is True  # import 이름으로만 선언된 경우도 인정


# ------------------------------------------------------------ requirements 초안
def test_build_requirements_never_guesses_versions_and_marks_windows_only():
    usage = {
        "yaml": ({"a.py"}, set()),
        "win32com": (set(), {"b.py"}),
        "nothere": ({"c.py"}, set()),
    }
    text = deps.build_requirements(deps.resolve(usage, set()), today="2026-09-30")
    lines = text.splitlines()
    assert "PyYAML>=6.0.3  # yaml" in lines  # 필수: 설치된 버전을 하한으로
    assert any(
        ln.startswith("# pywin32>=312") and 'sys_platform == "win32"' in ln for ln in lines
    )  # 선택 + Windows 마커
    unknown = [ln for ln in lines if "nothere" in ln]
    assert (
        len(unknown) == 1 and unknown[0].startswith("# ") and ">=" not in unknown[0]
    )  # 버전을 지어내지 않음
    assert "2026-09-30" in text


def test_required_windows_only_dist_gets_marker_without_comment():
    deps_list = deps.resolve({"win32com": ({"a.py"}, set())}, set())
    assert deps.requirement_line(deps_list[0]) == 'pywin32>=312; sys_platform == "win32"'


# ------------------------------------------------------------ 보고·종료코드
def test_report_flags_missing_declarations_and_unused_declared(tmp_path):
    root = _proj(
        tmp_path, {"app.py": "import yaml\nimport PIL\n", "requirements.txt": "pillow>=10\nblack\n"}
    )
    usage, broken, declared = _scan(root)
    report, has_missing = deps.render_report(deps.resolve(usage, declared), declared, broken)
    assert has_missing is True
    assert "선언 누락" in report and "PyYAML" in report
    assert "PyYAML [필수, 선언 없음]" in report and "pillow [필수, 선언됨]" in report
    assert "선언돼 있지만 코드에서 import 가 보이지 않는" in report and "black" in report
    assert (
        "단정" not in report and "도구·간접 의존성일 수 있음" in report
    )  # 미사용이라 단언하지 않음


def test_exit_code_zero_when_everything_is_declared(tmp_path, capsys):
    root = _proj(tmp_path, {"app.py": "import yaml\n", "requirements.txt": "PyYAML>=6\n"})
    assert cli.main(["deps", "--path", str(root)]) == 0
    assert "선언 누락" not in capsys.readouterr().out


def test_declared_in_pyproject_counts(tmp_path, capsys):
    root = _proj(
        tmp_path,
        {
            "app.py": "import yaml\n",
            "pyproject.toml": '[project]\nname = "p"\nversion = "0"\ndependencies = ["PyYAML>=6"]\n',
        },
    )
    assert cli.main(["deps", "--path", str(root)]) == 0


# ------------------------------------------------------------ 미리보기 / --write
def test_preview_writes_nothing_and_exits_1_when_missing(tmp_path, capsys):
    root = _proj(tmp_path, {"app.py": "import yaml\n"})
    assert cli.main(["deps", "--path", str(root)]) == 1
    out = capsys.readouterr().out
    assert "미리보기" in out and "아무 파일도 쓰지 않았습니다" in out and "deps --write" in out
    assert not (root / "requirements.txt").exists()


def test_write_creates_file_then_refuses_to_overwrite(tmp_path, capsys):
    root = _proj(tmp_path, {"app.py": "import yaml\n"})
    assert cli.main(["deps", "--path", str(root), "--write"]) == 0
    written = (root / "requirements.txt").read_text(encoding="utf-8")
    assert "PyYAML>=6.0.3" in written
    (root / "requirements.txt").write_text("my own file\n", encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["deps", "--path", str(root), "--write"]) == 2
    assert "덮어쓰지 않았습니다" in capsys.readouterr().err
    assert (root / "requirements.txt").read_text(
        encoding="utf-8"
    ) == "my own file\n"  # 사용자 파일은 그대로


def test_write_with_custom_output_name(tmp_path):
    root = _proj(tmp_path, {"app.py": "import yaml\n", "requirements.txt": "keep\n"})
    assert (
        cli.main(["deps", "--path", str(root), "--write", "--output", "requirements.generated.txt"])
        == 0
    )
    assert (root / "requirements.generated.txt").exists()
    assert (root / "requirements.txt").read_text(encoding="utf-8") == "keep\n"


# ------------------------------------------------------------ 실제 환경(가짜 없이)
def test_e2e_real_environment_maps_pytest_to_its_distribution(tmp_path, capsys, monkeypatch):
    monkeypatch.undo()  # autouse 가짜 해제: 이 PC 의 실제 설치 메타데이터 사용
    root = _proj(tmp_path, {"t.py": "import pytest\n"})
    assert cli.main(["deps", "--path", str(root)]) == 1  # 선언 없음
    out = capsys.readouterr().out
    assert "pytest [필수, 선언 없음]" in out
    assert any(ln.startswith("pytest>=") for ln in out.splitlines())

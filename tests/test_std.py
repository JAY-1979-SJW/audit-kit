"""audit-kit std: 조항별 위반이 정확한 조항 ID 로 보고되는지 확인한다."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from audit_kit import cli
from audit_kit.config import load_config
from audit_kit.std import checks
from audit_kit.std.rules import (
    bundled,
    implemented_custom_names,
    load_registry,
    load_rules,
    ruff_rule_for,
    rule_for_custom,
    source_version,
)
from audit_kit.std.run import run_std


def lines(*rows: str) -> str:
    return "\n".join(rows) + "\n"


VIOLATIONS = lines(
    "import os",
    "import subprocess",
    "import totally_fake_module_xyz",
    "import pluggy",
    "",
    'BASE = "C:\\\\Users\\\\me\\\\data"',
    'password = "hunter2"',
    "",
    "",
    "def load_workbook(p):",
    "    return p",
    "",
    "",
    "def Dispatch(name):",
    "    return name",
    "",
    "",
    "def read(path, cache=[]):",
    "    f = open(path)",
    "    print(f.read())",
    "    try:",
    "        return cache",
    "    except:",
    "        pass",
    "",
    "",
    "def read(path):",
    "    return path",
    "",
    "",
    "def collect(items):",
    "    allowed = ['a', 'b']",
    "    out = []",
    "    for x in items:",
    "        if x in allowed:",
    "            continue",
    "        out.append(x * 2)",
    "        with open('fixed.txt') as fh:",
    "            fh.read()",
    "    return out",
    "",
    "",
    "def sheet(ws, path):",
    "    wb = load_workbook(path)",
    "    for i in range(3):",
    "        for j in range(3):",
    "            ws.cell(row=i, column=j)",
    "    return wb",
    "",
    "",
    "def excel():",
    '    app = Dispatch("Excel.Application")',
    "    return app",
    "",
    "",
    "def doubled(items):",
    "    out = []",
    "    for x in items:",
    "        out.append(x * 2)",
    "    return out",
    "",
    "",
    "def shell(cmd):",
    "    subprocess.run(cmd, shell=True)",
    "",
    "",
    "def run_text(cmd):",
    "    return subprocess.run(cmd, text=True)",
)
DUP_BODY = lines(
    "def calc(a, b):",
    "    total = a + b",
    "    total = total * 2",
    "    total = total - 1",
    "    total = total / 3",
    "    return total",
)
CLEAN = lines(
    "from __future__ import annotations",
    "",
    "from pathlib import Path",
    "",
    "",
    "def read_text(path: Path) -> str:",
    '    return path.read_text(encoding="utf-8")',
)
EXPECTED = {
    "STD-01", "STD-02", "STD-03", "STD-04", "STD-05", "STD-09", "STD-10", "STD-11", "STD-12",
    "DUP-01", "DUP-02", "DUP-03",
    "EFF-01", "EFF-02", "EFF-03", "EFF-04", "EFF-05", "EFF-06",
    "ERR-01", "ERR-03", "ERR-06", "ERR-07",
    "DOC-01", "DOC-02",
    "SEC-03", "SEC-04",
    "OPS-01", "OPS-03", "OPS-04", "OPS-05", "OPS-06",
    "OPS-07", "OPS-08", "OPS-10", "OPS-11", "OPS-13", "OPS-14", "OPS-16",
    "ADR-01", "DB-01", "DB-02", "API-01",
}  # fmt: skip
FAKE_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"  # gitleaks 예시와 같은 형식(진짜 값 아님)


def make_project(root: Path) -> Path:
    files = {
        "pyproject.toml": '[project]\nname = "sample"\nversion = "0"\n',
        "app/__init__.py": "",
        "app/violations.py": VIOLATIONS,
        "app/dup_a.py": DUP_BODY,
        "app/dup_b.py": DUP_BODY,
        "app/report_old.py": "VALUE = 1\n",
        "app/clean.py": CLEAN,
        "app/leaked_key.py": f'AWS_KEY = "{FAKE_AWS_KEY}"\n',
        "app/hooks/bad_hook.py": lines(
            "import json",
            "import sys",
            "",
            "",
            "def gate(p):",
            "    if p.exists():",
            "        return 'deny'",
            "    return 'allow'",
            "",
            "",
            "def main():",
            "    data = json.loads(sys.stdin.read())",
            "    if data.get('x'):",
            "        return 0",
            "    return 0",
            "",
            "",
            'if __name__ == "__main__":',
            "    sys.exit(main())",
        ),
        ".env": "SOME_VALUE=1\n",
        "test_quick.py": "VALUE = 2\n",
        "CLAUDE.md": "\n".join(f"규칙 {i}" for i in range(250)) + "\n",
    }
    for rel, text in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return root


def findings_of(root: Path) -> list:
    cfg = load_config(root)
    return [f for r in run_std(cfg, with_mypy=False) for f in r.findings]


def test_every_clause_is_reported(tmp_path):
    found = {f.rule for f in findings_of(make_project(tmp_path)) if f.severity != "ignore"}
    assert found >= EXPECTED, f"조항 누락: {sorted(EXPECTED - found)}"


def test_findings_carry_clause_and_source(tmp_path):
    items = [f for f in findings_of(make_project(tmp_path)) if f.rule == "EFF-03"]
    assert items
    assert items[0].extra["clause"] == "EFF-03"
    assert "근거:" in items[0].evidence
    assert items[0].file == "app/violations.py"


def test_clean_file_has_no_findings(tmp_path):
    items = findings_of(make_project(tmp_path))
    assert [f for f in items if f.file == "app/clean.py" and f.severity != "ignore"] == []


def test_string_lines_are_not_imports():
    tree = ast.parse('x = """\nimport requests\n"""\n')
    assert checks.guarded_imports(tree) == []


def test_optional_imports_are_not_required():
    src = lines(
        "from typing import TYPE_CHECKING",
        "try:",
        "    import optional_thing",
        "except ImportError:",
        "    optional_thing = None",
        "if TYPE_CHECKING:",
        "    import typing_only",
        "import required_thing",
    )
    names = [n for n, _ in checks.guarded_imports(ast.parse(src))]
    assert "required_thing" in names
    assert "optional_thing" not in names
    assert "typing_only" not in names


def test_ruff_codes_map_to_most_specific_clause():
    rules = load_rules()
    assert ruff_rule_for(rules, "PLW1514").id == "STD-01"
    assert ruff_rule_for(rules, "PERF401").id == "EFF-01"
    assert ruff_rule_for(rules, "PTH123").id == "STD-02"
    assert ruff_rule_for(rules, "ZZZ999") is None


def test_every_custom_check_has_a_rule():
    """checks.py 가 실제로 보고하는 custom 이름 전부가 rules.toml 에 조항으로 있어야 한다.

    2026-09-28: 하드코딩된 이름 목록(사람이 새 검사를 추가할 때마다 여기도 손으로 갱신해야
    했음 - COMMUNITY-FILES-PRESENT 도 그렇게 손으로 추가됐었다) 대신, checks.py 소스를 직접
    스캔하는 implemented_custom_names() 로 바꿔서 갱신을 잊는 걸 구조적으로 막는다.
    """
    rules = load_rules()
    names = implemented_custom_names()
    assert names, "checks.py 에서 Hit(...) 이름을 하나도 못 찾음 - HIT_NAME_RE 가 깨졌을 수 있음"
    missing = sorted(n for n in names if rule_for_custom(rules, n) is None)
    assert missing == [], f"rules.toml 에 조항이 없는 custom 검사: {missing}"


# 2026-09-28: 위 테스트를 하드코딩 목록에서 소스 스캔으로 바꾸자마자 실제로 "선언만 해두고
# 구현을 잊은" 조항 3개가 그동안 조용히 미검사 상태였던 게 드러났다(ERR-06/07, STD-12 - 전부
# 2026-09-27 실측 사례로 rules.toml 에만 기록되고 checks.py 구현은 안 됨). 휴리스틱이 까다로워
# 바로 구현하기보다, "구현 대기 중"임을 여기 명시해서 숨기지 않는다 - 아래 테스트가 이 목록과
# 실제 구현 상태가 어긋나면(즉 누가 구현해놓고 여기서 안 지웠거나, 새로 또 빠뜨렸으면) 잡는다.
PENDING_CUSTOM_RULES = frozenset({
    "DEPLOY-DRIFT-CHECK-PRESENT",  # OPS-17 (2026-09-29) - Docker 배포 프로젝트에만 해당하는
    # 조건부 검사라 대상 판별(Dockerfile+docker-compose.yml 존재) 로직부터 설계 필요, 미구현
})


def test_every_custom_rule_is_implemented_or_explicitly_pending():
    """rules.toml 에 kind=custom 으로 선언된 조항은 checks.py 구현이 있거나, PENDING_CUSTOM_RULES 에
    사유와 함께 올라있어야 한다 - 조항만 선언해두고 구현을 잊은 채 조용히 넘어가는 걸 막는다.
    """
    rules = load_rules()
    implemented = implemented_custom_names()
    declared = {r.custom for r in rules if r.kind == "custom" and r.custom}
    unaccounted = sorted(declared - implemented - PENDING_CUSTOM_RULES)
    assert unaccounted == [], (
        f"checks.py 에 구현이 없고 PENDING_CUSTOM_RULES 에도 없는 custom 조항(새로 빠뜨림): {unaccounted}"
    )


def test_pending_custom_rules_list_is_still_accurate():
    """PENDING_CUSTOM_RULES 에 올려둔 것 중 누가 이미 구현해놓고 여기서 안 지운 게 있으면 잡는다
    (방치되는 걸 막는다 - 구현되면 이 목록에서 반드시 빼야 한다)."""
    implemented = implemented_custom_names()
    stale = sorted(PENDING_CUSTOM_RULES & implemented)
    assert stale == [], (
        f"이미 구현됐는데 PENDING_CUSTOM_RULES 에 남아있음(목록에서 제거할 것): {stale}"
    )


def test_source_version_stamp_present_and_valid():
    """번들이 '32' 저장소의 어느 커밋 기준인지 SOURCE_VERSION.json 에 기록돼 있어야 한다.

    다른 PC(번들만 배포받은 경우)에서도 버전을 알 수 있게 하는 최소 장치 - 2026-09-28.
    커밋이 실제로 그 저장소 히스토리에 존재하는지까지 확인해 가짜/오타 SHA 를 막는다.
    """
    import re
    import subprocess

    version = source_version()
    assert version.get("commit"), (
        "SOURCE_VERSION.json 없음/commit 없음 - scripts/sync_standard.py 재실행 필요"
    )
    assert re.fullmatch(r"[0-9a-f]{40}", version["commit"]), (
        f"커밋 형식이 이상함: {version['commit']}"
    )
    source = Path(__file__).resolve().parents[2] / "32. Claude 개발표준"
    if not source.is_dir():
        return  # 원본 폴더가 없는 PC 에서는 히스토리 존재 확인을 생략한다
    found = subprocess.run(
        ["git", "-C", str(source), "cat-file", "-e", version["commit"]],
        capture_output=True,
    )
    assert found.returncode == 0, (
        f"SOURCE_VERSION.json 의 커밋이 32 저장소 히스토리에 없음: {version['commit']}"
    )


def test_bundle_matches_source_of_truth():
    source = Path(__file__).resolve().parents[2] / "32. Claude 개발표준"
    pairs = {
        "rules.toml": "standard/rules.toml",
        "docs_registry.toml": "standard/docs_registry.toml",
        "ruff.toml": "project/ruff.toml",
    }
    if not source.is_dir():
        return  # 원본 폴더가 없는 PC(번들만 있는 경우)에서는 비교하지 않는다
    for name, rel in pairs.items():
        same = bundled(name).read_bytes() == (source / rel).read_bytes()
        assert same, f"{name} 이 원본과 다름: python scripts/sync_standard.py"


def test_registry_loads():
    registry = load_registry()
    assert any(lib["name"] == "requests" for lib in registry)


def test_cli_exit_code_and_report(tmp_path):
    root = make_project(tmp_path)
    # --solo: 이 테스트는 std 자체의 종료코드·리포트만 검증한다(run 세트 실행은 별도 테스트).
    assert cli.main(["std", "--path", str(root), "--no-mypy", "--solo"]) == 1
    latest = next((root / "audit-reports").glob("std_*"))
    payload = json.loads((latest / "findings.json").read_text(encoding="utf-8"))
    assert any(f["rule"] == "EFF-03" for f in payload["findings"])
    assert cli.main(["std", "--path", str(root), "--no-mypy", "--fail-on", "never", "--solo"]) == 0


def test_std_and_run_are_paired_by_default(tmp_path, capsys):
    """2026-09-29 사용자 지시: std/run 은 항상 세트로 실행된다(기본), --solo 로만 끈다."""
    root = make_project(tmp_path)

    cli.main(["std", "--path", str(root), "--no-mypy", "--fail-on", "never"])
    out = capsys.readouterr().out
    assert "[세트] std 뒤에 run" in out
    assert "코드 건강도" in out, "run 이 실제로 이어서 실행되지 않음"

    cli.main(["run", "--path", str(root), "--fail-on", "never"])
    out = capsys.readouterr().out
    assert "[세트] run 뒤에 std" in out
    assert "조항별 위반" in out or "치명 0 / 개선 0" in out, "std 가 실제로 이어서 실행되지 않음"


def test_std_and_run_solo_skips_pair(tmp_path, capsys):
    root = make_project(tmp_path)

    cli.main(["std", "--path", str(root), "--no-mypy", "--fail-on", "never", "--solo"])
    out = capsys.readouterr().out
    assert "[세트]" not in out
    assert "코드 건강도" not in out, "--solo 인데도 run 이 실행됨"

    cli.main(["run", "--path", str(root), "--fail-on", "never", "--solo"])
    out = capsys.readouterr().out
    assert "[세트]" not in out


def test_declared_dependency_not_installed_is_only_review(tmp_path):
    root = tmp_path
    (root / "pyproject.toml").write_text(
        '[project]\nname = "s"\nversion = "0"\ndependencies = ["zzz-declared-pkg>=1"]\n',
        encoding="utf-8",
    )
    (root / "a.py").write_text(
        "import zzz_declared_pkg\nimport zzz_undeclared_pkg\n", encoding="utf-8"
    )
    by_name = {f.message.split("'")[1]: f for f in findings_of(root) if f.rule == "ERR-01"}
    assert by_name["zzz_declared_pkg"].severity == "review"
    assert by_name["zzz_undeclared_pkg"].severity == "critical"


def test_mypy_reports_missing_attribute_as_err02(tmp_path):
    root = tmp_path
    (root / "pyproject.toml").write_text('[project]\nname = "s"\nversion = "0"\n', encoding="utf-8")
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "m.py").write_text("from os import not_a_real_function\n", encoding="utf-8")
    found = [f for r in run_std(load_config(root)) for f in r.findings]
    assert any(f.rule == "ERR-02" and f.file == "pkg/m.py" for f in found)


def test_version_conditional_import_is_optional():
    src = lines(
        "import sys",
        "if sys.version_info >= (3, 11):",
        "    import tomllib",
        "else:",
        "    import tomli as tomllib",
    )
    names = [n for n, _ in checks.guarded_imports(ast.parse(src))]
    assert names == ["sys"]


def test_version_suffix_needs_original_file():
    hits = checks.check_copy_filenames([
        "a/design_v2.py",
        "a/report.py",
        "a/report_v2.py",
        "a/x_old.py",
    ])
    assert sorted(h.file for h in hits) == ["a/report_v2.py", "a/x_old.py"]


def test_fixtures_are_not_scanned(tmp_path):
    root = make_project(tmp_path)
    fixture = root / "tests" / "fixtures" / "bad.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("import os\n", encoding="utf-8")
    assert all(f.file != "tests/fixtures/bad.py" for f in findings_of(root))


def test_known_tool_missing_is_review_but_fake_is_critical(tmp_path):
    (tmp_path / "a.py").write_text(
        "import openpyxl\nimport totally_fake_module_xyz\n", encoding="utf-8"
    )
    by_name = {f.message.split("'")[1]: f for f in findings_of(tmp_path) if f.rule == "ERR-01"}
    openpyxl = by_name.get("openpyxl")  # 이 환경에 설치돼 있으면 보고되지 않는다
    assert openpyxl is None or openpyxl.severity == "review"
    assert by_name["totally_fake_module_xyz"].severity == "critical"


def test_requirements_txt_counts_as_declared(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "# 주석\nzzz-req-pkg>=1.0\n-r other.txt\n", encoding="utf-8"
    )
    assert checks.declared_requirements(tmp_path) == {"zzz_req_pkg"}
    (tmp_path / "a.py").write_text("import zzz_req_pkg\n", encoding="utf-8")
    found = [f for f in findings_of(tmp_path) if f.rule == "ERR-01"]
    assert [f.severity for f in found] == ["review"]


def test_pytest_settings_are_checked_only_for_projects_with_tests(tmp_path):
    assert checks.check_pytest_settings(tmp_path) == []  # tests/ 가 없으면 대상 아님
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "s"\nversion = "0"\n', encoding="utf-8"
    )
    hits = checks.check_pytest_settings(tmp_path)
    assert [h.check for h in hits] == ["PYTEST-SETTINGS"]
    assert "filterwarnings" in hits[0].message and "timeout" in hits[0].message


def test_complete_pytest_settings_pass(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'addopts = ["-ra", "--strict-markers"]\n'
        'filterwarnings = ["error"]\n'
        "timeout = 60\n",
        encoding="utf-8",
    )
    assert checks.check_pytest_settings(tmp_path) == []


def test_pytest_settings_partial_lists_only_what_is_missing(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = ["--strict-markers"]\n', encoding="utf-8"
    )
    message = checks.check_pytest_settings(tmp_path)[0].message
    assert "--strict-markers" not in message
    assert "filterwarnings" in message


def test_pytest_settings_clause_is_reported_by_std(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "s"\nversion = "0"\n', encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "def test_a():\n    assert True\n", encoding="utf-8"
    )
    found = [f for f in findings_of(tmp_path) if f.rule == "ERR-05"]
    assert found and found[0].severity == "review"


# ---------------------------------------------------------------- OPS 운영·관리
def test_release_config_present_flags_both_missing_pieces(tmp_path):
    hits = checks.check_release_config_present(tmp_path)
    assert [h.check for h in hits] == ["RELEASE-CONFIG-PRESENT", "RELEASE-CONFIG-PRESENT"]


def test_release_config_present_codeowners_in_github_dir_is_enough(tmp_path):
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "CODEOWNERS").write_text("* @octocat\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.semantic_release]\nversion_toml = ["pyproject.toml:project.version"]\n',
        encoding="utf-8",
    )
    assert checks.check_release_config_present(tmp_path) == []


def test_release_config_present_releaserc_file_is_enough(tmp_path):
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "CODEOWNERS").write_text("* @octocat\n", encoding="utf-8")
    (tmp_path / ".releaserc.json").write_text("{}\n", encoding="utf-8")
    assert checks.check_release_config_present(tmp_path) == []


def test_error_tracking_sdk_present_via_declared_dependency(tmp_path):
    assert checks.check_error_tracking_sdk_present(tmp_path, set()) != []
    assert checks.check_error_tracking_sdk_present(tmp_path, {"sentry_sdk"}) == []


def test_error_tracking_sdk_present_via_package_json(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"@sentry/node": "^8"}}\n', encoding="utf-8"
    )
    assert checks.check_error_tracking_sdk_present(tmp_path, set()) == []


def test_mkdocs_config_present(tmp_path):
    assert checks.check_mkdocs_config_present(tmp_path) != []
    (tmp_path / "mkdocs.yml").write_text("site_name: x\n", encoding="utf-8")
    assert checks.check_mkdocs_config_present(tmp_path) == []


def test_sonarqube_config_present_via_properties_file(tmp_path):
    assert checks.check_sonarqube_config_present(tmp_path) != []
    (tmp_path / "sonar-project.properties").write_text("sonar.projectKey=x\n", encoding="utf-8")
    assert checks.check_sonarqube_config_present(tmp_path) == []


def test_sonarqube_config_present_via_ci_workflow(tmp_path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "- uses: SonarSource/sonarqube-scan-action@v5\n", encoding="utf-8"
    )
    assert checks.check_sonarqube_config_present(tmp_path) == []


def test_sbom_present(tmp_path):
    assert checks.check_sbom_present(tmp_path) != []
    (tmp_path / "bom.json").write_text("{}\n", encoding="utf-8")
    assert checks.check_sbom_present(tmp_path) == []


def test_dependabot_config_present(tmp_path):
    assert checks.check_dependabot_config_present(tmp_path) != []
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "dependabot.yml").write_text(
        'version: 2\nupdates:\n  - package-ecosystem: "pip"\n    directory: "/"\n    schedule:\n      interval: "weekly"\n',
        encoding="utf-8",
    )
    assert checks.check_dependabot_config_present(tmp_path) == []


def test_runbook_present(tmp_path):
    assert checks.check_runbook_present(tmp_path) != []
    (tmp_path / "RUNBOOK.md").write_text("# Runbook\n", encoding="utf-8")
    assert checks.check_runbook_present(tmp_path) == []


def test_structured_logging_present(tmp_path):
    assert checks.check_structured_logging_present(set()) != []
    assert checks.check_structured_logging_present({"structlog"}) == []


def test_privacy_policy_present(tmp_path):
    assert checks.check_privacy_policy_present(tmp_path) != []
    (tmp_path / "PRIVACY.md").write_text("# 개인정보 처리방침\n", encoding="utf-8")
    assert checks.check_privacy_policy_present(tmp_path) == []


def test_apm_sdk_present(tmp_path):
    assert checks.check_apm_sdk_present(set()) != []
    assert checks.check_apm_sdk_present({"opentelemetry_distro"}) == []


def test_deployment_doc_present(tmp_path):
    assert checks.check_deployment_doc_present(tmp_path) != []
    (tmp_path / "DEPLOYMENT.md").write_text("# 배포 절차\n", encoding="utf-8")
    assert checks.check_deployment_doc_present(tmp_path) == []


def test_community_files_present(tmp_path):
    hits = checks.check_community_files_present(tmp_path)
    assert {h.message for h in hits} == {
        "LICENSE 없음", "CONTRIBUTING.md 없음", "CODE_OF_CONDUCT.md 없음", "SECURITY.md 없음",
    }  # fmt: skip
    (tmp_path / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (tmp_path / "CONTRIBUTING.md").write_text("# Contributing\n", encoding="utf-8")
    (tmp_path / "CODE_OF_CONDUCT.md").write_text("# CoC\n", encoding="utf-8")
    (tmp_path / "SECURITY.md").write_text("# Security\n", encoding="utf-8")
    assert checks.check_community_files_present(tmp_path) == []


def test_ops_checks_are_reported_by_std(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "s"\nversion = "0"\n', encoding="utf-8"
    )
    (tmp_path / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    found = {f.rule for f in findings_of(tmp_path) if f.severity != "ignore"}
    assert {
        "OPS-01", "OPS-03", "OPS-04", "OPS-05", "OPS-06",
        "OPS-07", "OPS-08", "OPS-10", "OPS-11", "OPS-13", "OPS-14", "OPS-16",
    } <= found  # fmt: skip


# ---------------------------------------------------------------- STD-12/ERR-06/ERR-07/ADR/DB/API
def test_subprocess_text_no_encoding():
    tree = ast.parse("import subprocess\nsubprocess.run(cmd, text=True)\n")
    hits = checks.check_subprocess_text_no_encoding(tree, "a.py")
    assert [h.check for h in hits] == ["SUBPROCESS-TEXT-NO-ENCODING"]


def test_subprocess_text_with_encoding_is_clean():
    tree = ast.parse("import subprocess\nsubprocess.run(cmd, text=True, encoding='utf-8')\n")
    assert checks.check_subprocess_text_no_encoding(tree, "a.py") == []


def test_subprocess_universal_newlines_no_encoding():
    tree = ast.parse("import subprocess\nsubprocess.check_output(cmd, universal_newlines=True)\n")
    hits = checks.check_subprocess_text_no_encoding(tree, "a.py")
    assert [h.check for h in hits] == ["SUBPROCESS-TEXT-NO-ENCODING"]


def test_subprocess_without_text_is_clean():
    tree = ast.parse("import subprocess\nsubprocess.run(cmd)\n")
    assert checks.check_subprocess_text_no_encoding(tree, "a.py") == []


def test_hook_entrypoint_unguarded_outside_hooks_dir_is_ignored():
    src = (
        "import sys\n"
        "from py_std_common import read_hook_input\n"
        "def main():\n"
        "    data = read_hook_input()\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )
    tree = ast.parse(src)
    assert checks.check_hook_entrypoint_guarded(tree, "app/script.py") == []


def test_hook_entrypoint_unguarded_flags_bare_main():
    src = (
        "import sys\n"
        "from py_std_common import read_hook_input\n"
        "def main():\n"
        "    data = read_hook_input()\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )
    tree = ast.parse(src)
    hits = checks.check_hook_entrypoint_guarded(tree, "hooks/bad.py")
    assert [h.check for h in hits] == ["HOOK-ENTRYPOINT-UNGUARDED"]


def test_hook_entrypoint_flags_narrow_except():
    src = (
        "import sys\nimport subprocess\n"
        "from py_std_common import read_hook_input\n"
        "def main():\n"
        "    data = read_hook_input()\n"
        "    try:\n"
        "        do()\n"
        "    except subprocess.TimeoutExpired:\n"
        "        return 0\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )
    tree = ast.parse(src)
    hits = checks.check_hook_entrypoint_guarded(tree, "hooks/bad.py")
    assert [h.check for h in hits] == ["HOOK-ENTRYPOINT-UNGUARDED"]


def test_hook_entrypoint_guarded_via_broad_except():
    src = (
        "import sys\n"
        "from py_std_common import read_hook_input\n"
        "def main():\n"
        "    data = read_hook_input()\n"
        "    try:\n"
        "        do()\n"
        "    except (OSError, ValueError, TypeError):\n"
        "        return 0\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )
    tree = ast.parse(src)
    assert checks.check_hook_entrypoint_guarded(tree, "hooks/good.py") == []


def test_hook_entrypoint_guarded_via_safe_wrapper():
    src = (
        "import sys\n"
        "from py_std_common import run_guard_main\n"
        "def main():\n"
        "    return run_guard_main('x', mode, dispatch)\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )
    tree = ast.parse(src)
    assert checks.check_hook_entrypoint_guarded(tree, "hooks/good.py") == []


def test_hook_entrypoint_without_stdin_evidence_is_ignored():
    """py_stats.py·check_frontend.py 류: hooks 폴더 안에 있고 __main__ 도 있지만 Claude Code
    stdin JSON 을 안 읽는 CLI 스크립트라 ERR-06 대상이 아니다(2026-09-28 실측 오탐 수정)."""
    src = (
        "import argparse\n"
        "import sys\n"
        "def main():\n"
        "    args = argparse.ArgumentParser().parse_args()\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )
    tree = ast.parse(src)
    assert checks.check_hook_entrypoint_guarded(tree, "hooks/py_stats.py") == []


def test_hook_entrypoint_follows_one_level_of_delegation():
    """main() 이 로직을 헬퍼로 나눈 경우(_main_impl 패턴) 그 헬퍼의 stdin 호출까지 본다."""
    src = (
        "import sys\n"
        "from py_std_common import read_hook_input\n"
        "def main():\n"
        "    return _main_impl()\n"
        "def _main_impl():\n"
        "    data = read_hook_input()\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )
    tree = ast.parse(src)
    hits = checks.check_hook_entrypoint_guarded(tree, "hooks/bad.py")
    assert [h.check for h in hits] == ["HOOK-ENTRYPOINT-UNGUARDED"]


def test_existence_only_gating_flags_bare_exists_then_deny():
    tree = ast.parse("def gate(p):\n    if p.exists():\n        return 'deny'\n")
    hits = checks.check_existence_only_gating(tree, "hooks/bad.py")
    assert [h.check for h in hits] == ["EXISTENCE-ONLY-GATING"]


def test_existence_only_gating_allows_content_check():
    tree = ast.parse(
        "def gate(p):\n    if p.exists() and 'x' in p.read_text():\n        return 'deny'\n"
    )
    assert checks.check_existence_only_gating(tree, "hooks/good.py") == []


def test_existence_only_gating_ignores_non_decision_branch():
    tree = ast.parse("def touch(p):\n    if p.exists():\n        log(p)\n")
    assert checks.check_existence_only_gating(tree, "hooks/good.py") == []


def test_existence_only_gating_outside_hooks_dir_is_ignored():
    """OPS-* 류 '설정 파일이 있는가' 검사(존재 자체가 신호)는 훅이 아니라 걸리면 안 된다
    — checks.py 자기 자신에서 실제로 오탐이 났던 사례(2026-09-28)."""
    tree = ast.parse("def gate(p):\n    if p.exists():\n        return True\n")
    assert checks.check_existence_only_gating(tree, "app/lib.py") == []


def test_vacuous_collection_assert_flags_inline_call_source_without_guard():
    """실제 사례 재현(다른 프로젝트, 2026-09-29): build_manifest() 가 계속 빈 리스트를
    반환해도 bad 가 항상 [] 라 assert not bad 가 영원히 통과 — sanity assert 가 전혀 없다."""
    src = (
        "def test_destructive_actions_never_auto_safe():\n"
        "    bad = [a for a in build_manifest() if a['risk'] == 'SAFE']\n"
        "    assert not bad\n"
    )
    tree = ast.parse(src)
    hits = checks.check_vacuous_collection_assert(tree, "tests/test_x.py")
    assert [h.check for h in hits] == ["VACUOUS-COLLECTION-ASSERT"]


def test_vacuous_collection_assert_allows_len_guard_on_bound_source():
    """man = build_manifest(); assert len(man) > 50 로 원본이 비어있지 않음을 먼저 확인하면 통과."""
    src = (
        "def test_destructive_actions_never_auto_safe():\n"
        "    man = build_manifest()\n"
        "    assert len(man) > 50\n"
        "    bad = [a for a in man if a['risk'] == 'SAFE']\n"
        "    assert not bad\n"
    )
    tree = ast.parse(src)
    assert checks.check_vacuous_collection_assert(tree, "tests/test_x.py") == []


def test_vacuous_collection_assert_flags_len_eq_zero_form():
    """assert len(bad) == 0 형태도 같은 방식으로 잡는다."""
    src = "def test_x():\n    bad = [a for a in fetch() if a.bad]\n    assert len(bad) == 0\n"
    tree = ast.parse(src)
    hits = checks.check_vacuous_collection_assert(tree, "tests/test_x.py")
    assert [h.check for h in hits] == ["VACUOUS-COLLECTION-ASSERT"]


def test_vacuous_collection_assert_allows_truthy_source_guard():
    """assert man (참이면 비어있지 않음) 도 유효한 sanity 확인으로 인정."""
    src = (
        "def test_x():\n"
        "    man = fetch()\n"
        "    assert man\n"
        "    bad = [a for a in man if a.bad]\n"
        "    assert not bad\n"
    )
    tree = ast.parse(src)
    assert checks.check_vacuous_collection_assert(tree, "tests/test_x.py") == []


def test_vacuous_collection_assert_ignores_non_test_function():
    """test_ 로 시작하지 않는 일반 함수의 방어적 assert 는 범위 밖."""
    src = "def helper():\n    bad = [a for a in fetch() if a.bad]\n    assert not bad\n"
    tree = ast.parse(src)
    assert checks.check_vacuous_collection_assert(tree, "tests/test_x.py") == []


_ERR08_CASES = [
    ('audit_result_violation_name',
     "def test_a():\n    issues = audit_issues()\n    bad = [i for i in issues if i['sev']=='warn']\n    assert not bad\n",
     False),
    ('get_issues_key',
     "def test_b():\n    d = load()\n    fi = [i for i in d.get('issues', []) if i['code']=='X']\n    assert len(fi) == 0\n",
     False),
    ('accumulator_append',
     'def test_c():\n    errors = []\n    for f in files():\n        if bad(f):\n            errors.append(f)\n    x = [e for e in errors if e.fatal]\n    assert not x\n',
     False),
    ('nonempty_literal',
     "def test_d():\n    banned = ['a', 'b']\n    hits = [p for p in banned if p in text]\n    assert not hits\n",
     False),
    ('inline_out_name',
     "def test_e():\n    out = sim()\n    assert not [e for e in out if e['event'] == 'anomaly']\n",
     False),
    ('attribute_calls',
     'def test_f():\n    r = Foo()\n    assert not [c for c in r.calls if c.bad]\n',
     False),
    ('findings_audit_call',
     "def test_g():\n    findings = audit.audit()\n    failed = [f for f in findings if f.status == 'FAIL']\n    assert failed == []\n",
     False),
    ('new_files_prefix',
     "def test_h():\n    new_files = after - before\n    md = [f for f in new_files if f.endswith('.md')]\n    assert not md\n",
     False),
    ('splitlines_population',
     "def test_i():\n    bad = [l for l in src.splitlines() if 'x' in l]\n    assert not bad\n",
     True),
    ('rglob_population',
     "def test_j():\n    bad = [f for f in policy_dir.rglob('*') if f.suffix == '.tsx']\n    assert bad == []\n",
     True),
    ('upper_constant_items',
     "def test_k():\n    bad = [k for k, v in DEPENDENCY_TABLE.items() if v['x'] == 'Y']\n    assert len(bad) == 0\n",
     True),
    ('registry_call',
     'def test_l():\n    bad = [s for s in gate.all_steps() if s.bad]\n    assert not bad\n',
     True),
    ('parameter_population',
     'def test_m(rows):\n    bad = [r for r in rows if r.bad]\n    assert not bad\n',
     True),
    ('inline_manifest_call',
     "def test_n():\n    bad = [a for a in build_manifest() if a['risk'] == 'SAFE']\n    assert not bad\n",
     True),
    ('results_from_rglob',
     "def test_o():\n    results = [p for p in ROOT.rglob('*.py')]\n    bad = [r for r in results if r.stat().st_size == 0]\n    assert not bad\n",
     True),
    ('matches_findall_excluded_by_policy',
     "def test_p():\n    matches = re.findall(PAT, path.read_text())\n    real = [m for m in matches if '<' not in m]\n    assert real == []\n",
     False),
    ('matches_from_splitlines_flagged',
     "def test_p2():\n    matches = [l for l in text.splitlines() if 'x' in l]\n    real = [m for m in matches if '<' not in m]\n    assert real == []\n",
     True),
    ('augassign_accumulator',
     'def test_q():\n    issues = []\n    issues += collect()\n    bad = [i for i in issues if i.x]\n    assert not bad\n',
     False),
    ('sanity_assert_still_wins',
     'def test_r():\n    issues = collect()\n    assert issues\n    bad = [i for i in issues if i.x]\n    assert not bad\n',
     False),
    ('subscript_errors_key',
     "def test_s():\n    bad = [i for i in cfg['errors'] if i.x]\n    assert not bad\n",
     False),
    ('subscript_rows_key',
     "def test_s2():\n    bad = [i for i in cfg['rows'] if i.x]\n    assert not bad\n",
     True),
    ('set_difference_new_items',
     "def test_t():\n    new_items = after - before\n    md = [f for f in new_items if f.endswith('.md')]\n    assert not md\n",
     False),
    ('subprocess_stdout_lines_not_file_population',
     "def test_u():\n    proc = run_git()\n    changed = proc.stdout.splitlines()\n    ui = [f for f in changed if f.startswith('ui/')]\n    assert ui == []\n",
     False),
]


@pytest.mark.parametrize(
    "src, flagged",
    [pytest.param(src, flagged, id=cid) for cid, src, flagged in _ERR08_CASES],
)
def test_vacuous_collection_assert_source_kinds(src, flagged):
    """ERR-08: 위반/결과 목록(비어 있어야 정상)은 제외하고 glob/rglob/splitlines/상수/레지스트리
    같은 모집단은 계속 표시한다(설계: _reports/err08_checker_design.md, 권장안 C)."""
    hits = checks.check_vacuous_collection_assert(ast.parse(src), "tests/test_x.py")
    assert [h.check for h in hits] == (["VACUOUS-COLLECTION-ASSERT"] if flagged else [])


_ERR08_WRAP_CASES = [
    ("len_list_wrapped", "def test_a():\n    SRC = fetch()\n    assert len(list(SRC)) > 0\n    bad = [a for a in SRC if a.bad]\n    assert not bad\n", False),
    ("list_wrapped_truthy", "def test_b():\n    SRC = fetch()\n    assert list(SRC)\n    bad = [a for a in SRC if a.bad]\n    assert not bad\n", False),
    ("len_tuple_wrapped_ge", "def test_c():\n    SRC = fetch()\n    assert len(tuple(SRC)) >= 1\n    bad = [a for a in SRC if a.bad]\n    assert not bad\n", False),
    ("no_sanity_assert", "def test_d():\n    SRC = fetch()\n    bad = [a for a in SRC if a.bad]\n    assert not bad\n", True),
    ("sanity_on_other_source", "def test_e():\n    SRC = fetch()\n    OTHER = fetch2()\n    assert list(OTHER)\n    bad = [a for a in SRC if a.bad]\n    assert not bad\n", True),
    ("len_sorted_wrapped_ne_zero", "def test_f():\n    SRC = fetch()\n    assert len(sorted(SRC)) != 0\n    bad = [a for a in SRC if a.bad]\n    assert not bad\n", False),
    ("wrapped_source_plain_assert", "def test_g():\n    SRC = fetch()\n    assert SRC\n    bad = [a for a in list(SRC) if a.bad]\n    assert not bad\n", False),
    ('len_reversed_compare', 'def test_i():\n    SRC = fetch()\n    assert 15 <= len(SRC)\n    bad = [a for a in SRC if a.bad]\n    assert not bad\n', False),
    ('len_and_truthy_conjunction', 'def test_j():\n    SRC = fetch()\n    assert len(SRC) >= 15 and SRC[0]\n    bad = [a for a in SRC if a.bad]\n    assert not bad\n', False),
    ('len_or_is_not_sanity', 'def test_k():\n    SRC = fetch()\n    assert len(SRC) >= 15 or flag\n    bad = [a for a in SRC if a.bad]\n    assert not bad\n', True),
    ('len_reversed_upper_bound_not_sanity', 'def test_l():\n    SRC = fetch()\n    assert 15 >= len(SRC)\n    bad = [a for a in SRC if a.bad]\n    assert not bad\n', True),
    ('len_on_mapping_items_view', 'def test_m():\n    SRC = fetch()\n    assert len(SRC) >= 15\n    bad = [k for k, v in SRC.items() if not v]\n    assert not bad\n', False),
    ('len_on_mapping_values_view', 'def test_n():\n    SRC = fetch()\n    assert SRC\n    bad = [v for v in SRC.values() if not v]\n    assert not bad\n', False),
    ('len_on_other_mapping_items_view', 'def test_o():\n    SRC = fetch()\n    assert len(OTHER) >= 15\n    bad = [k for k, v in SRC.items() if not v]\n    assert not bad\n', True),
    ("len_wrapped_eq_zero_not_sanity", "def test_h():\n    SRC = fetch()\n    assert len(list(SRC)) == 0\n    bad = [a for a in SRC if a.bad]\n    assert not bad\n", True),
]


@pytest.mark.parametrize(
    "src, flagged",
    [pytest.param(src, flagged, id=cid) for cid, src, flagged in _ERR08_WRAP_CASES],
)
def test_vacuous_collection_assert_wrapped_sanity(src, flagged):
    """ERR-08: 같은 SRC 를 list()/tuple() 등으로 단순 래핑한 sanity 단언도 인정한다.
    다른 SRC 에 걸린 단언이나 sanity 가 아닌 단언은 인정하지 않는다."""
    hits = checks.check_vacuous_collection_assert(ast.parse(src), "tests/test_x.py")
    assert [h.check for h in hits] == (["VACUOUS-COLLECTION-ASSERT"] if flagged else [])


_ERR08_ENUM_ZIP_CASES = [
    ('enum_len_sanity', 'def test_x():\n    SRC = fetch()\n    assert len(SRC) > 0\n    bad = [i for i, v in enumerate(SRC) if v.bad]\n    assert not bad\n', False),
    ('enum_truthy_sanity', 'def test_x():\n    SRC = fetch()\n    assert SRC\n    bad = [i for i, v in enumerate(SRC) if v.bad]\n    assert not bad\n', False),
    ('enum_start_arg', 'def test_x():\n    SRC = fetch()\n    assert SRC\n    bad = [i for i, v in enumerate(SRC, 1) if v.bad]\n    assert not bad\n', False),
    ('enum_no_sanity', 'def test_x():\n    SRC = fetch()\n    bad = [i for i, v in enumerate(SRC) if v.bad]\n    assert not bad\n', True),
    ('enum_sanity_on_other', 'def test_x():\n    SRC = fetch()\n    OTHER = fetch2()\n    assert OTHER\n    bad = [i for i, v in enumerate(SRC) if v.bad]\n    assert not bad\n', True),
    ('enum_wrapped_inner', 'def test_x():\n    SRC = fetch()\n    assert list(SRC)\n    bad = [i for i, v in enumerate(sorted(SRC)) if v.bad]\n    assert not bad\n', False),
    ('enum_items_view', 'def test_x():\n    D = fetch()\n    assert D\n    bad = [i for i, (k, v) in enumerate(D.items()) if not v]\n    assert not bad\n', False),
    ('enum_len_eq_zero_not_sanity', 'def test_x():\n    SRC = fetch()\n    assert len(SRC) == 0\n    bad = [i for i, v in enumerate(SRC) if v.bad]\n    assert not bad\n', True),
    ('zip_both_len', 'def test_x():\n    X = fx()\n    Y = fy()\n    assert len(X) > 0\n    assert len(Y) > 0\n    bad = [a for a, b in zip(X, Y) if a != b]\n    assert not bad\n', False),
    ('zip_both_and', 'def test_x():\n    X = fx()\n    Y = fy()\n    assert X and Y\n    bad = [a for a, b in zip(X, Y) if a != b]\n    assert not bad\n', False),
    ('zip_only_first', 'def test_x():\n    X = fx()\n    Y = fy()\n    assert len(X) > 0\n    bad = [a for a, b in zip(X, Y) if a != b]\n    assert not bad\n', True),
    ('zip_only_second', 'def test_x():\n    X = fx()\n    Y = fy()\n    assert Y\n    bad = [a for a, b in zip(X, Y) if a != b]\n    assert not bad\n', True),
    ('zip_no_sanity', 'def test_x():\n    X = fx()\n    Y = fy()\n    bad = [a for a, b in zip(X, Y) if a != b]\n    assert not bad\n', True),
    ('zip_three_args_one_missing', 'def test_x():\n    X = fx()\n    Y = fy()\n    Z = fz()\n    assert X and Y\n    bad = [a for a, b, c in zip(X, Y, Z) if a != b]\n    assert not bad\n', True),
    ('zip_three_args_all', 'def test_x():\n    X = fx()\n    Y = fy()\n    Z = fz()\n    assert X and Y and Z\n    bad = [a for a, b, c in zip(X, Y, Z) if a != b]\n    assert not bad\n', False),
    ('zip_strict_all', 'def test_x():\n    X = fx()\n    Y = fy()\n    assert X and Y\n    bad = [a for a, b in zip(X, Y, strict=True) if a != b]\n    assert not bad\n', False),
    ('zip_or_not_sanity', 'def test_x():\n    X = fx()\n    Y = fy()\n    assert X or Y\n    bad = [a for a, b in zip(X, Y) if a != b]\n    assert not bad\n', True),
    ('enum_zip_nested_all', 'def test_x():\n    X = fx()\n    Y = fy()\n    assert X and Y\n    bad = [i for i, (a, b) in enumerate(zip(X, Y)) if a != b]\n    assert not bad\n', False),
    ('enum_zip_nested_partial', 'def test_x():\n    X = fx()\n    Y = fy()\n    assert X\n    bad = [i for i, (a, b) in enumerate(zip(X, Y)) if a != b]\n    assert not bad\n', True),
    ('zip_with_literal_arg', 'def test_x():\n    X = fx()\n    assert X\n    bad = [a for a, b in zip(X, [1, 2, 3]) if a != b]\n    assert not bad\n', False),
    ('zip_len_eq_chain_conservative', 'def test_x():\n    X = fx()\n    Y = fy()\n    assert len(X) == len(Y) > 0\n    bad = [a for a, b in zip(X, Y) if a != b]\n    assert not bad\n', True),
    ('enum_exact_wrapped_expr', 'def test_x():\n    SRC = fetch()\n    assert len(list(enumerate(SRC))) > 0\n    bad = [i for i, v in enumerate(SRC) if v.bad]\n    assert not bad\n', False),
    ('zip_items_view_all', 'def test_x():\n    X = fx()\n    Y = fy()\n    assert X and Y\n    bad = [k for (k, v), b in zip(X.items(), Y) if v != b]\n    assert not bad\n', False),
    ('enum_sorted_no_sanity', 'def test_x():\n    SRC = fetch()\n    bad = [i for i, v in enumerate(sorted(SRC)) if v.bad]\n    assert not bad\n', True),
    ('enum_unknown_keyword_not_unwrapped', 'def test_x():\n    SRC = fetch()\n    assert SRC\n    bad = [i for i, v in enumerate(SRC, start=1) if v.bad]\n    assert not bad\n', True),
]


@pytest.mark.parametrize(
    "src, flagged",
    [pytest.param(src, flagged, id=cid) for cid, src, flagged in _ERR08_ENUM_ZIP_CASES],
)
def test_vacuous_collection_assert_enumerate_zip(src, flagged):
    """ERR-08: enumerate(x) 는 x 와 비어 있음 여부가 같으므로 x 의 sanity 단언을 인정하고,
    zip(x, y, ...) 은 모든 위치 인자에 단언이 있을 때만 인정한다(하나라도 비면 결과가 비므로)."""
    hits = checks.check_vacuous_collection_assert(ast.parse(src), "tests/test_x.py")
    assert [h.check for h in hits] == (["VACUOUS-COLLECTION-ASSERT"] if flagged else [])


def test_adr_dir_present(tmp_path):
    assert checks.check_adr_dir_present(tmp_path) != []
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "adr" / "0001-x.md").write_text("# ADR\n", encoding="utf-8")
    assert checks.check_adr_dir_present(tmp_path) == []


def test_db_migration_risk_config_present(tmp_path):
    assert checks.check_db_migration_risk_config_present(tmp_path) != []
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("- uses: sbdchd/squawk-action@v1\n", encoding="utf-8")
    assert checks.check_db_migration_risk_config_present(tmp_path) == []


def test_db_schema_drift_config_present(tmp_path):
    assert checks.check_db_schema_drift_config_present(tmp_path) != []
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("- run: tbls diff\n", encoding="utf-8")
    assert checks.check_db_schema_drift_config_present(tmp_path) == []


def test_api_contract_test_config_present_via_dependency(tmp_path):
    assert checks.check_api_contract_test_config_present(tmp_path, set()) != []
    assert checks.check_api_contract_test_config_present(tmp_path, {"schemathesis"}) == []


def test_api_contract_test_config_present_via_ci(tmp_path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("- run: schemathesis run openapi.json\n", encoding="utf-8")
    assert checks.check_api_contract_test_config_present(tmp_path, set()) == []


def test_effective_ruff_config_unchanged_without_project_override(tmp_path):
    """대상 프로젝트에 ruff 설정이 없으면 번들 경로를 그대로 돌려준다(임시 파일 없음)."""
    from audit_kit.std.run import effective_ruff_config

    assert effective_ruff_config(tmp_path, bundled("ruff.toml")) == bundled("ruff.toml")


def test_effective_ruff_config_merges_project_extend_immutable_calls(tmp_path):
    """실제 사례 재현(다른 프로젝트, 2026-09-29): Depends(require_role(...))
    처럼 프로젝트 고유 의존성 팩토리가 Depends() 안에 중첩되면, 표준 번들의
    extend-immutable-calls(fastapi.Depends 만 있음)로는 안쪽 호출이 여전히 B008 로 잡힌다.
    대상 프로젝트가 configs/ruff.toml 에 선언한 추가 항목만 병합해 해결한다."""
    from audit_kit.std.run import effective_ruff_config

    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "ruff.toml").write_text(
        '[lint.flake8-bugbear]\nextend-immutable-calls = ["myapp.auth.require_role"]\n',
        encoding="utf-8",
    )
    merged = effective_ruff_config(tmp_path, bundled("ruff.toml"))
    assert merged != bundled("ruff.toml")
    text = merged.read_text(encoding="utf-8")
    assert "myapp.auth.require_role" in text
    assert "fastapi.Depends" in text  # 번들의 기존 항목도 그대로 유지


def test_effective_ruff_config_end_to_end_suppresses_nested_factory_b008(tmp_path):
    """병합된 설정으로 실제 ruff 를 돌려 require_role 류 중첩 팩토리 호출의 B008 이
    사라지는지 종단 확인(단위 텍스트 대조가 아니라 실제 ruff 실행 결과로 검증)."""
    from audit_kit.config import load_config
    from audit_kit.std.run import effective_ruff_config, run_std_ruff

    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "ruff.toml").write_text(
        '[lint.flake8-bugbear]\nextend-immutable-calls = ["auth.require_role"]\n',
        encoding="utf-8",
    )
    (tmp_path / "auth.py").write_text(
        "def require_role(*roles):\n    def dep():\n        return roles\n    return dep\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text(
        "from fastapi import Depends\nfrom auth import require_role\n\n\n"
        'def endpoint(user=Depends(require_role("admin"))):\n    return user\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    files = ["app.py", "auth.py"]
    rules = load_rules()
    merged = effective_ruff_config(tmp_path, bundled("ruff.toml"))
    result = run_std_ruff(cfg, rules, files, merged)
    b008 = [f for f in result.findings if f.extra.get("tool_rule") == "B008"]
    assert b008 == [], f"require_role 가 여전히 B008 로 잡힘: {b008}"

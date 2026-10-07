"""자사 패키지 import 가 DOC-02 로, 편입 헤더 주석이 ERA001 로 잡히는 오탐을 막는다."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from audit_kit.config import AuditConfig
from audit_kit.std import checks, run


def _make_pkg(root: Path, name: str) -> None:
    pkg = root / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("X = 1\n", encoding="utf-8")


def test_first_party_source_package_is_recognised(tmp_path, monkeypatch):
    _make_pkg(tmp_path, "ownpkg_fp")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert checks.is_first_party_source("ownpkg_fp") is True


def test_installed_distribution_and_unknown_names_are_not_first_party():
    assert checks.is_first_party_source("pytest") is False  # site-packages 에 설치된 배포본
    assert checks.is_first_party_source("no_such_module_zz") is False
    assert checks.is_first_party_source("sys") is False  # 내장은 자사가 아니다


def test_a_package_in_site_packages_path_is_not_first_party(tmp_path, monkeypatch):
    sp = tmp_path / "Lib" / "site-packages"
    sp.mkdir(parents=True)
    _make_pkg(sp, "thirdparty_fp")
    monkeypatch.syspath_prepend(str(sp))
    assert checks.is_first_party_source("thirdparty_fp") is False


def test_a_package_in_dist_packages_path_is_not_first_party(tmp_path, monkeypatch):
    dp = tmp_path / "lib" / "dist-packages"
    dp.mkdir(parents=True)
    _make_pkg(dp, "thirdparty_dp")
    monkeypatch.syspath_prepend(str(dp))
    assert checks.is_first_party_source("thirdparty_dp") is False


def _gaps(tmp_path: Path, code: str) -> list:
    tree = ast.parse(code)
    _, gaps = checks.check_imports({"a.py": tree}, set(), set())
    return gaps


def test_registry_gap_is_not_reported_for_first_party_but_is_for_installed(tmp_path, monkeypatch):
    _make_pkg(tmp_path, "ownpkg_gap")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert _gaps(tmp_path, "import ownpkg_gap\n") == []
    assert [
        h.message for h in _gaps(tmp_path, "import pytest\n")
    ]  # 설치 배포본은 등록부 누락으로 계속 보고


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# module_category: parser", True),
        ("  # primary_trade: fire", True),
        ("#layer: L1", True),
        ("# pipeline_stage: takeoff", True),
        ("# data: reads=a writes=b", True),
        ("# expects: x ; test=y", True),
        ("# x = 1", False),
        ("# other_key: value", False),
        ("value = 1  # layer: L1", False),
    ],
)
def test_header_comment_filter(tmp_path, text, expected):
    (tmp_path / "m.py").write_text(text + "\n", encoding="utf-8")
    cfg = AuditConfig(root=tmp_path)
    assert run._is_header_comment(cfg, "m.py", 1) is expected


def test_header_filter_ignores_missing_file_and_bad_line(tmp_path):
    cfg = AuditConfig(root=tmp_path)
    assert run._is_header_comment(cfg, "nope.py", 1) is False
    (tmp_path / "m.py").write_text("# layer: L1\n", encoding="utf-8")
    assert run._is_header_comment(cfg, "m.py", 0) is False
    assert run._is_header_comment(cfg, "m.py", 5) is False
    assert run._is_header_comment(cfg, "m.py", None) is False


def test_era001_on_header_line_is_dropped_but_real_dead_code_is_kept(tmp_path):
    (tmp_path / "m.py").write_text("# module_category: tool\n# x = compute(1)\n", encoding="utf-8")
    cfg = AuditConfig(root=tmp_path)
    head = {
        "filename": str(tmp_path / "m.py"),
        "code": "ERA001",
        "location": {"row": 1},
        "message": "m",
    }
    dead = {
        "filename": str(tmp_path / "m.py"),
        "code": "ERA001",
        "location": {"row": 2},
        "message": "m",
    }
    assert run._ruff_finding(cfg, [], head) is None
    assert run._ruff_finding(cfg, [], dead) is not None


def test_other_rules_on_a_header_looking_line_are_not_dropped(tmp_path):
    (tmp_path / "m.py").write_text("# layer: L1\n", encoding="utf-8")
    cfg = AuditConfig(root=tmp_path)
    item = {
        "filename": str(tmp_path / "m.py"),
        "code": "E501",
        "location": {"row": 1},
        "message": "m",
    }
    assert run._ruff_finding(cfg, [], item) is not None

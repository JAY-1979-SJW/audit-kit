"""scripts/changed_functions.py 시험: 바뀐 함수만 골라 mutmut 패턴을 만드는지 확인한다."""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
from pathlib import Path
from types import ModuleType

NL = chr(10)


def _load() -> ModuleType:
    """scripts/ 는 패키지가 아니라 sys.path 조작 없이 파일 경로로 직접 불러온다."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "changed_functions.py"
    spec = importlib.util.spec_from_file_location("_changed_functions_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cf = _load()


# ------------------------------------------------------------ module_dotted_path
def test_module_dotted_path_under_src():
    assert cf.module_dotted_path(Path("src/audit_kit/std/checks.py")) == "audit_kit.std.checks"


def test_module_dotted_path_init_drops_init():
    assert cf.module_dotted_path(Path("src/audit_kit/__init__.py")) == "audit_kit"


def test_module_dotted_path_outside_src_is_none():
    assert cf.module_dotted_path(Path("tests/test_std.py")) is None


def test_module_dotted_path_non_python_is_none():
    assert cf.module_dotted_path(Path("src/audit_kit/data.json")) is None


# ------------------------------------------------------------ _added_lines
def test_added_lines_from_hunk_header():
    assert list(cf._added_lines("@@ -10,2 +12,3 @@ def f():")) == [12, 13, 14]


def test_added_lines_deletion_only_hunk_is_empty():
    assert list(cf._added_lines("@@ -10,3 +12,0 @@")) == []


def test_added_lines_single_line_has_no_count():
    assert list(cf._added_lines("@@ -5 +5 @@")) == [5]


# ------------------------------------------------------------ functions_touching
def test_top_level_function_is_found():
    src = NL.join(["def foo():", "    return 1", "", "", "def bar():", "    return 2", ""])
    tree = ast.parse(src)
    assert cf.functions_touching(tree, {2}) == {"foo"}
    assert cf.functions_touching(tree, {6}) == {"bar"}


def test_class_method_is_dotted():
    src = NL.join(["class Foo:", "    def bar(self):", "        return 1", ""])
    tree = ast.parse(src)
    assert cf.functions_touching(tree, {3}) == {"Foo.bar"}


def test_line_outside_any_function_finds_nothing():
    tree = ast.parse("VALUE = 1" + NL)
    assert cf.functions_touching(tree, {1}) == set()


def test_nested_function_included():
    """줄이 안쪽 함수 범위이면서 바깥 함수 범위이기도 하면 둘 다 후보로 남긴다(안전 쪽으로)."""
    src = NL.join([
        "def outer():",
        "    def inner():",
        "        return 1",
        "    return inner()",
        "",
    ])
    tree = ast.parse(src)
    assert cf.functions_touching(tree, {3}) == {"outer", "outer.inner"}


# ------------------------------------------------------------ 실제 git 저장소로 종단 시험
def init_repo(root: Path) -> None:
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=root, check=True, capture_output=True)


def commit(root: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=root, check=True, capture_output=True
    )


def patterns_in(root: Path, base: str, head: str) -> list[str]:
    old_cwd = Path.cwd()
    os.chdir(root)
    try:
        return cf.patterns_for_diff(base, head)
    finally:
        os.chdir(old_cwd)


def test_end_to_end_with_real_git_repo(tmp_path):
    init_repo(tmp_path)
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    mod = pkg / "mod.py"
    mod.write_text(
        NL.join(["def keep():", "    return 1", "", "", "def change_me():", "    return 2", ""]),
        encoding="utf-8",
    )
    commit(tmp_path, "초기")

    mod.write_text(
        NL.join(["def keep():", "    return 1", "", "", "def change_me():", "    return 99", ""]),
        encoding="utf-8",
    )
    commit(tmp_path, "수정")

    assert patterns_in(tmp_path, "HEAD~1", "HEAD") == ["*pkg.mod.change_me*"]


def test_no_changes_yields_no_patterns(tmp_path):
    init_repo(tmp_path)
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "mod.py").write_text("def f():" + NL + "    return 1" + NL, encoding="utf-8")
    commit(tmp_path, "초기")
    (tmp_path / "README.md").write_text("문서만 바뀜" + NL, encoding="utf-8")
    commit(tmp_path, "문서")

    assert patterns_in(tmp_path, "HEAD~1", "HEAD") == []

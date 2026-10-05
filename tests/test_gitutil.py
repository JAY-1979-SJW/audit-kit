"""gitutil 시험: 실제 git 저장소를 만들어 diff 파싱이 옳은지 확인(mock 아님)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from audit_kit.gitutil import changed_lines, changed_lines_for_file


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "a@a")
    _git(root, "config", "user.name", "a")
    return root


def test_changed_lines_finds_modified_and_added_lines(tmp_path):
    root = _repo(tmp_path)
    mod = root / "mod.py"
    mod.write_text("def a():\n    return 1\n\n\ndef b():\n    return 2\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")

    mod.write_text("def a():\n    return 100\n\n\ndef b():\n    return 2\n", encoding="utf-8")

    result = changed_lines(root)
    assert result is not None
    assert result["mod.py"] == {2}


def test_untracked_new_file_is_entirely_new(tmp_path):
    root = _repo(tmp_path)
    (root / "existing.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")

    (root / "new_file.py").write_text("def brand_new():\n    return 1\n", encoding="utf-8")

    result = changed_lines(root)
    assert result is not None
    assert result["new_file.py"] == {1, 2}  # 전체 줄 수 기준 새 파일 전체가 "바뀐 줄"
    assert "existing.py" not in result  # 안 건드린 파일은 아예 안 나옴 = 전부 기존


def test_unmodified_file_has_no_changed_lines(tmp_path):
    root = _repo(tmp_path)
    (root / "mod.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")

    assert changed_lines_for_file(root, "mod.py") == set()


def test_new_tracked_file_maps_to_full_line_range(tmp_path):
    """git add 는 했지만 아직 커밋 안 한 새 파일 — hunk 파싱만으로 전체 줄이 잡혀야 함."""
    root = _repo(tmp_path)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")

    (root / "b.py").write_text("y = 1\ny = 2\ny = 3\n", encoding="utf-8")
    _git(root, "add", "b.py")

    result = changed_lines(root)
    assert result is not None
    assert result["b.py"] == {1, 2, 3}


def test_no_git_repo_returns_none(tmp_path):
    assert changed_lines(tmp_path) is None
    assert changed_lines_for_file(tmp_path, "whatever.py") is None


def test_untracked_file_via_changed_lines_for_file_is_entirely_new(tmp_path):
    root = _repo(tmp_path)
    (root / "existing.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")

    (root / "new_file.py").write_text("def brand_new():\n    return 1\n", encoding="utf-8")

    assert changed_lines_for_file(root, "new_file.py") == {1, 2}

"""audit-kit baseline: 테스트 실패 기준선 저장·비교."""

import json
import os
import subprocess
from pathlib import Path

from audit_kit import baseline, cli
from audit_kit.baseline import NOT_RUN, compare, diff_snapshots, git_snapshot, save
from audit_kit.config import AuditConfig

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}
NO_EFFECTS: dict = {"modified": [], "created": []}


def _cfg(tmp_path: Path) -> AuditConfig:
    (tmp_path / "tests").mkdir(exist_ok=True)
    cfg = AuditConfig()
    cfg.root = tmp_path
    return cfg


def _git(args: list, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=GIT_ENV)


def _fake(failing: set, effects: dict | None = None):
    return lambda cfg: (set(failing), effects if effects is not None else NO_EFFECTS)


# ------------------------------------------------------------ 순수 로직
def test_save_writes_sorted_failures_and_python_version(tmp_path):
    cfg = _cfg(tmp_path)
    assert save(cfg, "t1", runner=_fake({"b.t2", "a.t1"})) == 0
    data = json.loads(baseline.baseline_path(cfg, "t1").read_text(encoding="utf-8"))
    assert data["failures"] == ["a.t1", "b.t2"]
    assert data["count"] == 2
    assert data["python"]


def test_save_refuses_when_pytest_did_not_run(tmp_path, capsys):
    """pytest 미실행은 '실패 0건'이 아니다 — 기준선을 저장하면 안 된다."""
    cfg = _cfg(tmp_path)
    assert save(cfg, "t1", runner=_fake({NOT_RUN})) == 2
    assert not baseline.baseline_path(cfg, "t1").exists()
    assert "측정 실패" in capsys.readouterr().err


def test_compare_separates_new_fixed_and_still_failing(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    save(cfg, "t1", runner=_fake({"old.a", "old.b"}))
    code = compare(cfg, "t1", runner=_fake({"old.b", "new.c"}))
    out = capsys.readouterr().out
    assert code == 1
    assert "새로 생긴 실패: 1건" in out and "new.c" in out
    assert "고쳐진 테스트(기준선에는 실패): 1건" in out and "old.a" in out
    assert "기준선에도 있던 실패: 1건" in out


def test_compare_passes_when_no_new_failures(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    save(cfg, "t1", runner=_fake({"old.a"}))
    assert compare(cfg, "t1", runner=_fake({"old.a"})) == 0
    assert "새 실패 없음" in capsys.readouterr().out


def test_compare_without_baseline_is_an_error_not_a_pass(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    assert compare(cfg, "none", runner=_fake(set())) == 2
    assert "baseline save" in capsys.readouterr().err


def test_compare_refuses_when_pytest_did_not_run(tmp_path):
    cfg = _cfg(tmp_path)
    save(cfg, "t1", runner=_fake({"a.b"}))
    assert compare(cfg, "t1", runner=_fake({NOT_RUN})) == 2


def test_compare_reports_corrupt_baseline(tmp_path):
    cfg = _cfg(tmp_path)
    path = baseline.baseline_path(cfg, "t1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert compare(cfg, "t1", runner=_fake(set())) == 2


def test_rejects_unsafe_baseline_name(tmp_path):
    cfg = _cfg(tmp_path)
    assert save(cfg, "../evil", runner=_fake(set())) == 2
    assert not (tmp_path / "evil.json").exists()


def test_requires_tests_folder(tmp_path):
    cfg = AuditConfig()
    cfg.root = tmp_path
    assert save(cfg, "t1", runner=_fake(set())) == 2


def test_effects_are_printed_as_warning(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    save(cfg, "t1", runner=_fake(set(), {"modified": ["data/r.json"], "created": ["x.tmp"]}))
    out = capsys.readouterr().out
    assert "data/r.json" in out and "git checkout" in out
    assert "새 파일 1개" in out


def test_diff_snapshots_detects_tracked_change_created_file_and_ignores_noise():
    before = {"a.py": (" M", "h1"), "u.txt": ("??", None)}
    after = {
        "a.py": (" M", "h2"),  # 이미 수정 중이던 파일의 내용이 또 바뀜 -> 테스트가 바꾼 것
        "b.py": (" M", "h3"),  # 테스트가 새로 바꾼 추적 파일
        "u.txt": ("??", None),  # 그대로
        "new.log": ("??", None),  # 테스트가 만든 새 파일
    }
    eff = diff_snapshots(before, after)
    assert eff["modified"] == ["a.py", "b.py"]
    assert eff["created"] == ["new.log"]
    assert diff_snapshots(None, after) == NO_EFFECTS  # git 이 아니면 판정 불가 -> 빈 값


# ------------------------------------------------------------ 실제 git
def test_git_snapshot_sees_content_change_of_already_modified_file(tmp_path):
    _git(["init", "-q"], tmp_path)
    (tmp_path / "f.txt").write_text("v1\n", encoding="utf-8")
    _git(["add", "f.txt"], tmp_path)
    _git(["commit", "-q", "-m", "c"], tmp_path)
    (tmp_path / "f.txt").write_text("v2\n", encoding="utf-8")
    before = git_snapshot(tmp_path)
    (tmp_path / "f.txt").write_text("v3\n", encoding="utf-8")  # 테스트가 또 바꾼 상황
    after = git_snapshot(tmp_path)
    assert diff_snapshots(before, after)["modified"] == ["f.txt"]


def test_git_snapshot_handles_korean_filenames(tmp_path):
    _git(["init", "-q"], tmp_path)
    (tmp_path / "한글.txt").write_text("x\n", encoding="utf-8")
    assert "한글.txt" in (git_snapshot(tmp_path) or {})


def test_git_snapshot_returns_none_outside_git(tmp_path):
    assert git_snapshot(tmp_path) is None


# ------------------------------------------------------------ 실제 pytest 를 돌리는 e2e
def test_e2e_save_then_compare_finds_only_the_new_failure(tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='p'\nversion='0'\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text(
        "def test_ok():\n    assert True\n\n\ndef test_old_fail():\n    assert False\n",
        encoding="utf-8",
    )
    assert cli.main(["baseline", "save", "--path", str(tmp_path)]) == 0
    saved = json.loads(
        (tmp_path / "audit-reports" / "baseline_default.json").read_text(encoding="utf-8")
    )
    assert saved["count"] == 1 and "test_old_fail" in saved["failures"][0]

    (tests / "test_b.py").write_text("def test_new_fail():\n    assert False\n", encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["baseline", "compare", "--path", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "test_new_fail" in out
    assert "새로 생긴 실패: 1건" in out
    assert "기준선에도 있던 실패: 1건" in out

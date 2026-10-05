import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from audit_kit.config import AuditConfig
from audit_kit.fixflow import (
    KNOWN_ISSUES_MD,
    hint_for,
    load_known_issues,
    save_known_issues,
    update_known_issues,
)
from audit_kit.models import CRITICAL, IMPROVE
from audit_kit.tools import has_ruff_config, parse_mypy

FIXTURE = Path(__file__).parent / "fixtures" / "sample_proj"
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _ak(args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "audit_kit", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=GIT_ENV,
    )


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", env=GIT_ENV
    )


def _edit(path: Path, old: str, new: str):
    t = path.read_text(encoding="utf-8")
    assert old in t
    path.write_text(t.replace(old, new), encoding="utf-8")


def test_mypy_real_bug_codes_are_critical(tmp_path):
    cfg = AuditConfig()
    cfg.root = tmp_path
    out = parse_mypy(
        cfg,
        "\n".join([
            'a.py:1: error: Name "warnings" is not defined  [name-defined]',
            'a.py:2: error: Unexpected keyword argument "x" for "f"  [call-arg]',
            'a.py:3: error: Module "m" has no attribute "g"  [attr-defined]',
            'a.py:4: error: "int" has no attribute "g"  [attr-defined]',
            "a.py:5: error: Incompatible types in assignment  [assignment]",
        ]),
    )
    assert [f.severity for f in out] == [CRITICAL, CRITICAL, CRITICAL, IMPROVE, IMPROVE]


def test_ruff_config_detection(tmp_path):
    (tmp_path / "proj").mkdir()
    assert not has_ruff_config(tmp_path / "proj")
    (tmp_path / "ruff.toml").write_text("line-length = 100\n", encoding="utf-8")
    assert has_ruff_config(tmp_path / "proj")  # 상위 폴더 설정도 인정 (ruff 와 동일)


def test_known_issues_roundtrip(tmp_path):
    cfg = AuditConfig()
    cfg.root = tmp_path

    # 처음엔 빈 목록
    assert load_known_issues(cfg) == {"issues": {}}

    # 여전히 실패 중인(기준선에도 있고 지금도 실패하는) 테스트를 기록
    update_known_issues(cfg, {"tests/test_x.py::test_y"})
    data = load_known_issues(cfg)
    assert "tests/test_x.py::test_y" in data["issues"]
    assert data["issues"]["tests/test_x.py::test_y"]["first_seen"]

    # 사람이 읽는 md 파일도 함께 생성됨
    md = (tmp_path / KNOWN_ISSUES_MD).read_text(encoding="utf-8")
    assert "tests/test_x.py::test_y" in md

    # 원인 메모를 수동으로 채운 뒤 다시 갱신해도 note 는 보존된다(덮어쓰지 않음)
    data["issues"]["tests/test_x.py::test_y"]["note"] = "진짜 원인 발견함"
    save_known_issues(cfg, data)
    update_known_issues(cfg, {"tests/test_x.py::test_y"})
    data2 = load_known_issues(cfg)
    assert data2["issues"]["tests/test_x.py::test_y"]["note"] == "진짜 원인 발견함"

    # 빈 집합으로 호출하면 파일을 새로 만들지 않는다(아무 일도 안 함)
    other_root = tmp_path / "untouched"
    other_root.mkdir()
    cfg2 = AuditConfig()
    cfg2.root = other_root
    update_known_issues(cfg2, set())
    assert not (other_root / KNOWN_ISSUES_MD).exists()


def test_hint_matching():
    assert "import" in hint_for("F821")
    assert "shell=False" in hint_for("B602")
    assert "arch fix" in hint_for("ARCH-LAYER")
    assert hint_for("E999").startswith("구문 오류")


def test_fix_flow_end_to_end(tmp_path):
    root = tmp_path / "proj"
    shutil.copytree(
        FIXTURE, root, ignore=shutil.ignore_patterns("__pycache__", "audit-reports", ".claude")
    )
    _git(["init", "-q", "-b", "main"], root)
    assert _ak(["init"], root).returncode == 0
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "init"], root)
    _ak(["run", "--fail-on", "never", "--solo"], root)

    p = _ak(["fix", "plan"], root)
    assert p.returncode == 0 and "F004" in p.stdout, p.stdout + p.stderr
    p = _ak(["fix", "start"], root)  # .coverage·__pycache__ 는 변경으로 보지 않아야 함
    assert p.returncode == 0, p.stdout + p.stderr
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], root).stdout.startswith("audit-fix/")

    qty = root / "app/services/quantity.py"
    # 고치지 않고 check → 실패, done 거부
    assert _ak(["fix", "check", "F003"], root).returncode == 1
    assert _ak(["fix", "done", "F003"], root).returncode != 0

    _edit(qty, "subprocess.call(cmd, shell=True)", "subprocess.call(cmd.split())")
    assert _ak(["fix", "check", "F003"], root).returncode == 0
    assert _ak(["fix", "done", "F003"], root).returncode == 0

    _edit(
        qty,
        """db.execute(f"SELECT * FROM items WHERE name = '{keyword}'")""",
        """db.execute("SELECT * FROM items WHERE name = :n", {"n": keyword})""",
    )
    _edit(
        qty,
        """query = "SELECT * FROM items WHERE name = '" + keyword + "'\"""",
        """query = "SELECT * FROM items WHERE name = :n\"""",
    )
    assert _ak(["fix", "check", "F001"], root).returncode == 0
    assert _ak(["fix", "check", "F002"], root).returncode == 0
    assert _ak(["fix", "done", "F001", "F002"], root).returncode == 0  # 한 커밋으로

    # 새 치명을 만드는 수정은 거부
    _edit(root / "app/models/wall.py", "return self.width", "return undefined_name + self.width")
    _edit(root / "tests/test_wall.py", "== 2.0", "== 1.5")
    p = _ak(["fix", "check", "F004"], root)
    assert p.returncode == 1 and "새 치명" in p.stdout, p.stdout
    _edit(root / "app/models/wall.py", "return undefined_name + self.width", "return self.width")
    assert _ak(["fix", "check", "F004"], root).returncode == 0
    assert _ak(["fix", "skip", "F004", "--reason", "기대값 확인 필요"], root).returncode == 0
    _git(["checkout", "--", "tests/test_wall.py"], root)

    log = _git(["log", "--format=%s", "main..HEAD"], root).stdout.splitlines()
    assert len(log) == 2 and all(s.startswith("fix(audit):") for s in log)
    committed = _git(["show", "--name-only", "--format=", "HEAD~1..HEAD"], root).stdout
    assert "__pycache__" not in committed and ".coverage" not in committed

    p = _ak(["fix", "report"], root)
    assert p.returncode == 0 and "새 실패 0건" in p.stdout, p.stdout
    session = json.loads((root / "audit-reports/fix-session.json").read_text(encoding="utf-8"))
    status = {k: v["status"] for k, v in session["items"].items()}
    assert status == {"F001": "done", "F002": "done", "F003": "done", "F004": "skipped"}
    assert _ak(["fix", "end"], root).returncode == 0


def test_protected_paths_block_commit(tmp_path):
    root = tmp_path / "proj"
    shutil.copytree(
        FIXTURE, root, ignore=shutil.ignore_patterns("__pycache__", "audit-reports", ".claude")
    )
    _git(["init", "-q", "-b", "main"], root)
    _ak(["init"], root)
    pp = root / "pyproject.toml"
    pp.write_text(
        pp.read_text(encoding="utf-8").replace(
            "protected_paths = []", 'protected_paths = ["app/services/*"]'
        ),
        encoding="utf-8",
    )
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "init"], root)
    _ak(["run", "--no-tests", "--fail-on", "never", "--solo"], root)
    assert "🔒" in _ak(["fix", "plan"], root).stdout
    assert _ak(["fix", "start", "--no-tests"], root).returncode == 0
    _edit(
        root / "app/services/quantity.py",
        "subprocess.call(cmd, shell=True)",
        "subprocess.call(cmd.split())",
    )
    plan = json.loads((root / "audit-reports/fix-session.json").read_text(encoding="utf-8"))[
        "items"
    ]
    fid = next(k for k, v in plan.items() if v["rule"] == "B602")
    assert _ak(["fix", "check", fid], root).returncode == 0
    p = _ak(["fix", "done", fid], root)
    assert p.returncode != 0 and "보호 경로" in p.stderr

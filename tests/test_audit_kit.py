import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from audit_kit.config import AuditConfig, load_config
from audit_kit.heuristics import _matches, run_heuristics
from audit_kit.importgraph import ImportGraph
from audit_kit.init_project import init_project
from audit_kit.models import CRITICAL, IGNORE, IMPROVE, REVIEW, Finding, ToolResult
from audit_kit.report import dedupe
from audit_kit.tools import (
    bandit_severity,
    parse_import_linter,
    parse_mypy,
    ruff_severity,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_proj"


@pytest.fixture
def sample(tmp_path):
    dst = tmp_path / "sample"
    shutil.copytree(
        FIXTURE, dst, ignore=shutil.ignore_patterns("audit-reports", "__pycache__", ".claude")
    )
    return dst


def write(root: Path, files: dict):
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")


def cfg_for(root: Path, **kw) -> AuditConfig:
    cfg = AuditConfig(packages=["pkg"], **kw)
    cfg.root = root
    return cfg


# ---------------------------------------------------------------- import graph
def test_cycle_detected_and_lazy_imports_ignored(tmp_path):
    write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "from pkg import b\n",
            "pkg/b.py": "from . import c\n",
            "pkg/c.py": "import pkg.a\n",
            "pkg/d.py": """
            from typing import TYPE_CHECKING
            if TYPE_CHECKING:
                from pkg import e
            def f():
                from pkg import e
        """,
            "pkg/e.py": "from pkg import d\n",
        },
    )
    g = ImportGraph.build(cfg_for(tmp_path).package_paths())
    cycles = g.cycles()
    assert len(cycles) == 1
    assert set(cycles[0]) == {"pkg.a", "pkg.b", "pkg.c"}
    assert cycles[0][0] == cycles[0][-1]


def test_bom_file_parsed(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg/a.py").write_bytes("﻿from pkg import b\n".encode())
    (tmp_path / "pkg/b.py").write_text("from pkg import a\n", encoding="utf-8")
    assert len(ImportGraph.build(cfg_for(tmp_path).package_paths()).cycles()) == 1


# ---------------------------------------------------------------- heuristics
def test_glob_matching():
    globs = ["**/api/**/*.py"]
    assert _matches("app/api/wall.py", globs)
    assert _matches("app/api/v1/wall.py", globs)
    assert _matches("api/wall.py", globs)
    assert not _matches("app/apis/wall.py", globs)


def test_heuristics_rules(tmp_path):
    write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/db.py": """
            SessionLocal = sessionmaker()
            shared = SessionLocal()
            def get_db():
                db = SessionLocal()
                yield db
            def ok():
                with SessionLocal() as s:
                    s.commit()
            def bad(name):
                s = SessionLocal()
                s.execute(f"SELECT * FROM t WHERE n = '{name}'")
            def safe(name):
                s.execute(text("SELECT * FROM t WHERE n = :n"), {"n": name})
        """,
            "pkg/api/__init__.py": "",
            "pkg/api/r.py": """
            @router.get("/x")
            def x(db):
                for i in range(3):
                    pass
                db.query(1)
            @router.get("/y")
            def y():
                return 1
        """,
        },
    )
    res = run_heuristics(cfg_for(tmp_path))
    got = {(f.rule, f.file, f.line) for f in res.findings}
    assert ("GLOBAL-SESSION", "pkg/db.py", 3) in got
    assert ("SESSION-IN-FUNC", "pkg/db.py", 11) in got
    assert ("SQL-STRING", "pkg/db.py", 12) in got
    assert ("ROUTER-LOGIC", "pkg/api/r.py", 3) in got
    lines = {line for _, _, line in got}
    assert 5 not in lines and 8 not in lines and 14 not in lines  # get_db, with, 바인드 파라미터
    assert all(f.severity == REVIEW for f in res.findings)


# ---------------------------------------------------------------- severity / parsing
def test_severity_rules():
    cfg = AuditConfig()
    assert ruff_severity(cfg, "F821") == CRITICAL
    assert ruff_severity(cfg, "E999") == CRITICAL
    assert ruff_severity(cfg, "F401") == IMPROVE
    assert ruff_severity(cfg, "I001") == IGNORE
    assert bandit_severity(cfg, "B608", "MEDIUM", "LOW") == CRITICAL
    assert bandit_severity(cfg, "B301", "HIGH", "HIGH") == CRITICAL
    assert bandit_severity(cfg, "B301", "HIGH", "LOW") == IMPROVE
    assert bandit_severity(cfg, "B105", "LOW", "MEDIUM") == IMPROVE
    assert bandit_severity(cfg, "B101", "LOW", "HIGH") == IGNORE


def test_parse_mypy(tmp_path):
    cfg = cfg_for(tmp_path)
    out = parse_mypy(
        cfg,
        "pkg/a.py:3: error: Bad thing  [assignment]\npkg/a.py:3: note: hint\n"
        "pkg/b.py:9:5: error: Other  [attr-defined]\n",
    )
    assert [(f.file, f.line, f.rule) for f in out] == [
        ("pkg/a.py", 3, "assignment"),
        ("pkg/b.py", 9, "attr-defined"),
    ]
    assert (
        len(
            parse_mypy(
                cfg,
                "pkg/a.py:3: error: X  [misc]\npkg/b.py:1: error: Y  [misc]",
                only_file="pkg/b.py",
            )
        )
        == 1
    )


IMPORT_LINTER_OUTPUT = """\
=============
Import Linter
=============

Contracts: 0 kept, 1 broken.


----------------
Broken contracts
----------------

app layers
----------

app.models is not allowed to import app.services:

- app.models.wall -> app.services.quantity (l.1)

"""


def test_parse_import_linter(tmp_path):
    write(tmp_path, {"app/__init__.py": "", "app/models/__init__.py": "", "app/models/wall.py": ""})
    cfg = cfg_for(tmp_path)
    out = parse_import_linter(cfg, IMPORT_LINTER_OUTPUT)
    assert len(out) == 1
    f = out[0]
    assert (f.file, f.line, f.severity) == ("app/models/wall.py", 1, REVIEW)
    assert "app layers" in f.evidence


def test_dedupe_sql_with_bandit():
    b = ToolResult(
        "bandit", "findings", [Finding("bandit", "보안", "B608", "x", "a.py", 3, CRITICAL)]
    )
    h = ToolResult(
        "heuristic",
        "findings",
        [
            Finding("heuristic", "보안", "SQL-STRING", "x", "a.py", 3, REVIEW),
            Finding("heuristic", "보안", "SQL-STRING", "y", "a.py", 9, REVIEW),
            Finding("heuristic", "보안", "SQL-STRING", "y2", "a.py", 9, REVIEW),
        ],
    )
    dedupe([b, h])
    assert [f.line for f in h.findings] == [9]


# ---------------------------------------------------------------- init
def test_init_idempotent(sample):
    init_project(sample)
    init_project(sample)
    pp = (sample / "pyproject.toml").read_text(encoding="utf-8")
    assert pp.count("[tool.audit-kit]") == 1
    assert pp.count("[tool.importlinter]") == 1
    assert '"api",' in pp and '"core",' in pp
    settings = json.loads((sample / ".claude/settings.json").read_text(encoding="utf-8"))
    hooks = settings["hooks"]["PostToolUse"]
    assert len(hooks) == 1 and "audit_kit hook" in hooks[0]["hooks"][0]["command"]
    assert (sample / ".claude/skills/audit/SKILL.md").is_file()
    assert "{{PYTHON}}" not in (sample / ".claude/skills/audit/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert (sample / ".gitignore").read_text(encoding="utf-8").count("audit-reports") == 1
    assert load_config(sample).packages == ["app"]


def test_init_keeps_existing_settings(sample):
    (sample / ".claude").mkdir()
    (sample / ".claude/settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(ls)"]}}), encoding="utf-8"
    )
    init_project(sample)
    data = json.loads((sample / ".claude/settings.json").read_text(encoding="utf-8"))
    assert data["permissions"]["allow"] == ["Bash(ls)"]
    assert "PostToolUse" in data["hooks"]


def test_init_only_auto_installs_pre_push_hook_type(sample, monkeypatch):
    """2026-09-27 실측으로 발견한 회귀: `--hook-type pre-commit`까지 자동 설치하면
    `audit-kit fix`(fixflow.py)가 세션 안에서 직접 만드는 커밋까지 훅이 걸려
    test_fixflow.py 의 fix start/done 이 깨졌다(훅이 파일을 조용히 고쳐써 미커밋 상태가 남음).
    그래서 init 은 pre-push 단계만 자동 설치해야 한다 — subprocess.run 에 실제로 넘어가는
    인자를 가로채서 고정한다(파이썬 코드가 아닌 pre-commit-config.yaml 은 ruff 검사 대상이
    아니라서, 이 테스트가 유일한 자동 회귀 방지선이다)."""
    (sample / ".git").mkdir()  # _install_precommit_hooks 는 .git 존재만 확인한다
    calls = []

    def fake_module_available(name):
        return name == "pre_commit"

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("audit_kit.init_project.module_available", fake_module_available)
    monkeypatch.setattr(subprocess, "run", fake_run)
    init_project(sample)
    assert len(calls) == 1
    assert calls[0][-2:] == ["--hook-type", "pre-push"]
    assert "pre-commit" not in calls[0][calls[0].index("install") + 1 :]


# ---------------------------------------------------------------- end-to-end
def _run(args, cwd, stdin=None):
    return subprocess.run(
        [sys.executable, "-m", "audit_kit", *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_end_to_end_run(sample):
    init_project(sample)
    p = _run(["run", "--solo"], sample)  # --solo: run 자체의 리포트 구조를 검증하는 테스트
    assert p.returncode == 1, p.stdout + p.stderr  # 치명 존재
    latest = (sample / "audit-reports/LATEST.txt").read_text(encoding="utf-8")
    data = json.loads(
        (sample / "audit-reports" / latest / "findings.json").read_text(encoding="utf-8")
    )
    status = {t["tool"]: t["status"] for t in data["tools"]}
    assert all(s != "error" for s in status.values()), status
    got = {(f["rule"], f["severity"]) for f in data["findings"]}
    for expected in [
        ("B608", CRITICAL),
        ("B602", CRITICAL),
        ("TEST-FAIL", CRITICAL),
        ("IMPORT-CYCLE", IMPROVE),
        ("assignment", IMPROVE),
        ("CC-C", IMPROVE),
        ("LAYER", REVIEW),
        ("ROUTER-LOGIC", REVIEW),
        ("SESSION-IN-FUNC", REVIEW),
        ("GLOBAL-SESSION", REVIEW),
        ("V-ARG", IGNORE),
    ]:
        assert expected in got, expected
    fail = next(f for f in data["findings"] if f["rule"] == "TEST-FAIL")
    assert fail["file"] == "tests/test_wall.py"
    for name in ("report.md", "ai-review.md", "deps.md"):
        assert (sample / "audit-reports" / latest / name).is_file()


def test_hook(sample):
    init_project(sample)
    pp = sample / "pyproject.toml"  # 이 PC 의 전역 hook 유무와 무관하게 ruff/mypy 포함 동작을 검사
    pp.write_text(
        pp.read_text(encoding="utf-8").replace(
            'hook_mode = "block"', 'hook_mode = "block"\nhook_tools = "all"'
        ),
        encoding="utf-8",
    )
    bad = json.dumps({"tool_input": {"file_path": str(sample / "app/services/quantity.py")}})
    p = _run(["hook"], sample, bad)
    assert p.returncode == 2 and "F841" in p.stderr and "assignment" in p.stderr
    clean = json.dumps({"tool_input": {"file_path": str(sample / "app/core/router.py")}})
    assert _run(["hook"], sample, clean).returncode == 0
    other = json.dumps({"tool_input": {"file_path": str(sample / "pyproject.toml")}})
    assert _run(["hook"], sample, other).returncode == 0

    pp = sample / "pyproject.toml"
    pp.write_text(
        pp.read_text(encoding="utf-8").replace('hook_mode = "block"', 'hook_mode = "warn"'),
        encoding="utf-8",
    )
    p = _run(["hook"], sample, bad)
    assert p.returncode == 0
    assert "F841" in json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]


def test_hook_reports_file_scoped_std_custom_checks_immediately(sample):
    """ruff/mypy 가 아닌 audit-kit 자체 조항(EFF-02 등, checks.FILE_CHECKS)도 저장 시점에
    바로 잡혀야 한다(2026-09-28, strat_move 분리로 EFF-02 를 놓친 사각지대를 메우며 추가)."""
    init_project(sample)
    target = sample / "app/services/eff02_sample.py"
    target.write_text(
        "def f(xs, allowed: list):\n"
        "    out = []\n"
        "    for x in xs:\n"
        "        if x in allowed:\n"
        "            out.append(x)\n"
        "    return out\n",
        encoding="utf-8",
    )
    data = json.dumps({"tool_input": {"file_path": str(target)}})
    p = _run(["hook"], sample, data)
    assert p.returncode == 2, p.stdout + p.stderr
    assert "[표준 EFF-02]" in p.stderr and "in` 검사" in p.stderr


def test_hook_file_paths_batch_checks_all_and_matches_single_file_calls(sample):
    """`tool_input.file_paths`(목록)로 여러 파일을 한 번에 보내면, 파일마다 따로
    `hook`을 부른 것과 같은 결과(둘 다 지적)를 한 프로세스 안에서 낸다(2026-10-08,
    PR #160 CI verify 90분 초과 — build_project_graph 를 파일마다 새 프로세스에서
    다시 계산하던 문제의 공식 해결책)."""
    init_project(sample)
    pp = sample / "pyproject.toml"
    pp.write_text(
        pp.read_text(encoding="utf-8").replace(
            'hook_mode = "block"', 'hook_mode = "block"\nhook_tools = "all"'
        ),
        encoding="utf-8",
    )
    bad = sample / "app/services/quantity.py"
    clean = sample / "app/core/router.py"

    single_bad = _run(["hook"], sample, json.dumps({"tool_input": {"file_path": str(bad)}}))
    single_clean = _run(["hook"], sample, json.dumps({"tool_input": {"file_path": str(clean)}}))

    batch = _run(
        ["hook"], sample, json.dumps({"tool_input": {"file_paths": [str(bad), str(clean)]}})
    )
    assert batch.returncode == 2, batch.stdout + batch.stderr
    assert "F841" in batch.stderr and "assignment" in batch.stderr
    assert single_bad.returncode == 2 and single_clean.returncode == 0
    assert "app/services/quantity.py" in batch.stderr.replace("\\", "/")
    # clean 파일은 지적이 없으니 배치 출력에 등장하지 않는다(파일별 구획)
    assert "app/core/router.py" not in batch.stderr.replace("\\", "/")

    only_clean = _run(["hook"], sample, json.dumps({"tool_input": {"file_paths": [str(clean)]}}))
    assert only_clean.returncode == 0


def test_hook_check_file_reuses_cached_graph_within_process(sample, monkeypatch):
    """같은 프로세스 안에서 여러 파일을 검사할 때 `build_project_graph`가 1회만 불려야 한다
    (파일당 약 8.7초짜리 전체 임포트 그래프 재계산이 원인이었던 느림, 2026-10-08 보고)."""
    init_project(sample)
    from audit_kit import hook
    from audit_kit import scope as scope_mod

    monkeypatch.setattr(hook, "_GRAPH_CACHE", {})
    calls = []
    real = scope_mod.build_project_graph

    def counting(cfg, *a, **kw):
        calls.append(1)
        return real(cfg, *a, **kw)

    monkeypatch.setattr(hook, "build_project_graph", counting)
    hook.check_file(sample / "app/services/quantity.py")
    hook.check_file(sample / "app/core/router.py")
    assert len(calls) == 1, "같은 cfg.root 에 대해 build_project_graph 가 두 번 불렸다"


# ---------------------------------------------------------------- pre-push (로컬 push 게이트)
def test_pre_push_blocks_on_failing_test(sample):
    """GitHub 개인 계정+비공개 저장소는 required status check API 가 막혀 있어(2026-09-27 실측:
    403 Upgrade to GitHub Pro) 서버 쪽 강제가 안 된다 — 그 대신 pre-push 단계에서 로컬로
    막는다. sample 픽스처는 실패하는 테스트 1개를 일부러 심어 둔 것(test_end_to_end_run 참고)."""
    init_project(sample)
    p = _run(["pre-push"], sample)
    assert p.returncode == 1, p.stdout + p.stderr
    assert "pytest 실패" in p.stderr and "test_fails" in p.stdout


def test_pre_push_blocks_on_critical_std_violation(sample):
    """테스트는 통과해도 std --fail-on critical 이 걸리면(subprocess shell=True) 막아야 한다."""
    init_project(sample)
    wall_test = sample / "tests/test_wall.py"
    wall_test.write_text(
        wall_test.read_text(encoding="utf-8").replace("== 2.0", "== 1.5"), encoding="utf-8"
    )
    p = _run(["pre-push"], sample)
    assert p.returncode == 1, p.stdout + p.stderr
    assert "2 passed" in p.stdout  # 테스트 단계는 통과
    assert "std 치명 위반" in p.stderr


def test_pre_push_passes_when_clean(sample):
    init_project(sample)
    wall_test = sample / "tests/test_wall.py"
    wall_test.write_text(
        wall_test.read_text(encoding="utf-8").replace("== 2.0", "== 1.5"), encoding="utf-8"
    )
    quantity = sample / "app/services/quantity.py"
    quantity.write_text(
        quantity.read_text(encoding="utf-8").replace(
            "subprocess.call(cmd, shell=True)", "subprocess.call(cmd.split())"
        ),
        encoding="utf-8",
    )
    p = _run(["pre-push"], sample)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "통과" in p.stdout

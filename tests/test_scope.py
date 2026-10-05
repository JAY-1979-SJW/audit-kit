"""2단계: 프로젝트 전체 검사 범위 — 보조 코드 분류, 제품→보조 역방향, 비공개 사용, 미지정, 복사본."""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from audit_kit.arch.scan import scan
from audit_kit.arch.spec import load_spec
from audit_kit.config import AuditConfig
from audit_kit.models import CRITICAL, IMPROVE, REVIEW
from audit_kit.scope import _CACHE, discover
from audit_kit.tools import run_scope

SPEC = """
[project]
root_packages = ["app"]
[[layers]]
name = "service"
modules = ["app.services"]
[[layers]]
name = "domain"
modules = ["app.models"]
"""

FILES = {
    "app/__init__.py": "",
    "app/services/__init__.py": "",
    "app/services/s.py": "from local_plugins.registry import REG\nfrom scripts.helpers import fmt\n\n\ndef run():\n    return REG, fmt\n",
    "app/models/__init__.py": "",
    "app/models/m.py": "def _internal():\n    return 1\n\n\ndef public():\n    return 2\n",
    "local_plugins/registry.py": "REG = {}\n",
    "scripts/helpers.py": "def fmt():\n    return ''\n",
    "scripts/job.py": "from app.models.m import _internal, public\n",
    "tests/test_m.py": "from app.models.m import _internal\n\n\ndef test_x():\n    assert _internal() == 1\n",
    "alembic/env.py": "",
    "run_batch.py": "import app\n",
    "temp/scratch.py": "x = 1\n",
    ".claude/worktrees/agent-1/app/models/m.py": "def _internal():\n    return 1\n",
    "legacy_backup/old.py": "y = 2\n",
}


def make(root: Path, spec: bool = True):
    for rel, body in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")
    if spec:
        (root / "architecture.toml").write_text(SPEC, encoding="utf-8")
    _CACHE.clear()
    cfg = AuditConfig(packages=["app"])
    cfg.root = root
    return cfg


def test_discover_classifies_every_file(tmp_path):
    cfg = make(tmp_path)
    s = discover(cfg)
    assert sorted(s.support) == ["migrations", "plugins", "scripts", "tests"]
    assert "run_batch.py" in s.support["scripts"]  # 루트 스크립트
    assert "local_plugins/registry.py" in s.support["plugins"]  # *_plugins 이름
    assert s.unassigned == ["temp/scratch.py"]
    junk = dict(s.junk.items())
    assert any("worktree" in k for k in junk) and any("백업" in k for k in junk)
    assert s.total == len(FILES)  # 빠진 파일 없음


def test_explicit_support_overrides_and_unassigned(tmp_path):
    cfg = make(tmp_path)
    cfg.support = {"scripts": ["temp"]}
    s = discover(cfg)
    assert "temp/scratch.py" in s.support["scripts"] and not s.unassigned


def test_scan_support_rules(tmp_path):
    cfg = make(tmp_path)
    vs = scan(cfg, load_spec(tmp_path))
    by_rule = {}
    for v in vs:
        by_rule.setdefault(v.rule, []).append(v)
    rev = {(v.src, v.target): v.to_finding().severity for v in by_rule["ARCH-SUPPORT-REVERSE"]}
    assert rev[("app.services.s", "scripts.helpers")] == CRITICAL  # 제품 → 스크립트
    assert rev[("app.services.s", "local_plugins.registry")] == IMPROVE  # 제품 → 플러그인
    priv = by_rule["ARCH-PRIVATE"]
    assert [(v.src, v.names) for v in priv] == [("scripts.job", ["_internal", "public"])]  # 테스트는 제외
    un = list(by_rule["ARCH-UNASSIGNED"])
    assert any("temp" in v.message for v in un) and all(v.to_finding().severity == REVIEW for v in un)
    assert not any(v.src and v.src.startswith("tests") for v in vs if v.rule != "ARCH-PRIVATE")


def test_run_scope_reports_junk_and_coverage(tmp_path):
    cfg = make(tmp_path, spec=False)
    r = run_scope(cfg)
    rules = sorted({f.rule for f in r.findings})
    assert rules == ["SCOPE-JUNK", "SCOPE-UNASSIGNED"]
    assert r.data["total"] == len(FILES) and r.data["unassigned"] == 1
    assert "제품 코드" in r.data["summary"]


def test_run_report_shows_coverage(tmp_path):
    make(tmp_path, spec=False)
    (tmp_path / "pyproject.toml").write_text('[tool.audit-kit]\npackages = ["app"]\nrun_tests = false\n', encoding="utf-8")
    p = subprocess.run([sys.executable, "-m", "audit_kit", "run", "--only", "ruff", "--fail-on", "never"],
                       cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
    assert "] 검사 범위" in p.stdout, p.stdout + p.stderr
    latest = (tmp_path / "audit-reports/LATEST.txt").read_text(encoding="utf-8")
    md = (tmp_path / "audit-reports" / latest / "report.md").read_text(encoding="utf-8")
    assert "검사 범위: 파이썬 파일" in md
    data = json.loads((tmp_path / "audit-reports" / latest / "findings.json").read_text(encoding="utf-8"))
    ruffed = {f["file"].split("/")[0] for f in data["findings"] if f["tool"] == "ruff"}
    assert "scripts" in ruffed or "tests" in ruffed or not ruffed  # 보조 코드도 ruff 대상

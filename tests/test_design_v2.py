"""1단계-B: 설계 모델 v2 — 프로젝트 유형 템플릿, 계층 책임, 공통 관심사 위치(ARCH-CONCERN)."""

from pathlib import Path

from audit_kit.arch.concerns import find_uses
from audit_kit.arch.scan import scan
from audit_kit.arch.spec import infer_spec, load_spec, render_spec
from audit_kit.arch.templates import detect_types, merged
from audit_kit.config import AuditConfig
from audit_kit.scope import _CACHE, build_project_graph, get_scope

FILES = {
    "app/__init__.py": "",
    "app/api/__init__.py": "",
    "app/api/routes.py": ("import os\nfrom fastapi import APIRouter\n\nrouter = APIRouter()\n"
                          "TOKEN = os.getenv('TOKEN')\n"),
    "app/services/__init__.py": "",
    "app/services/job.py": ("import requests\nfrom win32com.client import Dispatch\n\n\n"
                            "def run():\n    acad = Dispatch('AutoCAD.Application')\n    return requests.get('http://x')\n"),
    "app/core/__init__.py": "",
    "app/core/config.py": "import os\n\nDB_URL = os.environ['DB_URL']\nDEBUG = os.environ.get('DEBUG')\n",
    "app/core/com_session.py": "import win32com.client\n\n\ndef connect():\n    return win32com.client.GetActiveObject('x')\n",
    "app/main.py": "import logging\n\nlogging.basicConfig(level=logging.INFO)\n",
}


def make(root: Path):
    for rel, body in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    _CACHE.clear()
    cfg = AuditConfig(packages=["app"])
    cfg.root = root
    return cfg


def test_detect_and_merge_types():
    assert detect_types({"fastapi": 3, "win32com": 5}) == ["cad", "fastapi"]
    assert detect_types({}) == ["library"]
    m = merged(["cad", "fastapi"])
    assert "COM(win32com)" in m["layers"]["domain"]["must_not"]
    assert m["concerns"]["com"] == "infra" and m["concerns"]["db"] == "infra"


def test_find_concern_uses_resolves_aliases(tmp_path):
    cfg = make(tmp_path)
    uses = {(u.concern, u.module) for u in find_uses(build_project_graph(cfg))}
    assert ("com", "app.services.job") in uses  # from win32com.client import Dispatch → Dispatch(...)
    assert ("com", "app.core.com_session") in uses  # win32com.client.GetActiveObject(...)
    assert ("config", "app.core.config") in uses  # os.environ[...] 첨자, os.environ.get
    assert ("http", "app.services.job") in uses
    assert ("logging", "app.main") in uses


def test_infer_v2_and_concern_violations(tmp_path):
    cfg = make(tmp_path)
    graph = build_project_graph(cfg)
    spec, _ = infer_spec(graph, ["app"], get_scope(cfg))
    assert spec.types[:2] == ["cad", "fastapi"]
    assert spec.concerns["config"] == ["app.core.config"]  # 이름이 전용 모듈
    assert spec.concerns["com"] == ["app.core.com_session"]  # 가장 많이 쓰는 곳이 아니라 전용 모듈
    infra = next(lay for lay in spec.layers if lay.name == "infra")
    assert "COM 어댑터" in infra.responsibility
    (tmp_path / "architecture.toml").write_text(render_spec(spec, []), encoding="utf-8")
    loaded = load_spec(tmp_path)  # 왕복: 파일로 쓰고 다시 읽어도 같은 설계
    assert loaded.types == spec.types and loaded.concerns == spec.concerns
    assert next(lay for lay in loaded.layers if lay.name == "infra").must_not == infra.must_not
    vs = [v for v in scan(cfg, loaded) if v.rule == "ARCH-CONCERN"]
    got = {(v.src, v.target) for v in vs}
    assert ("app.api.routes", "config") in got  # 라우터가 환경 변수를 직접 읽음
    assert ("app.services.job", "com") in got  # 서비스가 COM 을 직접 연결
    assert not any(v.src == "app.main" for v in vs)  # 진입점은 허용
    assert not any(v.src in ("app.core.config", "app.core.com_session") for v in vs)  # 지정 위치는 허용

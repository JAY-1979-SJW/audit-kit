"""1단계: 파일 형식 보존 (줄바꿈·인코딩·BOM), 설정 병합 안전성, hook 입력 처리."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from audit_kit.arch.fix import run_fix, write_back
from audit_kit.arch.spec import load_spec
from audit_kit.config import AuditConfig
from audit_kit.init_project import SettingsError, init_project, merge_settings
from audit_kit.textio import TextFormat, decode, encode, read_source

CRLF, LF = b"\r\n", b"\n"

SPEC = """
[project]
root_packages = ["pkg"]
[[layers]]
name = "service"
modules = ["pkg.services"]
[[layers]]
name = "domain"
modules = ["pkg.models"]
"""


@pytest.mark.parametrize("raw,enc,bom,nl", [
    (b"a = 1\nb = 2\n", "utf-8", False, "\n"),
    (b"a = 1\r\nb = 2\r\n", "utf-8", False, "\r\n"),
    (b"\xef\xbb\xbfa = 1\n", "utf-8", True, "\n"),
    ("# 한글\nX = 1\n".encode("cp949"), "cp949", False, "\n"),
    ("# 한글\r\nX = 1\r\n".encode(), "utf-8", False, "\r\n"),
])
def test_roundtrip_is_byte_identical(raw, enc, bom, nl):
    text, fmt = decode(raw)
    assert "\r" not in text
    assert (fmt.encoding, fmt.bom, fmt.newline) == (enc, bom, nl)
    assert encode(text, fmt) == raw


def test_encode_rejects_unrepresentable_char():
    with pytest.raises(UnicodeEncodeError):
        encode("x = '😀'\n", TextFormat("cp949"))


def _project(root: Path, files: dict, eol: bytes = LF, encoding: str = "utf-8"):
    for rel, body in {"pkg/__init__.py": "", "pkg/services/__init__.py": "", "pkg/models/__init__.py": "",
                      **files}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body.encode(encoding).replace(b"\n", eol))
    (root / "architecture.toml").write_text(SPEC, encoding="utf-8")
    cfg = AuditConfig(packages=["pkg"])
    cfg.root = root
    return cfg, load_spec(root)


MOVE_CASE = {
    "pkg/services/s.py": "# 서비스\n\n\ndef price():\n    return 2\n",
    "pkg/models/m.py": "# 모델\nfrom pkg.services.s import price\n\nP = price()\n",
}


@pytest.mark.parametrize("eol", [LF, CRLF])
def test_arch_fix_preserves_line_endings(tmp_path, eol):
    cfg, spec = _project(tmp_path, MOVE_CASE, eol)
    res = run_fix(cfg, spec, import_check=False)
    try:
        assert [a.kind for a in res.applied] == ["move"]
        write_back(res, tmp_path / "backup")
    finally:
        res.workspace.cleanup()
    for rel in MOVE_CASE:
        raw = (tmp_path / rel).read_bytes()
        crlf = raw.count(CRLF)
        lf_only = raw.count(LF) - crlf
        assert (lf_only == 0) if eol == CRLF else (crlf == 0), (rel, raw)
    assert b"def price" in (tmp_path / "pkg/models/m.py").read_bytes()


def test_arch_fix_handles_cp949_files(tmp_path):
    cfg, spec = _project(tmp_path, {**MOVE_CASE, "pkg/models/legacy.py": "# 구형 파일\nY = 1\n"}, encoding="cp949")
    res = run_fix(cfg, spec, import_check=False)  # cp949 파일이 있어도 멈추지 않아야 함
    try:
        assert [a.kind for a in res.applied] == ["move"]
        write_back(res, tmp_path / "backup")
    finally:
        res.workspace.cleanup()
    text, fmt = read_source(tmp_path / "pkg/models/m.py")
    assert fmt.encoding == "cp949" and "# 모델" in text and "def price" in text  # 원래 인코딩 유지
    assert read_source(tmp_path / "pkg/models/legacy.py")[1].encoding == "cp949"


def test_init_keeps_line_endings_of_existing_files(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/__init__.py").write_bytes(b"")
    (tmp_path / "pyproject.toml").write_bytes(b'[project]\nname = "x"\n')  # LF
    (tmp_path / ".gitignore").write_bytes(b"*.pyc\r\n.venv/\r\n")  # CRLF
    init_project(tmp_path, with_precommit=False)
    pp = (tmp_path / "pyproject.toml").read_bytes()
    gi = (tmp_path / ".gitignore").read_bytes()
    assert CRLF not in pp and b"[tool.audit-kit]" in pp
    assert gi.count(LF) == gi.count(CRLF) and b"audit-reports/" in gi


def test_merge_settings_never_overwrites_broken_json(tmp_path):
    s = tmp_path / ".claude" / "settings.json"
    s.parent.mkdir()
    broken = b'{ "permissions": { "allow": ["Bash(ls)"], } '
    s.write_bytes(broken)
    with pytest.raises(SettingsError):
        merge_settings(s, "cmd")
    assert s.read_bytes() == broken
    log = init_project(tmp_path, packages=["pkg"], with_precommit=False)
    assert any("hook 등록 건너뜀" in line for line in log)
    assert s.read_bytes() == broken


def _hook(stdin: bytes):
    return subprocess.run([sys.executable, "-m", "audit_kit", "hook"], input=stdin, capture_output=True)


def test_global_lint_hook_detection(tmp_path):
    from audit_kit.hook import global_lint_hook_present

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    s = home / ".claude" / "settings.json"
    assert not global_lint_hook_present(home)  # 설정 없음
    s.write_text(json.dumps({"hooks": {"PostToolUse": [{"hooks": [
        {"type": "command", "command": '"py" -m audit_kit hook'}]}]}}), encoding="utf-8")
    assert not global_lint_hook_present(home)  # audit-kit 자신은 제외
    s.write_text(json.dumps({"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [
        {"type": "command", "command": 'py -3.14 "C:/x/.claude/hooks/py_post_edit.py"'}]}]}}), encoding="utf-8")
    assert global_lint_hook_present(home)
    s.write_text("{ broken", encoding="utf-8")
    assert not global_lint_hook_present(home)


def test_hook_accepts_bom_and_reports_bad_input(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.audit-kit]\nhook_tools = "all"\n', encoding="utf-8")
    f = tmp_path / "x.py"
    f.write_text("import os\n", encoding="utf-8")
    payload = json.dumps({"tool_input": {"file_path": str(f)}}).encode("utf-8")
    assert _hook(b"\xef\xbb\xbf" + payload).returncode == 2  # BOM 있어도 검사함 (F401)
    bad = _hook(b"not json")
    assert bad.returncode == 1 and "검사하지 않았습니다" in bad.stderr.decode("utf-8")
    assert _hook(b"").returncode == 0

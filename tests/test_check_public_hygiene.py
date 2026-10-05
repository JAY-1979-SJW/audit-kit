"""scripts/check_public_hygiene.py 시험: 가상 임시 git 저장소에서 위반·정상 파일로 검증한다."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_public_hygiene.py"
FAKE = "FORBIDDEN_TOKEN_X"  # 가상 금지어(시험 전용)

# 위반 문자열은 조각을 이어 붙여 만든다(이 시험 파일 자체가 일반 규칙에 걸리지 않게).
WIN_PATH = "D" + ":" + "\\" + "data" + "\\" + "file.txt"
EMAIL = "someone" + "@" + "example" + ".com"
PRIV_KEY = "-----" + "BEGIN RSA PRIVATE KEY" + "-----"
GH_TOKEN = "ghp" + "_" + "a" * 36
AWS_KEY = "AKIA" + "A" * 16
HOME_DIR = "/" + "home" + "/" + "someuser" + "/" + "project"


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def make_repo(tmp_path: Path, files: dict[str, str], *, track: bool = True) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
        if track:
            git(root, "add", rel)
    return root


def run(root: Path, *args: str, env_patterns: str | None = None):
    env = {k: v for k, v in os.environ.items() if k != "HYGIENE_PATTERNS"}
    if env_patterns is not None:
        env["HYGIENE_PATTERNS"] = env_patterns
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


@pytest.mark.parametrize(
    ("content", "rule"),
    [
        (f"path = '{WIN_PATH}'\n", "PH-WINPATH"),
        (f"mail {EMAIL}\n", "PH-EMAIL"),
        (f"{PRIV_KEY}\n", "PH-PRIVKEY"),
        (f"t = {GH_TOKEN}\n", "PH-TOKEN"),
        (f"k = {AWS_KEY}\n", "PH-TOKEN"),
        (f"cd {HOME_DIR}\n", "PH-HOMEDIR"),
    ],
)
def test_general_rules_detect(tmp_path, content, rule):
    root = make_repo(tmp_path, {"a.txt": "ok\n" + content})
    r = run(root)
    assert r.returncode == 1
    assert f"a.txt:2: {rule}" in r.stdout


def test_clean_repo_passes(tmp_path):
    root = make_repo(tmp_path, {"a.txt": "hello\n", "b.py": "x = 1\n"})
    r = run(root)
    assert r.returncode == 0


def test_untracked_files_not_scanned(tmp_path):
    root = make_repo(tmp_path, {"a.txt": "ok\n"})
    (root / "u.txt").write_text(f"{EMAIL}\n", encoding="utf-8")
    assert run(root).returncode == 0


def test_no_patterns_notice_and_general_result(tmp_path):
    root = make_repo(tmp_path, {"a.txt": f"{FAKE}\n"})
    r = run(root)
    assert "패턴 미설정" in r.stdout
    assert r.returncode == 0  # 일반 규칙 위반이 없으므로


def test_env_patterns_injection(tmp_path):
    root = make_repo(tmp_path, {"a.txt": f"x\nuse {FAKE} here\n", "b.txt": "clean\n"})
    r = run(root, env_patterns=f"unrelated_zzz\n{FAKE}\n")
    assert r.returncode == 1
    assert "a.txt:2: PH-CUSTOM-2" in r.stdout
    assert "b.txt" not in r.stdout
    assert "패턴 미설정" not in r.stdout
    assert FAKE not in r.stdout + r.stderr


def test_patterns_file_injection(tmp_path):
    root = make_repo(tmp_path, {"a.txt": f"{FAKE}\n"})
    pf = tmp_path / "outside_patterns.txt"
    pf.write_text(f"# 주석\n\n{FAKE}\n", encoding="utf-8")
    r = run(root, "--patterns-file", str(pf))
    assert r.returncode == 1
    assert "a.txt:1: PH-CUSTOM-1" in r.stdout
    assert FAKE not in r.stdout + r.stderr


def test_custom_pattern_applies_to_paths(tmp_path):
    root = make_repo(tmp_path, {f"dir_{FAKE}/f.txt": "ok\n"})
    r = run(root, env_patterns=FAKE)
    assert r.returncode == 1
    assert "PH-CUSTOM-1" in r.stdout


def test_patterns_file_inside_repo_rejected(tmp_path):
    root = make_repo(tmp_path, {"a.txt": "ok\n"})
    pf = root / "p.txt"
    pf.write_text(FAKE + "\n", encoding="utf-8")
    r = run(root, "--patterns-file", str(pf))
    assert r.returncode == 2


def test_patterns_file_missing_and_bad_regex(tmp_path):
    root = make_repo(tmp_path, {"a.txt": "ok\n"})
    assert run(root, "--patterns-file", str(tmp_path / "nope.txt")).returncode == 2
    assert run(root, env_patterns="(unclosed").returncode == 2


def test_allowlist_by_line_and_regex(tmp_path):
    files = {
        "doc.md": f"x\n{WIN_PATH}\n{EMAIL}\n{EMAIL}\n",
        "scripts/hygiene_allowlist.txt": "# 예제값\ndoc.md:2\ndoc.md:someone\\S+example\n",
    }
    root = make_repo(tmp_path, files)
    r = run(root)
    # 2번 줄은 줄 번호로, 3·4번 줄은 정규식으로 허용되어 위반 없음
    assert r.returncode == 0, r.stdout


def test_allowlist_is_scoped_to_path_and_line(tmp_path):
    files = {
        "doc.md": f"{EMAIL}\n{EMAIL}\n",
        "other.md": f"{EMAIL}\n",
        "scripts/hygiene_allowlist.txt": "doc.md:1\n",
    }
    root = make_repo(tmp_path, files)
    r = run(root)
    assert r.returncode == 1
    assert "doc.md:2: PH-EMAIL" in r.stdout
    assert "doc.md:1:" not in r.stdout
    assert "other.md:1: PH-EMAIL" in r.stdout


def test_output_never_contains_line_content(tmp_path):
    marker = "UNIQUE_MARKER_VALUE_12345"
    root = make_repo(tmp_path, {"a.txt": f"pw {marker} {EMAIL} {WIN_PATH}\n"})
    r = run(root)
    assert r.returncode == 1
    out = r.stdout + r.stderr
    for leaked in (marker, EMAIL, WIN_PATH, "someone"):
        assert leaked not in out


def test_binary_file_skipped(tmp_path):
    root = make_repo(tmp_path, {"a.txt": "ok\n"})
    (root / "b.bin").write_bytes(b"\x00\x01" + EMAIL.encode() + b"\x00")
    git(root, "add", "b.bin")
    assert run(root).returncode == 0

"""scripts/sync_standard.py — 스크러브(이름 치환) 단계: fail-closed, 치환, 금지어 검사, 출력에 내용 미노출.

사설 이름 목록은 저장소에 두지 않으므로 이 시험은 가상 이름(PRIVATE_NAME_X)과 임시 치환표를 쓴다.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_standard.py"
_spec = importlib.util.spec_from_file_location("sync_standard_under_test", SCRIPT)
assert _spec and _spec.loader
ss = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ss
_spec.loader.exec_module(ss)

NAME = "PRIVATE_NAME_X"  # 가상 사설 이름(시험 전용)


def make_source(root: Path, extra: str = "") -> Path:
    src = root / "source"
    (src / "standard").mkdir(parents=True)
    (src / "project").mkdir()
    (src / "standard" / "rules.toml").write_bytes(
        (f'source = "{NAME} 실측 사례 — 외부 근거 문서 없음"\r\n# {NAME} 세션에서 본 것\r\n' + extra).encode("utf-8")
    )
    (src / "standard" / "docs_registry.toml").write_bytes(b"# registry\r\n")
    (src / "project" / "ruff.toml").write_bytes(f"# {NAME} 실측\n".encode("utf-8"))
    return src


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "check_public_hygiene.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    return repo


def write_map(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def run_sync(tmp_path: Path, source: Path, scrub_map: Path, forbidden: Path | None = None, repo: Path | None = None) -> int:
    repo = repo or make_repo(tmp_path)
    return ss.sync(
        source,
        target=repo / "data",
        scrub_map=scrub_map,
        forbidden=forbidden or (tmp_path / "no_forbidden.txt"),
        root=repo,
        hygiene=False,
    )


def test_missing_scrub_map_refuses_and_writes_nothing(tmp_path, capsys):
    src = make_source(tmp_path)
    repo = make_repo(tmp_path)
    assert run_sync(tmp_path, src, tmp_path / "absent.txt", repo=repo) == 1
    assert "거부" in capsys.readouterr().err
    assert not (repo / "data").exists()  # fail-closed: 아무것도 쓰지 않는다


def test_empty_scrub_map_refuses(tmp_path, capsys):
    src = make_source(tmp_path)
    assert run_sync(tmp_path, src, write_map(tmp_path / "map.txt", "# 주석만\n\n")) == 1
    assert "규칙이 없습니다" in capsys.readouterr().err


def test_bad_regex_or_format_refuses(tmp_path, capsys):
    src = make_source(tmp_path)
    repo = make_repo(tmp_path)
    assert run_sync(tmp_path, src, write_map(tmp_path / "m1.txt", "([ => x\n"), repo=repo) == 1
    assert run_sync(tmp_path, src, write_map(tmp_path / "m2.txt", "no separator here\n"), repo=repo) == 1
    assert "오류" in capsys.readouterr().err


def test_scrub_replaces_names_and_preserves_line_endings(tmp_path):
    src = make_source(tmp_path)
    rules = f'(?m)^source = ".*실측.*외부 근거 문서 없음.*"(?=\\r?$) => source = "실측 사례 — 외부 근거 문서 없음"\n{NAME} 세션에서 => 다른 프로젝트 세션에서\n{NAME} 실측 => 다른 프로젝트 실측\n'
    repo = make_repo(tmp_path)
    assert run_sync(tmp_path, src, write_map(tmp_path / "map.txt", rules), repo=repo) == 0
    rules_out = (repo / "data" / "rules.toml").read_bytes().decode("utf-8")
    assert NAME not in rules_out
    assert rules_out == 'source = "실측 사례 — 외부 근거 문서 없음"\r\n# 다른 프로젝트 세션에서 본 것\r\n'  # CRLF 보존
    assert (repo / "data" / "ruff.toml").read_bytes().decode("utf-8") == "# 다른 프로젝트 실측\n"  # LF 보존
    assert (repo / "data" / "SOURCE_VERSION.json").is_file()


def test_forbidden_term_left_after_scrub_fails_without_echoing_content(tmp_path, capsys):
    src = make_source(tmp_path, extra=f"# 또 다른 곳: {NAME}\r\n")
    map_path = write_map(tmp_path / "map.txt", f"{NAME} 세션에서 => 다른 프로젝트 세션에서\n")
    forbidden = write_map(tmp_path / "forbidden.txt", f"{NAME}\n")
    assert run_sync(tmp_path, src, map_path, forbidden) == 1
    err = capsys.readouterr().err
    assert "금지어 검사 실패" in err and "rules.toml:" in err
    assert NAME not in err  # 걸린 줄 내용·패턴을 출력하지 않는다


def test_forbidden_scan_covers_tracked_repo_files(tmp_path, capsys):
    src = make_source(tmp_path)
    repo = make_repo(tmp_path)
    (repo / "README.md").write_text(f"leak {NAME}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    map_path = write_map(tmp_path / "map.txt", f"{NAME} 세션에서 => 다른 프로젝트 세션에서\n{NAME} 실측 => 다른 프로젝트 실측\n")
    forbidden = write_map(tmp_path / "forbidden.txt", f"{NAME}"+chr(10))
    assert run_sync(tmp_path, src, map_path, forbidden, repo=repo) == 1
    assert "README.md:1" in capsys.readouterr().err


def test_self_matching_pattern_is_not_a_forbidden_term(tmp_path):
    rules = ss.load_scrub_rules(write_map(tmp_path / "map.txt", '(?m)^source = ".*" => source = "x"\nfoo_private => bar\n'))
    patterns = ss.load_forbidden(tmp_path / "none.txt", rules)
    assert [p.pattern for p in patterns] == ["foo_private"]  # 자기 치환 결과에도 맞는 첫 규칙은 금지어에서 빠진다


def test_forbidden_lines_returns_numbers_only():
    assert ss.forbidden_lines("a\nbad here\nc\nbad again\n", [re.compile("bad")]) == [2, 4]

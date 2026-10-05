"""scripts/summarize_mutation.py 시험: mutmut-cicd-stats.json 을 사람이 읽을 요약으로 바꾸는지."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "summarize_mutation.py"
    spec = importlib.util.spec_from_file_location("_summarize_mutation_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sm = _load()


def test_all_killed_has_no_warning():
    text = sm.summarize({"killed": 10, "survived": 0, "total": 10})
    assert "100.0%" in text


def test_score_uses_tested_count_not_file_total():
    """실측(2026-09-27): total 은 파일 전체 누적 변이체라, 패턴으로 좁히면 total 이 커도
    점수는 이번에 검사한 것(killed+survived)만으로 계산해야 한다."""
    text = sm.summarize({"killed": 3, "survived": 1, "total": 123})
    assert "75.0%" in text
    assert "이번에 검사한 변이체: 4" in text
    assert "아직 검사 안 한 것" in text


def test_survivors_are_flagged():
    text = sm.summarize({"killed": 7, "survived": 3, "total": 10})
    assert "70.0%" in text
    assert "생존한 변이체 3개" in text


def test_zero_total_does_not_divide_by_zero():
    text = sm.summarize({"killed": 0, "survived": 0, "total": 0})
    assert "0.0%" in text


def test_main_writes_missing_file_message(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert sm.main() == 0
    assert "결과 파일이 없습니다" in summary.read_text(encoding="utf-8")


def test_main_writes_summary_from_real_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mutants").mkdir()
    (tmp_path / "mutants" / "mutmut-cicd-stats.json").write_text(
        json.dumps({"killed": 5, "survived": 1, "total": 6}), encoding="utf-8"
    )
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert sm.main() == 0
    text = summary.read_text(encoding="utf-8")
    assert "생존한 변이체 1개" in text


def test_main_handles_corrupted_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mutants").mkdir()
    (tmp_path / "mutants" / "mutmut-cicd-stats.json").write_text("{broken", encoding="utf-8")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert sm.main() == 0
    assert "읽지 못했습니다" in summary.read_text(encoding="utf-8")

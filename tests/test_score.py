"""`run` 감사 결과 → 0~100 코드 건강도 점수 테스트."""

from pathlib import Path

from audit_kit.config import AuditConfig
from audit_kit.score import compute_health_score, count_loc


def _write(path: Path, lines: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"x = {i}" for i in range(lines)) + "\n", encoding="utf-8")


def test_count_loc_src_layout(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "app" / "a.py", 10)
    _write(tmp_path / "src" / "app" / "b.py", 5)
    cfg = AuditConfig(root=tmp_path, packages=["app"])
    assert count_loc(cfg) == 15


def test_count_loc_flat_layout(tmp_path: Path) -> None:
    _write(tmp_path / "app" / "a.py", 7)
    cfg = AuditConfig(root=tmp_path, packages=["app"])
    assert count_loc(cfg) == 7


def test_count_loc_ignores_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "src" / "app" / "a.py"
    path.parent.mkdir(parents=True)
    path.write_text("x = 1\n\n\ny = 2\n", encoding="utf-8")
    cfg = AuditConfig(root=tmp_path, packages=["app"])
    assert count_loc(cfg) == 2


def test_count_loc_excludes_excluded_dirs(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "app" / "a.py", 10)
    _write(tmp_path / "src" / "app" / "tests" / "test_a.py", 100)
    cfg = AuditConfig(root=tmp_path, packages=["app"])
    assert count_loc(cfg) == 10


def test_count_loc_missing_package_is_zero(tmp_path: Path) -> None:
    cfg = AuditConfig(root=tmp_path, packages=["nope"])
    assert count_loc(cfg) == 0


def test_perfect_project_scores_100_grade_a(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "app" / "a.py", 1000)
    cfg = AuditConfig(root=tmp_path, packages=["app"])
    result = compute_health_score(cfg, {})
    assert result.score == 100.0
    assert result.grade == "A"
    assert result.loc == 1000


def test_zero_loc_is_worst_grade(tmp_path: Path) -> None:
    cfg = AuditConfig(root=tmp_path, packages=["nope"])
    result = compute_health_score(cfg, {"critical": 1})
    assert result.score == 0.0
    assert result.grade == "E"


def test_critical_findings_weigh_more_than_improve(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "app" / "a.py", 1000)
    cfg = AuditConfig(root=tmp_path, packages=["app"])
    critical = compute_health_score(cfg, {"critical": 1})
    improve = compute_health_score(cfg, {"improve": 1})
    assert critical.score < improve.score


def test_ignore_severity_does_not_affect_score(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "app" / "a.py", 1000)
    cfg = AuditConfig(root=tmp_path, packages=["app"])
    result = compute_health_score(cfg, {"ignore": 50})
    assert result.score == 100.0


def test_score_never_goes_below_zero(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "app" / "a.py", 10)
    cfg = AuditConfig(root=tmp_path, packages=["app"])
    result = compute_health_score(cfg, {"critical": 1000})
    assert result.score == 0.0
    assert result.grade == "E"


def test_findings_by_severity_is_preserved(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "app" / "a.py", 1000)
    cfg = AuditConfig(root=tmp_path, packages=["app"])
    result = compute_health_score(cfg, {"critical": 2, "improve": 3})
    assert result.findings_by_severity == {"critical": 2, "improve": 3}


def test_grade_band_boundaries(tmp_path: Path) -> None:
    # loc=1000, weight(improve)=2 이므로 improve N개 -> score = 100 - 2N.
    _write(tmp_path / "src" / "app" / "a.py", 1000)
    cfg = AuditConfig(root=tmp_path, packages=["app"])
    cases = [
        (0, "A"),  # score 100
        (5, "A"),  # score 90 (A 경계)
        (10, "B"),  # score 80 (B 경계)
        (15, "C"),  # score 70
        (20, "D"),  # score 60
        (26, "E"),  # score 48
    ]
    for n, expected_grade in cases:
        result = compute_health_score(cfg, {"improve": n})
        assert result.grade == expected_grade, f"n={n}: score={result.score}"

"""`run` 감사 결과를 0~100 코드 건강도 점수 하나로 요약한다.

SonarQube 유지보수성 등급(docs.sonarsource.com "Metric definitions": 기술부채비율 = 고치는
비용 / 개발 비용, A~E 등급)과 CodeClimate Quality GPA(codeclimate.com/changelog "Quality
GPA": 등급을 코드량으로 가중평균)의 핵심 아이디어 — "심각도별 이슈를 코드량 대비 밀도로 보고
등급을 매긴다" — 를 그대로 가져오되, SonarQube의 "이슈별 분당 수리비용 DB" 같은 무거운 부분은
뺐다. 그래서 등급 구간(A/B/C/D/E)은 SonarQube 수치를 그대로 베낀 게 아니라 이 도구 자체 기준으로
새로 잡은 값이다(단위가 다르므로 그대로 옮기면 의미가 없다) — 이 점을 정직하게 밝혀 둔다.

산식: 심각도별 findings 개수에 가중치를 곱해 합산 → 코드 1000줄당 밀도로 정규화 →
100점에서 그만큼 뺀다. LOC 를 못 세면(빈 프로젝트 등) 점수를 매길 근거가 없으므로 가장
낮은 등급(E)으로 안전하게 처리한다(거짓으로 좋은 점수를 주지 않는다).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from audit_kit.config import EXCLUDE_DIRS, AuditConfig
from audit_kit.models import CRITICAL, IGNORE, IMPROVE, REVIEW

# 심각도별 가중치: 치명은 개선의 5배, AI 리뷰 필요는 개선의 절반, 무시는 0(점수에 안 넣음).
SEVERITY_WEIGHT: dict[str, float] = {CRITICAL: 10.0, IMPROVE: 2.0, REVIEW: 1.0, IGNORE: 0.0}

# 이 도구 자체 기준의 등급 구간(점수 기준, SonarQube 원래 비율과 단위가 달라 그대로 옮기지 않음).
_GRADE_BANDS: tuple[tuple[float, str], ...] = ((90, "A"), (80, "B"), (65, "C"), (50, "D"))


@dataclass(frozen=True)
class HealthScore:
    score: float  # 0(최악)~100(완벽)
    grade: str  # A~E
    loc: int
    findings_by_severity: dict[str, int] = field(default_factory=dict)


def _grade_for(score: float) -> str:
    for minimum, grade in _GRADE_BANDS:
        if score >= minimum:
            return grade
    return "E"


def count_loc(cfg: AuditConfig) -> int:
    """`cfg.packages` 아래 `.py` 파일의 공백 아닌 줄 수 합계(src 레이아웃·평면 레이아웃 둘 다 지원)."""
    total = 0
    for pkg in cfg.packages:
        base = next(
            (c for c in (cfg.root / "src" / pkg, cfg.root / pkg) if c.is_dir()),
            None,
        )
        if base is None:
            continue
        for path in base.rglob("*.py"):
            if any(part in EXCLUDE_DIRS for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            total += sum(1 for line in text.splitlines() if line.strip())
    return total


def compute_health_score(cfg: AuditConfig, by_severity: dict[str, int]) -> HealthScore:
    """`by_severity` 는 `write_reports()`가 돌려주는 `info["counts"]` 와 같은 형식이다."""
    loc = count_loc(cfg)
    if loc == 0:
        return HealthScore(score=0.0, grade="E", loc=0, findings_by_severity=by_severity)
    weighted = sum(SEVERITY_WEIGHT.get(sev, 1.0) * n for sev, n in by_severity.items())
    density_per_1000 = weighted / (loc / 1000)
    score = max(0.0, min(100.0, 100.0 - density_per_1000))
    return HealthScore(
        score=round(score, 1),
        grade=_grade_for(score),
        loc=loc,
        findings_by_severity=by_severity,
    )

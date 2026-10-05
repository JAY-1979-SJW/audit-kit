"""감사 결과 공통 데이터 구조."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

CRITICAL = "critical"
IMPROVE = "improve"
IGNORE = "ignore"
REVIEW = "review"  # AI 리뷰(9단계)가 등급을 정해야 하는 후보

SEVERITY_ORDER = {CRITICAL: 0, IMPROVE: 1, REVIEW: 2, IGNORE: 3}
SEVERITY_LABEL = {
    CRITICAL: "치명",
    IMPROVE: "개선",
    REVIEW: "AI 리뷰 필요",
    IGNORE: "무시",
}


@dataclass
class Finding:
    tool: str  # ruff, mypy, import-linter, cycles, vulture, radon, bandit, pytest, heuristic
    category: str  # 리포트 표시용 분류: 보안, 타입, 구조, 복잡도, 죽은코드, 스타일, 테스트, DB
    rule: str  # 도구 규칙 ID (F401, B608, CC-D ...)
    message: str
    file: str | None = None
    line: int | None = None
    severity: str = IMPROVE
    evidence: str = ""  # 근거: 어떤 도구·어떤 원칙
    extra: dict = field(default_factory=dict)
    # 이번 변경(마지막 커밋 대비 작업 트리)으로 생긴 줄인지: True=기존부터 있었음, False=이번에
    # 새로 생김, None=판정 불가(git 정보 없음, 또는 file/line 자체가 없는 발견). std/run.py의
    # run_std()가 gitutil.changed_lines() 로 채운다(2026-09-28, Anthropic Code Review의
    # 🟣Pre-existing 태깅과 같은 구분).
    pre_existing: bool | None = None

    def location(self) -> str:
        if not self.file:
            return "(프로젝트 전체)"
        return f"{self.file}:{self.line}" if self.line else self.file

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ToolResult:
    tool: str
    status: str  # ok, findings, error, skipped
    findings: list = field(default_factory=list)
    detail: str = ""  # 오류/스킵 사유
    duration: float = 0.0
    data: dict = field(default_factory=dict)  # 커버리지 등 부가 정보

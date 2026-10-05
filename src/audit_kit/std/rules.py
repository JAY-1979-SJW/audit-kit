"""기준서 규칙 원장(rules.toml)·등록부(docs_registry.toml) 로더.

기준서는 패키지에 번들(std/data)되어 있어 다른 PC 에서도 프로젝트 설정 없이 동작한다.
`--rules` 등으로 다른 파일을 지정하면 그것을 쓴다.
원본은 '32. Claude 개발표준/standard' 이고 scripts/sync_standard.py 로 번들을 갱신한다.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from audit_kit.models import CRITICAL, IMPROVE, REVIEW

CHECKS_SOURCE = Path(__file__).resolve().parent / "checks.py"
HIT_NAME_RE = re.compile(r'Hit\(\s*"([A-Z0-9-]+)"')

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

DATA_DIR = Path(__file__).resolve().parent / "data"
SEVERITY = {"critical": CRITICAL, "improve": IMPROVE, "info": REVIEW}
CATEGORY = {
    "STD": "표준",
    "DUP": "중복",
    "EFF": "효율",
    "ERR": "오류",
    "DOC": "문서",
    "SEC": "안전",
    "OPS": "운영",
    "PROC": "절차",
    "ADR": "설계결정",
    "DB": "데이터베이스",
    "API": "API",
}


@dataclass
class Rule:
    id: str
    title: str
    severity: str  # audit_kit.models 의 CRITICAL / IMPROVE / REVIEW
    kind: str  # ruff / tool / custom / manual / hook
    ruff_ids: list = field(default_factory=list)
    custom: str = ""  # audit-kit std 가 직접 구현한 검사 이름
    source: str = ""

    @property
    def category(self) -> str:
        return CATEGORY.get(self.id.split("-")[0], "기준서")


def _load(path: Path) -> dict:
    with Path(path).open("rb") as fh:
        return tomllib.load(fh)


def bundled(name: str) -> Path:
    return DATA_DIR / name


def load_rules(path: Path | None = None) -> list:
    """rules.toml 의 [[rule]] 을 Rule 목록으로."""
    rules = []
    for r in _load(path or bundled("rules.toml")).get("rule", []):
        check = r.get("check", {})
        rules.append(
            Rule(
                id=r["id"],
                title=r["title"],
                severity=SEVERITY.get(r.get("severity", "improve"), IMPROVE),
                kind=check.get("kind", "manual"),
                ruff_ids=list(check.get("ids", [])),
                custom=r.get("custom")
                or (check.get("name", "") if check.get("kind") == "custom" else ""),
                source=r.get("source", ""),
            )
        )
    return rules


def load_registry(path: Path | None = None) -> list:
    """docs_registry.toml 의 [[lib]] 목록."""
    return _load(path or bundled("docs_registry.toml")).get("lib", [])


def ruff_rule_for(rules: list, code: str) -> Rule | None:
    """ruff 코드(PTH123 등)가 속한 조항. 가장 구체적인(긴) ID 접두어를 고른다."""
    best, best_len = None, 0
    for rule in rules:
        for rid in rule.ruff_ids:
            if code.startswith(rid) and len(rid) > best_len:
                best, best_len = rule, len(rid)
    return best


def rule_for_custom(rules: list, name: str) -> Rule | None:
    return next((r for r in rules if r.custom == name), None)


def rule_by_id(rules: list, rule_id: str) -> Rule | None:
    return next((r for r in rules if r.id == rule_id), None)


def implemented_custom_names(source: Path | None = None) -> set:
    """checks.py 가 실제로 `Hit("이름", ...)` 로 보고하는 custom 검사 이름 전부.

    2026-09-28: rules.toml <-> checks.py 대응을 사람이 손으로 맞춘 목록(테스트에 하드코딩)에
    의존하던 걸, checks.py 소스를 직접 스캔하는 방식으로 바꿨다 — 새 조항을 추가하고 이 목록
    갱신을 잊는 걸 구조적으로 막는다(규칙 ID 단일 출처 문제).
    """
    text = (source or CHECKS_SOURCE).read_text(encoding="utf-8")
    return set(HIT_NAME_RE.findall(text))


def source_version(path: Path | None = None) -> dict:
    """번들된 rules.toml 등이 '32. Claude 개발표준' 저장소의 어느 커밋 기준인지.

    scripts/sync_standard.py 가 동기화할 때마다 기록한다(std/data/SOURCE_VERSION.json).
    원본 폴더가 없는 PC(번들만 배포받은 경우)에서도 "지금 이 규칙이 언제 것인지"를
    알 수 있게 하기 위한 버전 고정 — 2026-09-28.
    """
    p = path or bundled("SOURCE_VERSION.json")
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))

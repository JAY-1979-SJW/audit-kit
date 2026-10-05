"""리포트 생성: report.md(사람용), findings.json(기계용), ai-review.md(9단계 AI 리뷰 입력)."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from audit_kit.config import AuditConfig
from audit_kit.models import (
    CRITICAL,
    IGNORE,
    IMPROVE,
    REVIEW,
    SEVERITY_LABEL,
    SEVERITY_ORDER,
)

STATUS_LABEL = {"ok": "통과", "findings": "발견", "error": "오류", "skipped": "건너뜀"}
MAX_PER_CATEGORY = 40


def dedupe(results: list) -> list:
    """bandit B608과 같은 줄의 SQL 휴리스틱, 같은 줄 중복 휴리스틱 제거."""
    bandit_sql = {
        (f.file, f.line)
        for r in results
        for f in r.findings
        if f.tool == "bandit" and f.rule == "B608"
    }
    for r in results:
        if r.tool != "heuristic":
            continue
        seen, kept = set(), []
        for f in r.findings:
            key = (f.file, f.line, f.category)
            if key in seen or (f.rule == "SQL-STRING" and (f.file, f.line) in bandit_sql):
                continue
            seen.add(key)
            kept.append(f)
        r.findings = kept
    return results


def all_findings(results: list) -> list:
    items = [f for r in results for f in r.findings]
    items.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.category, f.file or "", f.line or 0))
    return items


def _item(i: int, f) -> str:
    # pre_existing: True=이번 변경 전부터 있었음, False=이번에 새로 생김, None=판정 불가(git 정보
    # 없음) — Anthropic Code Review 의 🟣Pre-existing 태깅과 같은 구분(2026-09-28). 억지로 단정하지
    # 않으려고 None 일 땐 태그를 아예 안 붙인다.
    if f.pre_existing is True:
        tag = "[기존] "
    elif f.pre_existing is False:
        tag = "[신규] "
    else:
        tag = ""
    return f"{i}. {tag}[{f.category}] {f.location()} — {f.message}\n   근거: {f.evidence}"


def _summary_section(cfg: AuditConfig, by_sev: dict, cov, sc) -> list:
    L = [
        "## 요약",
        f"- 치명: {len(by_sev[CRITICAL])}건 / 개선: {len(by_sev[IMPROVE])}건 / "
        f"AI 리뷰 필요: {len(by_sev[REVIEW])}건 / 무시: {len(by_sev[IGNORE])}건",
    ]
    if cov:
        L.append(f"- 테스트 커버리지: {cov['coverage']}% (목표 {cov['coverage_target']:g}%)")
    else:
        L.append("- 테스트 커버리지: 측정 안 됨")
    L.append(f"- 제품 패키지: {', '.join(cfg.packages) or '(없음)'}")
    if sc:
        covered = sc["production"] + sum(sc["support"].values())
        L.append(
            f"- **검사 범위: 파이썬 파일 {sc['total']}개 중 {covered}개** ({sc['summary']}) — 목록 출처: {sc['source']}"
        )
        if sc["unassigned"]:
            L.append(f"  - ⚠ 어디에도 속하지 않는 파일 {sc['unassigned']}개 — '구조' 항목 참고")
    L.append("")
    return L


def _improve_section(by_sev: dict) -> list:
    L = ["## 개선 (순차 처리)"]
    if not by_sev[IMPROVE]:
        L.append("없음")
    groups = defaultdict(list)
    for f in by_sev[IMPROVE]:
        groups[f.category].append(f)
    for cat, fs in groups.items():
        L.append(f"### {cat} ({len(fs)}건)")
        L += [_item(i, f) for i, f in enumerate(fs[:MAX_PER_CATEGORY], 1)]
        if len(fs) > MAX_PER_CATEGORY:
            L.append(f"… 외 {len(fs) - MAX_PER_CATEGORY}건 (findings.json 참고)")
    L.append("")
    return L


def _ignore_section(by_sev: dict) -> list:
    L = ["## 무시 (재검토 불필요)"]
    if by_sev[IGNORE]:
        cnt = Counter(f"{f.tool} {f.rule}" for f in by_sev[IGNORE])
        L.append("| 도구·규칙 | 건수 |")
        L.append("|---|---|")
        L += [f"| {k} | {v} |" for k, v in cnt.most_common(20)]
        L.append("")
        L.append("오탐으로 확인된 vulture 항목은 `audit-kit whitelist`로 화이트리스트에 등록.")
    else:
        L.append("없음")
    L.append("")
    return L


def _coverage_section(cov) -> list:
    if not (cov and cov.get("lowest")):
        return []
    L = ["## 커버리지 낮은 파일", "| 파일 | 커버리지 | 문장 수 |", "|---|---|---|"]
    L += [f"| {x['file']} | {x['percent']}% | {x['statements']} |" for x in cov["lowest"]]
    L.append("")
    return L


def _tool_status_section(results: list) -> list:
    L = [
        "## 도구 실행 상태",
        "| 단계 | 도구 | 상태 | 건수 | 시간 | 비고 |",
        "|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(results, 1):
        note = (r.detail or "").replace("\n", " ").replace("|", "/")[:160]
        if r.data.get("graph"):
            note = f"그래프: {r.data['graph']}" + (
                f", {', '.join(r.data['pydeps'])}" if r.data.get("pydeps") else ""
            )
        L.append(
            f"| {i} | {r.tool} | {STATUS_LABEL.get(r.status, r.status)} | {len(r.findings)} | "
            f"{r.duration:.1f}s | {note} |"
        )
    L.append("")
    return L


def render_markdown(cfg: AuditConfig, results: list, when: datetime) -> str:
    """감사 리포트(사람용 markdown). 섹션별로 `_*_section()` 헬퍼로 나눠뒀다(STD-08: 원래
    이 함수 하나가 복잡도 13·분기 14·문장 59였다 — 각 섹션이 서로 상태를 공유하지 않는 독립적인
    출력이라 그대로 뽑아내기만 했다, 2026-09-28)."""
    items = all_findings(results)
    by_sev = defaultdict(list)
    for f in items:
        by_sev[f.severity].append(f)
    cov = next((r.data for r in results if r.tool == "pytest" and "coverage" in r.data), None)
    sc = next((r.data for r in results if r.tool == "scope" and r.data), None)

    L = [f"# {cfg.root.name} 감사 리포트 — {when:%Y-%m-%d %H:%M}", ""]
    L += _summary_section(cfg, by_sev, cov, sc)
    L.append("## 치명 (즉시 수정)")
    L += [_item(i, f) for i, f in enumerate(by_sev[CRITICAL], 1)] or ["없음"]
    L.append("")
    L += _improve_section(by_sev)
    L.append("## AI 리뷰 필요 (도구가 확정 못 함 → /audit 스킬이 판정)")
    L += [_item(i, f) for i, f in enumerate(by_sev[REVIEW], 1)] or ["없음"]
    L.append("")
    L += _ignore_section(by_sev)
    L += _coverage_section(cov)
    L += _tool_status_section(results)
    return "\n".join(L)


def render_ai_review(results: list, router_files: list) -> str:
    items = all_findings(results)
    L = [
        "# AI 설계 리뷰 입력 (9단계)",
        "",
        "이 파일은 1~8단계 결과를 요약한 것이다. 체크리스트(.claude/skills/audit/checklist.md)에 따라",
        "아래 후보를 코드에서 직접 확인하고 치명/개선/무시로 판정한다.",
        "",
    ]
    L.append("## 휴리스틱 후보 (판정 필요)")
    rev = [f for f in items if f.severity == REVIEW]
    L += [f"- [{f.rule}] {f.location()} — {f.message}" for f in rev] or ["- 없음"]
    L.append("")
    L.append("## 도구 확정 결과 요약 (참고용, 재판정 불필요)")
    cnt = Counter(
        (SEVERITY_LABEL[f.severity], f.tool) for f in items if f.severity in (CRITICAL, IMPROVE)
    )
    L += [f"- {sev} / {tool}: {n}건" for (sev, tool), n in sorted(cnt.items())] or ["- 없음"]
    hot = Counter(f.file for f in items if f.file and f.severity in (CRITICAL, IMPROVE, REVIEW))
    L.append("")
    L.append("## 문제가 몰린 파일 (우선 읽을 곳)")
    L += [f"- {f}: {n}건" for f, n in hot.most_common(15)] or ["- 없음"]
    L.append("")
    L.append("## 라우터 파일 (비즈니스 로직 혼입 점검 대상)")
    L += [f"- {f}" for f in router_files] or ["- 없음 (router_globs 설정 확인)"]
    L.append("")
    return "\n".join(L)


def write_reports(
    cfg: AuditConfig, results: list, out_dir: Path, when: datetime, router_files: list
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_markdown(cfg, results, when)
    (out_dir / "report.md").write_text(md, encoding="utf-8")
    (out_dir / "ai-review.md").write_text(render_ai_review(results, router_files), encoding="utf-8")
    payload = {
        "project": cfg.root.name,
        "generated_at": when.isoformat(timespec="seconds"),
        "packages": cfg.packages,
        "tools": [
            {
                "tool": r.tool,
                "status": r.status,
                "count": len(r.findings),
                "detail": r.detail,
                "duration": round(r.duration, 2),
                "data": r.data,
            }
            for r in results
        ],
        "findings": [f.to_dict() for f in all_findings(results)],
    }
    (out_dir / "findings.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    latest = out_dir.parent / "LATEST.txt"
    latest.write_text(out_dir.name, encoding="utf-8")
    counts = Counter(f.severity for f in all_findings(results))
    return {"dir": out_dir, "counts": counts}

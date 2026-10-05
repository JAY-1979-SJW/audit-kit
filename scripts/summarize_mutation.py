"""`mutmut export-cicd-stats` 가 만든 mutants/mutmut-cicd-stats.json 을
GitHub Actions 요약(GITHUB_STEP_SUMMARY)에 사람이 읽을 수 있게 적는다.

JSON 키(killed/survived/total 등)는 mutmut 3.x 소스(mutmut/__main__.py 의
save_cicd_stats)로 2026-09-27 에 직접 확인했다. 판정 기준(몇 % 밑이면 실패)은
아직 정하지 않았다 — 첫 실행 결과를 보고 정한다(그래서 이 스크립트는 항상 exit 0).

**주의(2026-09-27 실측으로 확인)**: `total` 은 이번 실행에서 검사한 개수가 아니라
그 파일에 지금까지 생성된 전체 변이체 수다(mutmut 은 상태를 mutants/ 에 이어서
쌓는다). 패턴으로 좁혀 실행하면 `total` 은 그대로인데 killed+survived+... 의
합("검사한 것")만 늘어난다. 그래서 변이 점수는 total 이 아니라 "검사한 것" 을
분모로 한다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

STATS_FILE = Path("mutants/mutmut-cicd-stats.json")
OUTCOME_KEYS = ("killed", "survived", "no_tests", "skipped", "suspicious", "timeout")


def summarize(stats: dict) -> str:
    total = stats.get("total", 0)
    killed = stats.get("killed", 0)
    survived = stats.get("survived", 0)
    tested = sum(stats.get(k, 0) for k in OUTCOME_KEYS)
    not_yet_tested = total - tested
    denom = killed + survived  # no_tests/skipped/suspicious 는 잡음/못잡음 판정이 아니라 뺀다
    score = (killed / denom * 100) if denom else 0.0
    lines = [
        "## 변이 테스트 결과",
        "",
        f"- 이번에 검사한 변이체: {tested} (파일 전체 누적 변이체 {total}개 중)",
        f"- 죽음(killed, 시험이 잡음): {killed}",
        f"- 생존(survived, 시험이 못 잡음): {survived}",
        (
            f"- 그 외(no_tests/skipped/suspicious/timeout): "
            f"{stats.get('no_tests', 0)}/{stats.get('skipped', 0)}/"
            f"{stats.get('suspicious', 0)}/{stats.get('timeout', 0)}"
        ),
        f"- 변이 점수(killed / (killed+survived)): {score:.1f}%",
    ]
    if not_yet_tested:
        lines.append(f"- 아직 검사 안 한 것(패턴에 안 걸림): {not_yet_tested}")
    if survived:
        lines += [
            "",
            (
                f"**생존한 변이체 {survived}개**: 해당 코드를 실제로 검증하는 시험이 "
                "부족할 수 있습니다. `mutants/mutmut-cicd-stats.json` 을 내려받아 확인하세요."
            ),
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not STATS_FILE.is_file():
        message = "결과 파일이 없습니다(변이체 생성 실패 또는 검사 대상 없음).\n"
    else:
        try:
            stats = json.loads(STATS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            message = f"결과 파일을 읽지 못했습니다: {err!r}\n"
        else:
            message = summarize(stats)
    print(message, end="")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as f:
            f.write(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

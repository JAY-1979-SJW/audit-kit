"""수정 모드: 감사 리포트의 문제를 브랜치에서 한 건씩 고치고, 건마다 재검사·커밋한다.

  audit-kit fix plan    최근 감사 결과로 수정 계획(fix-plan.md) 작성
  audit-kit fix start   수정 브랜치 생성 + 기존 테스트 실패 기록(기준선)
  audit-kit fix check   한 건 재검사 (해당 문제 해소 + 같은 파일에 새 치명 없음 + 구문 정상)
  audit-kit fix done    재검사 통과 건을 커밋 (1건 = 1커밋, 개별 되돌리기 가능)
  audit-kit fix skip    못 고치는 건을 사유와 함께 건너뜀
  audit-kit fix status  진행 상황
  audit-kit fix report  전체 테스트를 기준선과 비교하고 결과 보고서 작성

코드 수정 자체는 Claude(/audit-fix 스킬) 또는 사람이 한다. 이 모듈은 범위·검증·기록을 강제한다.
"""

from __future__ import annotations

import ast
import fnmatch
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from audit_kit.config import AuditConfig, load_config
from audit_kit.gitutil import run_git
from audit_kit.models import CRITICAL, IMPROVE, REVIEW
from audit_kit.textio import read_text

SESSION_FILE = "fix-session.json"
PLAN_JSON = "fix-plan.json"
DEFAULT_EXCLUDE_RULES = {"COVERAGE", "NO-TESTS"}  # 수정 모드로 다루지 않는 항목(별도 작업)
KNOWN_ISSUES_JSON = "docs/known_preexisting_issues.json"
KNOWN_ISSUES_MD = "docs/known_preexisting_issues.md"

# 규칙별 수정 방향 (Claude/사람용 안내)
HINTS = [
    (
        ("F821", "name-defined"),
        "빠진 import 또는 정의. `git log -S <이름>` 으로 원래 있던 위치를 찾아 import 를 복구한다. "
        "새로 만들지 말고 기존 정의를 재사용",
    ),
    (
        ("call-arg",),
        "호출 대상의 현재 시그니처를 확인. 호출부가 틀렸는지(인자 제거/이름 수정), 대상이 기능을 잃었는지 판단. "
        "대상 함수를 바꿀 때는 다른 호출부 영향 확인",
    ),
    (
        ("attr-defined",),
        "임포트하는 이름이 대상 모듈에 없음. 이름이 바뀌었는지(`git log -S`), 다른 모듈로 옮겨졌는지 찾아 "
        "임포트를 고친다. 기능 자체가 삭제됐으면 호출부 처리 방법을 사용자에게 확인",
    ),
    (
        ("syntax", "E999", "E9"),
        "구문 오류. 지원 파이썬 버전(requires-python)에서 동작하는 문법으로 고친다",
    ),
    (("F63", "F7"), "명백한 코드 오류(잘못된 비교/제어문). 의도를 파악해 최소 수정"),
    (
        ("B602", "B605"),
        "shell=True 제거: 인자를 리스트로 넘기고 shell=False. 셸 기능(파이프·start 등)이 꼭 필요하면 "
        "입력값 검증 후 사유 주석",
    ),
    (
        ("B608", "SQL-STRING"),
        "문자열 조합 SQL → 바인드 파라미터(:name, ?)로. 테이블/컬럼명처럼 파라미터화 불가한 값은 "
        "허용 목록 검증",
    ),
    (
        ("B324",),
        "보안 용도가 아니면 hashlib.sha1(..., usedforsecurity=False), 보안 용도면 sha256 이상",
    ),
    (
        ("TEST-FAIL", "TEST-ERROR"),
        "실패 원인이 코드 버그인지 테스트 기대값 문제인지 먼저 판단. 테스트 기대값은 근거 없이 "
        "바꾸지 않는다. 테스트 데이터 파일 누락이면 사용자에게 보고하고 건너뛴다",
    ),
    (
        ("COLLECT-ERROR",),
        "테스트 파일 임포트 실패. 누락 패키지(환경 문제)면 건너뛰고 보고, 코드의 임포트 오류면 코드를 고친다",
    ),
    (
        ("SESSION-IN-FUNC", "GLOBAL-SESSION"),
        "세션을 요청 단위 의존성(Depends) 또는 with 블록으로. 쓰기 작업이면 commit/rollback 경로 확인",
    ),
    (("ROUTER-LOGIC",), "계산·DB 로직을 services 계층 함수로 옮기고 라우터는 호출만"),
    (("ARCH-",), "audit-kit arch fix 로 자동 수정을 먼저 시도하고, 남은 것은 /arch 스킬 절차로"),
]


def hint_for(rule: str) -> str:
    for keys, text in HINTS:
        if any(
            rule == k
            or (k.endswith("-") and rule.startswith(k))
            or (len(k) <= 3 and rule.startswith(k))
            for k in keys
        ):
            return text
    return "문제 원인을 확인해 최소 범위로 수정. 동작을 바꾸는 수정이면 사용자에게 먼저 확인"


# ---------------------------------------------------------------- 공통
_git = run_git  # gitutil 과 공유(2026-09-28 Stop hook 중복 코드 지적으로 합침)


def _report_dir(cfg: AuditConfig) -> Path:
    latest = cfg.root / cfg.report_dir / "LATEST.txt"
    if not latest.exists():
        raise SystemExit("감사 리포트가 없습니다. 먼저 `audit-kit run` 을 실행하세요.")
    return cfg.root / cfg.report_dir / latest.read_text(encoding="utf-8").strip()


def _session_path(cfg: AuditConfig) -> Path:
    return cfg.root / cfg.report_dir / SESSION_FILE


def load_session(cfg: AuditConfig) -> dict:
    p = _session_path(cfg)
    if not p.exists():
        raise SystemExit(
            "진행 중인 수정 세션이 없습니다. `audit-kit fix plan` → `audit-kit fix start` 순서로 시작하세요."
        )
    return json.loads(p.read_text(encoding="utf-8"))


def save_session(cfg: AuditConfig, s: dict):
    _session_path(cfg).write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


def _key(f: dict) -> list:
    return [f["tool"], f["rule"], f.get("file"), f["message"]]


def _is_protected(cfg: AuditConfig, file) -> bool:
    return bool(file) and any(fnmatch.fnmatch(file, g) for g in cfg.protected_paths)


def _nodeid(f: dict):
    """pytest 항목의 node id."""
    if f["tool"] != "pytest" or not f.get("file"):
        return None
    if f["rule"] == "COLLECT-ERROR":
        return f["file"]
    # 메시지 "테스트 실패: tests.unit.test_x.Cls.test_y — ..."
    name = f["message"].split(": ", 1)[-1].split(" — ")[0].strip()
    mod = f["file"][:-3].replace("/", ".")
    rest = name[len(mod) + 1 :] if name.startswith(mod + ".") else name.split(".")[-1]
    return f["file"] + "::" + rest.replace(".", "::")


# ---------------------------------------------------------------- plan
def build_plan(cfg: AuditConfig, include: set, rules=None, files=None, limit: int = 0) -> dict:
    rdir = _report_dir(cfg)
    data = json.loads((rdir / "findings.json").read_text(encoding="utf-8"))
    findings = data["findings"]
    key_count = Counter(tuple(_key(f)) for f in findings)
    items = []
    for f in findings:
        if f["severity"] not in include or (f["rule"] in DEFAULT_EXCLUDE_RULES and not rules):
            continue
        if rules and not any(f["rule"] == r or f["rule"].startswith(r) for r in rules):
            continue
        if files and not any(fnmatch.fnmatch(f.get("file") or "", g) for g in files):
            continue
        items.append({
            "tool": f["tool"],
            "rule": f["rule"],
            "category": f["category"],
            "severity": f["severity"],
            "file": f.get("file"),
            "line": f.get("line"),
            "message": f["message"],
            "evidence": f["evidence"],
            "hint": hint_for(f["rule"]),
            "protected": _is_protected(cfg, f.get("file")),
            "key": _key(f),
            "baseline_count": key_count[tuple(_key(f))],
            "nodeid": _nodeid(f),
            "status": "pending",
            "attempts": 0,
            "reason": "",
            "commit": "",
        })
    order = {CRITICAL: 0, REVIEW: 1, IMPROVE: 2}
    items.sort(
        key=lambda i: (i["protected"], order.get(i["severity"], 9), i["file"] or "", i["line"] or 0)
    )
    if limit:
        items = items[:limit]
    for n, it in enumerate(items, 1):
        it["id"] = f"F{n:03d}"
    plan = {
        "report_dir": rdir.name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "include": sorted(include),
        "items": items,
    }
    (rdir / PLAN_JSON).write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    (rdir / "fix-plan.md").write_text(render_plan(plan), encoding="utf-8")
    return plan


def render_plan(plan: dict) -> str:
    items = plan["items"]
    L = [
        f"# 수정 계획 — 감사 {plan['report_dir']}",
        "",
        f"- 대상: {len(items)}건 (보호 경로 {sum(i['protected'] for i in items)}건은 사용자 승인 없이 수정 안 함)",
        "",
        "| ID | 등급 | 분류 | 위치 | 문제 |",
        "|---|---|---|---|---|",
    ]
    for i in items:
        loc = f"{i['file']}:{i['line']}" if i["line"] else (i["file"] or "-")
        mark = " 🔒" if i["protected"] else ""
        L.append(
            f"| {i['id']}{mark} | {i['severity']} | {i['category']} | `{loc}` | {i['message'][:90]} |"
        )
    L += ["", "## 항목별 수정 방향", ""]
    for i in items:
        L += [
            f"### {i['id']} [{i['rule']}] {i['file']}:{i['line']}",
            f"- 문제: {i['message']}",
            f"- 근거: {i['evidence']}",
            f"- 수정 방향: {i['hint']}",
            "",
        ]
    return "\n".join(L)


# ---------------------------------------------------------------- start
# 검사 도구·파이썬이 만드는 파일 — 변경으로 보지도, 커밋하지도 않는다
GENERATED = [
    "*__pycache__*",
    "*.pyc",
    ".coverage",
    ".coverage.*",
    "*.mypy_cache*",
    "*.ruff_cache*",
    "*.pytest_cache*",
    "coverage.json",
    "junit*.xml",
    "*.egg-info*",
]


def _changes(cfg: AuditConfig) -> list:
    """(상태, 경로) — 생성 파일과 리포트 폴더 제외."""
    p = _git(cfg.root, "status", "--porcelain", "-uall")
    out = []
    for ln in p.stdout.splitlines():
        if not ln.strip():
            continue
        status, path = ln[:2], ln[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path.startswith(cfg.report_dir + "/") or any(
            fnmatch.fnmatch(path, g) for g in GENERATED
        ):
            continue
        out.append((status, path))
    return out


def _dirty(cfg: AuditConfig) -> list:
    return [f"{s} {p}" for s, p in _changes(cfg)]


def _failing_tests(cfg: AuditConfig) -> list:
    from audit_kit.arch.commands import _failing_tests as ft

    return sorted(ft(cfg.root, cfg.pytest_timeout))


# ---------------------------------------------------------------- known issues
# "기준선 대비 여전히 실패 중인 테스트"를 세션이 끝나도 남는 영구 목록으로 쌓는다.
# 목적: fix 세션마다 같은 기존 실패의 원인을 매번 새로 조사하지 않도록, 한 번 밝혀낸
# 원인(note)을 대상 프로젝트에 문서로 남기고 다음 세션이 먼저 참조하게 한다.
# (2026-09-28 다른 프로젝트 STD-02/04 작업 중 같은 기존 실패를 여러 서브에이전트가
#  매번 재조사하는 낭비가 실측됨 — 이를 막기 위해 audit-kit 자체에 반영)


def _known_issues_path(cfg: AuditConfig) -> Path:
    return cfg.root / KNOWN_ISSUES_JSON


def load_known_issues(cfg: AuditConfig) -> dict:
    p = _known_issues_path(cfg)
    if not p.exists():
        return {"issues": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"issues": {}}
    data.setdefault("issues", {})
    return data


def save_known_issues(cfg: AuditConfig, data: dict) -> None:
    p = _known_issues_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    md_path = cfg.root / KNOWN_ISSUES_MD
    lines = [
        "# 기존(pre-existing) 결함 목록",
        "",
        "`audit-kit fix report` 가 기준선(수정 시작 시점) 대비 여전히 실패 중인 테스트를",
        "자동으로 여기 쌓는다. **새 수정 작업을 시작하기 전에 이 목록을 먼저 확인**해서,",
        "이미 원인이 밝혀진 실패를 또 조사하지 않는다.",
        "",
        "`원인 메모` 는 자동으로 채워지지 않는다 — 원인을 알아내면 "
        f"`{KNOWN_ISSUES_JSON}` 의 해당 항목 `note` 필드에 직접 적어 넣는다.",
        "",
        "| 테스트 | 처음 발견 | 최근 확인 | 원인 메모 |",
        "|---|---|---|---|",
    ]
    for test_id, info in sorted(data.get("issues", {}).items()):
        note = info.get("note") or "(미확인)"
        lines.append(
            f"| `{test_id}` | {info.get('first_seen', '?')} | {info.get('last_confirmed', '?')} | {note} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_known_issues(cfg: AuditConfig, still_failing: set) -> dict:
    """기준선에도 있고 지금도 실패 중인 테스트를 영구 목록에 추가/갱신한다."""
    if not still_failing:
        return load_known_issues(cfg)
    data = load_known_issues(cfg)
    today = datetime.now().date().isoformat()
    for test_id in still_failing:
        entry = data["issues"].setdefault(test_id, {"first_seen": today, "note": ""})
        entry["last_confirmed"] = today
    save_known_issues(cfg, data)
    return data


def start(cfg: AuditConfig, branch: str = "", force: bool = False, tests: bool = True) -> dict:
    if _git(cfg.root, "rev-parse", "--is-inside-work-tree").returncode != 0:
        raise SystemExit(
            "git 저장소가 아닙니다. 수정 모드는 되돌리기를 위해 git 이 필요합니다 (`git init` 후 커밋)."
        )
    if _session_path(cfg).exists() and not force:
        raise SystemExit(
            "이미 진행 중인 수정 세션이 있습니다. `audit-kit fix status` 로 확인하거나 --force 로 새로 시작."
        )
    dirty = _dirty(cfg)
    if dirty and not force:
        raise SystemExit(
            "커밋되지 않은 변경이 있습니다. 먼저 커밋/정리하세요 (--force 로 무시):\n  "
            + "\n  ".join(dirty[:10])
        )
    rdir = _report_dir(cfg)
    plan_file = rdir / PLAN_JSON
    plan = (
        json.loads(plan_file.read_text(encoding="utf-8"))
        if plan_file.exists()
        else build_plan(cfg, {CRITICAL})
    )
    base_branch = _git(cfg.root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    base_commit = _git(cfg.root, "rev-parse", "HEAD").stdout.strip()
    branch = branch or cfg.fix_branch_prefix + datetime.now().strftime("%Y%m%d-%H%M")
    p = _git(cfg.root, "switch", "-c", branch)
    if p.returncode != 0:
        raise SystemExit(f"브랜치 생성 실패: {p.stderr.strip()}")
    baseline = _failing_tests(cfg) if tests and cfg.run_tests else None
    s = {
        "branch": branch,
        "base_branch": base_branch,
        "base_commit": base_commit,
        "report_dir": rdir.name,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "baseline_failures": baseline,
        "items": {i["id"]: i for i in plan["items"]},
    }
    save_session(cfg, s)
    return s


# ---------------------------------------------------------------- check
def _current_findings(cfg: AuditConfig, item: dict) -> list:
    from audit_kit import tools
    from audit_kit.heuristics import run_heuristics

    f, tool = item["file"], item["tool"]
    if tool == "ruff":
        return [x.to_dict() for x in tools.run_ruff(cfg, targets=[f]).findings]
    if tool == "mypy":
        return [x.to_dict() for x in tools.run_mypy(cfg, targets=[f]).findings if x.file == f]
    if tool == "bandit":
        return [x.to_dict() for x in tools.run_bandit(cfg, targets=[f]).findings]
    if tool == "heuristic":
        return [x.to_dict() for x in run_heuristics(cfg, only_files={f}).findings]
    if tool == "arch":
        from audit_kit.arch.scan import scan
        from audit_kit.arch.spec import load_spec

        spec = load_spec(cfg.root)
        return [v.to_finding().to_dict() for v in scan(cfg, spec) if v.file == f] if spec else []
    if tool in ("cycles", "import-linter"):
        r = tools.run_cycles(cfg) if tool == "cycles" else tools.run_import_linter(cfg)
        return [x.to_dict() for x in r.findings]
    return []


def _new_critical(cfg: AuditConfig, files: list, report_findings: list) -> list:
    """변경된 파일들에서 감사 당시에 없던 치명(ruff/mypy)이 새로 생겼는가."""
    from audit_kit import tools

    files = sorted({f for f in files if f and f.endswith(".py") and (cfg.root / f).exists()})
    if not files:
        return []
    fs = set(files)
    before = Counter(
        tuple(_key(x)) for x in report_findings if x.get("file") in fs and x["severity"] == CRITICAL
    )
    now = [
        x.to_dict()
        for x in tools.run_ruff(cfg, targets=files).findings
        + tools.run_mypy(cfg, targets=files).findings
        if x.file in fs and x.severity == CRITICAL
    ]
    cnt = Counter(tuple(_key(x)) for x in now)
    return [f"{k[2]} [{k[1]}] {k[3]}" for k, n in cnt.items() if n > before.get(k, 0)]


def _check_syntax(cfg: AuditConfig, f: str | None) -> list:
    if not (f and f.endswith(".py")):
        return []
    path = cfg.root / f
    if not path.exists():
        return [f"{f} 파일이 없음"]
    try:
        ast.parse(read_text(path))
    except SyntaxError as e:
        return [f"구문 오류 {f}:{e.lineno} {e.msg}"]
    return []


def _check_pytest_item(cfg: AuditConfig, item: dict, f: str | None) -> list:
    from audit_kit.runner import run_module

    node = item.get("nodeid") or f
    p = run_module(
        "pytest",
        ["-q", "-p", "no:cacheprovider", "-o", "addopts=", node],
        cfg.root,
        timeout=cfg.pytest_timeout,
    )
    if p.returncode not in (0,):
        tail = [ln for ln in p.stdout.splitlines() if ln.strip()][-3:]
        return ["테스트가 아직 실패: " + " / ".join(tail)]
    return []


def _check_rule_count(cfg: AuditConfig, s: dict, item: dict, item_id: str) -> list:
    done_same = sum(
        1
        for i in s["items"].values()
        if i["key"] == item["key"] and i["status"] == "done" and i["id"] != item_id
    )
    allowed = item["baseline_count"] - done_same - 1
    now = sum(1 for x in _current_findings(cfg, item) if _key(x) == item["key"])
    if now > max(allowed, 0):
        return [f"문제가 아직 남아 있음 ({item['rule']} 현재 {now}건, 허용 {max(allowed, 0)}건)"]
    return []


def check(cfg: AuditConfig, s: dict, item_id: str) -> tuple:
    """`audit-kit fix check` — 항목별로 검증 종류가 다른 부분(구문/테스트/조항 건수)을
    `_check_*()`로 뽑아냈다(STD-08: 원래 이 함수 하나가 복잡도 11이었다, 2026-09-28)."""
    item = s["items"].get(item_id)
    if item is None:
        return False, f"{item_id} 가 계획에 없습니다"
    item["attempts"] += 1
    f = item["file"]
    problems = _check_syntax(cfg, f)
    if not problems:
        if item["tool"] == "pytest":
            problems = _check_pytest_item(cfg, item, f)
        else:
            problems = _check_rule_count(cfg, s, item, item_id)
    # 이번에 바뀐 모든 파일(+대상 파일)에 새 치명이 생기지 않았는지 — 다른 파일을 망가뜨린 수정도 잡는다
    changed = [p for _, p in _changes(cfg)] + ([f] if f else [])
    if not any(pr.startswith("구문 오류") for pr in problems):
        rdir = cfg.root / cfg.report_dir / s["report_dir"]
        report = json.loads((rdir / "findings.json").read_text(encoding="utf-8"))["findings"]
        new = _new_critical(cfg, changed, report)
        if new:
            problems.append("변경한 파일에 새 치명 발생: " + "; ".join(new[:3]))
    item["status"] = "verified" if not problems else "failed"
    item["reason"] = "; ".join(problems)
    save_session(cfg, s)
    return not problems, item["reason"]


# ---------------------------------------------------------------- done / skip
def _collect_done_items(s: dict, item_ids: list, force: bool) -> list:
    items = []
    for item_id in item_ids:
        item = s["items"].get(item_id)
        if item is None:
            raise SystemExit(f"{item_id} 가 계획에 없습니다")
        if item["status"] != "verified" and not force:
            raise SystemExit(
                f"{item_id} 는 재검사를 통과하지 않았습니다 (`audit-kit fix check {item_id}`)."
            )
        if item["protected"] and not force:
            raise SystemExit(
                f"{item_id} 는 보호 경로({item['file']})입니다. 사용자 승인 후 --force 로 커밋하세요."
            )
        items.append(item)
    return items


def _done_via_earlier_commit(cfg: AuditConfig, s: dict, items: list) -> str:
    """이번에 손볼 변경이 없다 — 이미 앞선 커밋에서 같이 고쳐졌는지 찾아서 그걸로 완료 처리."""
    found = []
    for item in items:
        c = _git(
            cfg.root,
            "log",
            "-1",
            "--format=%h",
            f"{s['base_commit']}..HEAD",
            "--",
            item["file"] or ".",
        ).stdout.strip()
        if not c:
            raise SystemExit(
                f"커밋할 변경이 없고, 이번 세션에서 {item['file']} 를 고친 커밋도 없습니다."
            )
        item["commit"], item["status"], item["reason"] = c, "done", "앞 커밋에서 함께 수정됨"
        found.append(c)
    save_session(cfg, s)
    return ", ".join(sorted(set(found))) + " (앞 커밋에 포함)"


def done(cfg: AuditConfig, s: dict, item_ids: list, force: bool = False, trailer: str = "") -> str:
    """재검사 통과 항목들을 한 커밋으로. 변경이 없으면(앞 커밋에서 함께 고쳐짐) 그 커밋으로 완료 처리.
    (STD-08: 원래 이 함수 하나가 복잡도 11이었다 — 항목 검증과 "변경 없음" 대안 경로를 각각
    `_collect_done_items`/`_done_via_earlier_commit`으로 뽑아냈다, 2026-09-28)"""
    items = _collect_done_items(s, item_ids, force)
    paths = [p for _, p in _changes(cfg)]
    if not paths:
        return _done_via_earlier_commit(cfg, s, items)
    _git(cfg.root, "add", "-A", "--", *paths)
    first = items[0]
    loc = f"{first['file']}:{first['line']}" if first["line"] else (first["file"] or "")
    title = f"fix(audit): [{first['rule']}] {loc}" + (
        f" 외 {len(items) - 1}건" if len(items) > 1 else ""
    )
    body = "\n".join(
        f"{i['id']} [{i['rule']}] {i['file']}:{i['line']} — {i['message'][:150]}" for i in items
    )
    msg = f"{title}\n\n{body}\n근거: {first['evidence'][:200]}"
    if trailer:
        msg += f"\n\n{trailer}"
    # --no-verify: 이 커밋 전에 이미 fix check 로 컴파일·재검사·(옵션)테스트를 다 통과했다.
    # pre-commit 훅(ruff --fix 등)이 여기서 또 걸리면 방금 검증한 내용을 훅이 조용히 다시
    # 고쳐써서 검증-커밋 사이 상태가 어긋날 수 있다(2026-09-27: `audit-kit init` 이 pre-commit
    # 훅을 자동 설치하게 되면서 실측으로 확인).
    p = _git(cfg.root, "commit", "-q", "-m", msg, "--no-verify")
    if p.returncode != 0:
        raise SystemExit(f"커밋 실패: {p.stderr.strip() or p.stdout.strip()}")
    commit = _git(cfg.root, "rev-parse", "--short", "HEAD").stdout.strip()
    for item in items:
        item["commit"], item["status"] = commit, "done"
    save_session(cfg, s)
    return commit


def skip(cfg: AuditConfig, s: dict, item_id: str, reason: str):
    item = s["items"][item_id]
    item["status"], item["reason"] = "skipped", reason
    save_session(cfg, s)


# ---------------------------------------------------------------- report
def report(cfg: AuditConfig, s: dict, tests: bool = True) -> Path:
    items = list(s["items"].values())
    cnt = Counter(i["status"] for i in items)
    after = (
        _failing_tests(cfg)
        if tests and cfg.run_tests and s.get("baseline_failures") is not None
        else None
    )
    base = set(s.get("baseline_failures") or [])
    L = [
        f"# 감사 수정 결과 — {s['branch']}",
        "",
        f"- 기준: `{s['base_branch']}` @ {s['base_commit'][:8]} / 감사 {s['report_dir']}",
        f"- 완료 {cnt.get('done', 0)} / 건너뜀 {cnt.get('skipped', 0)} / 실패 {cnt.get('failed', 0)} / "
        f"미처리 {cnt.get('pending', 0) + cnt.get('verified', 0)} (총 {len(items)})",
        "",
    ]
    if after is not None:
        still_failing = base & set(after)
        new, fixed = sorted(set(after) - base), sorted(base - set(after))
        known = update_known_issues(cfg, still_failing)
        documented = sum(1 for t in still_failing if known["issues"].get(t, {}).get("note"))
        L += [
            "## 테스트 (기준선 대비)",
            "",
            f"- 기존 실패 {len(base)}건 → 현재 {len(after)}건 / **새 실패 {len(new)}건** / 해결 {len(fixed)}건",
            "",
        ]
        if still_failing:
            L.append(
                f"- 여전히 실패 중인 기존 {len(still_failing)}건은 `{KNOWN_ISSUES_MD}`에 기록/갱신됨"
                f"(원인 문서화 {documented}/{len(still_failing)}건)"
            )
            L.append("")
        L += [f"  - ❌ 새 실패: {t}" for t in new] + [f"  - ✅ 해결: {t}" for t in fixed]
        L.append("")
    L += ["## 항목", "", "| ID | 상태 | 커밋 | 위치 | 문제 | 비고 |", "|---|---|---|---|---|---|"]
    icon = {"done": "✅", "skipped": "⏭", "failed": "❌", "pending": "…", "verified": "☑"}
    for i in items:
        loc = f"{i['file']}:{i['line']}" if i["line"] else (i["file"] or "-")
        L.append(
            f"| {i['id']} | {icon.get(i['status'], i['status'])} | {i['commit']} | `{loc}` | "
            f"{i['message'][:70]} | {i['reason'][:80]} |"
        )
    L += [
        "",
        "## 되돌리기",
        "",
        "- 한 건: `git revert <커밋>`",
        f"- 전체: `git switch {s['base_branch']}` 후 `git branch -D {s['branch']}`",
        "",
    ]
    out = cfg.root / cfg.report_dir / s["report_dir"] / "fix-report.md"
    out.write_text("\n".join(L), encoding="utf-8")
    return out


# ---------------------------------------------------------------- CLI
def _print_status(s: dict):
    items = list(s["items"].values())
    cnt = Counter(i["status"] for i in items)
    print(
        f"브랜치 {s['branch']} (기준 {s['base_branch']}) — "
        + ", ".join(f"{k} {v}" for k, v in cnt.most_common())
    )
    for i in items:
        loc = f"{i['file']}:{i['line']}" if i["line"] else (i["file"] or "-")
        lock = "🔒" if i["protected"] else "  "
        print(
            f"  {i['id']} {lock} {i['status']:<8} [{i['rule']}] {loc} — {i['message'][:70]}"
            + (f"  ({i['reason'][:60]})" if i["reason"] else "")
        )


def _cmd_plan(cfg: AuditConfig, args) -> int:
    include = (
        {CRITICAL}
        | ({REVIEW} if args.include_review else set())
        | ({IMPROVE} if args.include_improve else set())
    )
    plan = build_plan(
        cfg,
        include,
        args.rules.split(",") if args.rules else None,
        args.files.split(",") if args.files else None,
        args.limit,
    )
    rdir = _report_dir(cfg)
    print(f"수정 계획 {len(plan['items'])}건 → {rdir / 'fix-plan.md'}")
    for i in plan["items"][:40]:
        loc = f"{i['file']}:{i['line']}" if i["line"] else (i["file"] or "-")
        print(
            f"  {i['id']}{' 🔒' if i['protected'] else ''} [{i['rule']}] {loc} — {i['message'][:70]}"
        )
    if len(plan["items"]) > 40:
        print(f"  … 외 {len(plan['items']) - 40}건")
    return 0


def _cmd_start(cfg: AuditConfig, args) -> int:
    s = start(cfg, args.branch or "", args.force, not args.no_tests)
    print(
        f"수정 브랜치 {s['branch']} 생성 (기준 {s['base_branch']} @ {s['base_commit'][:8]}), 대상 {len(s['items'])}건"
    )
    if s["baseline_failures"] is not None:
        print(f"기준선: 기존 실패 테스트 {len(s['baseline_failures'])}건 기록")
        known = load_known_issues(cfg)
        documented = [t for t in s["baseline_failures"] if known["issues"].get(t, {}).get("note")]
        if documented:
            print(
                f"[known-issues] 그중 {len(documented)}건은 이미 원인이 문서화돼 있습니다 "
                f"— {KNOWN_ISSUES_MD} 를 먼저 확인하세요(재조사 불필요)."
            )
    return 0


def _cmd_check(cfg: AuditConfig, s: dict, args) -> int:
    ok, why = check(cfg, s, args.id)
    print(
        f"{args.id}: {'통과 ✅ — audit-kit fix done ' + args.id + ' 로 커밋' if ok else '실패 ❌ — ' + why}"
    )
    return 0 if ok else 1


def _cmd_done(cfg: AuditConfig, s: dict, args) -> int:
    c = done(cfg, s, args.id, args.force, args.trailer or "")
    print(f"{', '.join(args.id)} 커밋 {c}")
    return 0


def _cmd_report(cfg: AuditConfig, s: dict, args) -> int:
    out = report(cfg, s, not args.no_tests)
    print(out.read_text(encoding="utf-8"))
    print(f"\n보고서: {out}")
    return 0


def _cmd_status(_cfg: AuditConfig, s: dict, _args) -> int:
    _print_status(s)
    return 0


def _cmd_skip(cfg: AuditConfig, s: dict, args) -> int:
    skip(cfg, s, args.id, args.reason)
    print(f"{args.id} 건너뜀: {args.reason}")
    return 0


def _cmd_end(cfg: AuditConfig, _s: dict, _args) -> int:
    _session_path(cfg).unlink(missing_ok=True)
    print("수정 세션 종료 (브랜치와 커밋은 그대로 남음)")
    return 0


# 세션(s)이 필요한 하위 명령. plan/start 는 세션 이전 단계라 따로 처리한다.
_SESSION_SUBCOMMANDS: dict = {
    "status": _cmd_status,
    "check": _cmd_check,
    "done": _cmd_done,
    "skip": _cmd_skip,
    "report": _cmd_report,
    "end": _cmd_end,
}


def cmd(args) -> int:
    """`audit-kit fix ...` 진입점. 하위 명령마다 `_cmd_*()`로 나눠뒀다(STD-08: 원래 이 함수
    하나가 복잡도 12였다, 2026-09-28)."""
    cfg = load_config(args.path)
    a = args.fix_cmd
    if a == "plan":
        return _cmd_plan(cfg, args)
    if a == "start":
        return _cmd_start(cfg, args)
    fn = _SESSION_SUBCOMMANDS.get(a)
    if fn is None:
        return 2
    return fn(cfg, load_session(cfg), args)


def register(sub):
    p = sub.add_parser("fix", help="감사 결과 수정 모드 (브랜치·재검사·건별 커밋)")
    fsub = p.add_subparsers(dest="fix_cmd", required=True)

    def add(name, help_):
        q = fsub.add_parser(name, help=help_)
        q.add_argument("--path")
        q.set_defaults(func=cmd)
        return q

    q = add("plan", "최근 감사 결과로 수정 계획 작성 (기본: 치명만)")
    q.add_argument("--include-review", action="store_true", help="AI 리뷰 필요 항목 포함")
    q.add_argument(
        "--include-improve", action="store_true", help="개선 항목 포함 (--rules 와 함께 권장)"
    )
    q.add_argument("--rules", help="규칙 제한, 예: F821,name-defined,B602")
    q.add_argument("--files", help="파일 glob 제한, 예: mcp_server/tools/*")
    q.add_argument("--limit", type=int, default=0)
    q = add("start", "수정 브랜치 생성 + 테스트 기준선 기록")
    q.add_argument("--branch")
    q.add_argument("--force", action="store_true")
    q.add_argument("--no-tests", action="store_true")
    add("status", "진행 상황")
    q = add("check", "한 건 재검사")
    q.add_argument("id")
    q = add("done", "재검사 통과 건 커밋 (여러 ID 를 한 커밋으로 가능)")
    q.add_argument("id", nargs="+")
    q.add_argument(
        "--force", action="store_true", help="재검사 미통과/보호 경로여도 커밋 (사용자 승인 시만)"
    )
    q.add_argument("--trailer", help="커밋 메시지 끝에 붙일 줄 (예: Co-Authored-By: ...)")
    q = add("skip", "건너뛰기")
    q.add_argument("id")
    q.add_argument("--reason", required=True)
    q = add("report", "기준선 대비 테스트 비교 + 결과 보고서")
    q.add_argument("--no-tests", action="store_true")
    add("end", "세션 종료 (기록 파일만 삭제)")

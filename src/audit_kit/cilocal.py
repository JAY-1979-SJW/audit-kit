"""`audit-kit ci-local`: .github/workflows/*.yml 의 run 단계를 이 PC 에서 순서대로 실행한다.

배경(2026-09-30): GitHub Actions 는 결제 실패로 job 이 시작되지 않았다(코드 문제 아님 — 로그에
"recent account payments have failed"). 로컬에 `act` 도 없어서 워크플로를 읽고 명령을 손으로 옮겨
치며 같은 검사를 돌렸다. 그 작업을 도구로 만든 것이다.

한계(숨기지 않는다):
- GitHub 의 ubuntu 러너가 아니라 이 PC 에서 Git Bash 로 실행한다. 환경이 달라 결과가 다를 수 있다.
- `uses:` 단계(checkout, setup-python 등)는 대신할 수 없어 건너뛴다. 단 setup-python 의 python-version 은
  읽어서 `python`/`pip` 이 그 버전을 가리키게 한다(PATH 앞에 임시 shim).
- `pip install` 은 기본으로 건너뛴다(현재 파이썬 환경을 바꾸지 않기 위해). `--run-installs` 로만 실행한다.
- `${{ }}` 식이 든 단계, `if:` 조건이 있는 단계, Git Bash 가 아닌 `shell:` 은 건너뛰고 이유를 표시한다.
- on: push / pull_request 인 워크플로만 기본 실행한다(schedule·dispatch 전용은 제외, 이유 표시).

종료코드: 0 전부 통과(건너뜀 포함) / 1 실패 있음 / 2 실행할 수 없음(워크플로 없음, Git Bash 없음 등)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from audit_kit import ghcheck

WORKFLOW_DIR = ".github/workflows"
RUN_TRIGGERS = {"push", "pull_request"}
DEFAULT_STEP_TIMEOUT = 900
TAIL_LINES = 15
PIP_INSTALL_RE = re.compile(r"^\s*(?:python3?\s+-m\s+)?pip3?\s+install\b")
BILLING_HINTS = ("payments have failed", "spending limit")
# 이 PC 의 파이썬 환경 전체를 대상으로 하는 명령: GitHub 은 새 환경에서 돌리므로 로컬 결과와 다를 수 있다.
ENV_DEPENDENT_RE = re.compile(r"\bpip-audit\b|\bpip3?\s+(?:list|freeze)\b")

Runner = Callable[[str, str, Path, dict, int], tuple[int, str]]


@dataclass
class StepResult:
    workflow: str
    job: str
    name: str
    status: str  # pass | fail | warn | skip
    detail: str = ""
    seconds: float = 0.0
    env_dependent: bool = (
        False  # pip-audit 처럼 이 PC 환경 전체를 검사하는 명령이라 GitHub 결과와 다를 수 있음
    )


# ---------------------------------------------------------------- 워크플로 읽기
def load_workflow(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def triggers(wf: dict) -> set[str]:
    """`on:` 의 이벤트 이름 집합. YAML 1.1 에서는 키 `on` 이 True 로 읽힌다(PyYAML 공식 문서)."""
    on = wf.get("on", wf.get(True))
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(x) for x in on}
    if isinstance(on, dict):
        return {str(k) for k in on}
    return set()


def select_workflows(root: Path, names: list[str] | None = None) -> tuple[list, list[str]]:
    """(실행할 [(경로, 내용)], 건너뛴 이유 목록). names 가 있으면 그 이름만(트리거 무시)."""
    folder = root / WORKFLOW_DIR
    files = sorted([*folder.glob("*.yml"), *folder.glob("*.yaml")]) if folder.is_dir() else []
    chosen, skipped = [], []
    for f in files:
        wf = load_workflow(f)
        if names:
            if f.name in names or f.stem in names:
                chosen.append((f, wf))
            continue
        trig = triggers(wf)
        if trig & RUN_TRIGGERS:
            chosen.append((f, wf))
        else:
            shown = ", ".join(sorted(trig)) or "없음"
            skipped.append(
                f"{f.name}: push/pull_request 로 실행되는 워크플로가 아님(트리거: {shown}) — 건너뜀"
            )
    return chosen, skipped


# ---------------------------------------------------------------- 실행 환경
def find_bash() -> str | None:
    """Git Bash 를 찾는다. PATH 의 bash 는 WSL(C:\\Windows\\System32\\bash.exe)일 수 있어 쓰지 않는다."""
    env = os.environ.get("AUDIT_KIT_BASH")
    if env and Path(env).is_file():
        return env
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    candidates = [Path(git).parents[1] / "bin" / "bash.exe"] if git else []
    for var in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(var)
        if base:
            candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
    return next((str(c) for c in candidates if c.is_file()), None)


def python_launcher(version: str) -> list[str] | None:
    """`py -3.14` 같은 실행 명령. 그 버전이 이 PC 에 없으면 None."""
    for cmd in (["py", f"-{version}"], [f"python{version}"]):
        try:
            p = subprocess.run([*cmd, "--version"], capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if p.returncode == 0:
            return cmd
    return None


def make_shims(folder: Path, launcher: list[str]) -> None:
    """임시 폴더에 `python`, `python3`, `pip` 스크립트를 만든다(PATH 맨 앞에 두면 워크플로 명령이 지정 버전을 쓴다)."""
    exe = " ".join(launcher)
    body = {
        "python": f'exec {exe} "$@"\n',
        "python3": f'exec {exe} "$@"\n',
        "pip": f'exec {exe} -m pip "$@"\n',
    }
    for name, line in body.items():
        (folder / name).write_bytes(f"#!/bin/sh\n{line}".encode())


def setup_python_version(steps: list) -> str | None:
    for step in steps:
        if str(step.get("uses", "")).startswith("actions/setup-python"):
            version = (step.get("with") or {}).get("python-version")
            return str(version) if version is not None else None
    return None


def strip_installs(script: str) -> str | None:
    """pip install 줄을 뺀 스크립트. 줄 잇기(\\)가 있어 안전하게 못 자르면, 또는 남는 게 없으면 None."""
    lines = script.splitlines()
    if any(ln.rstrip().endswith("\\") for ln in lines):
        return None
    kept = [ln for ln in lines if ln.strip() and not PIP_INSTALL_RE.match(ln)]
    return "\n".join(kept) + "\n" if kept else None


def run_script(bash: str, script: str, cwd: Path, env: dict, timeout: int) -> tuple[int, str]:
    """스크립트를 임시 .sh 로 저장해 Git Bash 로 실행한다(여러 줄, `|| true` 등 bash 문법 지원)."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "step.sh"
        f.write_bytes(script.encode("utf-8"))
        try:
            p = subprocess.run(
                [bash, str(f)], cwd=cwd, env=env, capture_output=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired:
            return -1, f"시간 초과({timeout}초)"
        except OSError as e:
            return -1, f"실행 실패: {e}"
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", errors="replace")


# ---------------------------------------------------------------- 단계 실행
def _skip_reason(step: dict, script: str | None) -> str | None:
    if "uses" in step:
        return f"`uses: {step['uses']}` 는 로컬에서 대신할 수 없음"
    if script is None:
        return "run 이 없음"
    if "${{" in script or "${{" in json.dumps(step.get("env", {}), ensure_ascii=False):
        return "GitHub 식(${{ }}) 이 있어 로컬에서 계산할 수 없음"
    if "if" in step:
        return f"`if: {step['if']}` 조건이 있어 건너뜀"
    if str(step.get("shell", "bash")).split()[0] not in {"bash", "sh"}:
        return f"`shell: {step['shell']}` 는 Git Bash 가 아니라 건너뜀"
    return None


def _step_name(step: dict, idx: int) -> str:
    return " ".join(str(step.get("name") or step.get("run", f"단계 {idx}")).split())[:80]


def run_job(ctx: dict, wf_name: str, job_name: str, job: dict) -> list[StepResult]:
    """한 job 의 단계를 순서대로 실행한다. GitHub 처럼 첫 실패에서 멈추고 나머지는 건너뜀 처리한다."""
    results: list[StepResult] = []
    steps = job.get("steps") or []
    env = {**ctx["env"], **{str(k): str(v) for k, v in (job.get("env") or {}).items()}}
    timeout = int(job.get("timeout-minutes", 15)) * 60 or DEFAULT_STEP_TIMEOUT
    failed = False
    for i, step in enumerate(steps, 1):
        name = _step_name(step, i)
        raw = step.get("run")
        script = str(raw) if raw is not None else None
        reason = _skip_reason(step, script)
        if not reason and script is not None and not ctx["run_installs"]:
            script = (
                strip_installs(script)
                if any(PIP_INSTALL_RE.match(x) for x in script.splitlines())
                else script
            )
            if script is None:
                reason = (
                    "pip install 단계 — 현재 환경을 바꾸지 않으려고 건너뜀(--run-installs 로 실행)"
                )
        if failed:
            results.append(StepResult(wf_name, job_name, name, "skip", "앞 단계가 실패해 건너뜀"))
            continue
        if reason or script is None:
            results.append(StepResult(wf_name, job_name, name, "skip", reason or ""))
            continue
        step_env = {**env, **{str(k): str(v) for k, v in (step.get("env") or {}).items()}}
        cwd = ctx["root"] / str(step.get("working-directory", "."))
        t0 = time.monotonic()
        rc, out = ctx["runner"](ctx["bash"], script, cwd, step_env, timeout)
        secs = time.monotonic() - t0
        tail = "\n".join(out.strip().splitlines()[-TAIL_LINES:])
        if rc == 0:
            results.append(StepResult(wf_name, job_name, name, "pass", tail, secs))
        elif step.get("continue-on-error") is True:
            results.append(StepResult(wf_name, job_name, name, "warn", tail, secs))
        else:
            env_dep = bool(ENV_DEPENDENT_RE.search(script))
            results.append(StepResult(wf_name, job_name, name, "fail", tail, secs, env_dep))
            failed = not ctx.get("keep_going")
    return results


@dataclass
class Options:
    """ci-local 실행 옵션(명령줄 플래그와 1:1)."""

    names: list[str] | None = None  # 이 워크플로만
    only_job: str | None = None  # 이 job 만
    run_installs: bool = False  # pip install 단계도 실행
    keep_going: bool = False  # 실패해도 계속


def run_ci(
    root: Path,
    opts: Options | None = None,
    runner: Runner = run_script,
    bash: str | None = None,
) -> tuple[int, list[StepResult], list[str]]:
    """(종료코드, 단계 결과, 안내 문구)."""
    opts = opts or Options()
    notes: list[str] = []
    chosen, skipped = select_workflows(root, opts.names)
    notes += skipped
    if not chosen:
        notes.append("실행할 워크플로가 없습니다(.github/workflows 확인).")
        return 2, [], notes
    bash = bash or find_bash()
    if not bash:
        notes.append(
            "Git Bash 를 찾지 못했습니다. Git for Windows 를 설치하거나 AUDIT_KIT_BASH 에 bash.exe 경로를 지정하세요."
        )
        return 2, [], notes
    results: list[StepResult] = []
    with tempfile.TemporaryDirectory() as shim_dir:
        for path, wf in chosen:
            for job_name, job in (wf.get("jobs") or {}).items():
                if opts.only_job and job_name != opts.only_job:
                    continue
                env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "CI": "true"}
                env.update({str(k): str(v) for k, v in (wf.get("env") or {}).items()})
                version = setup_python_version(job.get("steps") or [])
                if version:
                    launcher = python_launcher(version)
                    if launcher is None:
                        notes.append(
                            f"{path.name}/{job_name}: 이 PC 에 Python {version} 이 없어 실행할 수 없습니다(py -0 으로 설치된 버전 확인)."
                        )
                        return 2, results, notes
                    make_shims(Path(shim_dir), launcher)
                    env["PATH"] = shim_dir + os.pathsep + env.get("PATH", "")
                ctx = {
                    "root": root,
                    "env": env,
                    "bash": bash,
                    "runner": runner,
                    "run_installs": opts.run_installs,
                    "keep_going": opts.keep_going,
                }
                results += run_job(ctx, path.name, str(job_name), job or {})
    code = 1 if any(r.status == "fail" for r in results) else 0
    return code, results, notes


# ---------------------------------------------------------------- 원격 상태 설명
def remote_explain(root: Path) -> list[str]:
    """최근 커밋의 GitHub 검사 상태를 한국어로 풀어 준다(결제 문제는 코드 문제와 구분해서 말한다)."""
    try:
        repo = ghcheck.repo_slug(root)
        runs = ghcheck.fetch_check_runs(root, repo, ghcheck.head_sha(root))
    except ghcheck.GhCheckError as e:
        return [f"원격 상태를 확인하지 못했습니다: {e}"]
    if not runs:
        return ["원격: 이 커밋의 검사 기록이 없습니다(아직 push 전이거나 Actions 가 꺼져 있음)."]
    bad = [r for r in runs if r.get("conclusion") not in ghcheck.TERMINAL_OK]
    if not bad:
        return ["원격: 이 커밋의 GitHub 검사가 모두 통과 상태입니다."]
    lines = []
    for r in bad:
        msgs = _annotation_messages(root, r)
        if any(h in m for m in msgs for h in BILLING_HINTS):
            lines.append(
                f"원격 '{r.get('name')}': 코드 문제가 아니라 GitHub 결제(지출 한도/결제 실패) 문제로 job 이 시작되지 못했습니다. "
                "GitHub Settings > Billing & plans 를 확인하세요. 그 전까지는 이 로컬 결과가 기준입니다."
            )
        else:
            detail = msgs[0][:200] if msgs else "자세한 사유 없음"
            lines.append(
                f"원격 '{r.get('name')}' 실패: {detail} (자세히: gh run view --log-failed)"
            )
    return lines


def _annotation_messages(root: Path, check_run: dict) -> list[str]:
    url = str((check_run.get("output") or {}).get("annotations_url") or "")
    if not url:
        return []
    try:
        data = json.loads(
            ghcheck._run_gh(["api", url.removeprefix("https://api.github.com/")], root)
        )
    except (ghcheck.GhCheckError, ValueError):
        return []
    return [str(a.get("message", "")) for a in data if isinstance(a, dict)]


# ---------------------------------------------------------------- 출력
ICONS = {"pass": "통과", "fail": "실패", "warn": "경고(continue-on-error)", "skip": "건너뜀"}


def render(results: list[StepResult], notes: list[str]) -> str:
    out: list[str] = list(notes)
    cur = ""
    for r in results:
        head = f"{r.workflow} / {r.job}"
        if head != cur:
            out.append(f"\n■ {head}")
            cur = head
        if r.status in {"pass", "fail", "warn"}:
            extra = f"  ({r.seconds:.0f}초)"
        else:
            extra = f"  — {r.detail}" if r.detail else ""
        out.append(f"  [{ICONS[r.status]}] {r.name}{extra}")
        if r.status == "fail" and r.detail:
            out.append("      " + r.detail.replace("\n", "\n      "))
    n = {s: sum(1 for r in results if r.status == s) for s in ICONS}
    out.append(
        f"\n합계: 통과 {n['pass']} / 실패 {n['fail']} / 경고 {n['warn']} / 건너뜀 {n['skip']}"
    )
    fails = [r for r in results if r.status == "fail"]
    stopped = any("앞 단계가 실패" in r.detail for r in results if r.status == "skip")
    if fails and all(r.env_dependent for r in fails):
        out.append(
            "결론: 실패한 단계는 모두 '이 PC 의 파이썬 환경 전체를 검사하는 도구'(pip-audit 등)입니다. "
            "GitHub 은 새 환경에서 실행하므로 이 저장소의 문제가 아닐 수 있습니다(위 목록은 이 PC 에 설치된 패키지의 문제)."
        )
        if stopped:
            out.append(
                "다음에 할 일: 나머지 단계(pytest, mypy 등)도 확인하려면 `--keep-going` 으로 다시 실행하세요."
            )
    elif fails:
        out.append(
            "결론: 실패한 단계가 있습니다. 위 출력의 마지막 줄들을 먼저 확인하세요(GitHub 결제 문제와는 무관한 실제 실패입니다)."
        )
    else:
        out.append(
            "결론: 실행한 단계는 모두 통과했습니다. 건너뛴 단계는 이 PC 에서 대신할 수 없었던 것이라 GitHub 결과와 다를 수 있습니다."
        )
    return "\n".join(out)


def cmd_ci_local(args) -> int:
    from audit_kit.config import load_config

    root = load_config(args.path).root
    opts = Options(
        names=args.workflow or None,
        only_job=args.job,
        run_installs=args.run_installs,
        keep_going=args.keep_going,
    )
    code, results, notes = run_ci(root, opts)
    print(render(results, notes))
    if args.remote:
        print()
        for line in remote_explain(root):
            print(line)
    return code


def register(sub) -> None:
    p = sub.add_parser(
        "ci-local", help="GitHub Actions 워크플로(.github/workflows)의 run 단계를 로컬에서 실행"
    )
    p.add_argument("--path", default=None)
    p.add_argument(
        "--workflow", action="append", help="이 워크플로만 실행(파일명, 여러 번 지정 가능)"
    )
    p.add_argument("--job", default=None, help="이 job 만 실행")
    p.add_argument(
        "--run-installs",
        action="store_true",
        help="pip install 단계도 실행(현재 파이썬 환경이 바뀜)",
    )
    p.add_argument(
        "--keep-going",
        action="store_true",
        help="단계가 실패해도 나머지 단계를 계속 실행(기본은 GitHub 처럼 첫 실패에서 멈춤)",
    )
    p.add_argument(
        "--remote", action="store_true", help="원격(GitHub) 최근 검사 상태도 한국어로 설명"
    )
    p.set_defaults(func=cmd_ci_local)

"""audit-kit ci-local: GitHub Actions 워크플로를 로컬에서 실행."""

import json
import os
from pathlib import Path

import pytest
from audit_kit import cilocal, cli, ghcheck
from audit_kit.cilocal import (
    Options,
    StepResult,
    find_bash,
    make_shims,
    python_launcher,
    remote_explain,
    render,
    run_ci,
    run_job,
    select_workflows,
    setup_python_version,
    strip_installs,
    triggers,
)

PUSH_YML = """\
name: CI
on:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: 설치
        run: |
          python -m pip install --upgrade pip
          pip install -e .
      - name: 테스트
        run: echo test-step
"""
SCHEDULE_YML = """\
name: Mutation
on:
  schedule:
    - cron: "0 0 * * 0"
  workflow_dispatch:
jobs:
  m:
    runs-on: ubuntu-latest
    steps:
      - run: echo never
"""


def _write_wf(root: Path, name: str, text: str) -> None:
    folder = root / ".github" / "workflows"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


def _recorder(results: dict | None = None):
    """(runner, 호출 기록). results: 스크립트에 들어 있는 문자열 -> 종료코드."""
    calls: list = []

    def runner(bash, script, cwd, env, timeout):
        calls.append({"script": script, "cwd": cwd, "env": env, "timeout": timeout})
        for needle, rc in (results or {}).items():
            if needle in script:
                return rc, f"출력:{needle}"
        return 0, "ok"

    return runner, calls


def _ctx(tmp_path: Path, runner, run_installs: bool = False) -> dict:
    return {
        "root": tmp_path,
        "env": {},
        "bash": "bash",
        "runner": runner,
        "run_installs": run_installs,
    }


# ------------------------------------------------------------ 워크플로 읽기
def test_triggers_handles_yaml11_on_key_and_all_shapes(tmp_path):
    _write_wf(tmp_path, "a.yml", PUSH_YML)
    wf = cilocal.load_workflow(tmp_path / ".github" / "workflows" / "a.yml")
    assert True in wf and "on" not in wf  # YAML 1.1: on -> True (PyYAML 공식 문서)
    assert triggers(wf) == {"push"}
    assert triggers({"on": "push"}) == {"push"}
    assert triggers({True: ["push", "pull_request"]}) == {"push", "pull_request"}
    assert triggers({}) == set()


def test_select_runs_only_push_or_pr_and_explains_skips(tmp_path):
    _write_wf(tmp_path, "ci.yml", PUSH_YML)
    _write_wf(tmp_path, "mutation.yml", SCHEDULE_YML)
    chosen, skipped = select_workflows(tmp_path)
    assert [p.name for p, _ in chosen] == ["ci.yml"]
    assert len(skipped) == 1 and "mutation.yml" in skipped[0] and "schedule" in skipped[0]
    chosen, _ = select_workflows(tmp_path, ["mutation"])  # 이름을 지정하면 트리거 무시
    assert [p.name for p, _ in chosen] == ["mutation.yml"]


def test_multiline_step_name_is_joined(tmp_path):
    """32 저장소 ci.yml 처럼 name 이 여러 줄로 이어진 경우."""
    _write_wf(
        tmp_path,
        "ci.yml",
        "on: push\njobs:\n  t:\n    steps:\n      - name: ruff (경고만 —\n          여러 줄 이름)\n        run: echo x\n",
    )
    chosen, _ = select_workflows(tmp_path)
    runner, _calls = _recorder()
    res = run_job(_ctx(tmp_path, runner), "ci.yml", "t", chosen[0][1]["jobs"]["t"])
    assert res[0].name == "ruff (경고만 — 여러 줄 이름)"


# ------------------------------------------------------------ 보조 함수
def test_strip_installs():
    assert strip_installs("python -m pip install -U pip\npip install -e .\n") is None
    assert strip_installs("pip install x\npytest -q\n") == "pytest -q\n"
    assert strip_installs("pip install a \\\n  b\n") is None  # 줄 잇기: 안전하게 못 자름
    assert (
        strip_installs("echo pip install\n") == "echo pip install\n"
    )  # 줄 시작이 pip 이 아니면 그대로


def test_setup_python_version_reads_quoted_number():
    assert (
        setup_python_version([
            {"uses": "actions/setup-python@v5", "with": {"python-version": "3.14"}}
        ])
        == "3.14"
    )
    assert (
        setup_python_version([{"uses": "actions/setup-python@v5", "with": {"python-version": 3.1}}])
        == "3.1"
    )
    assert setup_python_version([{"run": "echo"}]) is None


def test_make_shims_write_lf_exec_scripts(tmp_path):
    make_shims(tmp_path, ["py", "-3.14"])
    assert (tmp_path / "python").read_bytes() == b'#!/bin/sh\nexec py -3.14 "$@"\n'
    assert (tmp_path / "pip").read_bytes() == b'#!/bin/sh\nexec py -3.14 -m pip "$@"\n'


# ------------------------------------------------------------ 단계 실행 규칙
def test_job_skips_uses_expressions_conditions_and_installs(tmp_path):
    job = {
        "steps": [
            {"uses": "actions/checkout@v4"},
            {"name": "식", "run": "echo ${{ github.sha }}"},
            {"name": "조건", "if": "github.ref == 'x'", "run": "echo a"},
            {"name": "powershell", "shell": "pwsh", "run": "Write-Host a"},
            {"name": "설치", "run": "pip install -e ."},
            {"name": "진짜", "run": "echo real"},
        ]
    }
    runner, calls = _recorder()
    res = run_job(_ctx(tmp_path, runner), "ci.yml", "t", job)
    assert [r.status for r in res] == ["skip"] * 5 + ["pass"]
    assert "actions/checkout" in res[0].detail
    assert "${{" in res[1].detail and "if:" in res[2].detail and "pwsh" in res[3].detail
    assert "pip install" in res[4].detail and "--run-installs" in res[4].detail
    assert len(calls) == 1 and calls[0]["script"] == "echo real"


def test_mixed_install_and_test_script_runs_only_the_test_line(tmp_path):
    runner, calls = _recorder()
    job = {"steps": [{"run": "pip install -e .\npytest -q\n"}]}
    run_job(_ctx(tmp_path, runner), "ci.yml", "t", job)
    assert calls[0]["script"] == "pytest -q\n"
    runner2, calls2 = _recorder()
    run_job(_ctx(tmp_path, runner2, run_installs=True), "ci.yml", "t", job)
    assert "pip install" in calls2[0]["script"]


def test_first_failure_stops_job_but_continue_on_error_does_not(tmp_path):
    runner, calls = _recorder({"boom": 3, "soft": 1})
    job = {
        "steps": [
            {"name": "a", "run": "echo soft", "continue-on-error": True},
            {"name": "b", "run": "echo boom"},
            {"name": "c", "run": "echo after"},
        ]
    }
    res = run_job(_ctx(tmp_path, runner), "ci.yml", "t", job)
    assert [r.status for r in res] == ["warn", "fail", "skip"]
    assert "앞 단계가 실패" in res[2].detail
    assert len(calls) == 2  # c 는 실행되지 않음


def test_env_working_directory_and_timeout_are_passed(tmp_path):
    runner, calls = _recorder()
    job = {
        "timeout-minutes": 2,
        "env": {"A": "job"},
        "steps": [{"run": "echo x", "env": {"B": 1}, "working-directory": "sub"}],
    }
    run_job(_ctx(tmp_path, runner), "ci.yml", "t", job)
    assert calls[0]["env"] == {"A": "job", "B": "1"}
    assert calls[0]["cwd"] == tmp_path / "sub"
    assert calls[0]["timeout"] == 120


def test_keep_going_runs_steps_after_a_failure(tmp_path):
    runner, calls = _recorder({"boom": 3})
    job = {"steps": [{"name": "a", "run": "echo boom"}, {"name": "b", "run": "echo after"}]}
    res = run_job({**_ctx(tmp_path, runner), "keep_going": True}, "ci.yml", "t", job)
    assert [r.status for r in res] == ["fail", "pass"]
    assert len(calls) == 2


def test_run_ci_passes_keep_going_and_still_exits_1(tmp_path):
    _write_wf(
        tmp_path,
        "ci.yml",
        "on: push\njobs:\n  t:\n    steps:\n      - run: echo boom\n      - run: echo after\n",
    )
    runner, calls = _recorder({"boom": 1})
    code, results, _ = run_ci(tmp_path, Options(keep_going=True), runner=runner, bash="bash")
    assert code == 1 and [r.status for r in results] == ["fail", "pass"] and len(calls) == 2


def test_env_dependent_failure_is_flagged_and_concluded_differently(tmp_path):
    runner, _calls = _recorder({"pip-audit": 1})
    job = {"steps": [{"name": "audit", "run": "pip-audit"}, {"name": "t", "run": "pytest -q"}]}
    res = run_job(_ctx(tmp_path, runner), "ci.yml", "t", job)
    assert res[0].status == "fail" and res[0].env_dependent
    text = render(res, [])
    assert "파이썬 환경 전체를 검사하는 도구" in text and "--keep-going" in text
    # 진짜 실패가 섞이면 환경 탓으로 돌리지 않는다
    mixed = render([res[0], StepResult("ci.yml", "t", "pytest", "fail", "x", 1.0)], [])
    assert "실제 실패" in mixed and "환경 전체" not in mixed


# ------------------------------------------------------------ run_ci 종료코드
def test_run_ci_exit_codes(tmp_path, monkeypatch):
    assert run_ci(tmp_path, bash="bash")[0] == 2  # 워크플로 없음
    _write_wf(tmp_path, "ci.yml", PUSH_YML)
    monkeypatch.setattr(cilocal, "find_bash", lambda: None)
    code, _, notes = run_ci(tmp_path)
    assert code == 2 and any("Git Bash" in n for n in notes)
    runner, _calls = _recorder()
    code, results, _ = run_ci(tmp_path, runner=runner, bash="bash")
    assert code == 0 and [r.status for r in results] == ["skip", "skip", "pass"]
    runner, _calls = _recorder({"test-step": 1})
    assert run_ci(tmp_path, runner=runner, bash="bash")[0] == 1


def test_run_ci_reports_missing_python_version(tmp_path, monkeypatch):
    _write_wf(
        tmp_path,
        "ci.yml",
        "on: push\njobs:\n  t:\n    steps:\n      - uses: actions/setup-python@v5\n        with:\n          python-version: '9.99'\n      - run: echo x\n",
    )
    monkeypatch.setattr(cilocal, "python_launcher", lambda v: None)
    code, _, notes = run_ci(tmp_path, bash="bash")
    assert code == 2 and "9.99" in notes[0]


def test_render_has_korean_conclusion_for_pass_and_fail():
    ok = render(
        [
            StepResult("ci.yml", "t", "테스트", "pass", "", 1.0),
            StepResult("ci.yml", "t", "설치", "skip", "이유"),
        ],
        [],
    )
    assert "통과 1 / 실패 0" in ok and "모두 통과" in ok and "— 이유" in ok
    bad = render([StepResult("ci.yml", "t", "테스트", "fail", "a\nb", 1.0)], [])
    assert "실패한 단계가 있습니다" in bad and "결제 문제와는 무관" in bad


# ------------------------------------------------------------ 원격 설명
def _fake_remote(monkeypatch, runs, annotations):
    monkeypatch.setattr(ghcheck, "repo_slug", lambda root: "o/r")
    monkeypatch.setattr(ghcheck, "head_sha", lambda root: "abc")
    monkeypatch.setattr(ghcheck, "fetch_check_runs", lambda root, repo, sha: runs)
    monkeypatch.setattr(ghcheck, "_run_gh", lambda args, cwd, timeout=30: json.dumps(annotations))


def test_remote_explain_translates_billing_failure(monkeypatch, tmp_path):
    runs = [
        {
            "name": "test",
            "conclusion": "failure",
            "output": {
                "annotations_url": "https://api.github.com/repos/o/r/check-runs/1/annotations"
            },
        }
    ]
    _fake_remote(
        monkeypatch,
        runs,
        [{"message": "The job was not started because recent account payments have failed"}],
    )
    text = "\n".join(remote_explain(tmp_path))
    assert "코드 문제가 아니라 GitHub 결제" in text and "Billing & plans" in text


def test_remote_explain_shows_other_failures_and_success(monkeypatch, tmp_path):
    runs = [
        {
            "name": "test",
            "conclusion": "failure",
            "output": {"annotations_url": "https://api.github.com/x"},
        }
    ]
    _fake_remote(monkeypatch, runs, [{"message": "pytest failed: 3 errors"}])
    assert "pytest failed" in "\n".join(remote_explain(tmp_path))
    _fake_remote(monkeypatch, [{"name": "t", "conclusion": "success"}], [])
    assert "모두 통과" in remote_explain(tmp_path)[0]
    _fake_remote(monkeypatch, [], [])
    assert "검사 기록이 없습니다" in remote_explain(tmp_path)[0]


def test_remote_explain_reports_gh_error_instead_of_raising(monkeypatch, tmp_path):
    def boom(root):
        raise ghcheck.GhCheckError("gh 없음")

    monkeypatch.setattr(ghcheck, "repo_slug", boom)
    assert "gh 없음" in remote_explain(tmp_path)[0]


# ------------------------------------------------------------ Git Bash 선택
@pytest.mark.skipif(os.name != "nt", reason="Windows 전용 경로 규칙")
def test_find_bash_prefers_git_bash_over_wsl_bash_on_path(monkeypatch, tmp_path):
    git_dir = tmp_path / "Git"
    (git_dir / "cmd").mkdir(parents=True)
    (git_dir / "bin").mkdir()
    (git_dir / "cmd" / "git.exe").write_bytes(b"")
    fake_bash = git_dir / "bin" / "bash.exe"
    fake_bash.write_bytes(b"")
    monkeypatch.delenv("AUDIT_KIT_BASH", raising=False)
    monkeypatch.setattr(
        cilocal.shutil,
        "which",
        lambda name: (
            str(git_dir / "cmd" / "git.exe")
            if name == "git"
            else str(tmp_path / "System32" / "bash.exe")
        ),
    )
    assert find_bash() == str(fake_bash)


def test_find_bash_honors_explicit_env(monkeypatch, tmp_path):
    custom = tmp_path / "mybash"
    custom.write_bytes(b"")
    monkeypatch.setenv("AUDIT_KIT_BASH", str(custom))
    assert find_bash() == str(custom)


# ------------------------------------------------------------ 실제 Git Bash / py 런처
needs_bash = pytest.mark.skipif(find_bash() is None, reason="Git Bash 없음")


@needs_bash
def test_e2e_real_bash_runs_steps_and_failure_gives_exit_code_1(tmp_path, capsys):
    _write_wf(
        tmp_path,
        "ci.yml",
        "on: push\njobs:\n  t:\n    steps:\n      - name: ok\n        run: echo 안녕-step\n      - name: bad\n        run: |\n          echo about-to-fail\n          exit 3\n      - name: never\n        run: echo unreachable\n",
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname='p'\nversion='0'\n", encoding="utf-8")
    assert cli.main(["ci-local", "--path", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "[통과] ok" in out and "[실패] bad" in out and "about-to-fail" in out
    assert "[건너뜀] never" in out and "앞 단계가 실패해 건너뜀" in out
    assert "unreachable" not in out  # 실패 뒤 단계는 실제로 실행되지 않았다


@needs_bash
@pytest.mark.skipif(python_launcher("3.14") is None, reason="Python 3.14 없음")
def test_e2e_setup_python_shim_makes_python_point_to_requested_version(tmp_path):
    _write_wf(
        tmp_path,
        "ci.yml",
        "on: push\njobs:\n  t:\n    steps:\n      - uses: actions/setup-python@v5\n        with:\n          python-version: '3.14'\n      - name: ver\n        run: |\n          python -c \"import sys; print('PYVER', sys.version_info[:2])\"\n          pip --version\n",
    )
    code, results, _ = run_ci(tmp_path)
    step = next(r for r in results if r.name == "ver")
    assert code == 0 and step.status == "pass"
    assert "PYVER (3, 14)" in step.detail
    assert "3.14" in step.detail.splitlines()[-1]  # pip --version 도 같은 버전을 가리킴

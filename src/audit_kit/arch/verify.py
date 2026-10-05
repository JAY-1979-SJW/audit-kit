"""`audit-kit new` 완료 기준 자동 검증: arch check(0건) → ruff → mypy → pytest
(→ 선택적 `pip install -e .`). ROADMAP.md 2단계 완료 기준과 동일하다.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from audit_kit.arch.scan import scan
from audit_kit.arch.spec import ArchSpec
from audit_kit.config import AuditConfig
from audit_kit.runner import Proc, run_module
from audit_kit.scope import build_project_graph


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class VerifyResult:
    steps: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)


def _proc_detail(p: Proc) -> str:
    return (p.stdout[-2000:] + "\n" + p.stderr[-500:]).strip()


def _target_python_launcher(root: Path) -> list[str]:
    """생성된 프로젝트의 `requires-python` 을 만족하는 인터프리터를 고른다.

    `run_module`/`run_python`(runner.py)은 항상 audit-kit 자신을 실행 중인 인터프리터
    (`sys.executable`)를 쓴다 — audit-kit이 대상 프로젝트의 venv 안에서 실행될 때를 가정한 것이라
    `arch check`/`arch fix`처럼 기존 프로젝트를 다룰 땐 맞다. 하지만 `new`가 방금 만든 프로젝트는
    아직 자기 venv가 없고, audit-kit 자신은 다른 버전(예: 3.11)으로 돌고 있을 수 있어 `pip install`이
    `pyproject.toml`의 `requires-python`(32 템플릿 기준 `>=3.14`)과 안 맞아 실패한다(2026-09-27 실측:
    "Package 'x' requires a different Python: 3.11.0 not in '>=3.14'"). Windows `py` 런처로 그
    버전을 직접 지정해서 돈다. `py`나 그 버전이 없으면 `sys.executable` 로 되돌아간다(경고만 남긴다).
    """
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return [sys.executable]
    requires = str(data.get("project", {}).get("requires-python", ""))
    match = re.search(r"(\d+)\.(\d+)", requires)
    if not match:
        return [sys.executable]
    version = f"{match.group(1)}.{match.group(2)}"
    probe = subprocess.run(
        ["py", f"-{version}", "--version"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    if probe.returncode == 0:
        return ["py", f"-{version}"]
    return [sys.executable]


def _pip_install_editable(root: Path, timeout: int = 600) -> Proc:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.setdefault("NO_COLOR", "1")
    launcher = _target_python_launcher(root)
    try:
        cp = subprocess.run(
            [*launcher, "-m", "pip", "install", "-e", "."],
            cwd=str(root),
            capture_output=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        stdout = err.stdout.decode("utf-8", errors="replace") if err.stdout else ""
        return Proc(-1, stdout, f"시간 초과({timeout}s)")
    return Proc(
        cp.returncode,
        cp.stdout.decode("utf-8", errors="replace"),
        cp.stderr.decode("utf-8", errors="replace"),
    )


def verify_project(
    root: Path, spec: ArchSpec, package_name: str, full: bool = False
) -> VerifyResult:
    """생성 직후 완료 기준을 순서대로 확인한다. 실패한 단계가 있으면 보고서를 남긴다."""
    result = VerifyResult()
    cfg = AuditConfig(packages=[package_name])
    cfg.root = root

    graph = build_project_graph(cfg)
    violations = scan(cfg, spec, graph)
    result.steps.append(StepResult("arch check", not violations, f"위반 {len(violations)}건"))

    ruff = run_module("ruff", ["check", "."], root)
    result.steps.append(StepResult("ruff", ruff.returncode == 0, _proc_detail(ruff)))

    mypy = run_module("mypy", ["src"], root)
    result.steps.append(StepResult("mypy", mypy.returncode == 0, _proc_detail(mypy)))

    pytest_run = run_module("pytest", ["-q"], root)
    result.steps.append(StepResult("pytest", pytest_run.returncode == 0, _proc_detail(pytest_run)))

    if full:
        install = _pip_install_editable(root)
        result.steps.append(
            StepResult("pip install -e .", install.returncode == 0, _proc_detail(install))
        )

    if not result.ok:
        _write_report(root, result)
    return result


def _write_report(root: Path, result: VerifyResult) -> None:
    ts = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
    out_dir = root / "audit-reports" / f"new_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = ["# audit-kit new 완료 기준 검증 실패", ""]
    for s in result.steps:
        lines.append(f"## {s.name}: {'통과' if s.ok else '실패'}")
        if not s.ok and s.detail:
            lines += ["```", s.detail, "```"]
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")

"""`audit-kit init`: 대상 프로젝트에 설정·hook·스킬을 설치한다. 기존 설정은 덮어쓰지 않는다."""

from __future__ import annotations

import json
import subprocess
import sys
from importlib import resources
from pathlib import Path

from audit_kit._proc import no_window_kwargs
from audit_kit.config import detect_packages, read_pyproject
from audit_kit.runner import module_available
from audit_kit.textio import (
    TextFormat,
    append_text,
    read_source,
    read_text,
    write_source,
)

# 위(상위 계층) → 아래(하위 계층). 같은 줄은 같은 높이.
LAYER_CANDIDATES = [
    ["api", "routers", "routes", "endpoints", "views", "cli"],
    ["services", "service", "usecases"],
    ["repositories", "repository", "crud"],
    ["models", "schemas", "domain", "entities"],
    ["core", "db", "database", "utils", "common", "config"],
]


def python_cmd() -> str:
    return Path(sys.executable).as_posix()


def _template(name: str) -> str:
    return (
        resources
        .files("audit_kit")
        .joinpath("templates", *name.split("/"))
        .read_text(encoding="utf-8")
    )


def _layers_for(root: Path, pkg: str) -> list:
    base = root / pkg if (root / pkg).is_dir() else root / "src" / pkg
    if not base.is_dir():  # 잘못된 패키지 이름 — 초안 없이 진행 (init 전체가 죽지 않게)
        return []
    subs = {d.name for d in base.iterdir() if d.is_dir() and (d / "__init__.py").is_file()} | {
        f.stem for f in base.glob("*.py") if f.stem != "__init__"
    }
    layers = []
    for level in LAYER_CANDIDATES:
        found = [n for n in level if n in subs]
        if found:
            layers.append(" : ".join(found))  # ':' = 같은 층, 서로 임포트 허용 (import-linter 2.1+)
    return layers


def _sqlalchemy_major():
    from importlib.metadata import PackageNotFoundError, version

    try:
        return int(version("sqlalchemy").split(".")[0])
    except PackageNotFoundError:
        return None  # 설치 안 됨
    except (ValueError, IndexError):
        return None  # 버전 문자열이 예상 형식이 아님


def pyproject_snippet(root: Path, packages: list, existing_tool: dict) -> str:
    parts = []
    if "ruff" not in existing_tool:
        parts.append("""[tool.ruff]
line-length = 120
extend-exclude = ["migrations", "alembic", "audit-reports"]

[tool.ruff.lint]
# E/W: PEP8, F: pyflakes(미사용 임포트·정의 안 된 이름), I: 임포트 정렬,
# B: 버그 유발 패턴, UP: 구식 문법, SIM: 단순화, C4: 컴프리헨션
select = ["E", "W", "F", "I", "B", "UP", "SIM", "C4"]
ignore = ["E501"]

[tool.ruff.lint.flake8-bugbear]
# FastAPI Depends()/Query() 기본값은 B008 예외
extend-immutable-calls = ["fastapi.Depends", "fastapi.Query", "fastapi.Body", "fastapi.Path", "fastapi.File", "fastapi.Form", "fastapi.Header"]
""")
    if "mypy" not in existing_tool:
        plugins = []
        if module_available("pydantic"):
            plugins.append('"pydantic.mypy"')
        sa = _sqlalchemy_major()
        if sa is not None and sa < 2:
            plugins.append('"sqlalchemy.ext.mypy.plugin"')
        parts.append(f"""[tool.mypy]
ignore_missing_imports = true
check_untyped_defs = true
warn_unused_ignores = true
warn_redundant_casts = true
no_implicit_optional = true
exclude = ["^migrations/", "^alembic/", "^audit-reports/"]
plugins = [{", ".join(plugins)}]
# SQLAlchemy 2.0은 Mapped[] 문법을 쓰면 플러그인 불필요. 1.x면 sqlalchemy.ext.mypy.plugin 사용.
""")
    if "importlinter" not in existing_tool and packages:
        pkg = packages[0]
        layers = _layers_for(root, pkg)
        root_pkgs = ", ".join(f'"{p}"' for p in packages)
        if len(layers) >= 2:
            layer_lines = "\n".join(f'    "{lay}",' for lay in layers)
            contract = f'''[[tool.importlinter.contracts]]
# 자동 생성 초안 — 프로젝트 구조에 맞게 반드시 검토. 위가 상위 계층(아래 계층만 임포트 가능).
name = "{pkg} 계층 구조"
type = "layers"
containers = ["{pkg}"]
layers = [
{layer_lines}
]
'''
        else:
            contract = f'''# [[tool.importlinter.contracts]]
# name = "{pkg} 계층 구조"
# type = "layers"
# containers = ["{pkg}"]
# layers = ["api", "services", "models", "core"]
'''
        parts.append(f'''[tool.importlinter]
root_packages = [{root_pkgs}]

{contract}
# 예) 도면 계산 로직이 플러그인/웹 계층을 모르게 강제
# [[tool.importlinter.contracts]]
# name = "ezdxf 계산 로직 독립"
# type = "forbidden"
# source_modules = ["{pkg}.core"]
# forbidden_modules = ["{pkg}.api", "{pkg}.plugin", "fastapi"]
''')
    if "coverage" not in existing_tool:
        parts.append("""[tool.coverage.run]
branch = true
omit = ["tests/*", "*/migrations/*", "*/alembic/*"]

[tool.coverage.report]
show_missing = true
""")
    if "audit-kit" not in existing_tool:
        pk = ", ".join(f'"{p}"' for p in packages)
        parts.append(f"""[tool.audit-kit]
packages = [{pk}]
coverage_target = 80
radon_min_rank = "C"
hook_mode = "block"   # block: 문제 시 Claude가 반드시 수정 / warn: 경고만
# 수정 모드(/audit-fix)에서 사용자 승인 없이 고치지 않을 파일 (glob)
protected_paths = []
# 전체 옵션은 audit-kit README 참고
""")
    if not parts:
        return ""
    return "\n# ===== audit-kit 표준 설정 =====\n" + "\n".join(parts)


class SettingsError(Exception):
    pass


def merge_settings(settings_path: Path, command: str) -> bool:
    """settings.json 에 hook 병합. 기존 파일이 JSON 으로 읽히지 않으면 **건드리지 않고** 오류를 낸다."""
    data: dict = {}
    fmt = TextFormat()
    if settings_path.is_file():
        text, fmt = read_source(settings_path)
        try:
            data = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as e:
            raise SettingsError(
                f"{settings_path} 가 올바른 JSON 이 아닙니다 (줄 {e.lineno}). "
                "파일을 고친 뒤 다시 실행하거나 --no-hook 으로 건너뛰세요. 파일은 변경하지 않았습니다."
            ) from e
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("hooks", {}), dict)
            or not isinstance(data.get("hooks", {}).get("PostToolUse", []), list)
        ):
            raise SettingsError(
                f"{settings_path} 의 구조가 예상과 다릅니다 (hooks.PostToolUse 가 목록이 아님). 변경하지 않았습니다."
            )
    hooks = data.setdefault("hooks", {})
    post = hooks.setdefault("PostToolUse", [])
    added = True
    for entry in post:
        for h in entry.get("hooks", []) if isinstance(entry, dict) else []:
            if "audit_kit hook" in h.get("command", ""):
                h["command"] = command  # 파이썬 경로가 바뀌었을 수 있으니 갱신
                added = False
    if added:
        post.append({
            "matcher": "Edit|Write|MultiEdit",
            "hooks": [{"type": "command", "command": command, "timeout": 120}],
        })
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    write_source(settings_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", fmt)
    return added


def _install_precommit_hooks(root: Path) -> str:
    """`pre-commit install --hook-type pre-push`를 바로 실행한다.

    GitHub 개인 계정 + 비공개 저장소는 branch protection/rulesets API 가 막혀 있어(GitHub Pro
    필요 — 2026-09-27 `gh api` 실측 확인: 403 "Upgrade to GitHub Pro or make this repository
    public") 서버 쪽 required status check 를 못 쓴다. pre-push 단계가 그 대안(cli.py 의
    `pre-push` 명령 참고)인데, 사람이 설치 명령을 직접 쳐야 하면 잊어버려서 실제로는 안 걸릴 수
    있다 — init 이 여기서 바로 설치까지 끝내야 실제로 강제된다.

    **pre-commit 단계(ruff-fix, audit-kit-check)는 여기서 자동 설치하지 않는다** — 2026-09-27
    실측으로 확인한 실제 회귀: 그 단계까지 자동 설치하면 `audit-kit fix`(fixflow.py)가 세션 안에서
    직접 만드는 커밋까지 매번 걸려서, 훅이 파일을 조용히 고쳐 쓰면 "커밋됐어야 할 게 커밋 안 된"
    상태가 된다(원래 `fix done`은 자기 검증을 이미 끝낸 뒤 커밋하므로 또 걸릴 필요가 없다 —
    fixflow.py 의 커밋에 `--no-verify`를 붙여 방어했지만, 사람이 직접 하는 `git commit`까지
    막을 수는 없다). pre-commit 단계는 기존대로 "직접 설치" 안내만 남긴다.

    `python_cmd()`(=sys.executable) 를 쓴다: audit-kit 의 다른 실행부(runner.py)와 같은 전제 —
    audit-kit 자신이 대상 프로젝트 venv 안에서 실행 중이라고 가정한다.
    """
    if not (root / ".git").is_dir():
        return "  (git 저장소 아님 — `git init` 뒤 `pre-commit install --hook-type pre-push` 를 직접 실행하세요)"
    if not module_available("pre_commit"):
        return (
            "  (pre-commit 미설치 — pip install pre-commit && "
            "pre-commit install --hook-type pre-push 를 직접 실행하세요. "
            "커밋 단계(ruff-fix 등)까지 쓰려면 --hook-type pre-commit 도 추가)"
        )
    try:
        p = subprocess.run(
            [python_cmd(), "-m", "pre_commit", "install", "--hook-type", "pre-push"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
            **no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"  ! pre-commit install 실패: {e}"
    if p.returncode == 0:
        return "  + pre-commit install --hook-type pre-push 완료 (push 전 로컬 게이트 활성화됨)"
    return f"  ! pre-commit install 실패: {(p.stderr or p.stdout).strip()[:300]}"


def _step_pyproject(root: Path, packages: list) -> list:
    pp = root / "pyproject.toml"
    existing = read_pyproject(root).get("tool", {}) if pp.is_file() else {}
    snippet = pyproject_snippet(root, packages, existing)
    if not snippet:
        return ["= pyproject.toml 설정 이미 있음 (변경 없음)"]
    append_text(pp, snippet)  # 기존 파일의 줄바꿈·인코딩 유지
    added = ", ".join(
        s for s in ["ruff", "mypy", "importlinter", "coverage", "audit-kit"] if s not in existing
    )
    return [f"+ pyproject.toml 에 설정 추가 ({added})"]


def _step_hook(root: Path, py: str) -> list:
    cmd = f'"{py}" -m audit_kit hook'
    try:
        added = merge_settings(root / ".claude" / "settings.json", cmd)
        return [
            ("+ " if added else "= ")
            + ".claude/settings.json PostToolUse hook "
            + ("등록" if added else "이미 있음(경로 갱신)")
        ]
    except SettingsError as e:
        return [f"! hook 등록 건너뜀: {e}"]


def _step_skills(root: Path, py: str) -> list:
    log = []
    skill_dir = root / ".claude" / "skills" / "audit"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        _template("skill/SKILL.md").replace("{{PYTHON}}", py), encoding="utf-8"
    )
    cl = skill_dir / "checklist.md"
    if not cl.exists():
        cl.write_text(_template("skill/checklist.md"), encoding="utf-8")
        log.append("+ .claude/skills/audit/ (SKILL.md, checklist.md)")
    else:
        log.append("+ .claude/skills/audit/SKILL.md 갱신 (checklist.md 는 기존 유지)")
    arch_dir = root / ".claude" / "skills" / "arch"
    arch_dir.mkdir(parents=True, exist_ok=True)
    (arch_dir / "SKILL.md").write_text(
        _template("skill-arch/SKILL.md").replace("{{PYTHON}}", py), encoding="utf-8"
    )
    log.append("+ .claude/skills/arch/SKILL.md (/arch: 구조 설계 → 스캔 → 수정)")
    fix_dir = root / ".claude" / "skills" / "audit-fix"
    fix_dir.mkdir(parents=True, exist_ok=True)
    (fix_dir / "SKILL.md").write_text(
        _template("skill-fix/SKILL.md").replace("{{PYTHON}}", py), encoding="utf-8"
    )
    log.append("+ .claude/skills/audit-fix/SKILL.md (/audit-fix: 감사 결과 브랜치에서 건별 수정)")
    return log


def _step_precommit(root: Path) -> list:
    pc = root / ".pre-commit-config.yaml"
    if pc.exists():
        return ["= .pre-commit-config.yaml 이미 있음 — 템플릿의 audit-kit 항목을 수동 병합하세요"]
    pc.write_text(_template("pre-commit-config.yaml"), encoding="utf-8")
    return ["+ .pre-commit-config.yaml", _install_precommit_hooks(root)]


def _step_gitignore(root: Path) -> list:
    gi = root / ".gitignore"
    text = read_text(gi) if gi.is_file() else ""
    if "audit-reports" in text:
        return []
    append_text(gi, "audit-reports/\n")
    return ["+ .gitignore 에 audit-reports/ 추가"]


def init_project(
    root: Path, packages=None, with_hook: bool = True, with_precommit: bool = True
) -> list:
    """`audit-kit init` 본체. 단계마다 `_step_*()`로 나눠뒀다(STD-08: 원래 이 함수 하나가
    문장 51개로 상한(50)을 살짝 넘었다, 2026-09-28)."""
    root = root.resolve()
    log = []
    packages = packages or detect_packages(root)
    if not packages:
        log.append(
            "! 패키지를 찾지 못함 — --packages 로 지정하거나 [tool.audit-kit] packages 를 직접 채우세요"
        )
    log += _step_pyproject(root, packages)
    py = python_cmd()
    if with_hook:
        log += _step_hook(root, py)
    log += _step_skills(root, py)
    if with_precommit:
        log += _step_precommit(root)
    log += _step_gitignore(root)
    return log

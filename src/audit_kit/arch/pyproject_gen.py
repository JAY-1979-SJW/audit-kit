"""새 프로젝트의 `pyproject.toml` 생성.

`[project]` 헤더(이름·설명·유형별 의존성)는 여기서 만들고, ruff/mypy/import-linter/coverage/
`[tool.audit-kit]` 설정은 기존 `init_project.pyproject_snippet()` 을 그대로 재사용한다
(중복 구현하지 않는다 — 32. Claude 개발표준 CLAUDE.md 1장).
"""

from __future__ import annotations

from pathlib import Path

from audit_kit.init_project import pyproject_snippet
from audit_kit.textio import TextFormat, write_source

# 유형별 런타임 의존성. dev 의존성은 32. Claude 개발표준의 표준 목록과 동일하게 맞춘다.
TYPE_DEPENDENCIES: dict[str, list] = {
    "fastapi": ["fastapi", "uvicorn", "pydantic-settings"],
    "cad": ["pywin32", "ezdxf"],
    "mcp": ["mcp"],
    "cli": ["typer"],
    "desktop": ["PySide6"],
    "library": [],
}
DEV_DEPENDENCIES = [
    "ruff",
    "mypy",
    "pylint",
    "pytest",
    "pytest-cov",
    "pytest-timeout",
    "pre-commit",
    "bandit",
]


def _dependencies(types: list, has_db: bool) -> list:
    deps: list = []
    for t in types:
        for d in TYPE_DEPENDENCIES.get(t, []):
            if d not in deps:
                deps.append(d)
    if has_db and "sqlalchemy" not in deps:
        deps += ["sqlalchemy", "alembic"]
    return deps


def _dev_dependencies(types: list, has_db: bool) -> list:
    """표준 dev 목록 + db_api_fitness.py 가 만드는 적합성 테스트가 실제로 쓰는 도구.

    tbls(Go 바이너리)는 pip 로 못 깔아 여기 안 넣는다(README 안내로 대신함, docs_registry.local.toml
    참고). squawk-cli/migradiff/schemathesis 는 pip 설치 가능함을 2026-09-27 실측으로 확인."""
    deps = list(DEV_DEPENDENCIES)
    if has_db:
        deps += ["squawk-cli", "migradiff"]
    if "fastapi" in types:
        deps.append("schemathesis")
    return deps


def _script_name(package_name: str, entrypoint: str, single: bool) -> str:
    return package_name if single else entrypoint.rsplit(".", 1)[-1]


def _header(
    package_name: str, description: str, types: list, entrypoints: list, has_db: bool
) -> str:
    deps = _dependencies(types, has_db)
    dep_lines = "\n".join(f'    "{d}",' for d in deps)
    dev_deps = ", ".join(f'"{d}"' for d in _dev_dependencies(types, has_db))
    scripts_block = ""
    if entrypoints:
        single = len(entrypoints) == 1
        lines = "\n".join(
            f'{_script_name(package_name, e, single)} = "{e}:main"' for e in entrypoints
        )
        scripts_block = f"\n[project.scripts]\n{lines}\n"
    return f'''[project]
name = "{package_name}"
version = "0.1.0"
description = "{description}"
requires-python = ">=3.14"
dependencies = [
{dep_lines}
]

[project.optional-dependencies]
dev = [{dev_deps}]
{scripts_block}
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

# pytest 9.0+ 네이티브 TOML 설정(공식 문서 확인: [tool.pytest]). architecture 마커는
# tests/architecture/test_fitness.py(db 관심사가 있을 때만 생성)가 쓴다.
# pythonpath: 설치("pip install -e .") 전에도 src/ 의 패키지를 바로 임포트할 수 있게 한다
# (ROADMAP 완료 기준 "생성 직후 pytest 통과"는 설치 이전 단계이므로 필요).
[tool.pytest]
minversion = "9.0"
testpaths = ["tests"]
pythonpath = ["src"]
markers = ["architecture: 구조 적합성 테스트(계층 분리·마이그레이션 head 등)"]
'''


def generate_pyproject(
    root: Path,
    package_name: str,
    description: str,
    types: list,
    entrypoints: list,
    has_db: bool = False,
) -> Path:
    """`[project]` 헤더 + 유형별 의존성 + `init_project` 의 표준 도구 설정을 합쳐 pyproject.toml 을 쓴다."""
    text = _header(package_name, description, types, entrypoints, has_db)
    text += pyproject_snippet(root, [package_name], {})
    path = root / "pyproject.toml"
    write_source(path, text, TextFormat())
    return path

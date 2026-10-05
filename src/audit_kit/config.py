"""대상 프로젝트의 pyproject.toml [tool.audit-kit] 설정 로드."""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

EXCLUDE_DIRS = {
    ".venv",
    "venv",
    "env",
    ".git",
    "node_modules",
    "build",
    "dist",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "tests",
    "test",
    "migrations",
    "alembic",
    "audit-reports",
}


@dataclass
class AuditConfig:
    # 감사 대상 최상위 패키지 이름(제품 코드). 비우면 자동 탐지.
    packages: list = field(default_factory=list)
    # 보조 코드 역할 지정: {"tests": ["tests"], "scripts": ["scripts", "*.py"], "plugins": [...], "migrations": [...]}
    # 비우면 폴더 이름으로 자동 분류. 어디에도 속하지 않는 파일은 '미지정'으로 보고된다.
    support: dict = field(default_factory=dict)
    # 분석에서 뺄 경로(glob). 빠진 파일 수는 리포트에 사유와 함께 표시된다.
    exclude_paths: list = field(default_factory=list)
    # 커버리지 목표(%)
    coverage_target: float = 80.0
    report_dir: str = "audit-reports"
    run_tests: bool = True
    pytest_args: list = field(default_factory=list)

    # ruff: 치명으로 볼 규칙 접두어(문법 오류, 정의 안 된 이름 등 실행 시 즉시 터지는 것)
    ruff_critical: list = field(default_factory=lambda: ["E9", "F63", "F7", "F82"])
    # ruff: 무시 등급으로 볼 규칙 접두어(순수 스타일)
    ruff_ignore: list = field(
        default_factory=lambda: ["E1", "E2", "E3", "E5", "W", "I", "N", "D", "Q", "COM"]
    )

    # bandit: 심각도와 무관하게 치명으로 볼 규칙 (B602 shell=True, B605 os.system 계열, B608 SQL 문자열 조합)
    bandit_critical: list = field(default_factory=lambda: ["B602", "B605", "B608"])
    # bandit: LOW 심각도지만 개선으로 올릴 규칙 (하드코딩 비밀번호)
    bandit_promote: list = field(default_factory=lambda: ["B105", "B106", "B107"])
    pytest_timeout: int = 900

    vulture_min_confidence: int = 60
    # 이 신뢰도 이상이면 개선, 미만이면 무시
    vulture_improve_confidence: int = 80
    vulture_whitelist: str = "vulture_whitelist.py"
    vulture_ignore_decorators: list = field(
        default_factory=lambda: [
            "@app.*",
            "@router.*",
            "@*.get",
            "@*.post",
            "@*.put",
            "@*.patch",
            "@*.delete",
            "@*.on_event",
            "@*.middleware",
            "@*.exception_handler",
            "@*.validator",
            "@*.field_validator",
            "@*.model_validator",
            "@*.fixture",
            "@pytest.*",
            "@*.listens_for",
        ]
    )
    vulture_ignore_names: list = field(
        default_factory=lambda: [
            "model_config",
            "Config",
            "__tablename__",
            "__table_args__",
            "__abstract__",
        ]
    )

    # 구조 검사: 이 줄 수를 넘는 제품 모듈을 '거대 모듈'로 보고
    max_module_lines: int = 1000

    # radon: 이 등급 이상(더 나쁜 쪽) 함수를 보고. A(좋음)~F(나쁨)
    radon_min_rank: str = "C"

    # AI 리뷰 휴리스틱
    session_factories: list = field(
        default_factory=lambda: [
            "SessionLocal",
            "Session",
            "AsyncSession",
            "sessionmaker",
            "async_sessionmaker",
            "scoped_session",
            "get_session",
        ]
    )
    router_globs: list = field(
        default_factory=lambda: [
            "**/api/**/*.py",
            "**/routers/**/*.py",
            "**/routes/**/*.py",
            "**/endpoints/**/*.py",
        ]
    )
    router_max_statements: int = 15

    # mypy: 실제 버그일 가능성이 높아 치명으로 올릴 오류 코드
    mypy_critical: list = field(default_factory=lambda: ["name-defined", "call-arg", "syntax"])

    # 수정 모드(fix): 자동 수정 대상에서 제외할 경로(glob). 프로젝트 CLAUDE.md 의 보호 파일 등
    protected_paths: list = field(default_factory=list)
    fix_branch_prefix: str = "audit-fix/"

    # Claude Code hook
    # auto: 전역 hook(32. 개발표준 등)이 ruff/mypy 를 돌리면 설계 검사만, 없으면 ruff+mypy+설계
    # design: 설계 검사만 / all: 항상 ruff+mypy+설계
    hook_tools: str = "auto"
    hook_mypy: bool = True
    # block: 문제 시 exit 2로 Claude가 반드시 수정 / warn: 경고만 전달
    hook_mode: str = "block"

    # 런타임에 채워짐
    root: Path = field(default_factory=Path.cwd)

    def package_paths(self) -> list:
        paths = []
        for pkg in self.packages:
            for base in (self.root, self.root / "src"):
                p = base / pkg.replace(".", "/")
                if p.is_dir() or p.with_suffix(".py").is_file():
                    paths.append(p if p.is_dir() else p.with_suffix(".py"))
                    break
        return paths

    def rel(self, path) -> str:
        p = Path(path)
        with contextlib.suppress(ValueError):  # path 가 root 밖이면 절대경로 그대로 둔다
            p = p.resolve().relative_to(self.root.resolve())
        return p.as_posix()


def find_project_root(start: Path) -> Path:
    start = start.resolve()
    for p in [start, *start.parents]:
        if (p / "pyproject.toml").is_file():
            return p
    return start


def read_pyproject(root: Path) -> dict:
    f = root / "pyproject.toml"
    if not f.is_file():
        return {}
    with f.open("rb") as fh:
        return tomllib.load(fh)


def detect_packages(root: Path) -> list:
    found: list[str] = []
    for base in (root, root / "src"):
        if not base.is_dir():
            continue
        found.extend(
            d.name
            for d in sorted(base.iterdir())
            if d.is_dir()
            and d.name not in EXCLUDE_DIRS
            and not d.name.startswith(".")
            and (d / "__init__.py").is_file()
        )
    return found


def load_config(root=None) -> AuditConfig:
    root = find_project_root(Path(root) if root else Path.cwd())
    data = read_pyproject(root).get("tool", {}).get("audit-kit", {})
    known = {f.name for f in fields(AuditConfig)} - {"root"}
    kwargs = {k.replace("-", "_"): v for k, v in data.items()}
    unknown = set(kwargs) - known
    if unknown:
        # STD-03(print 대신 logging) 대상: 설정 로딩 시점 1회성 경고라 logging 설정 전이라
        # print 를 쓴다. cli.py 처럼 파일 전체를 빼기엔 이 파일에 이 한 줄뿐이라 그대로 두고
        # report 에 improve 로 남긴다(2026-09-28, 억지로 지우면 RUF103 오탐만 늘어 확인함).
        print(f"[audit-kit] 알 수 없는 설정 무시: {', '.join(sorted(unknown))}", file=sys.stderr)
    cfg = AuditConfig(**{k: v for k, v in kwargs.items() if k in known})
    cfg.root = root
    if not cfg.packages:
        cfg.packages = detect_packages(root)
    return cfg

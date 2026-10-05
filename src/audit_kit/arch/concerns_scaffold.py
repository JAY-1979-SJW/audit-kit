"""`spec.concerns` 로 지정된 위치에 공통 관심사(설정·로깅·DB·COM·HTTP)의 기본 구현을 생성한다.

`db` 관심사가 있으면 "사용자별 데이터" 패턴(리포지토리 함수의 첫 인자는 항상 소유자 식별자)
예시 스텁과, alembic 골격 + 마이그레이션 head 적합성 테스트도 함께 만든다.
"""

from __future__ import annotations

from pathlib import Path

from audit_kit.arch.layout import layer_folder
from audit_kit.arch.spec import ArchSpec
from audit_kit.textio import TextFormat, write_source

CONFIG_BODY = '''"""애플리케이션 설정 로더 — 환경 변수는 이 모듈에서만 읽는다."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    debug: bool = False


def load_settings() -> Settings:
    return Settings(debug=os.getenv("DEBUG", "").lower() in ("1", "true"))
'''

LOGGING_BODY = '''"""로깅 설정 — 진입점에서 한 번만 호출한다. 모듈은 logging.getLogger(__name__) 만 쓴다."""

import logging.config


def setup_logging(level: str = "INFO") -> None:
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"default": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
        "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "default"}},
        "root": {"handlers": ["console"], "level": level},
    })
'''

# SQLAlchemy 2.0 세션 패턴(공식 문서 "Session Basics" 확인): sessionmaker 는 모듈 레벨에서 한 번
# 만들고, 세션 수명은 이 contextmanager 하나로 관리한다(commit/rollback/close 를 함수마다 반복 안 함).
DB_BODY = '''"""DB 세션 팩토리 — 세션은 전역으로 두지 않고 매 호출 주입한다(SQLAlchemy 2.0 스타일)."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

_engine = None
_session_factory: sessionmaker | None = None


def init_engine(url: str) -> None:
    global _engine, _session_factory
    _engine = create_engine(url)
    _session_factory = sessionmaker(_engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    if _session_factory is None:
        raise RuntimeError("init_engine() 을 먼저 호출하세요")
    with _session_factory() as session, session.begin():
        yield session
'''

# pywin32 공식 QuickStart 확인: win32com.client.Dispatch("Object.Name") 로 생성한다.
COM_BODY = '''"""COM 자동화 어댑터 — 연결·재시도·해제를 이 클래스 한 곳에 모은다."""


class ComAdapter:
    def __init__(self, prog_id: str) -> None:
        self._prog_id = prog_id
        self._app = None

    def connect(self):
        import win32com.client

        if self._app is None:
            self._app = win32com.client.Dispatch(self._prog_id)
        return self._app

    def close(self) -> None:
        self._app = None
'''

# httpx 공식 compatibility 문서 확인: requests 와 달리 리다이렉트를 기본으로 따라가지 않는다
# (필요하면 호출부에서 follow_redirects=True 를 넘긴다). 기본 타임아웃은 httpx 가 이미 적용한다.
HTTP_BODY = '''"""외부 HTTP 클라이언트 — 타임아웃·재시도를 이 클래스 한 곳에서 관리한다.

httpx 는 requests 와 달리 리다이렉트를 기본으로 따라가지 않는다. 필요하면 요청마다
follow_redirects=True 를 넘긴다.
"""

import httpx


class HttpClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()
'''

CONCERN_BODY = {
    "config": CONFIG_BODY,
    "logging": LOGGING_BODY,
    "db": DB_BODY,
    "com": COM_BODY,
    "http": HTTP_BODY,
}

REPOSITORY_BODY = '''"""사용자별 데이터 접근 스텁 — 첫 인자는 항상 소유자 식별자(user_id)로 통일한다.

다른 사용자의 user_id 로 조회한 데이터에 접근하지 않는다.
"""

from sqlalchemy.orm import Session


def get_items_for_user(session: Session, user_id: str) -> list:
    """user_id 소유의 데이터만 반환한다."""
    raise NotImplementedError
'''

# Alembic 공식 튜토리얼 확인: alembic.ini 의 필수 키는 script_location 뿐이고,
# sqlalchemy.url 은 DB 연결 문자열이다. env.py 는 target_metadata 를 offline 마이그레이션에 쓴다.
ALEMBIC_INI = """[alembic]
script_location = migrations
sqlalchemy.url = sqlite:///./app.db
"""

ALEMBIC_ENV = '''"""alembic 마이그레이션 환경 — 실제 프로젝트의 모델에 맞게 target_metadata 를 채우세요."""

from alembic import context

target_metadata = None


def run_migrations_offline() -> None:
    context.configure(url="sqlite:///./app.db", target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


run_migrations_offline()
'''

# 공식 튜토리얼(alembic.sqlalchemy.org, "Create a Migration Script") 확인: revision·down_revision
# 만 있으면 유효한 마이그레이션 스크립트다 — head 가 0개면 test_single_migration_head 가 항상
# 실패하므로(2026-09-28 실측: db concern 있는 6유형 중 fastapi/cli 에서 재현), 초기 빈 리비전을
# 함께 만들어 생성 직후 head 1개가 보장되게 한다.
ALEMBIC_INITIAL_REVISION = '''"""initial

Revision ID: 0001_initial
Revises:
"""

from __future__ import annotations

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
'''

FITNESS_TEST = '''"""아키텍처 적합성 테스트 — DB 마이그레이션이 분기 없이 head 1개인지 확인한다."""

from pathlib import Path

import pytest

pytest.importorskip("alembic")

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.architecture
def test_single_migration_head() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, f"마이그레이션 head 가 여러 개입니다: {heads} (분기를 병합하세요)"
'''


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        write_source(path, text, TextFormat())
    return path


def _module_path(root: Path, module: str) -> Path:
    return (root / "src" / Path(*module.split("."))).with_suffix(".py")


def generate_concerns(spec: ArchSpec, root: Path, package_name: str) -> list:
    """`spec.concerns` 위치마다 기본 구현을 생성하고, `db` 가 있으면 부가물(리포지토리 예시·
    alembic 골격·`tests/architecture/test_fitness.py`)도 만든다. 생성한 파일 경로 목록을 반환.
    """
    created: list = []
    for concern, places in spec.concerns.items():
        body = CONCERN_BODY.get(concern)
        if body is None or not places:
            continue
        created.append(_write(_module_path(root, places[0]), body))

    if spec.concerns.get("db"):
        repo_folder = layer_folder("repository", spec.types) or "repositories"
        created.append(
            _write(
                root / "src" / package_name / repo_folder / "user_repository.py",
                REPOSITORY_BODY,
            )
        )
        created.append(_write(root / "alembic.ini", ALEMBIC_INI))
        created.append(_write(root / "migrations" / "env.py", ALEMBIC_ENV))
        created.append(
            _write(
                root / "migrations" / "versions" / "0001_initial.py",
                ALEMBIC_INITIAL_REVISION,
            )
        )
        created.append(_write(root / "tests" / "architecture" / "__init__.py", ""))
        created.append(_write(root / "tests" / "architecture" / "test_fitness.py", FITNESS_TEST))
    return created

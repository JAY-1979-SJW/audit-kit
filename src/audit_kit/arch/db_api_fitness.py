"""DB(Squawk/tbls/migra)·API(Schemathesis) 적합성 테스트 스캐폴딩 (ROADMAP 4단계).

기존 `concerns_scaffold.py`의 `db` 관심사가 이미 alembic 골격 + 마이그레이션 head 1개 검사
fitness test를 만든다 — 이 파일은 거기에 4개 도구를 **추가**한다(새 CI 메커니즘을 만들지 않고
기존 `tests/architecture/*.py` pytest 패턴을 그대로 따른다. `test_single_migration_head`가
`pytest.importorskip`으로 우아하게 건너뛰듯, 여기 테스트들도 도구가 없거나 전제(Postgres 등)가
안 맞으면 `pytest.skip`한다 — 그래야 어떤 CI가 이 프로젝트를 돌리든(peer 세션이 만드는 base
ci.yml 포함) `pytest`만 실행하면 자동으로 같이 도는, CI 워크플로 자체를 건드리지 않는 방식이다).

**2026-09-27 실측 검증 상태** (추측과 확인 구분, 근거는 `~/.claude/docs_registry.local.toml`):
- **Squawk**: 실제 설치(`pip install squawk-cli`)해 위험한 DDL(`ADD COLUMN ... NOT NULL`)에서
  exit 1, 안전한 SQL에서 exit 0 확인. `alembic upgrade head --sql | squawk` 조합도 공식 CLI
  문서의 stdin 지원("cat migration.sql | squawk", `--stdin-filepath`)을 그대로 따름.
- **Schemathesis**: 실제 설치해 공식 in-process ASGI 패턴(`schemathesis.openapi.from_asgi`
  + `@schema.parametrize()`)을 실제 FastAPI 앱(정상/버그 주입 둘 다)에 돌려 통과·실패 둘 다
  확인. 서버를 띄울 필요가 없다.
- **tbls**: [미확인] Go 바이너리라 이 환경에 설치해 실행하지 못했다 — 공식 문서(SQLite DSN 지원,
  `tbls diff`가 문서와 실제 스키마 차이를 보고)만 근거로 작성했다.
- **migra**: [미확인] Postgres 전용 도구라 이 환경(Postgres/Docker 데몬 없음)에서 실제 DB 비교는
  못 했다 — CLI 자체는 설치·실행 확인(`migra --from-file`, `psycopg` 필요함을 실측으로 확인).
  범용 "모델 vs 마이그레이션 드리프트" 검사는 프로젝트마다 모델 위치가 달라 자동 완성할 수 없어,
  자리와 실행 골격만 제공하고 기본은 건너뛴다(`DATABASE_URL` 없으면 skip) — 사람이 채워야 한다.
"""

from __future__ import annotations

from pathlib import Path

from audit_kit.arch.layout import layer_folder
from audit_kit.arch.spec import ArchSpec
from audit_kit.textio import TextFormat, write_source

# squawkhq.com/docs/ 확인: 기본 설정으로 충분하면 파일을 안 만들어도 되지만, 프로젝트마다
# pg_version 을 명시하는 걸 공식 문서가 권장(버전별 규칙이 다름 — 예: 11 이하는 다른 잠금 규칙).
SQUAWK_CONFIG = """# Squawk(https://squawkhq.com) 설정 — Postgres 마이그레이션 정적 린트.
# 프로젝트가 쓰는 실제 Postgres 버전으로 바꾸세요.
pg_version = "15.0"
"""

# github.com/k1LoW/tbls 확인: 최소 설정은 dsn + docPath 뿐. 기본 스캐폴딩은 sqlite(alembic.ini
# 의 sqlalchemy.url 과 동일한 파일)를 그대로 가리킨다 — Postgres로 옮기면 dsn만 바꾸면 된다.
TBLS_CONFIG = """# tbls(https://github.com/k1LoW/tbls) 설정 — DB 문서 자동 생성 + 드리프트 검사.
dsn: sqlite:///./app.db
docPath: doc/schema
"""

SQUAWK_LINT_TEST = '''"""아키텍처 적합성 테스트 — 마이그레이션에 위험한 DDL 패턴이 없는지 Squawk 로 확인한다.

Squawk(정적 SQL 린터, DB 연결 불필요)은 실제 적용될 SQL이 필요하다 - `alembic upgrade head
--sql`로 오프라인 렌더링한 뒤 stdin 으로 넘긴다(공식 CLI 문서: squawkhq.com/docs/cli).
2026-09-27 실측: 위험한 DDL(NOT NULL 컬럼 추가 등)에서 exit 1, 안전하면 exit 0.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.architecture
def test_migrations_pass_squawk_lint() -> None:
    if not shutil.which("squawk"):
        pytest.skip("squawk 미설치 - pip install squawk-cli")
    rendered = subprocess.run(
        ["alembic", "upgrade", "head", "--sql"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if rendered.returncode != 0 or not rendered.stdout.strip():
        pytest.skip(f"alembic --sql 렌더링 실패(마이그레이션이 아직 없을 수 있음): {rendered.stderr[:200]}")
    result = subprocess.run(
        ["squawk", "--stdin-filepath", "migration.sql"],
        input=rendered.stdout,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"Squawk 가 위험한 마이그레이션 패턴을 발견했습니다:\\n{result.stdout}"
'''

TBLS_DOC_TEST = '''"""아키텍처 적합성 테스트 - DB 문서(doc/schema)가 실제 스키마와 다르면 실패한다.

tbls(https://github.com/k1LoW/tbls)는 SQLite 를 직접 지원한다(.tbls.yml 은
db_api_fitness.py 가 생성). 문서가 오래되면(스키마는 바뀌었는데 `tbls doc` 를 안 돌렸으면)
`tbls diff`가 차이를 보고한다.

[미확인] 이 프로젝트를 만든 환경엔 tbls(Go 바이너리)가 없어 실제 실행을 확인하지 못했다 -
공식 문서의 명령·용도 설명만 근거로 작성했다. 처음 받는 사람이 한 번은 직접 확인해야 한다.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.architecture
def test_db_docs_match_schema() -> None:
    if not shutil.which("tbls"):
        pytest.skip("tbls 미설치 - https://github.com/k1LoW/tbls#install")
    if not (PROJECT_ROOT / "app.db").exists():
        pytest.skip("app.db 없음 - 먼저 `alembic upgrade head` 로 마이그레이션을 적용하세요")
    result = subprocess.run(
        ["tbls", "diff"], cwd=PROJECT_ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, (
        f"DB 문서가 최신이 아닙니다. `tbls doc` 로 갱신하고 커밋하세요:\\n{result.stdout}"
    )
'''

MIGRA_DRIFT_TEST = '''"""아키텍처 적합성 테스트 자리(스텁) - ORM 모델과 실제 마이그레이션 결과 스키마의 드리프트.

migra(활발히 유지되는 포크: github.com/postgresql-tools/migra, PyPI 패키지명 migradiff, CLI
이름은 이전 그대로 `migra`)로 "마이그레이션을 head 까지 적용한 실제 DB"와 "SQLAlchemy 모델
메타데이터로 새로 만든 DB"를 비교해 드리프트를 잡을 수 있다. **Postgres 전용**이라(SQLite 미지원)
기본 스캐폴딩(sqlite)에는 자동으로 안 걸리게 `DATABASE_URL`(Postgres 접속 문자열) 환경변수가
있을 때만 실행한다.

모델 메타데이터의 실제 위치는 프로젝트마다 달라 자동으로 채울 수 없다 - `_shadow_db_url()` 을
실제 프로젝트에 맞게 채우세요(예: 임시 스키마에 `Base.metadata.create_all()`).

[미확인] 이 환경엔 Postgres/Docker 데몬이 없어 실제 비교를 실행해보지 못했다 - migra CLI 자체
(`migra --from-file`, psycopg 필요)는 설치·실행까지 확인했다. Postgres 로 전환한 뒤 사람이
한 번 직접 채우고 확인해야 하는 자리다.
"""

import os
import shutil
import subprocess

import pytest


def _shadow_db_url() -> str | None:
    """모델 메타데이터로 새로 만든 비교 대상 DB의 접속 문자열. 프로젝트에 맞게 채우세요."""
    return None


@pytest.mark.architecture
def test_no_schema_drift_from_models() -> None:
    if not shutil.which("migra"):
        pytest.skip("migra 미설치 - pip install migradiff")
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        pytest.skip("DATABASE_URL 없음 - migra 는 Postgres 전용, sqlite 기본 설정에선 해당 없음")
    shadow_url = _shadow_db_url()
    if shadow_url is None:
        pytest.skip("_shadow_db_url() 이 아직 안 채워짐 - 이 프로젝트의 모델 메타데이터 위치에 맞게 채우세요")
    result = subprocess.run(
        ["migra", db_url, shadow_url], capture_output=True, text=True, check=False
    )
    assert not result.stdout.strip(), f"모델과 마이그레이션 사이에 드리프트가 있습니다:\\n{result.stdout}"
'''

API_CONTRACT_TEST = '''"""아키텍처 적합성 테스트 - FastAPI 앱이 자신의 OpenAPI 계약을 실제로 지키는지 확인한다.

Schemathesis 공식 in-process ASGI 패턴(schemathesis.readthedocs.io/en/stable/guides/
python-apps/): 실제 서버를 띄우지 않고 앱 함수를 직접 호출해 빠르고 네트워크 문제가 없다.
2026-09-27 실측: 정상 앱은 통과, 버그(예: ZeroDivisionError로 500)가 있으면 실제로 잡아냄을
직접 확인했다.

이 골격은 `{app_import}` 에서 `app`(FastAPI 인스턴스)을 가져온다 - 아직 실제 FastAPI() 앱을
안 만들었으면(방금 생성된 골격은 최소 스텁이라 없을 수 있음) 건너뛴다. 실제 앱을 만들면 자동으로
검사가 시작된다.
"""

import pytest

try:
    from {app_import} import app
except ImportError:
    app = None

if app is not None:
    import schemathesis

    schema = schemathesis.openapi.from_asgi("/openapi.json", app)

    @schema.parametrize()
    def test_api_contract(case) -> None:
        case.call_and_validate()
else:

    @pytest.mark.architecture
    def test_api_contract() -> None:
        pytest.skip(
            "{app_import} 에 app(FastAPI 인스턴스)이 아직 없음 - 만들면 자동으로 계약 테스트가 시작됩니다"
        )
'''


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        write_source(path, text, TextFormat())
    return path


def generate_db_fitness(spec: ArchSpec, root: Path) -> list:
    """`db` 관심사가 있으면 Squawk/tbls/migra 설정·적합성 테스트를 추가한다."""
    if not spec.concerns.get("db"):
        return []
    created: list = []
    created.append(_write(root / ".squawk.toml", SQUAWK_CONFIG))
    created.append(_write(root / ".tbls.yml", TBLS_CONFIG))
    created.append(_write(root / "tests" / "architecture" / "__init__.py", ""))
    created.append(
        _write(root / "tests" / "architecture" / "test_migration_lint.py", SQUAWK_LINT_TEST)
    )
    created.append(_write(root / "tests" / "architecture" / "test_db_docs.py", TBLS_DOC_TEST))
    created.append(
        _write(root / "tests" / "architecture" / "test_schema_drift.py", MIGRA_DRIFT_TEST)
    )
    return created


def generate_api_contract_fitness(spec: ArchSpec, root: Path, package_name: str) -> list:
    """`fastapi` 유형이면 Schemathesis 계약 테스트를 추가한다(OpenAPI 가 자동 생성되는
    유일한 유형이라 fastapi 전용 - templates.py TYPES 참고)."""
    if "fastapi" not in spec.types:
        return []
    folder = layer_folder("interface", spec.types) or "api"
    app_import = f"{package_name}.{folder}.core"
    body = API_CONTRACT_TEST.format(app_import=app_import)
    path = root / "tests" / "architecture" / "test_api_contract.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        write_source(path, body, TextFormat())
        return [path]
    return []

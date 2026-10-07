# audit-kit

Python 프로그램의 **전체 설계 구조를 정하고(설계), 만들고(생성), 지키고(검사), 기존 코드를 그 구조로 옮기는(이행)** 도구.
구조·임포트·DB·보안 문제 감사 기능도 함께 들어 있다. 전체 계획: [docs/ROADMAP.md](docs/ROADMAP.md)

| 역할 분담 | |
|---|---|
| 32. Claude 개발표준 | 코딩 규칙, 저장 시 ruff·mypy (audit-kit hook 은 이것이 있으면 설계 검사만 한다) |
| 33. 중복코드 분석기 | 프로젝트 간 중복 코드 |
| **audit-kit** | **프로그램 하나의 전체 구조** |

```
[구조 설계]  architecture.toml 설계 → arch check(스캔) → arch fix(자동 수정) → 남은 것 수동 수정   (/arch)
[감사]      1 ruff → 2 mypy → 3 설계 검사(또는 import-linter) → 4 순환임포트+의존성그래프 → 5 vulture
            → 6 radon → 7 bandit → 8 pytest-cov → 휴리스틱 → 9 AI 설계 리뷰                    (/audit)
```

감사 결과는 **치명 / 개선 / AI 리뷰 필요 / 무시** 로 자동 분류되어 `audit-reports/<시각>/report.md` 에 남는다.

---

## 기준서 검사 `audit-kit std`

**다른 PC에서 개발된 코드가 개발 기준서를 지켰는지 검사**한다. 기준서(규칙 원장·ruff 설정·개발자 센터 등록부)는 패키지에 번들되어 있어 대상 프로젝트에 설정이 없어도 동작한다. 결과의 규칙 ID 는 기준서 조항(STD 표준 / DUP 중복 / EFF 효율 / ERR 오류 / DOC 문서 / SEC 비밀값)이고 근거 URL 이 함께 나온다. SEC-03(SECRET-SCAN)·SEC-04(GITIGNORE-SECRETS)는 `.py` 전체에서 이미 저장된 비밀값과 `.env`류의 `.gitignore` 누락을 찾으며, 값은 결과에 담지 않는다.

```
audit-kit std --path <프로젝트>          # 검사 + audit-reports/std_<시각>/report.md
audit-kit std --no-mypy                  # 없는 모듈·속성 검사(mypy) 생략
audit-kit std --fail-on improve          # 개선 이상이 있으면 종료코드 1 (CI)
audit-kit std --rules R.toml --ruff-config C.toml --registry D.toml   # 다른 기준서로 검사
```

| 단계 | 내용 |
|---|---|
| ① ruff | 번들된 기준서 `ruff.toml` 을 **강제 적용**(프로젝트 설정 무시). 결과를 조항에 매핑. 조항 없는 규칙은 '무시' 등급 |
| ② mypy | 없는 모듈·속성만 채택(ERR-01/02). 모듈에 없는 이름은 치명, 타입 표기 부족은 개선 |
| ③ 직접 구현(AST) | 같은 파일·DB·COM 을 반복해 열기(EFF-03), list `in`(EFF-02), 읽기 전용 openpyxl(EFF-04), 셀 값 하나씩 읽기(EFF-05), Office COM 정리(EFF-06). EFF-02·03·04·05 는 참고 등급, 절대경로(STD-02), 본문이 같은 함수(DUP-02), `_old`·복사본 파일(DUP-03), 루트 test 스크립트(STD-11), CLAUDE.md 200줄(DOC-01), 없는 import(ERR-01), 등록부에 없는 도구(DOC-02) |

- **환각 import 구분**: `pyproject.toml`·`requirements*.txt` 에 선언된 의존성, 또는 등록부에 있는 실존 도구가 이 환경에 없으면 '환경 미설치'(참고)로 낮춘다. 어디에도 없는 이름(환각 import)만 치명이다. 정확한 검사는 **대상 프로젝트의 venv 에서 실행**해야 한다.
- `try/except ImportError`, `TYPE_CHECKING`, `sys.version_info`·`sys.platform` 분기 안의 import 는 선택 import 로 본다.
- `tests/fixtures/` 는 검사하지 않는다.
- **검증**: 실제 코드 956개 파일 표본 검토(정밀도)와 결함 525개 심기(재현율 100%)로 확인했고, 그 과정에서 오탐·미탐 8종을 고쳤다. 결과와 알려진 한계는 `docs/검증결과_std.md`, 재실행은 `scripts/validation/`.
- **2026-10-08 실측 교훈 추가(원장 v3)**: 표준 모듈 이름 가림(STD-13, ruff A005 strict), stdout/stderr 한쪽만 UTF-8(STD-14), `parents[N]` 루트 계산 분산(STD-15, 참고), `__init__` 의 하위 패키지 재수출(STD-16, 참고), 도구를 못 돌리고 '0건 통과'하는 게이트(ERR-10), PyInstaller 진입 스크립트 상대 import·hiddenimports 손 나열(ERR-12), O_EXCL 잠금의 PermissionError 누락(ERR-13)은 직접 구현 검사. 이름 변경 인식 diff 게이트(ERR-11)·원자적 상태 파일 쓰기(ERR-14)·고정 포트 테스트 병렬 금지(ERR-15)·데스크톱 앱 시작/자식 프로세스 정리(FE-10/11)·사용자 데이터 폴더(OPS-18)는 manual(리뷰 가이드). `audit-kit std` 의 ruff 단계도 종료코드≠0·빈 출력이면 '0건'이 아니라 오류로 보고한다(ERR-10 자기 적용).
- **번들 갱신**: 원본은 `32. Claude 개발표준/standard`. 원본을 고친 뒤 `python scripts/sync_standard.py` (tests/test_std.py 의 번들 검사가 두 곳이 같은지 검사).
- 한계: 정적 검사라 크기·데이터에 따른 성능은 판단하지 못한다(EFF-04·05 는 참고 등급). Windows 전용 API 는 Mac/Linux 에서도 정적으로 검사된다.

## 빠른 설치

```bash
pip install "git+https://github.com/JAY-1979-SJW/audit-kit"
audit-kit --help
```

감사할 프로젝트의 가상환경(venv) 안에 설치한다(mypy·pytest 가 그 프로젝트의 의존성을 찾아야 한다). 라이선스: MIT.

## 1. 설치 (다른 PC)

> **중요:** audit-kit 은 **감사할 프로젝트의 가상환경(venv) 안에** 설치해야 한다.
> mypy·pytest 가 그 프로젝트의 의존성(fastapi, sqlalchemy, ezdxf …)을 찾아야 하기 때문이다.

### A. 인터넷 되는 PC

`audit-kit` 폴더를 복사한 뒤:

```powershell
.\scripts\install.ps1 -Project D:\work\MyProject
```

스크립트가 하는 일: 프로젝트의 `.venv` / `venv` / `env` 를 찾아 `pip install` → `audit-kit init` → `audit-kit doctor`.
수동으로 하려면:

```powershell
D:\work\MyProject\.venv\Scripts\activate
pip install C:\path\to\audit-kit
cd D:\work\MyProject
audit-kit init
```

### B. 인터넷 안 되는 PC (오프라인 묶음)

인터넷 되는 PC에서 (**대상 PC와 같은 OS·같은 파이썬 버전으로**):

```powershell
.\scripts\build_offline.ps1                          # → dist\wheelhouse\ (약 30개 wheel)
.\scripts\build_offline.ps1 -Python C:\Python312\python.exe   # 대상이 3.12면
```

`audit-kit` 폴더 전체(dist 포함)를 복사한 뒤 대상 PC에서:

```powershell
.\scripts\install.ps1 -Project D:\work\MyProject -Offline
```

### 요구 사항
- Python 3.9 이상 (3.11 권장)
- 선택: `pip install "audit-kit[graph]"` + Graphviz 설치 시 pydeps SVG 그래프 추가 생성
  (없어도 Mermaid 의존성 그래프 `deps.md` 는 항상 생성됨)

---

## 2. 프로젝트 적용 순서

```powershell
audit-kit init            # 설정·hook·스킬 설치 (기존 설정은 덮어쓰지 않음, 여러 번 실행해도 안전)
audit-kit arch init       # 구조 설계 초안 architecture.toml + ARCHITECTURE.md → 열어서 검토·확정
audit-kit arch check      # 설계 대비 위반 스캔
audit-kit arch fix        # 자동 수정 미리보기 → audit-kit arch fix --apply --verify
audit-kit whitelist       # vulture 오탐 화이트리스트 생성 → 열어서 '진짜 죽은 코드'는 지운다
audit-kit run             # 전체 감사
```

Claude Code 에서는 `/arch`(설계 확정 → 스캔 → 자동 수정 → 남은 위반 수동 수정) 다음 `/audit`(1~9단계 감사) 순서로 진행한다.

### `init` 이 만드는 것

| 대상 | 내용 |
|---|---|
| `pyproject.toml` | `[tool.ruff]` `[tool.mypy]` `[tool.importlinter]` `[tool.coverage]` `[tool.audit-kit]` 중 **없는 것만** 추가. import-linter 계층은 폴더 구조(api/services/models/core…)를 보고 초안 자동 생성. pydantic 이 있으면 `pydantic.mypy` 플러그인, SQLAlchemy 1.x 면 sqlalchemy mypy 플러그인 자동 추가 |
| `.claude/settings.json` | PostToolUse hook 등록 (기존 설정과 병합) |
| `.claude/skills/audit/` | `/audit` 스킬 + AI 리뷰 체크리스트(`checklist.md`, 프로젝트별로 수정 가능) |
| `.pre-commit-config.yaml` | ruff --fix + audit-kit check (없을 때만) |
| `.gitignore` | `audit-reports/` |

---

## 3. 구조 설계 → 스캔 → 수정 (`audit-kit arch`)

### 설계 파일 `architecture.toml`

프로젝트 루트에 두는 **구조의 단일 기준**. 초안은 `arch init` 이 폴더 이름(api/services/models/dxf/core …)과
실제 외부 라이브러리 사용을 보고 만든다. 사람이 검토해서 확정한다.

```toml
[project]
root_packages = ["myproject"]
entrypoints = ["myproject.main"]          # 조립만 하는 곳, 검사 제외

[rules]
allow_skip_layers = true                  # interface → infra 직접 임포트 허용
ignore_type_checking = true               # if TYPE_CHECKING: 임포트는 위반 아님
lazy_imports = "violation"                # 함수 안 지연 임포트도 계층 위반으로 봄
cycles = "forbid"
unassigned = "warn"

[[layers]]                                # 위 → 아래. 아래 → 위 임포트가 위반
name = "interface"
modules = ["myproject.api", "myproject.plugin"]
[[layers]]
name = "service"
modules = ["myproject.services"]
[[layers]]
name = "domain"
modules = ["myproject.models", "myproject.dxf"]
[[layers]]
name = "infra"
modules = ["myproject.core", "myproject.utils"]

[[external]]                              # 외부 라이브러리 사용 위치 제한
package = "fastapi"
allowed_in = ["interface", "entrypoints"]
[[external]]
package = "ezdxf"
allowed_in = ["myproject.dxf"]

[[forbidden]]
name = "도면 계산 로직은 웹/플러그인을 모른다"
source = ["myproject.dxf"]
forbidden = ["myproject.api", "myproject.plugin", "fastapi", "pyautocad"]

[[independent]]
name = "플러그인끼리 독립"
modules = ["myproject.plugin.autocad", "myproject.plugin.web"]
```

`ARCHITECTURE.md` 는 이 파일에서 생성된다(계층 표 + 실제 의존성 Mermaid 그래프, 위반은 빨간 선).

### 설계 v2 — 프로젝트 유형·계층 책임·공통 관심사 위치

`arch init` 은 사용하는 외부 라이브러리로 **프로젝트 유형**을 판단해 표준 설계를 채운다
(`fastapi` 웹 서버 / `cad` CAD·COM 자동화 / `mcp` MCP 도구 서버 / `cli` 배치 / `desktop` GUI / `library`). 섞여 있으면 합친다.

```toml
[project]
types = ["cad", "fastapi", "mcp"]

[[layers]]
name = "domain"
modules = ["app.quantity", "app.parsers"]
responsibility = "도면 해석·물량 계산 — ezdxf 데이터만 다루는 순수 로직"
must_not = ["COM(win32com)", "파일 저장", "UI"]

[concerns]                 # 이 작업은 지정 위치(+진입점)에서만 — 다른 곳에서 하면 ARCH-CONCERN 위반
config = ["app.core.config"]        # 환경 변수·.env 읽기
logging = ["app.core.logging_config"] # logging.basicConfig 등
db = ["app.core.database"]          # create_engine·sessionmaker·sqlite3.connect …
com = ["app.adapters.com_session"]  # win32com Dispatch·GetActiveObject …
http = ["app.core.http_client"]     # requests·httpx·urlopen
```

- 지정 위치 초안: ① 이름이 전용 모듈(`config`, `logging_config`, `database`, `com_session` …)인 곳 → ② 템플릿 권장 계층 안에서 가장 많이 쓰는 곳 → ③ 가장 많이 쓰는 곳(메모로 경고). 전용 모듈이 여러 개면 합치라고 메모한다.
- 임포트 별칭까지 따라간다 (`from win32com.client import Dispatch` → `Dispatch(...)` 도 COM 사용으로 인식).

### 검사 규칙 (`arch check`)

| 규칙 | 내용 |
|---|---|
| 계층 역방향 | 아래 계층이 위 계층 임포트 |
| 계층 건너뛰기 | `allow_skip_layers = false` 일 때 |
| 같은 계층 독립 | `siblings_independent = true` 인 계층의 형제끼리 |
| 진입점 임포트 | main 등 진입점을 다른 모듈이 임포트 |
| 금지 규칙 / 서로 독립 | `[[forbidden]]` `[[independent]]` |
| 외부 라이브러리 사용 위치 | `[[external]]` |
| 순환 임포트 | 모듈 최상위 import 순환 |
| 제품 → 보조 코드 | 제품 코드가 테스트·스크립트(치명)·플러그인·마이그레이션(개선)을 임포트 — 배포본에 없을 수 있는 코드에 의존 |
| 비공개 함수 사용 | 스크립트·플러그인이 제품의 `_함수`를 직접 사용 (테스트는 제외, `private_use = "ignore"` 로 끔) |
| 미배정 모듈 / 설계 불일치 | 어느 계층에도 없는 모듈, **제품에도 보조 코드에도 속하지 않는 폴더·파일**, 설계에 적었지만 코드에 없는 모듈 |

### 검사 범위 — 아무것도 조용히 빠지지 않는다

`audit-kit run` 은 먼저 프로젝트의 **모든 .py 파일**(git 추적 + 새 파일)을 분류해 리포트 맨 위에 보여 준다.

| 분류 | 기준 | 검사 |
|---|---|---|
| 제품 코드 | `packages` | 전체 (ruff·mypy·설계·vulture·radon·bandit·pytest) |
| 보조 코드 | 폴더 이름 자동 분류(tests/scripts/tools/*_plugins/alembic…, 루트 .py=scripts) 또는 `support` 설정 | ruff·bandit(테스트 제외)·설계(역방향·비공개 사용). vulture 는 보조 코드의 사용처도 '사용'으로 셈 |
| 복사본·백업 | `.claude/worktrees`, `*_backup*`, `*_old.py` 등이 git 에 들어 있음 | 분석 안 함, **저장소 관리 문제로 보고** |
| 제외 | 가상환경·빌드물, `exclude_paths` | 사유와 개수만 보고 |
| 미지정 | 위 어디에도 없음 (`temp/` 등) | **반드시 보고** — 역할을 정하거나 삭제 |

### 자동 수정 (`arch fix`)

프로젝트의 **임시 복사본**에서 한 건씩 고치고, 매번 ① 문법 컴파일 ② 설계 재검사(해당 위반 감소 + 새 위반 없음)
③ 변경 모듈 실제 임포트를 통과한 수정만 채택한다. 실패한 수정은 그 건만 되돌리고 사유를 남긴다.

| 전략 | 동작 |
|---|---|
| reexport | 상위 모듈을 거쳐 가져오던 이름을 원래 정의된 하위 모듈에서 직접 임포트 |
| unused | 쓰지 않는 위반 임포트 제거 |
| type_only | 타입 힌트에만 쓰는 임포트를 `if TYPE_CHECKING:` 으로 (어노테이션 문자열화). FastAPI 라우트·pydantic 필드처럼 런타임에 평가되는 곳에 쓰이면 건드리지 않음 |
| move | 상위 계층의 함수/클래스/상수를 그걸 쓰는 하위 모듈로 이동, 필요한 import 동반 복사, 다른 사용처 임포트 갱신, 원래 모듈이 계속 쓰면 재수출. 같은 모듈의 다른 함수·상태에 의존하면 이동하지 않음 |
| lazy | `--allow-lazy` 일 때만, 순환 전용. 함수 안 지연 임포트 (임시 처방) |

```powershell
audit-kit arch fix                    # 미리보기 (diff + audit-reports/arch-fix_*/arch-fix.md)
audit-kit arch fix --apply --verify   # 적용. --verify: pytest 결과를 원본과 비교해 새 실패가 있으면 중단
audit-kit arch undo                   # 마지막 적용 되돌리기 (백업에서 복원)
```

- git 저장소면 **커밋 안 된 파일은 수정하지 않는다** (`--force` 로 무시). 백업은 항상 `audit-reports/arch-fix_*/backup/` 에 남는다.
- 임포트 시 DB 연결 같은 부작용이 있는 프로젝트는 `--no-import-check`.
- 자동으로 못 고친 위반은 `arch-fix.md` 에 사유·수정 방향과 함께 남고, `/arch` 스킬이 Claude 로 한 건씩 수동 수정한다.
- 설계 파일이 있으면 hook 이 저장할 때마다 그 파일의 설계 위반을 Claude 에게 알려 준다.
- CI 에서 import-linter 를 쓰려면 `audit-kit arch export-importlinter` 로 같은 규칙을 계약으로 출력.

---

## 3-0. 구조 규칙 검사·자동 수정 (`audit-kit struct`) — 공식 문서 근거

```powershell
audit-kit struct check                     # 검사 (규칙마다 근거 문서 링크 표시)
audit-kit struct fix                       # 자동 수정 미리보기 (작업공간에서 검증)
audit-kit struct fix --apply --verify      # 적용 (테스트 비교). 되돌리기: audit-kit struct undo
```

| 규칙 | 근거 | 자동 수정 |
|---|---|---|
| `__init__.py` 없이 패키지로 쓰이는 폴더 | [PEP 420](https://peps.python.org/pep-0420/) | ✅ 빈 `__init__.py` 추가 (그 아래 모듈 실제 임포트 확인). tests·마이그레이션·설치 패키지와 같은 이름은 제외 |
| 내부 폴더를 최상위 이름으로 임포트 (`from util import …`) | [import system](https://docs.python.org/3/reference/import.html), [src layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/) | ✅ 제품 코드는 전체 경로로 교체 (대상이 하나로 확정될 때). 스크립트·테스트는 `--include-support` |
| 선언 안 된 외부 의존성 | [pyproject 가이드](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/) | ✅ `>=설치버전,<다음메이저` 로 추가 (제품 코드 → dependencies, 보조 코드만 → optional `dev`). 서식·주석 유지 |
| 상한 없는 의존성의 새 메이저 설치 | [Version specifiers](https://packaging.python.org/en/latest/specifications/version-specifiers/) | ⚙ `--pin-major` |
| `sys.path` 조작 | import system, src layout | 📋 안내 |
| 최소 지원 Python 버전 구문 오류 | pyproject 가이드 + [ast](https://docs.python.org/3/library/ast.html) (feature_version 은 best-effort) | 📋 해당 버전 **실제 인터프리터**로 확인 (`py -3.11`), 없으면 추정 |
| 임포트할 때 실행되는 코드 (print·basicConfig·mkdir·네트워크…) | import system | 📋 안내 |
| 표준 라이브러리와 같은 이름의 최상위 모듈 | import system | 📋 안내 |
| 거대 모듈 (`max_module_lines`, 기본 1000) | 설정값 | 📋 분할 제안 |

> 의존성 버전 하한은 **지금 실행 중인 환경에 설치된 버전**을 기준으로 한다. 반드시 프로젝트 가상환경에 audit-kit 을 설치해서 실행한다.

---

## 3-1. 수정 모드 (`audit-kit fix`) — 감사 결과를 브랜치에서 건별 수정

감사는 찾기만 한다. 찾은 문제를 **안전하게 고치는 절차**가 수정 모드다.
코드 수정은 Claude(`/audit-fix` 스킬) 또는 사람이 하고, audit-kit 은 범위·검증·기록을 강제한다.

```powershell
audit-kit run                      # 감사
audit-kit fix plan                 # 수정 계획 (기본: 치명만) → audit-reports/<시각>/fix-plan.md
audit-kit fix start                # 브랜치 audit-fix/<날짜> 생성 + 기존 실패 테스트 기록(기준선)
#   ... 한 건 수정 ...
audit-kit fix check F001           # 재검사: 해당 문제 해소 + 바뀐 파일에 새 치명 없음 + 구문 정상
audit-kit fix done F001            # 통과한 건만 커밋 (1건 = 1커밋, 여러 건은 fix done F001 F002)
audit-kit fix skip F004 --reason "테스트 데이터 파일 누락"
audit-kit fix status
audit-kit fix report               # 전체 테스트를 기준선과 비교 → fix-report.md (PR 본문으로 사용)
audit-kit fix end
```

| 안전장치 | 내용 |
|---|---|
| 브랜치 격리 | 항상 새 브랜치에서 작업. 커밋 안 된 변경이 있으면 시작 거부 |
| 건별 재검사 | 문제를 낸 도구(ruff/mypy/bandit/pytest/설계 검사)를 그 파일에 다시 돌려 확인 |
| 부작용 검사 | 바뀐 **모든** 파일에서 감사 때 없던 치명이 생기면 거부 (다른 파일을 망가뜨린 수정 차단) |
| 건별 커밋 | 한 건씩 커밋해 `git revert <커밋>` 으로 개별 되돌리기. `__pycache__`·`.coverage` 등 생성 파일은 커밋 안 함 |
| 보호 경로 | `protected_paths` 에 넣은 파일은 🔒 표시, 사용자 승인(`--force`) 없이 커밋 안 됨 |
| 테스트 기준선 | 시작 시 기존 실패를 기록하고, 마지막에 **새로 실패한 테스트만** 문제로 보고 |
| 기존 결함 목록 | `fix report` 가 기준선에도 있고 지금도 실패 중인 테스트를 대상 프로젝트의 `docs/known_preexisting_issues.json`(+ 사람이 읽는 `.md`)에 자동으로 쌓는다. `fix start` 는 이 목록과 대조해 이미 원인이 문서화된 게 몇 건인지 알려준다 — 다음 수정 세션이 같은 기존 실패를 또 조사하지 않게 하기 위함(2026-09-28, 다른 프로젝트 대규모 STD 정리 작업에서 이 낭비가 실측돼 반영). 원인(`note`)은 자동으로 채워지지 않으며, 알아내면 그 JSON 파일에 직접 적는다 |

계획 범위 조절: `--rules F821,call-arg` / `--files "mcp_server/*"` / `--include-review` / `--limit 10`

Claude Code 에서는 `/audit-fix` — 계획을 보여 주고 범위를 승인받은 뒤, 한 건씩 수정 → `fix check` → `fix done`,
두 번 실패하면 되돌리고 skip, 마지막에 `fix report` 로 새 실패 0건을 확인해 보고한다.

---

## 4. 명령

| 명령 | 설명 |
|---|---|
| `audit-kit run` | 전체 감사. `--no-tests` `--only ruff,mypy` `--fix`(ruff 자동수정) `--fail-on critical\|improve\|never` |
| `audit-kit check 파일…` | 파일 단위 빠른 검사(ruff·mypy·순환). pre-commit 용 |
| `audit-kit hook` | Claude Code PostToolUse hook (stdin JSON) |
| `audit-kit whitelist [--force]` | vulture 화이트리스트 생성 |
| `audit-kit doctor` | 도구 설치·설정 상태 점검 |
| `audit-kit init [--packages a,b] [--no-hook] [--no-precommit]` | 프로젝트 설정 |
| `audit-kit arch init / check / fix / undo / doc / export-importlinter` | 구조 설계·스캔·수정 (3장) |
| `audit-kit fix plan / start / check / done / skip / status / report / end` | 감사 결과 수정 모드 (3-1장) |

`audit-kit` 명령이 PATH 에 없으면 `python -m audit_kit run` 처럼 실행한다.

**CI:** `audit-kit run --no-tests --fail-on critical` → 치명이 있으면 종료코드 1.

---

## 5. 심각도 기준

도구가 명확한 근거를 주는 항목은 자동 분류, 설계 판단이 필요한 항목만 AI 리뷰로 넘긴다.

| 도구 | 치명 | 개선 | AI 리뷰 필요 | 무시 |
|---|---|---|---|---|
| ruff | E9·F63·F7·F82 (문법 오류, 정의 안 된 이름) | 그 외 (미사용 임포트, 버그 패턴…) | | E1~E5·W·I·N·D·Q·COM (순수 스타일) |
| mypy | name-defined·call-arg·syntax·없는 모듈 속성 임포트 | 그 외 error | | note |
| import-linter | | | 계약 위반 | |
| 순환 임포트 | (실제 ImportError 는 pytest 수집 오류로 치명) | 모듈 최상위 순환 | | |
| vulture | | 신뢰도 ≥ 80% | | < 80%, 함수 인자 |
| radon | | 등급 ≥ C | | |
| bandit | B602·B605·B608, HIGH(신뢰도 ≥ MEDIUM) | MEDIUM, B105~B107 | | LOW |
| pytest | 테스트 실패·수집 오류 | 커버리지 < 목표, 테스트 없음 | | |
| 휴리스틱 | | | DB 세션 직접 생성·전역 세션·라우터 로직·SQL 문자열 조합 | |

> 설계서와 다른 점: bandit B608(SQL 문자열 조합)의 기본 심각도는 MEDIUM 이라 "HIGH 만 치명" 규칙으로는 빠진다.
> 그래서 B608·B602·B605 는 규칙 ID 로 치명 처리한다.
>
> - 프로젝트(또는 상위 폴더)에 ruff 설정이 없으면 표준 규칙(`E,W,F,I,B,UP,SIM,C4`, E501 제외)을 쓴다.
>   ruff 버전마다 기본 규칙이 달라 결과 건수가 바뀌는 것을 막기 위해서다.
> - mypy 는 파일 하나의 구문 오류(또는 `# type: 설명` 처럼 타입 주석으로 오인되는 주석)에서 전체 검사를 멈춘다.
>   이때 실행 로그와 리포트에 `⚠ mypy 가 … 에서 중단` 으로 표시되니, 그 줄을 먼저 고치고 다시 돌린다.

### 휴리스틱(9단계 후보) 규칙
| 규칙 | 잡는 것 |
|---|---|
| `SESSION-IN-FUNC` | 함수 안에서 `SessionLocal()` 등을 직접 생성 (with 블록·yield 의존성 제외). commit/close 누락 표시 |
| `GLOBAL-SESSION` | 모듈 전역 세션 인스턴스 |
| `ROUTER-LOGIC` | 라우터 함수에 반복문 / DB 직접 호출 / 문장 수 초과 |
| `SQL-STRING` | `execute()`·`text()` 등에 f-string·`+`·`%`·`.format()` 으로 만든 SQL (bandit 이 못 잡는 변형 포함, bandit 과 같은 줄은 중복 제거) |

---

## 6. Claude Code hook

Claude 가 `.py` 파일을 저장(Edit/Write)할 때마다 그 파일의 **설계 위반**(계층·순환·외부 라이브러리·보조 코드)을 검사한다.
ruff·mypy 는 `hook_tools` 로 정한다:

| hook_tools | 동작 |
|---|---|
| `auto` (기본) | 사용자 전역 설정에 저장 시 ruff 를 돌리는 hook(32. 개발표준의 `py_post_edit.py` 등)이 있으면 **설계 검사만**, 없으면 ruff+mypy+설계 |
| `design` | 설계 검사만 |
| `all` | 항상 ruff(검사만)+mypy+설계 |

- `hook_mode = "block"` (기본): 문제가 있으면 exit 2 → Claude 가 내용을 받고 반드시 수정
- `hook_mode = "warn"`: 경고만 전달(additionalContext). **기존 오류가 많은 레거시 프로젝트는 warn 으로 시작** 권장

hook 에서 `ruff --fix` 를 쓰지 않는 이유: hook 이 파일을 바꾸면 Claude 가 알고 있는 파일 내용과 달라져 다음 Edit 이 실패한다.
자동 수정은 pre-commit(`ruff --fix`) 또는 `audit-kit run --fix` 에서 한다.

hook 명령은 init 을 실행한 파이썬의 **절대 경로**로 등록된다. venv 를 옮기거나 다시 만들면 `audit-kit init` 을 다시 실행하면 경로가 갱신된다.

---

## 7. 설정 (`pyproject.toml` `[tool.audit-kit]`)

| 키 | 기본값 | 설명 |
|---|---|---|
| `packages` | 자동 탐지 | 제품 코드 최상위 패키지 (루트 또는 `src/` 아래) |
| `support` | 폴더 이름 자동 | 보조 코드 역할 지정, 예: `{ scripts = ["scripts", "temp"], plugins = ["local_worker_plugins"] }` |
| `exclude_paths` | [] | 분석에서 뺄 경로 glob (개수·사유는 리포트에 표시) |
| `coverage_target` | 80 | 커버리지 목표(%) |
| `run_tests` | true | pytest 실행 여부 |
| `pytest_args` | [] | pytest 추가 인자 |
| `pytest_timeout` | 900 | 초 |
| `report_dir` | "audit-reports" | |
| `ruff_critical` / `ruff_ignore` | 위 표 | 규칙 접두어 목록 |
| `bandit_critical` | ["B602","B605","B608"] | |
| `bandit_promote` | ["B105","B106","B107"] | LOW 지만 개선으로 올릴 규칙 |
| `vulture_min_confidence` | 60 | |
| `vulture_improve_confidence` | 80 | 이상이면 개선, 미만이면 무시 |
| `vulture_whitelist` | "vulture_whitelist.py" | |
| `vulture_ignore_decorators` | `@app.*` `@router.*` `@*.get` … | FastAPI·pytest·pydantic 데코레이터 |
| `vulture_ignore_names` | `model_config` `__tablename__` … | |
| `radon_min_rank` | "C" | 이 등급 이상 보고 |
| `session_factories` | SessionLocal, Session, AsyncSession … | 세션 생성 함수 이름 |
| `router_globs` | `**/api/**/*.py` `**/routers/**/*.py` … | 라우터 파일 패턴 |
| `router_max_statements` | 15 | |
| `mypy_critical` | ["name-defined","call-arg","syntax"] | 치명으로 올릴 mypy 오류 코드 (없는 모듈 속성 임포트도 치명) |
| `protected_paths` | [] | 수정 모드에서 승인 없이 커밋하지 않을 파일 glob |
| `fix_branch_prefix` | "audit-fix/" | 수정 브랜치 이름 앞부분 |
| `hook_tools` | "auto" | auto / design / all — 6장 참고 |
| `hook_mode` | "block" | block / warn |
| `hook_mypy` | true | hook 에서 mypy 실행 여부 (느리면 false) |

### MyProject 예시: ezdxf 계산 로직 독립 강제

```toml
[[tool.importlinter.contracts]]
name = "ezdxf 계산 로직 독립"
type = "forbidden"
source_modules = ["myproject.core"]
forbidden_modules = ["myproject.api", "myproject.plugin", "fastapi"]
```

---

## 8. 리포트

`audit-reports/<YYYYMMDD_HHMMSS>/`

| 파일 | 용도 |
|---|---|
| `report.md` | 사람용 리포트 (요약·치명·개선·AI 리뷰 필요·무시·커버리지·도구 상태). `/audit` 가 끝에 "AI 리뷰 판정" 추가 |
| `ai-review.md` | 9단계 입력: 휴리스틱 후보, 문제 몰린 파일, 라우터 파일 목록 |
| `findings.json` | 기계용 전체 결과 |
| `deps.md` | Mermaid 모듈 의존성 그래프 (순환 경로 빨간 선) |

`audit-reports/LATEST.txt` 에 최신 폴더 이름이 기록된다.

---

## 9. 개발

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

`tests/fixtures/sample_proj` 는 문제를 일부러 심어 둔 샘플 프로젝트다 (SQL f-string, shell=True, 순환 임포트, 라우터 로직, 세션 누수, 테스트 실패 등).

## 10. CI·변이 테스트

- `.github/workflows/ci.yml`: push/PR 마다 pytest + `audit-kit std` 자기 검사(Python 3.11, 3.14는 참고용).
- `.github/workflows/mutation.yml`: 시험이 실제로 코드를 검증하는지 mutmut 로 확인한다. `workflow_dispatch`(원하는 함수 직접 지정 가능) 또는 매주 월요일, **이번에 바뀐 함수만** 검사한다(`scripts/changed_functions.py`). `mutmut` 는 네이티브 Windows 를 지원하지 않아 이 워크플로가 유일한 실행 환경이다. 결과는 Actions 요약과 `mutants/mutmut-cicd-stats.json`(아티팩트)에서 본다.


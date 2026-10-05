# 프로그램 설계 구조 생성기 — 전체 계획

- 작성: 2026-09-27
- 방향 전환: audit-kit(품질 감사 모음) → **프로그램 전체 설계 구조를 만들고, 검사하고, 코드를 그 구조로 옮기는 도구**
- 진행 방식: 단계마다 **점검(설계) → 개발 → 테스트 → 실제 프로젝트 검증 → 커밋 → 보고** 후 다음 단계

---

## 1. 목적과 역할 분담

| 프로젝트 | 책임 | 이 도구와의 관계 |
|---|---|---|
| 32. Claude 개발표준 | 코딩 규칙, 저장 시 ruff·mypy, 완료 전 중복·테스트 검사, 새 프로젝트 템플릿 | 이 도구는 **ruff·mypy 를 hook 에서 돌리지 않는다** (32 가 담당). 새 프로젝트 생성 시 32 의 `templates/` 를 **재사용**한다 |
| 33. 중복코드 분석기 (dupscan) | 프로젝트 간 중복 코드 → 공용 라이브러리 이전 후보 | 이 도구는 **중복을 찾지 않는다**. 설계 시 dupscan 보고서가 있으면 '공용 라이브러리로 뺄 부분'으로 참고만 한다 |
| **34. 이 도구** | **프로그램 하나의 전체 구조**: 계층·패키지·모듈 책임·의존 방향·외부 라이브러리 위치·진입점·설정/DB/로깅 위치·보조 코드 역할 | — |

한 줄 정의: **"이 프로그램은 어떤 구조여야 하는가"를 정하고(설계), 만들고(생성), 지키고(검사), 옮긴다(이행).**

## 2. 근거 원칙

구조 규칙은 공식 문서를 근거로 하고, 각 규칙에 출처를 남긴다.

| 영역 | 근거 |
|---|---|
| 패키지·모듈, `__init__.py`, namespace package | PEP 420, Python 언어 레퍼런스 "The import system" |
| 모듈 탐색·가림(shadowing)·순환 임포트·임포트 시 실행 | "The import system" 5.3~5.5 |
| src 레이아웃, 설치 가능한 구조 | PyPA "src layout vs flat layout" |
| 의존성·진입점·requires-python | PyPA "Writing your pyproject.toml", Dependency/Version specifiers |
| 임포트 규칙·이름·공개/비공개 | PEP 8 |
| 버전 호환 구문 검사 | `ast` 문서: `feature_version` 은 best-effort → **최소 버전 실제 인터프리터로 확인** |

## 3. 현재 가진 것 (재사용)

| 기능 | 상태 | 새 구조에서의 위치 |
|---|---|---|
| 설계 파일 `architecture.toml` (계층·외부 라이브러리·금지·독립·보조 코드) | ✅ | 설계 모델의 기반 → 확장 |
| 설계 대비 검사 `arch check` (역방향·순환·외부·진입점·보조 코드·비공개·미배정) | ✅ | 검사 단계 |
| 자동 수정 `arch fix` (reexport·unused·type_only·move·lazy, 작업공간 검증, 되돌리기) | ✅ | 이행 단계의 엔진 |
| 검사 범위 분류 `scope` (제품/보조/복사본/제외/미지정) | ✅ | 역설계 입력 |
| 파일 형식 보존 `textio` (줄바꿈·인코딩·BOM) | ✅ | 모든 쓰기 |
| 수정 모드 `fix` (브랜치·건별 재검사·커밋·기준선 테스트) | ✅ | 이행 단계의 실행 틀 |
| 품질 감사 `run` (ruff·mypy·vulture·radon·bandit·pytest·휴리스틱) | ✅ | **유지, 신규 개발 중단** (32 의 `/py-check` 와 겹치는 부분 정리) |

## 4. 단계별 계획

### 0단계 — 정리 (역할 분담 반영)
- hook 에서 ruff·mypy 제거 → **설계 위반만** 검사 (32 의 hook 과 중복 제거)
- 이름·README·스킬 문구를 새 목적에 맞게 개편 (프로그램 이름은 사용자 결정)
- 별도 작업 폴더로 이전 (다른 창의 복사 작업이 끝난 뒤), GitHub push
- 완료 기준: 저장 1회에 ruff 가 한 번만 돈다, 기존 테스트 전부 통과

### 진행 상황
- [x] 0단계 정리 (hook_tools, 역할 분담)
- [x] 1단계-A 구조 규칙 검사·자동 수정 `struct` (공식 문서 근거 9규칙, 자동 수정 3+1)
- [x] 1단계-B 설계 모델 v2 (프로젝트 유형 6종·계층 책임/금지·공통 관심사 위치 + ARCH-CONCERN 검사)
- [x] 기준서 검사 `audit-kit std` (2026-09-27): 조항 ID·근거가 붙은 검사, 번들 기준서, 직접 구현 AST 검사 12종. 3단계(다른 PC 배포)·4단계(검증) 남음
  - [x] 운영·관리(OPS-01~16) 검사 11개 추가(2026-09-28, add-ops-category-rules 세션): `checks.py`의
    `check_release_config_present` 등 — CODEOWNERS/dependabot/mkdocs/sonar/sbom/runbook/structlog/
    privacy/apm/deployment-doc/community-files 존재 확인. `rules.py`의 `CATEGORY`에 누락됐던 `SEC` 라벨도
    보충. 저장소 루트에 audit-kit 자신도 OPS-01/07/16 실제 적용(LICENSE·CODEOWNERS·dependabot.yml 등).
    세션 전체 기록은 작성자의 로컬 개발 목록(저장소에 포함하지 않음) 9번 항목
- [x] 2단계 `new` 커맨드 (2026-09-27): 아래 "2단계" 절 참고. **서브에이전트가 아니라 audit-kit CLI 커맨드 + 별도 Skill**로 구현 — 서브에이전트는 사용자에게 질문할 수 없어(35. 기준서 작성 체계가 실측으로 확인) `/design` 의 대화형 질문 단계를 처리할 수 없기 때문. `/design` Skill 오케스트레이션(사용자 질문 → 확정 → `audit-kit new` 호출 → `/new-project-standard` 연계)은 이 저장소 밖(`~/.claude/skills/`)에서 별도 진행 중
  - [x] 6개 유형 전부 ruff/mypy/pytest e2e 자동 검증(2026-09-28): 이전엔 library 유형만 자동 검증됐음.
    파라미터화해서 실제로 돌려보니 db concern 있는 유형(fastapi/cli)에서 alembic head 0개로
    fitness test가 항상 실패하는 실제 버그 발견 — 초기 빈 리비전 자동 생성으로 수정(`3646f2c`).
  - [x] `run` 커맨드에 0~100 코드 건강도 점수 추가(2026-09-28, `score.py`): SonarQube/CodeClimate
    방식 참고, 심각도별 findings 밀도 → A~E 등급. `df4bbf6`.
  - [x] **Cookiecutter+cruft 전면 재구성 완료** (2026-09-28~29, `restructure-audit-kit-cookiecutter`
    세션, 커밋 `7a0e16c`~`64d699d`): `new` 커맨드가 `cookiecutter-template/`(저장소 자체 하위 폴더)를
    `cruft.create()`로 펼치는 구조로 바뀜 — `cruft update`로 "이미 만든 프로젝트에 템플릿 개선사항
    반영"이 실제로 동작함을 e2e 로 확인(`tests/test_cruft_e2e.py`).
    - **핵심 설계**: 생성 로직(`spec_from_scope`/`templates.py`/`scaffold.py`/`pyproject_gen.py`/
      `ci_gen.py`/`concerns_scaffold.py`/`db_api_fitness.py`)을 설치된 `audit_kit` 패키지에서 바로
      import 하지 않고, 템플릿 자신의 `{{cookiecutter.package}}/_scaffold_engine/`으로 **벤더링**해
      커밋마다 함께 버전 관리되게 함 — 실측 확인한 이유: `cruft update`는 "옛 커밋/새 커밋에서 각각
      렌더 → diff" 방식인데, 훅이 설치된 패키지를 그대로 import 하면 `sys.executable`이 같은 한
      두 렌더가 항상 동일한 현재 코드를 참조해 diff 가 안 생기는 결함이 있었음(README 한 줄 추가로
      재현 확인 후 고침, `1366e32`). `sync_scaffold_engine.py`가 원본→벤더 사본 동기화를 담당.
    - `verify_project`(ruff/mypy/pytest 실행)는 결과물 파일에 영향 없는 품질 게이트라 벤더링 제외,
      설치된 `audit_kit`를 그대로 사용.
    - Windows 캐비엇: `cruft update`는 `cruft` 2.16.0 자체 버그로 `pyproject.toml`에 비ASCII 문자가
      있으면 비-UTF8 로케일에서 `UnicodeDecodeError` — `PYTHONUTF8=1`로 우회(우리 코드 문제 아님).
    - 6개 유형(fastapi/cad/mcp/cli/desktop/library) 전부 회귀 확인, 전체 테스트 307개 통과(2026-09-29).
    - 후속: FastAPI `Depends(팩토리(...))` 중첩 호출이 B008(STD-05)로 오탐되던 문제를 두 단계로 수정
      (`fd4ace0`: `Depends()`/`Query()` 자체 면제 → `ca1ebcd`: 프로젝트 자체 ruff 설정의
      `extend-immutable-calls` 추가 항목 병합, `다른 프로젝트` 실감사로 발견).

### 1단계 — 설계 모델 확장 (`architecture.toml` v2)
- 프로젝트 유형별 기본 구조: **FastAPI 서버 / CAD·COM 자동화 / CLI·배치 / 데스크톱(GUI) / 라이브러리**
- 계층마다 **책임**(무엇을 하고, 무엇을 하면 안 되는가), 모듈별 책임 한 줄
- 공통 관심사 위치 규칙: 설정(config)·로깅·DB 세션·외부 API 클라이언트·AutoCAD COM 은 어느 계층·모듈에 두는가
- 공용 라이브러리(`common_lib`) 사용 규칙: 설정·로깅·Excel·경로는 공용 라이브러리 사용
- **구조 규칙 검사(struct)** 추가 — 공식 문서 근거 (아래 표)
- 완료 기준: 유형 템플릿 5종, 설계 파일 검증(스키마·모순) 통과, MyProject 설계 초안이 v2 로 표현됨

| 구조 규칙 | 근거 | 자동 수정 |
|---|---|---|
| `__init__.py` 없이 패키지로 쓰이는 폴더 | PEP 420 | ✅ (다른 위치에 같은 이름 portion 이 없을 때) |
| 내부 폴더를 최상위 이름으로 임포트 (`import parsers`) | import system | ✅ (대상이 하나로 확정될 때) |
| 선언 안 된 외부 의존성 | PyPA pyproject | ✅ (`>=설치버전,<다음메이저`, 보조 코드 전용은 optional) |
| 상한 없는 의존성의 메이저 변경 위험 | Version specifiers | ⚙ 선택 |
| `sys.path` 조작 | import system, src layout | 📋 안내 |
| 최소 Python 버전 구문 호환 | ast 문서 → 실제 인터프리터 | 📋 안내 |
| 임포트 시 부작용 (모듈 최상위 실행문) | import system | 📋 안내 |
| 표준 라이브러리와 같은 모듈 이름 | import system | 📋 안내 |
| 거대 모듈 (설정 줄 수 초과) | (설정값) | 📋 분할 제안 |

### 2단계 — 새 프로젝트 설계·생성 (`new`) — `new` 커맨드 완료(2026-09-27), `/design` Skill 은 별도 진행
- `spec_from_scope()`(`arch/spec.py`): 코드 그래프 없이 유형·관심사 선택만으로 `ArchSpec` 초안 생성(`templates.merged()` 재사용).
  `Layer` 에 `standard_doc_id` 필드 추가 — 있으면 35(기준서 작성 체계)의 `00-master.md` §3 계층과 강제 동기화하는 근거로 쓴다(연결은 35 쪽에서 마무리 예정).
- `arch/layout.py`: 계층 이름(고정 5개) → 유형별 물리 폴더 이름(interface: fastapi=api/cad=plugin/mcp=tools/cli=cli/desktop=ui/library=없음, 나머지는 services/repositories/domain/core)
- `arch/scaffold.py`: 계층별 폴더·`__init__.py`·최소 스텁 모듈+대응 테스트, 진입점(`[project.scripts]`) 스텁 생성
- `arch/concerns_scaffold.py`: config/logging/db/com/http 기본 구현 생성. `db` 있으면 사용자별 데이터 패턴 예시(리포지토리 함수 첫 인자=`user_id`) + alembic 골격 + `tests/architecture/test_fitness.py`(마이그레이션 head 1개 검사)
- `arch/pyproject_gen.py`: `[project]` 헤더(유형별 의존성) + 기존 `init_project.pyproject_snippet()` 재사용(ruff/mypy/import-linter/coverage/`[tool.audit-kit]`) 으로 `pyproject.toml` 생성. `pythonpath = ["src"]` 로 설치 전에도 pytest 통과하게 함
- `arch/verify.py` + `audit-kit new`(`newproj.py`): 생성 직후 arch check(0건) → ruff → mypy → pytest 자동 실행, 실패 시 `audit-reports/new_*/report.md`. `--full` 로 `pip install -e .` 까지 포함
- 완료 기준(실측 확인, 2026-09-27): 6개 유형(fastapi/cad/mcp/cli/desktop/library) 전부 생성 직후 `arch check` 위반 0건. `library` 유형은 arch check+ruff+mypy+pytest 전체 파이프라인까지 확인. 전체 회귀 테스트(`tests/test_new.py` 15개 포함) 178개 통과, ruff·mypy 0건(새 코드 기준)
- 남은 것: `/design` Skill(사용자 질문 → 유형 확정 → `audit-kit new` 호출 → 완료 후 `/new-project-standard` 연계) 오케스트레이션, 35 쪽 `standard-writer.md`/`std_probe.py` 의 `standard_doc_id` 강제 동기화 반영(35 세션과 별도 조율 중)

### 3단계 — 기존 프로젝트 역설계 (`as-is` → `to-be`)
- 현재 구조 모델(as-is): 실제 의존 방향으로 계층 추정, 모듈 책임 추정(이름·임포트·외부 라이브러리·함수 이름), 공통 관심사 위치
- 목표 구조(to-be) 제안: 유형 템플릿 + 현재 구조의 차이 → 설계 파일 초안
- **차이 분석(gap)**: 옮길 모듈, 나눌 모듈, 새로 만들 계층, 끊을 의존 — 크기·위험도·순서
- 완료 기준: MyProject 의 as-is/to-be/gap 문서가 사람이 읽고 결정할 수 있는 수준

### 4단계 — 구조 이행 (`migrate`)
- gap 을 **순서 있는 이행 계획**으로: 선행 조건(예: 먼저 `__init__.py`, 그다음 이동)과 단계별 검증 기준
- 자동 이행 엔진 확장: **모듈(파일) 단위 이동 + 전체 임포트 재작성**, 패키지 생성, 구조 규칙 자동 수정, 계층 간 심볼 이동(기존 move)
- 사람 판단이 필요한 것(모듈 분할, 의존성 역전, sys.path 제거)은 Claude 가 `/migrate` 스킬로 한 건씩 — 수정 모드(브랜치·재검사·건별 커밋·기준선 테스트) 재사용
- 완료 기준: 샘플 프로젝트에서 as-is → to-be 자동 이행 후 테스트 동일, 되돌리기 가능

### 5단계 — 유지
- hook: 설계 위반만, **캐시로 저장 1회 3초 이내** (대형 프로젝트 기준)
- `ARCHITECTURE.md` 자동 갱신, CI 게이트(설계 위반 증가 시 실패)
- 완료 기준: MyProject 규모에서 hook 3초 이내

### 6단계 — 실전 적용 (MyProject)
- `deploy/production` 기준 feature 브랜치에서 as-is → to-be 설계 확정(사용자 결정) → 단계별 이행 → PR
- 보호 파일(CLAUDE.md 의 파서·SOT)은 `protected_paths` 로 제외

## 5. 결정이 필요한 것
- ~~프로그램 이름~~ → `audit-kit` 로 확정 (GitHub 저장소·README 기준)
- ~~품질 감사(`run`) 기능을 이 도구에 계속 둘지~~ → 이 도구에 유지, 0~100 건강도 점수까지 추가(`score.py`)
- ~~1단계 프로젝트 유형 5종이 맞는지~~ → 6종(fastapi/cad/mcp/cli/desktop/library)으로 확정, `new`
  커맨드 e2e 검증 완료
- **남은 결정**: 3단계(역설계 `as-is`→`to-be`)를 언제 시작할지 — 2단계(`new`)가 이제 안정 상태라
  다음 우선순위 후보지만, 사용자가 지금 급한 다른 작업(생태계 전체 STD-04 적체 정리 등)을 원할 수 있음

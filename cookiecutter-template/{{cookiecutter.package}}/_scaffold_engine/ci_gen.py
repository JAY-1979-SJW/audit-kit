# pylint: disable=duplicate-code
"""새 프로젝트의 GitHub Actions CI 워크플로(.github/workflows/ci.yml)와 README.md 생성.

지금까지 `audit-kit new`(python-scaffolder)와 html-scaffolder 결과물엔 CI 자체가 없었다
(electron-scaffolder만 있었음). ruff/mypy/pytest는 생성 직후 `verify_project()`가 한 번
검사하지만, 그 이후 커밋에는 아무 검사도 없었다 — 이 공백을 메운다.

포함하는 검사(전부 공식 문서로 확인, 2026-09-27):
- ruff/mypy/pytest: 기존 audit-kit 자신의 `.github/workflows/ci.yml`과 같은 패턴.
- bandit: `bandit -r src/` — 기본 동작은 이슈 발견 시 exit 1(실측 확인), 클린이면 0.
- semgrep: `semgrep scan --config auto` — 로그인 없이 쓸 수 있는 공식 권장 명령
  (docs.semgrep.dev: "If you don't have a GitHub or GitLab account, you can use
  `semgrep scan` in your CLI"). `--config auto`는 레지스트리에서 규칙을 내려받는데,
  이 작업 중 실측 확인 결과 **최초 실행 시 레지스트리 fetch가 느리다(수십 초)** — 이건
  audit-kit 개발 샌드박스의 네트워크 제약일 수도 있지만, GitHub Actions 러너에서도 완전히
  배제할 수 없는 위험(semgrep 레지스트리 자체 장애/지연)이라 `continue-on-error: true`로
  둔다. 코드 자체의 문제가 아니라 외부 서비스 문제로 전체 파이프라인이 막히는 것을 피하기
  위함이다(bandit·ruff·mypy·pytest는 전부 로컬 완결형이라 이 위험이 없다 — continue-on-error 없음).
- Claude Code 공식 GitHub Action(독립 AI 리뷰): code.claude.com/docs/en/github-actions
  "Run a skill" 절의 예시 그대로. `ANTHROPIC_API_KEY`(또는 `CLAUDE_CODE_OAUTH_TOKEN`) 시크릿이
  저장소에 없으면 이 잡은 실패한다 — 생성기가 시크릿을 만들 수 없으므로 README.md에 안내만 남긴다.
"""

from __future__ import annotations

from pathlib import Path

from _scaffold_engine.textio import TextFormat, write_source

CI_WORKFLOW = """name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-python@v6
        with:
          python-version: "3.14"
      - run: python -m pip install --upgrade pip
      - run: pip install -e ".[dev]"
      - name: ruff
        run: ruff check .
      - name: mypy
        run: mypy src
      - run: python -m pytest -q
      - name: bandit (보안 정적분석)
        run: bandit -r src/
      - name: semgrep (보안 정적분석, 계정 불필요)
        # --config auto 는 semgrep 레지스트리에서 규칙을 받아온다 — 그 외부 서비스가 느리거나
        # 장애가 나도 이 저장소 자체 파이프라인을 막지 않도록 continue-on-error 로 둔다.
        continue-on-error: true
        run: pip install semgrep && semgrep scan --config auto

  claude-review:
    runs-on: ubuntu-latest
    if: github.event_name == 'pull_request'
    permissions:
      contents: read
      pull-requests: read
      issues: read
      id-token: write
    steps:
      - uses: actions/checkout@v6
        with:
          fetch-depth: 1
      - uses: anthropics/claude-code-action@v1
        with:
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
          plugin_marketplaces: "https://github.com/anthropics/claude-code.git"
          plugins: "code-review@claude-code-plugins"
          prompt: "/code-review:code-review --comment ${{ github.repository }}/pull/${{ github.event.pull_request.number }}"
          claude_args: '--allowedTools "mcp__github_inline_comment__create_inline_comment"'
"""

README_TEMPLATE = """# {package_name}

{description}

## 개발 환경 준비
```
py -3.14 -m venv .venv
.venv\\Scripts\\python -m pip install -e ".[dev]"
```

## 검사
```
python -m pytest -q
bandit -r src/
```

## CI
`.github/workflows/ci.yml`이 커밋마다 ruff·mypy·pytest·bandit·semgrep을 자동으로 돌립니다.

**독립 AI 리뷰(`claude-review` 잡)를 쓰려면**: GitHub 저장소 Settings → Secrets and variables →
Actions 에 `ANTHROPIC_API_KEY`(Claude API 키, platform.claude.com) 또는 구독을 쓴다면
`CLAUDE_CODE_OAUTH_TOKEN`(`claude setup-token`으로 생성)을 추가하세요. 시크릿이 없으면 이 잡은
실패합니다(코드 문제가 아닙니다) — 원치 않으면 `.github/workflows/ci.yml`에서 `claude-review` 잡을
지우세요.

## 골격 업데이트 (`cruft update`)
이 프로젝트는 `audit-kit new`의 cookiecutter 템플릿에서 생성됐고 `.cruft.json`으로 그 버전을
추적합니다. 템플릿이 개선되면:
```
cruft update
```
**Windows + 비영문 로케일(한글 등) 주의**: cruft 2.16.0의 알려진 버그로, `pyproject.toml`에
비ASCII 문자가 있으면 `cruft update`가 인코딩 오류로 실패할 수 있습니다. 그럴 땐:
```
set PYTHONUTF8=1
cruft update
```

---
`audit-kit new`로 생성됨.

<!-- CRUFT-UPDATE-실측-마커-2 -->
"""


def generate_ci_and_readme(root: Path, package_name: str, description: str) -> list:
    """`.github/workflows/ci.yml`과 `README.md`를 생성한다. 둘 다 지금까지 없었다."""
    written: list = []
    ci_path = root / ".github" / "workflows" / "ci.yml"
    ci_path.parent.mkdir(parents=True, exist_ok=True)
    if not ci_path.exists():
        write_source(ci_path, CI_WORKFLOW, TextFormat())
        written.append(ci_path)
    readme_path = root / "README.md"
    if not readme_path.exists():
        text = README_TEMPLATE.format(package_name=package_name, description=description or "")
        write_source(readme_path, text, TextFormat())
        written.append(readme_path)
    return written

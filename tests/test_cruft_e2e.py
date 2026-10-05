"""`audit-kit new`(cruft.create)와 `cruft update`를 실제로 끝까지 실행해 확인한다.

**Windows 인코딩 주의(2026-09-28 실측)**: cruft 2.16.0 자체의 버그로, `cruft.update()`가
내부에서 `pyproject.toml`을 인코딩 지정 없이 읽다가(`cruft/_commands/utils/generate.py`의
`_get_skip_paths`) 시스템 로케일이 UTF-8이 아니면(이 PC는 cp949) 그 파일에 한글이 있을 때
`UnicodeDecodeError`로 죽는다. `cruft.create()`는 이 코드 경로를 안 타서 영향 없다(직접
확인함). 그래서 `cruft update`만 `PYTHONUTF8=1` 환경변수를 준 **별도 프로세스**로 실행한다
— 같은 프로세스 안에서는 `sys.flags.utf8_mode`가 인터프리터 시작 시점에 이미 고정돼
런타임에 되돌릴 수 없다(PEP 540).

`cookiecutter-template/hooks/post_gen_project.py`는 cookiecutter가 Jinja로 렌더링해
익명 임시 파일로 실행하는 스크립트라(그 안에 `{{ cookiecutter | jsonify }}` 같은 Jinja
문법이 남아 있어 그 자체로는 유효한 파이썬도 아니다), 정적 분석이나 일반적인 "파일을 직접
import 해서 실행" 방식의 실행 증거로는 검증할 수 없다. 이 테스트가 그 자리를 메운다 —
실제 cruft API를 통해 진짜로 프로젝트를 생성하고, 템플릿을 한 커밋 더 진행시킨 뒤
`cruft update`가 실제로 그 변경을 반영하는지까지 끝까지 확인한다(2026-09-28, README
템플릿에 문구를 추가해 수동으로 먼저 재현한 것을 자동화했다).

audit-kit 저장소 자체를 건드리지 않도록, `cookiecutter-template/`만 별도 임시 git
저장소로 복사해서 그 안에서 커밋을 두 번 만든다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import cruft
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_SRC = REPO_ROOT / "cookiecutter-template"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8"
    )


@pytest.fixture
def template_repo(tmp_path: Path) -> Path:
    """`cookiecutter-template/`만 담은 독립 git 저장소 사본. 실제 audit-kit 저장소의
    커밋 이력과는 무관하게, 테스트 안에서 자유롭게 새 커밋을 만들기 위함."""
    repo = tmp_path / "template_repo"
    shutil.copytree(TEMPLATE_SRC, repo / "cookiecutter-template")
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "test", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "initial template", cwd=repo)
    return repo


def _create(template_repo: Path, output_dir: Path, package: str) -> Path:
    return cruft.create(
        template_git_url=str(template_repo),
        output_dir=output_dir,
        directory="cookiecutter-template",
        extra_context={
            "package": package,
            "description": "",
            "types": "library",
            "concerns": "",
            "entrypoint": "main",
            "no_verify": "yes",
            "full_verify": "no",
        },
        no_input=True,
    )


def test_cruft_create_generates_project_with_cruft_json(template_repo, tmp_path):
    project_dir = _create(template_repo, tmp_path / "out", "demo")

    assert (project_dir / ".cruft.json").is_file()
    assert (project_dir / "architecture.toml").is_file()
    assert (project_dir / "src" / "demo" / "__init__.py").is_file()
    assert not (project_dir / "_scaffold_engine").exists(), "생성 엔진은 결과물에 남으면 안 됨"
    assert not (project_dir / ".audit_kit_scaffold_marker").exists()


def test_cruft_update_propagates_scaffold_engine_change(template_repo, tmp_path):
    project_dir = _create(template_repo, tmp_path / "out", "demo")
    before = (project_dir / "README.md").read_text(encoding="utf-8")
    assert "TEST-MARKER" not in before

    # 템플릿을 한 커밋 더 진행: 벤더링된 ci_gen.py의 README 템플릿에 마커를 추가.
    ci_gen = (
        template_repo
        / "cookiecutter-template"
        / "{{cookiecutter.package}}"
        / "_scaffold_engine"
        / "ci_gen.py"
    )
    text = ci_gen.read_text(encoding="utf-8")
    assert "`audit-kit new`로 생성됨." in text, (
        "README_TEMPLATE 문구가 바뀌면 이 테스트도 갱신 필요"
    )
    ci_gen.write_text(text.replace("`audit-kit new`로 생성됨.", "TEST-MARKER"), encoding="utf-8")
    _git("add", "-A", cwd=template_repo)
    _git("commit", "-q", "-m", "bump scaffold engine", cwd=template_repo)

    env = {**os.environ, "PYTHONUTF8": "1"}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cruft",
            "update",
            "--project-dir",
            str(project_dir),
            "--skip-apply-ask",
            "--allow-untracked-files",
            "-y",
        ],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    after = (project_dir / "README.md").read_text(encoding="utf-8")
    assert "TEST-MARKER" in after, "템플릿 변경이 cruft update로 실제 반영되지 않음"

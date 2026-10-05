"""`audit-kit new`: 확정된 설계로 새 프로젝트 골격을 생성한다 (ROADMAP 2단계).

`cookiecutter-template/`(audit-kit 저장소 자체 안의 하위 폴더)를 cruft로 펼친다 — 실제
골격 생성 로직은 `cookiecutter-template/hooks/post_gen_project.py`로 옮겨졌다(기존
`arch.*` 생성 함수를 그대로 재사용). 여기서는 CLI 인자를 cruft 컨텍스트로 바꿔 전달만 한다.

폴더 구조 변경 주의: `--out`은 이제 "그 자리에 바로"가 아니라 **부모 폴더**다.
cruft/cookiecutter 는 항상 `--out`/`--package` 로 새 하위 폴더(`<out>/<package>/`)를 만든다
(cruft 공식 소스 `cruft/_commands/create.py`로 확인 — "기존 폴더에 바로" 방식은 없음).

사용자와의 질문·확인 대화는 `/design` Skill(메인 세션)이 맡는다 — 서브에이전트는 사용자에게
질문할 수 없기 때문이다.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import cruft
from cookiecutter.exceptions import OutputDirExistsException

from audit_kit.arch.templates import TYPES

# audit-kit 저장소 루트: src/audit_kit/newproj.py 에서 두 단계 위.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_SUBDIR = "cookiecutter-template"


def _git_init_commit(project_dir: Path) -> None:
    """cruft update 는 대상이 '깨끗한 git 저장소'일 것을 요구한다(cruft 소스로 확인).
    git 이 없거나 실패해도 골격 생성 자체는 이미 끝난 뒤이므로 경고만 남기고 계속 진행한다."""
    git = shutil.which("git")
    if not git:
        print(
            f"경고: git 을 찾을 수 없습니다 — 나중에 `cruft update`를 쓰려면 "
            f"{project_dir}에서 직접 `git init && git add -A && git commit`을 실행하세요.",
            file=sys.stderr,
        )
        return
    try:
        subprocess.run([git, "init", "-q"], cwd=project_dir, check=True, capture_output=True)
        subprocess.run([git, "add", "-A"], cwd=project_dir, check=True, capture_output=True)
        subprocess.run(
            [git, "commit", "-q", "-m", "chore: audit-kit new 로 골격 생성"],
            cwd=project_dir,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(
            f"경고: git 초기화/커밋 실패({exc}) — 나중에 `cruft update`를 쓰려면 "
            f"{project_dir}에서 직접 `git init && git add -A && git commit`을 실행하세요.",
            file=sys.stderr,
        )


def cmd_new(args) -> int:
    types = [t.strip() for t in args.types.split(",") if t.strip()]
    unknown = [t for t in types if t not in TYPES]
    if unknown:
        print(f"알 수 없는 유형: {', '.join(unknown)} (가능: {', '.join(TYPES)})", file=sys.stderr)
        return 2

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        project_dir = cruft.create(
            template_git_url=str(_REPO_ROOT),
            output_dir=out_dir,
            directory=_TEMPLATE_SUBDIR,
            extra_context={
                "package": args.package,
                "description": args.description,
                "types": args.types,
                "concerns": args.concerns or "",
                "entrypoint": args.entrypoint,
                "no_verify": "yes" if args.no_verify else "no",
                "full_verify": "yes" if args.full else "no",
            },
            no_input=True,
            overwrite_if_exists=args.force,
        )
    except OutputDirExistsException:
        print(
            f"{out_dir / args.package} 이 이미 있습니다. --force 로 덮어쓰거나 "
            "다른 --package/--out 을 쓰세요.",
            file=sys.stderr,
        )
        return 1

    _git_init_commit(project_dir)
    return 0


def register(sub) -> None:
    p = sub.add_parser("new", help="확정된 설계로 새 프로젝트 골격 생성 (audit-kit 2단계)")
    p.add_argument(
        "--out",
        default=".",
        help="새 프로젝트를 만들 부모 폴더 (기본: 현재 폴더). --package 이름으로 그 안에 "
        "새 하위 폴더가 만들어진다(예: --out . --package myapp → ./myapp/)",
    )
    p.add_argument("--package", required=True, help="루트 패키지 이름 (동시에 생성될 폴더 이름)")
    p.add_argument("--types", required=True, help=f"프로젝트 유형(쉼표 구분): {', '.join(TYPES)}")
    p.add_argument("--concerns", help="공통 관심사(쉼표 구분). 생략 시 유형 기본값 전부")
    p.add_argument("--entrypoint", default="main", help="진입점 이름(쉼표 구분, 기본: main)")
    p.add_argument("--description", default="")
    p.add_argument("--force", action="store_true", help="<out>/<package> 폴더가 있어도 덮어쓴다")
    p.add_argument("--no-verify", action="store_true", help="생성 후 자동 검증 생략")
    p.add_argument("--full", action="store_true", help="검증에 `pip install -e .` 도 포함")
    p.set_defaults(func=cmd_new)

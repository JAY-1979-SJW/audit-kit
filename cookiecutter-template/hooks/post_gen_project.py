"""cookiecutter/cruft 가 자리표시자 파일을 만든 뒤 실행. 실제 골격 생성은 전부 여기서 한다.

cwd 는 cookiecutter 가 이미 새로 만든 프로젝트 폴더로 맞춰 준다(cookiecutter.hooks.run_script
가 `cwd=project_dir` 로 이 스크립트를 실행).

**왜 `_scaffold_engine/`을 따로 두는가(2026-09-28 실측으로 확인한 문제)**: 처음엔 이 훅이
설치된 `audit_kit` 패키지(editable install)에서 생성 함수를 바로 import 했다. 그런데
`cruft update`는 "템플릿을 옛 커밋/새 커밋에서 각각 렌더 → 두 결과를 diff" 방식으로 동작하는데,
이 훅 스크립트는 cookiecutter가 임시 파일로 렌더링해 실행하므로(cookiecutter.hooks.
run_script_with_context) `sys.executable`이 같은 한, 옛 렌더든 새 렌더든 **항상 지금 이
PC에 설치된 동일한 audit_kit 코드**를 불러온다 — 그래서 `src/audit_kit/arch/*.py`를 아무리
고쳐도 두 렌더 결과가 똑같아 diff가 안 생기고, `cruft update`가 "최신"이라고 오판했다(실제로
README 템플릿에 한 줄 추가 후 재현 확인함). 이를 고치기 위해, 생성 로직(스펙 모델·유형별
골격·pyproject/CI 생성)을 이 템플릿 자신의 `{{cookiecutter.package}}/_scaffold_engine/`
아래로 옮겨(벤더링) 커밋마다 함께 버전 관리되게 했다 — cookiecutter가 프로젝트 파일을
복사하는 시점(훅 실행보다 먼저)에 이 폴더도 함께 `project_dir`로 복사되므로, 훅은
`sys.path`에 `project_dir`을 넣어 `_scaffold_engine.*`을 import 하면 된다. 생성이 끝나면
이 폴더는 지운다(최종 결과물에는 남지 않음).

`verify_project`(ruff/mypy/pytest 실행)는 예외다 — 결과물의 파일 내용에는 영향을 주지 않고
콘솔에 통과/실패만 알려주는 품질 게이트라, diff 대상이 될 필요가 없어 벤더링하지 않고
설치된 audit_kit 패키지를 그대로 쓴다.

주의: 이 훅이 예외로 끝나면(0이 아닌 종료 코드) cookiecutter 는 방금 만든 프로젝트 폴더를
통째로 지운다(`generate.py`: `delete_project_on_failure = output_directory_created and
not keep_project_on_failure`, 소스로 확인함). 그래서 "골격 생성" 단계의 실패는 그대로
전파해 지우게 두지만(진짜로 깨진 결과물), "검증"(ruff/mypy/pytest) 단계는 결과물이 이미
완성된 뒤이므로 실패해도 절대 예외를 밖으로 내보내지 않는다 — 오늘의 `cmd_new` 동작(검증
실패해도 결과물은 남기고 audit-reports 에 사유만 남김)과 같게 유지한다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

CONTEXT = json.loads(r"""{{ cookiecutter | jsonify }}""")


def main() -> None:
    root = Path.cwd()
    (root / ".audit_kit_scaffold_marker").unlink(missing_ok=True)

    sys.path.insert(0, str(root))
    from _scaffold_engine.ci_gen import generate_ci_and_readme
    from _scaffold_engine.concerns_scaffold import generate_concerns
    from _scaffold_engine.db_api_fitness import (
        generate_api_contract_fitness,
        generate_db_fitness,
    )
    from _scaffold_engine.pyproject_gen import generate_pyproject
    from _scaffold_engine.scaffold import generate_project
    from _scaffold_engine.spec import SPEC_FILE, render_spec, spec_from_scope
    from audit_kit.arch.verify import verify_project

    package = CONTEXT["package"]
    description = CONTEXT.get("description") or ""
    types = [t.strip() for t in CONTEXT["types"].split(",") if t.strip()]
    concerns_raw = (CONTEXT.get("concerns") or "").strip()
    concerns = [c.strip() for c in concerns_raw.split(",") if c.strip()] or None
    entry_names = [e.strip() for e in CONTEXT["entrypoint"].split(",") if e.strip()]
    entrypoints = [f"{package}.{e}" for e in entry_names]

    # ---- 골격 생성: 실패하면 예외를 그대로 전파해 cookiecutter 가 정리하게 둔다.
    spec = spec_from_scope([package], types, concerns, entrypoints)
    generate_project(spec, root, package)
    generate_concerns(spec, root, package)
    generate_db_fitness(spec, root)
    generate_api_contract_fitness(spec, root, package)
    has_db = bool(spec.concerns.get("db"))
    generate_pyproject(root, package, description, types, entrypoints, has_db=has_db)
    generate_ci_and_readme(root, package, description)
    (root / SPEC_FILE).write_text(render_spec(spec, []), encoding="utf-8")

    print(f"+ {root} 에 '{package}' 골격 생성 ({', '.join(types)})")
    for i, lay in enumerate(spec.layers, 1):
        if lay.modules:
            print(f"    {i}. {lay.name:<11} {', '.join(lay.modules)}")
    if spec.entrypoints:
        print(f"    진입점      {', '.join(spec.entrypoints)}")

    # 벤더링된 생성 엔진은 최종 결과물이 아니다 — 검증(ruff/mypy)이 이걸 프로젝트 코드로
    # 오인해 잘못된 위반을 잡기 전에 지운다.
    shutil.rmtree(root / "_scaffold_engine", ignore_errors=True)

    # ---- 검증: 결과물이 이미 완성됐으므로, 실패해도 이 결과물을 지우지 않는다.
    if CONTEXT.get("no_verify") == "yes":
        return
    try:
        result = verify_project(root, spec, package, full=CONTEXT.get("full_verify") == "yes")
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"검증 중 오류(결과물은 그대로 유지됨): {exc}", file=sys.stderr)
        return
    for s in result.steps:
        print(f"  [{'OK' if s.ok else 'FAIL'}] {s.name}")
    if not result.ok:
        print(f"완료 기준 미달 — 상세: {root / 'audit-reports'}", file=sys.stderr)
    else:
        print("\n완료 기준 전부 통과. 다음: `/new-project-standard` 로 기준서 작성을 이어가세요.")


if __name__ == "__main__":
    main()

"""`audit-kit deps`: 코드의 import 를 조사해 의존성 선언(requirements.txt) 초안을 만든다.

배경(2026-09-30): 06 프로젝트에 requirements 가 없어 다른 PC 에서 재현이 안 됐고, AST 스크립트로 import 를
모아 손으로 requirements.txt 를 만들었다. 기준서 ERR-01(없는 모듈 import)은 'import 이름 = 배포 이름'을
가정해서 PIL -> pillow, fitz -> pymupdf 같은 이름 차이를 구분하지 못한다. 이 도구가 그 간극을 메운다.

- 재사용: std.run.parse_files/local_names, std.checks.guarded_imports/is_stdlib/normalize/declared_*
- import 이름 -> 배포 이름은 이 PC 에 설치된 패키지의 메타데이터(importlib.metadata)로 찾는다.
- 설치돼 있지 않은 패키지는 배포 이름·버전을 **추측하지 않고** '확인 필요'로 남긴다.
- 기본은 미리보기(아무 파일도 쓰지 않음). `--write` 로만 파일을 만들고, 이미 있는 파일은 덮어쓰지 않는다.

종료코드: 0 선언 누락 없음(또는 --write 성공) / 1 필수 import 중 선언되지 않은 것이 있음 / 2 실행할 수 없음
"""

from __future__ import annotations

import ast
import datetime as _dt
import sys
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from audit_kit.config import AuditConfig, load_config, read_pyproject
from audit_kit.importgraph import iter_py_files
from audit_kit.std import checks
from audit_kit.std.run import local_names, parse_files

SKIP_DIRS = {"node_modules", "site-packages", "build", "dist"}
# 이 배포본은 Windows 에서만 설치된다 -> requirements 에 환경 마커를 붙인다.
WINDOWS_ONLY = {"pywin32"}
WIN_MARKER = '; sys_platform == "win32"'


@dataclass
class Dep:
    key: str  # 정규화한 배포 이름(또는 설치 안 된 경우 '?import이름')
    dist: str | None  # 배포 이름(설치돼 있을 때만 앎)
    imports: list[str] = field(default_factory=list)
    version: str | None = None
    required_in: set[str] = field(default_factory=set)
    optional_in: set[str] = field(default_factory=set)
    declared: bool = False


# ---------------------------------------------------------------- 수집
def distributions_by_import() -> Mapping[str, list[str]]:
    return metadata.packages_distributions()


def dist_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def project_files(cfg: AuditConfig) -> list[str]:
    out = []
    for f in iter_py_files(cfg.root):
        rel = f.relative_to(cfg.root).as_posix()
        parts = rel.split("/")
        if parts[0] == cfg.report_dir or SKIP_DIRS & set(parts):
            continue
        out.append(rel)
    return out


def all_import_tops(tree: ast.AST) -> set[str]:
    tops: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            tops.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            tops.add(node.module.split(".")[0])
    return tops


def collect(cfg: AuditConfig, files: list[str]) -> tuple[dict[str, tuple[set, set]], list[str]]:
    """({외부 import 이름: (필수로 쓰는 파일, 선택 import 로만 쓰는 파일)}, 구문 오류로 못 읽은 파일)."""
    parsed, broken = parse_files(cfg, files)
    local = local_names(cfg, files)
    usage: dict[str, tuple[set, set]] = defaultdict(lambda: (set(), set()))
    for rel, tree in parsed.items():
        required = {top for top, _ in checks.guarded_imports(tree)}
        for top in all_import_tops(tree):
            if top in local or top == "__future__" or checks.is_stdlib(top):
                continue
            usage[top][0 if top in required else 1].add(rel)
    return dict(usage), broken


def resolve(usage: dict[str, tuple[set, set]], declared: set[str]) -> list[Dep]:
    """import 이름들을 배포 단위로 묶는다(pywin32 는 win32com/pythoncom/pywintypes 를 함께 제공)."""
    mapping = distributions_by_import()
    deps: dict[str, Dep] = {}
    for top in sorted(usage):
        dists = mapping.get(top)
        dist = dists[0] if dists else None
        key = checks.normalize(dist) if dist else f"?{top}"
        dep = deps.setdefault(key, Dep(key, dist, version=dist_version(dist) if dist else None))
        dep.imports.append(top)
        dep.required_in |= usage[top][0]
        dep.optional_in |= usage[top][1]
        dep.declared = dep.declared or key in declared or checks.normalize(top) in declared
    return sorted(deps.values(), key=lambda d: (d.dist or d.imports[0]).lower())


def unused_declared(deps: list[Dep], declared: set[str]) -> list[str]:
    """선언돼 있지만 코드에서 import 가 보이지 않는 이름(도구·간접 의존성일 수 있어 '확인 필요'로만 다룬다)."""
    seen = {d.key for d in deps} | {checks.normalize(i) for d in deps for i in d.imports}
    return sorted(declared - seen)


# ---------------------------------------------------------------- 출력
def requirement_line(dep: Dep) -> str:
    assert dep.dist is not None and dep.version is not None
    spec = f"{dep.dist}>={dep.version}"
    return spec + (WIN_MARKER if dep.key in WINDOWS_ONLY else "")


def _unknown_line(dep: Dep) -> str:
    return f"# {dep.imports[0]}  # 이 PC 에 설치돼 있지 않아 배포 이름·버전을 알 수 없음 — 확인 후 직접 추가"


def build_requirements(deps: list[Dep], today: str | None = None) -> str:
    today = today or _dt.date.today().isoformat()
    known = [d for d in deps if d.dist and d.version]
    required = [d for d in known if d.required_in]
    optional = [d for d in known if not d.required_in]
    unknown = [d for d in deps if not (d.dist and d.version)]
    lines = [
        f"# 자동 생성 초안 (audit-kit deps, {today}) — 내용을 확인하고 필요 없는 줄은 지우세요.",
        "# 버전은 이 PC 에 설치된 버전을 하한(>=)으로 적은 값이며, 설치돼 있지 않은 패키지는 추측하지 않고 주석으로 남겼습니다.",
        "",
        "# ---- 필수 (파일 맨 위 등 항상 import 되는 곳에서 사용)",
        *[requirement_line(d) + f"  # {', '.join(d.imports)}" for d in required],
        "",
        "# ---- 선택 (try/except ImportError 등 조건부로만 import)",
        *["# " + requirement_line(d) + f"  # {', '.join(d.imports)}" for d in optional],
        "",
        "# ---- 확인 필요 (설치돼 있지 않아 버전을 알 수 없음)",
        *[_unknown_line(d) for d in unknown],
    ]
    return "\n".join(lines) + "\n"


def render_report(deps: list[Dep], declared: set[str], broken: list[str]) -> tuple[str, bool]:
    """(사람이 읽는 보고서, 필수 import 중 선언 누락이 있는지)."""
    missing = [d for d in deps if d.required_in and not d.declared]
    out = [f"코드에서 찾은 외부 패키지 {len(deps)}개 (선언된 의존성 {len(declared)}개)"]
    for d in deps:
        name = d.dist or f"{d.imports[0]}(설치 안 됨)"
        where = "필수" if d.required_in else "선택"
        state = "선언됨" if d.declared else "선언 없음"
        ver = d.version or "버전 미확인"
        out.append(f"  - {name} [{where}, {state}] {ver} — import {', '.join(d.imports)}")
    if missing:
        names = ", ".join(d.dist or d.imports[0] for d in missing)
        out.append(f"\n선언 누락(필수 import 인데 선언되지 않음) {len(missing)}개: {names}")
    extra = unused_declared(deps, declared)
    if extra:
        out.append(
            f"참고: 선언돼 있지만 코드에서 import 가 보이지 않는 이름(도구·간접 의존성일 수 있음): {', '.join(extra)}"
        )
    if broken:
        out.append(
            f"참고: 구문 오류로 읽지 못한 파일 {len(broken)}개 — 실행 중인 파이썬보다 새 문법일 수 있어 그 파일의 import 는 빠졌습니다."
        )
    return "\n".join(out), bool(missing)


def write_requirements(target: Path, text: str) -> int:
    if target.exists():
        print(
            f"{target} 이(가) 이미 있어 덮어쓰지 않았습니다.\n다음에 할 일: 위 미리보기를 보고 직접 합치거나, `--output <다른이름>` 으로 저장하세요.",
            file=sys.stderr,
        )
        return 2
    target.write_text(text, encoding="utf-8")
    print(f"\n저장: {target} — 내용을 확인하고 '확인 필요' 줄을 직접 채우세요.")
    return 0


def cmd_deps(args) -> int:
    cfg = load_config(args.path)
    declared = checks.declared_dependencies(
        read_pyproject(cfg.root)
    ) | checks.declared_requirements(cfg.root)
    usage, broken = collect(cfg, project_files(cfg))
    deps = resolve(usage, declared)
    report, has_missing = render_report(deps, declared, broken)
    print(report)
    text = build_requirements(deps)
    if args.write:
        return write_requirements(cfg.root / (args.output or "requirements.txt"), text)
    print("\n--- requirements 초안 미리보기 (아무 파일도 쓰지 않았습니다) ---\n" + text)
    if has_missing:
        print(
            "다음에 할 일: 내용이 맞으면 `audit-kit deps --write` 로 requirements.txt 를 만드세요."
        )
    return 1 if has_missing else 0


def register(sub) -> None:
    p = sub.add_parser(
        "deps", help="코드의 import 를 조사해 의존성 선언(requirements.txt) 초안 생성"
    )
    p.add_argument("--path", default=None)
    p.add_argument(
        "--write",
        action="store_true",
        help="requirements.txt 를 실제로 생성(이미 있으면 덮어쓰지 않음)",
    )
    p.add_argument(
        "--output", default=None, help="--write 의 저장 파일 이름(기본 requirements.txt)"
    )
    p.set_defaults(func=cmd_deps)

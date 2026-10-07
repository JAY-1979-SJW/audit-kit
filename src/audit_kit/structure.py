"""프로그램 구조 규칙 검사 — 공식 문서 근거.

규칙마다 근거 문서(REFS)를 붙이고, 자동 수정 가능 여부를 표시한다.
자동 수정은 structfix.py 가 작업공간에서 검증 후 적용한다.
"""

from __future__ import annotations

import ast
import functools
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from audit_kit._proc import no_window_kwargs
from audit_kit.config import AuditConfig, read_pyproject
from audit_kit.models import CRITICAL, IGNORE, IMPROVE, REVIEW, Finding, ToolResult
from audit_kit.scope import build_project_graph, get_scope
from audit_kit.textio import read_text

REFS = {
    "pep420": "PEP 420 Implicit Namespace Packages — https://peps.python.org/pep-0420/",
    "import": "Python 언어 레퍼런스 'The import system' — https://docs.python.org/3/reference/import.html",
    "src": "PyPA 'src layout vs flat layout' — https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/",
    "pyproject": "PyPA 'Writing your pyproject.toml' — https://packaging.python.org/en/latest/guides/writing-pyproject-toml/",
    "specifiers": "PyPA Version specifiers — https://packaging.python.org/en/latest/specifications/version-specifiers/",
    "pep8": "PEP 8 — https://peps.python.org/pep-0008/",
    "ast": "ast.parse feature_version 은 best-effort — https://docs.python.org/3/library/ast.html",
}

RULE_LABEL = {
    "STRUCT-NO-INIT": "__init__.py 없이 패키지로 쓰이는 폴더",
    "STRUCT-TOPLEVEL-IMPORT": "내부 폴더를 최상위 이름으로 임포트 (sys.path 의존)",
    "STRUCT-DEP-MISSING": "선언 안 된 외부 의존성",
    "STRUCT-DEP-UNBOUNDED": "상한 없는 의존성 (새 메이저 설치 위험)",
    "STRUCT-SYSPATH": "sys.path 조작",
    "STRUCT-PYVER": "최소 지원 Python 버전에서 구문 오류",
    "STRUCT-SIDE-EFFECT": "임포트할 때 실행되는 코드",
    "STRUCT-SHADOW": "표준 라이브러리와 같은 이름의 최상위 모듈",
    "STRUCT-BIG-MODULE": "거대 모듈",
}
AUTO_FIX = {
    "STRUCT-NO-INIT",
    "STRUCT-TOPLEVEL-IMPORT",
    "STRUCT-DEP-MISSING",
}  # + DEP-UNBOUNDED (--pin-major)

# import 이름 → 배포판 이름 (설치 정보로 못 찾을 때)
DIST_ALIASES = {
    "win32com": "pywin32",
    "pythoncom": "pywin32",
    "pywintypes": "pywin32",
    "win32api": "pywin32",
    "win32con": "pywin32",
    "win32gui": "pywin32",
    "win32process": "pywin32",
    "win32event": "pywin32",
    "win32service": "pywin32",
    "win32serviceutil": "pywin32",
    "servicemanager": "pywin32",
    "win32clipboard": "pywin32",
    "PIL": "pillow",
    "cv2": "opencv-python",
    "yaml": "pyyaml",
    "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "dateutil": "python-dateutil",
    "jwt": "pyjwt",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "fitz": "pymupdf",
    "Crypto": "pycryptodome",
    "google": "google-api-core",
}
# 모듈 최상위에서 호출되면 '임포트만 해도 실행되는 부작용'으로 보는 함수 이름 (어느 모듈 것이든)
RISKY_CALLS = {
    "print",
    "basicConfig",
    "chdir",
    "Popen",
    "check_call",
    "check_output",
    "load_dotenv",
    "mkdir",
    "makedirs",
    "remove",
    "unlink",
    "rmtree",
    "urlopen",
    "Dispatch",
    "GetActiveObject",
    "DispatchEx",
}
# (모듈, 함수) 로만 위험한 것 — 예: os.system 은 위험, platform.system 은 무해
RISKY_QUALIFIED = {
    ("os", "system"),
    ("os", "startfile"),
    ("subprocess", "run"),
    ("subprocess", "call"),
    ("sqlite3", "connect"),
    ("psycopg2", "connect"),
    ("pymysql", "connect"),
    ("pyodbc", "connect"),
}
RISKY_MODULES = {"requests", "httpx", "socket", "urllib"}  # 이 모듈의 함수 호출은 네트워크 부작용


@dataclass
class StructIssue:
    rule: str
    message: str
    file: str | None = None
    line: int | None = None
    severity: str = IMPROVE
    ref: str = ""
    hint: str = ""
    data: dict = field(default_factory=dict)  # 자동 수정에 필요한 정보

    @property
    def fixable(self) -> bool:
        """자동 수정에 필요한 정보가 실제로 확정된 경우만."""
        need = {
            "STRUCT-NO-INIT": "create",
            "STRUCT-TOPLEVEL-IMPORT": "new",
            "STRUCT-DEP-MISSING": "spec",
        }
        if self.rule in need:
            return bool(self.data.get(need[self.rule]))
        return self.rule == "STRUCT-DEP-UNBOUNDED" and bool(self.data.get("pin"))

    def to_finding(self) -> Finding:
        auto = " [자동 수정 가능: audit-kit struct fix]" if self.fixable else ""
        return Finding(
            tool="struct",
            category="구조",
            rule=self.rule,
            message=self.message,
            file=self.file,
            line=self.line,
            severity=self.severity,
            evidence=f"{RULE_LABEL.get(self.rule, self.rule)} — 근거: {self.ref}"
            + (f". 수정: {self.hint}" if self.hint else "")
            + auto,
            extra={"fixable": self.fixable},
        )


# ---------------------------------------------------------------- 공통
def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _dist_name(prefix: str) -> str:
    return re.split(r"[\s<>=!~;\[(@]", prefix.strip(), maxsplit=1)[0]


def declared_dependencies(cfg: AuditConfig) -> dict:
    """{정규화 이름: (그룹, 원문)} — dependencies 와 optional-dependencies 전부."""
    proj = read_pyproject(cfg.root).get("project", {})
    out = {}
    for d in proj.get("dependencies", []) or []:
        out[_norm(_dist_name(d))] = ("dependencies", d)
    for group, deps in (proj.get("optional-dependencies", {}) or {}).items():
        for d in deps or []:
            out.setdefault(_norm(_dist_name(d)), (group, d))
    return out


@functools.lru_cache(maxsize=1)
def _pkg_dists() -> dict:
    try:
        from importlib.metadata import packages_distributions

        return dict(packages_distributions())
    except Exception:  # ruff: ignore[blind-except] — 3.9 는 이 함수가 없고(ImportError), 설치된 패키지의
        return {}  # 메타데이터가 손상된 경우 예외 종류를 예측할 수 없다. 별칭으로 대체한다.


def import_to_dist(name: str) -> str | None:
    dists = _pkg_dists().get(name)
    if dists:
        return dists[0]
    return DIST_ALIASES.get(name)


def installed_version(dist: str) -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(dist)
    except PackageNotFoundError:  # 미설치
        return None


def spec_for(dist: str, ver: str | None) -> str:
    """>=설치버전,<다음메이저 (PyPA version specifiers). 버전을 모르면 이름만."""
    if not ver:
        return dist
    m = re.match(r"(\d+)(?:\.(\d+))?", ver)
    if not m:
        return dist
    major, minor = int(m.group(1)), m.group(2) or "0"
    low = f"{major}.{minor}"
    return f"{dist}>={low},<{major + 1}" if major >= 1 else f"{dist}>={low}"


def min_python(cfg: AuditConfig) -> tuple | None:
    rp = read_pyproject(cfg.root).get("project", {}).get("requires-python", "")
    m = re.search(r">=\s*(\d+)\.(\d+)", rp or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


# ---------------------------------------------------------------- 규칙
def _no_init_dir_roles(cfg, scope) -> dict:
    """`__init__.py` 없는 폴더마다 → 역할(production/tests/scripts/...). 실제로 점 경로로
    임포트되는지는 `_dirs_used_as_packages()`가 이어서 확인한다."""
    roles: dict[str, str] = {}
    for role, files in [("production", scope.production), *scope.support.items()]:
        for f in files:
            parts = f.split("/")[:-1]
            if parts and parts[0] == "src":
                parts = parts[1:]
                base = "src/"
            else:
                base = ""
            for i in range(1, len(parts) + 1):
                d = base + "/".join(parts[:i])
                if not (cfg.root / d / "__init__.py").exists():
                    roles.setdefault(d, role)
    return roles


def _dirs_used_as_packages(cfg, scope, graph) -> dict:
    """__init__.py 없는 폴더 중 점 경로로 임포트되는 것 → {폴더: 역할}
    (STD-08: 원래 이 함수 하나가 복잡도 11이었다 — 폴더 후보를 찾는 부분을 `_no_init_dir_roles()`
    로 뽑아냈다, 2026-09-28)"""
    roles = _no_init_dir_roles(cfg, scope)
    used = {}
    for d, role in roles.items():
        dotted = d[4:].replace("/", ".") if d.startswith("src/") else d.replace("/", ".")
        importers = [
            r
            for r in graph.records
            if r.target
            and (r.target == dotted or r.target.startswith(dotted + "."))
            and not r.src.startswith(dotted + ".")
        ]
        if importers:
            used[d] = (role, dotted, len(importers))
    return used


def check_no_init(cfg, scope, graph) -> list:
    out = []
    for d, (role, dotted, n) in sorted(_dirs_used_as_packages(cfg, scope, graph).items()):
        top = dotted.split(".")[0]
        shared = import_to_dist(top) is not None and "/" not in d.replace("src/", "", 1)
        data: dict[str, str]
        if role == "tests":
            hint, sev, data = (
                "tests 폴더는 pytest 의 import 모드와 얽혀 자동 수정하지 않음 — pytest 설정 확인 후 결정",
                IGNORE,
                {},
            )
        elif role == "migrations":
            hint, sev, data = (
                (
                    "마이그레이션 도구(alembic 등)는 파일 경로로 읽으므로 폴더를 패키지로 쓰지 말고, "
                    "필요한 코드를 제품 패키지로 옮긴다"
                ),
                IMPROVE,
                {},
            )
        elif shared:
            hint, sev, data = (
                f"설치된 배포판이 같은 이름 '{top}' 을 제공 — namespace package 일 수 있어 자동 수정하지 않음",
                REVIEW,
                {},
            )
        else:
            hint, sev, data = (
                "빈 __init__.py 를 추가해 일반 패키지로 만든다",
                IMPROVE,
                {"create": f"{d}/__init__.py"},
            )
        out.append(
            StructIssue(
                "STRUCT-NO-INIT",
                f"{d}/ 에 __init__.py 가 없는데 '{dotted}' 로 {n}곳에서 임포트됨 ({role})",
                file=d + "/",
                severity=sev,
                ref=REFS["pep420"],
                hint=hint,
                data=data,
            )
        )
    return out


def internal_names(graph) -> set:
    """최상위가 아닌 내부 모듈·폴더 이름 전부 (sys.path 조작으로 최상위처럼 임포트될 수 있는 이름)."""
    return {seg for m in graph.modules for seg in m.split(".")[1:]}


def check_toplevel_imports(cfg, graph) -> list:
    """`import parsers.x` 처럼 내부 폴더·모듈을 최상위 이름으로 임포트 (sys.path 조작에 기대는 임포트)."""
    suffix: dict = {}
    for m in graph.modules:
        parts = m.split(".")
        for i in range(1, len(parts)):
            suffix.setdefault(".".join(parts[i:]), set()).add(m)
    # 설치된 외부 패키지와 이름이 같으면 외부 것으로 본다 (예: 내부 모듈 requests.py 가 있어도 외부 requests)
    internal_tops = {n for n in internal_names(graph) if import_to_dist(n) is None}
    rel = {m: cfg.rel(f) for m, f in graph.modules.items()}
    out = []
    for r in graph.records:
        if not r.external or r.external not in internal_tops:
            continue
        tree = graph.trees.get(r.src)
        node = (
            next(
                (
                    n
                    for n in ast.walk(tree)
                    if isinstance(n, (ast.Import, ast.ImportFrom)) and n.lineno == r.line
                ),
                None,
            )
            if tree
            else None
        )
        if node is None:
            continue
        if isinstance(node, ast.ImportFrom):
            wanted = node.module or ""
        else:
            alias = next((a for a in node.names if a.name.split(".")[0] == r.external), None)
            wanted = alias.name if alias else r.external
        cands = sorted(suffix.get(wanted, set()))
        data = {}
        is_prod = r.src.split(".")[0] in cfg.packages
        if len(cands) == 1 and (
            isinstance(node, ast.ImportFrom) or all(a.asname for a in node.names)
        ):
            # 제품 코드는 항상 패키지로 실행되므로 자동 교체. 스크립트·테스트는 실행 방식(현재 폴더, sys.path)에
            # 따라 전체 경로가 안 풀릴 수 있어 --include-support 일 때만.
            data = {
                "module": r.src,
                "line": r.line,
                "old": wanted,
                "new": cands[0],
                "support": not is_prod,
            }
            hint = f"'{wanted}' → '{cands[0]}' 전체 경로 임포트로 교체" + (
                ""
                if is_prod
                else " (스크립트·테스트: 실행 방식 확인 후 — struct fix --include-support)"
            )
        elif len(cands) > 1:
            hint = f"같은 이름 후보가 여러 개({', '.join(cands[:3])}) — 사람이 골라야 함"
        else:
            hint = "대상 모듈을 확정할 수 없음 (import x 뒤 x.y 로 쓰는 형태 등) — 전체 경로로 수동 교체"
        out.append(
            StructIssue(
                "STRUCT-TOPLEVEL-IMPORT",
                f"{r.src} 가 내부 폴더 '{wanted}' 를 최상위 이름으로 임포트",
                file=rel.get(r.src),
                line=r.line,
                ref=REFS["import"] + " / " + REFS["src"],
                hint=hint,
                data=data,
                severity=IMPROVE,
            )
        )
    return out


def check_dependencies(cfg, graph, pin_major: bool = False) -> list:
    declared = declared_dependencies(cfg)
    has_project = bool(read_pyproject(cfg.root).get("project"))
    own = {p.split(".")[0] for p in cfg.packages} | {m.split(".")[0] for m in graph.modules}
    # 내부 모듈 이름을 최상위로 임포트한 것은 의존성이 아니라 STRUCT-TOPLEVEL-IMPORT 대상
    internal_tops = {n for n in internal_names(graph) if import_to_dist(n) is None}
    prod_mods = {m for m in graph.modules if m.split(".")[0] in cfg.packages}
    use: dict = {}  # import 이름 -> {"prod": n, "support": n, "first": (file, line)}
    rel = {m: cfg.rel(f) for m, f in graph.modules.items()}
    for r in graph.records:
        if (
            not r.external
            or r.external in own
            or r.external in internal_tops
            or r.external.startswith("_")
        ):
            continue
        u = use.setdefault(r.external, {"prod": 0, "support": 0, "first": (rel.get(r.src), r.line)})
        u["prod" if r.src in prod_mods else "support"] += 1
    out = []
    for name, u in sorted(use.items()):
        dist = import_to_dist(name)
        key = _norm(dist or name)
        if key in declared:
            continue
        ver = installed_version(dist) if dist else None
        group = "dependencies" if u["prod"] else "dev"
        spec = spec_for(dist, ver) if dist else None
        where = f"제품 코드 {u['prod']}곳" if u["prod"] else f"보조 코드 {u['support']}곳"
        data = {"group": group, "spec": spec} if (spec and has_project) else {}
        hint = (
            f"pyproject.toml [{'project.dependencies' if group == 'dependencies' else 'project.optional-dependencies.dev'}]"
            f" 에 '{spec}' 추가 (버전 하한은 지금 환경에 설치된 버전 기준 — 프로젝트 가상환경에서 실행해야 정확)"
            if spec
            else f"'{name}' 의 배포판 이름을 확인해 수동 추가 (현재 환경에 설치 안 됨)"
        )
        if not has_project:
            hint = "pyproject.toml 에 [project] 가 없어 자동 추가 불가 — 먼저 프로젝트 메타데이터를 만든다"
        out.append(
            StructIssue(
                "STRUCT-DEP-MISSING",
                f"'{name}'{f' ({dist})' if dist and dist != name else ''} 를 {where}에서 쓰는데 선언 안 됨",
                file="pyproject.toml",
                severity=IMPROVE if u["prod"] else IGNORE,
                ref=REFS["pyproject"],
                hint=hint,
                data=data,
            )
        )
    # 상한 없는 의존성: 설치된 메이저가 선언 하한 메이저보다 크면 위험
    for _key, (group, text) in sorted(declared.items()):
        if "<" in text or "==" in text or "~=" in text:
            continue
        # X.Y 형식 하한만 '메이저' 비교가 의미 있음 (pywin32>=306 처럼 빌드 번호 체계는 제외)
        m = re.search(r">=\s*(\d+)\.\d+", text)
        dist = _dist_name(text)
        ver = installed_version(dist)
        inst_m = re.match(r"(\d+)", ver) if ver else None
        if not m or not inst_m:
            continue
        low_major, inst_major = int(m.group(1)), int(inst_m.group(1))
        if inst_major > low_major:
            new = text.rstrip() + f",<{inst_major + 1}" if pin_major else ""
            out.append(
                StructIssue(
                    "STRUCT-DEP-UNBOUNDED",
                    f"'{text}' 는 상한이 없어 새 메이저 {ver} 가 설치됨 (하한 메이저 {low_major}) — API 가 바뀌었을 수 있음",
                    file="pyproject.toml",
                    severity=IMPROVE,
                    ref=REFS["specifiers"],
                    hint="코드가 새 메이저에서 동작하는지 확인. 아니면 '<다음메이저' 상한 추가 (struct fix --pin-major)",
                    data={"group": group, "old": text, "new": new, "pin": bool(pin_major)},
                )
            )
    return out


def _is_main_guard(node) -> bool:
    return isinstance(node, ast.If) and "__name__" in ast.unparse(node.test)


def _call_name(call: ast.Call) -> tuple:
    f = call.func
    if isinstance(f, ast.Name):
        return None, f.id
    if isinstance(f, ast.Attribute):
        base = f.value
        chain = []
        while isinstance(base, ast.Attribute):
            chain.append(base.attr)
            base = base.value
        root = base.id if isinstance(base, ast.Name) else None
        return (root, chain[-1] if chain else None), f.attr
    return None, None


def _check_syspath(m: str, rel: str, tree, is_prod: bool, syspath_support: dict) -> list:
    """sys.path 조작 (sys.path.insert/append/extend 호출, sys.path = / += 대입)."""
    sp_calls = [
        n for n in ast.walk(tree) if isinstance(n, ast.Call) and _call_name(n)[0] == ("sys", "path")
    ]
    sp_assign = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.Assign, ast.AugAssign))
        and "sys.path" in ast.unparse(n.targets[0] if isinstance(n, ast.Assign) else n.target)
    ]
    hits = sorted({n.lineno for n in sp_calls + sp_assign})
    if not hits:
        return []
    if is_prod:
        return [
            StructIssue(
                "STRUCT-SYSPATH",
                f"{m} 가 sys.path 를 조작 ({len(hits)}곳)",
                file=rel,
                line=hits[0],
                ref=REFS["import"] + " / " + REFS["src"],
                hint="패키지를 설치(pip install -e .)하고 전체 경로 임포트로 바꾼 뒤 sys.path 조작을 제거",
            )
        ]
    top = rel.split("/", maxsplit=1)[0]
    syspath_support.setdefault(top, []).append(rel)
    return []


def _check_side_effects(m: str, rel: str, tree) -> list:
    """임포트 시 부작용 (제품 모듈, 진입점 제외 — 호출부에서 이미 걸러 넘긴다)."""
    out = []
    for node in tree.body:
        if _is_main_guard(node):
            continue
        calls = []
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)) or (
            isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call)
        ):
            calls = [node.value]
        for c in calls:
            base, name = _call_name(c)
            root = base[0] if base else None
            risky = name in RISKY_CALLS or (root, name) in RISKY_QUALIFIED or root in RISKY_MODULES
            if risky and base != ("sys", "path"):  # sys.path 는 STRUCT-SYSPATH 에서 따로 보고
                out.append(
                    StructIssue(
                        "STRUCT-SIDE-EFFECT",
                        f"{m} 를 임포트하기만 해도 '{ast.unparse(c)[:60]}' 가 실행됨",
                        file=rel,
                        line=node.lineno,
                        ref=REFS["import"],
                        hint="함수 안으로 옮기거나 진입점(main)에서 호출. 로깅 설정은 진입점에서 한 번만 (PEP 8/로깅 HOWTO)",
                    )
                )
    return out


def _check_stdlib_shadow(f: Path, rel: str, stdlib: set) -> list:
    """표준 라이브러리 이름 가림: 패키지 밖(최상위) 모듈만."""
    stem = f.stem
    if (
        stem in stdlib
        and stem not in ("__init__", "__main__")
        and not (f.parent / "__init__.py").exists()
    ):
        return [
            StructIssue(
                "STRUCT-SHADOW",
                f"{rel} 는 표준 라이브러리 '{stem}' 과 이름이 같고 패키지 밖에 있음 — 그 폴더가 sys.path 에 오르면 표준 모듈을 가림",
                file=rel,
                ref=REFS["import"],
                hint=f"이름을 바꾸거나({stem}_utils 등) 패키지 안으로 옮긴다",
            )
        ]
    return []


def _check_big_module(cfg, m: str, rel: str, f: Path) -> list:
    n = len(read_text(f).splitlines())
    if n <= cfg.max_module_lines:
        return []
    return [
        StructIssue(
            "STRUCT-BIG-MODULE",
            f"{m} 가 {n}줄 (기준 {cfg.max_module_lines})",
            file=rel,
            ref="설정값 max_module_lines (공식 기준 없음)",
            hint="책임별로 모듈을 나누고 원래 모듈에서 재수출해 호환 유지",
            data={"lines": n},
        )
    ]


def check_module_body(cfg, scope, graph) -> list:
    """sys.path 조작, 임포트 시 부작용, 표준 라이브러리 이름 가림, 거대 모듈. 검사 종류별로
    `_check_*()`로 나눠뒀다(STD-08: 원래 이 함수 하나가 복잡도 15·분기 15였다, 2026-09-28)."""
    out = []
    prod = set(scope.production)
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    syspath_support: dict = {}
    for m, f in graph.modules.items():
        rel = cfg.rel(f)
        tree = graph.trees.get(m)
        if tree is None:
            continue
        is_prod = rel in prod
        is_entry = f.name in (
            "__main__.py",
            "main.py",
            "cli.py",
            "run.py",
            "server.py",
            "manage.py",
            "conftest.py",
        )
        out.extend(_check_syspath(m, rel, tree, is_prod, syspath_support))
        if is_prod and not is_entry:
            out.extend(_check_side_effects(m, rel, tree))
        out.extend(_check_stdlib_shadow(f, rel, stdlib))
        if is_prod:
            out.extend(_check_big_module(cfg, m, rel, f))
    for top, files in sorted(syspath_support.items()):
        out.append(
            StructIssue(
                "STRUCT-SYSPATH",
                f"보조 코드 {top}/ 의 {len(files)}개 파일이 sys.path 를 조작",
                file=files[0],
                ref=REFS["import"],
                severity=IGNORE if top in ("tests", "test") else IMPROVE,
                hint="프로젝트를 설치(pip install -e .)하면 대부분 필요 없어짐. 공통 부트스트랩 한 곳으로 모으기",
            )
        )
    return out


def find_interpreter(ver: tuple) -> list | None:
    """해당 버전 파이썬 실행 명령. Windows 는 py 런처, 그 외 pythonX.Y."""
    cands = [["py", f"-{ver[0]}.{ver[1]}"], [f"python{ver[0]}.{ver[1]}"]]
    if sys.version_info[:2] == ver:
        return [sys.executable]
    want = f"{ver[0]}.{ver[1]}"
    for c in cands:
        try:
            p = subprocess.run(
                [*c, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                **no_window_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if p.returncode == 0 and p.stdout.strip() == want:
            return c
    return None


_COMPILE_CODE = (
    "import ast,json,sys\n"
    "bad=[]\n"
    "for f in json.load(open(sys.argv[1],encoding='utf-8')):\n"
    "    try:\n"
    "        raw=open(f,'rb').read()\n"
    "        try: src=raw.decode('utf-8-sig')\n"
    "        except UnicodeDecodeError: src=raw.decode('cp949','replace')\n"
    "        ast.parse(src,filename=f)\n"
    "    except SyntaxError as e: bad.append([f,e.lineno,e.msg])\n"
    "print(json.dumps(bad))\n"
)


def check_python_version(cfg, scope) -> list:
    ver = min_python(cfg)
    if not ver:
        return []
    files = list(scope.production) + [f for fs in scope.support.values() for f in fs]
    interp = find_interpreter(ver)
    out = []
    if interp:
        import json
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tf:
            json.dump([str(cfg.root / f) for f in files], tf)
        try:
            p = subprocess.run(
                [*interp, "-c", _COMPILE_CODE, tf.name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=600,
                **no_window_kwargs(),
            )
            bad = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else []
        finally:
            Path(tf.name).unlink(missing_ok=True)
        how = f"Python {ver[0]}.{ver[1]} 인터프리터로 실제 확인"
    else:
        bad = []
        for f in files:
            try:
                ast.parse(read_text(cfg.root / f), feature_version=ver)
            except SyntaxError as e:
                bad.append([str(cfg.root / f), e.lineno, e.msg])
        how = f"Python {ver[0]}.{ver[1]} 미설치 — ast feature_version 으로 추정(best-effort, 놓칠 수 있음)"
    for f, line, msg in bad:
        out.append(
            StructIssue(
                "STRUCT-PYVER",
                f"requires-python >= {ver[0]}.{ver[1]} 인데 이 버전에서 구문 오류: {msg}",
                file=cfg.rel(f),
                line=line,
                severity=CRITICAL,
                ref=REFS["pyproject"] + " / " + REFS["ast"],
                hint=f"{ver[0]}.{ver[1]} 문법으로 고치거나 requires-python 을 올린다 ({how})",
            )
        )
    return out


# ---------------------------------------------------------------- 실행
def check(cfg: AuditConfig, pin_major: bool = False, only=None) -> list:
    scope = get_scope(cfg)
    graph = build_project_graph(cfg)
    issues = []
    rules = {
        "init": lambda: check_no_init(cfg, scope, graph),
        "toplevel": lambda: check_toplevel_imports(cfg, graph),
        "deps": lambda: check_dependencies(cfg, graph, pin_major),
        "body": lambda: check_module_body(cfg, scope, graph),
        "pyver": lambda: check_python_version(cfg, scope),
    }
    for name, fn in rules.items():
        if only and name not in only:
            continue
        issues += fn()
    return issues


def run_struct(cfg: AuditConfig) -> ToolResult:
    import time

    t0 = time.perf_counter()
    issues = check(cfg)
    fixable = sum(1 for i in issues if i.fixable)
    r = ToolResult(
        "struct",
        "findings" if issues else "ok",
        [i.to_finding() for i in issues],
        detail=f"자동 수정 가능 {fixable}건 (audit-kit struct fix)" if fixable else "",
    )
    r.duration = time.perf_counter() - t0
    return r

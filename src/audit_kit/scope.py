"""검사 범위: 프로젝트의 모든 파이썬 파일을 분류한다. 아무것도 조용히 빠지지 않게 한다.

분류
  production   제품 코드 (packages)
  support      보조 코드 — 역할별: tests / scripts / plugins / migrations
  excluded     분석 제외 (사유와 함께 보고) — 가상환경·빌드물·복사본·백업
  unassigned   어디에도 속하지 않음 → 반드시 보고 (설계에서 역할을 정해야 함)
"""

from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# 폴더 이름 → 보조 역할 (자동 분류)
SUPPORT_NAMES = {
    "tests": ["tests", "test", "testing"],
    "scripts": ["scripts", "script", "tools", "bin", "ops", "examples", "notebooks"],
    "plugins": ["plugins", "plugin", "extensions", "addons"],
    "migrations": ["alembic", "migrations", "migration"],
}
SUPPORT_SUFFIX = {"_plugins": "plugins", "_plugin": "plugins", "_scripts": "scripts", "_tests": "tests"}

# 분석하지 않지만, git 에 들어 있으면 '저장소 관리 문제'로 보고할 패턴 (사유)
JUNK_PATTERNS = [
    (".claude/worktrees/*", "Claude Code 작업용 복사본(worktree)이 저장소에 포함됨"),
    ("*_backup*/*", "백업 폴더가 저장소에 포함됨"),
    ("*/backup/*", "백업 폴더가 저장소에 포함됨"),
    ("*백업*/*", "백업 폴더가 저장소에 포함됨"),
    ("*_old.py", "옛 버전 파일이 저장소에 포함됨"),
    ("*_copy.py", "복사본 파일이 저장소에 포함됨"),
    ("*_backup.py", "백업 파일이 저장소에 포함됨"),
]
# 분석 제외 (보고는 하되 문제로 보지 않음)
ENV_PATTERNS = ["*.venv/*", "*venv/*", "*site-packages/*", "*node_modules/*", "build/*", "dist/*",
                "*__pycache__/*", "*.egg-info/*", ".git/*", ".tox/*", ".nox/*"]


@dataclass
class Scope:
    production: list = field(default_factory=list)  # 상대 경로(posix)
    support: dict = field(default_factory=dict)  # role -> [상대 경로]
    excluded: dict = field(default_factory=dict)  # 사유 -> [상대 경로]
    junk: dict = field(default_factory=dict)  # 사유 -> [상대 경로] (저장소 관리 문제)
    unassigned: list = field(default_factory=list)
    source: str = "git"  # 파일 목록 출처: git / filesystem

    @property
    def total(self) -> int:
        return (len(self.production) + sum(map(len, self.support.values())) + sum(map(len, self.excluded.values()))
                + sum(map(len, self.junk.values())) + len(self.unassigned))

    def support_roots(self) -> dict:
        """role -> 최상위 경로(폴더 또는 루트 파일) — 임포트 그래프 구성용."""
        out: dict = {}
        for role, files in self.support.items():
            for f in files:
                top = f.split("/")[0]
                out.setdefault(role, set()).add(top)
        return {r: sorted(v) for r, v in out.items()}

    def summary(self) -> list:
        L = [f"제품 코드 {len(self.production)}"]
        L += [f"{role} {len(fs)}" for role, fs in sorted(self.support.items())]
        if self.unassigned:
            L.append(f"미지정 {len(self.unassigned)}")
        ex = sum(map(len, self.excluded.values()))
        if ex:
            L.append(f"제외 {ex}")
        jk = sum(map(len, self.junk.values()))
        if jk:
            L.append(f"복사본·백업 {jk}")
        return L


def _git(root: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)


def _filesystem_files(root: Path) -> list:
    files = []
    for f in root.rglob("*.py"):
        rel = f.relative_to(root).as_posix()
        if not any(fnmatch.fnmatch(rel, g) for g in ENV_PATTERNS):
            files.append(rel)
    return sorted(files)


def list_python_files(root: Path) -> tuple:
    """(상대 경로 목록, 출처). git 저장소면 추적 + 새 파일(무시 규칙 제외), 아니면 파일시스템.

    프로젝트 폴더가 상위 저장소(예: 홈 폴더 저장소)의 무시 규칙에 걸리면 git 목록이 비어 버린다.
    그런 경우 조용히 0개를 검사하지 않도록 파일시스템 목록으로 바꾼다.
    """
    try:
        p = _git(root, "ls-files", "-co", "--exclude-standard", "--", "*.py")
        if p.returncode == 0:
            files = sorted({ln.strip().strip('"') for ln in p.stdout.splitlines() if ln.strip()})
            files = [f for f in files if (root / f).is_file()]
            ignored_root = _git(root, "check-ignore", "-q", str(root)).returncode == 0
            if files and not ignored_root:
                return files, "git"
            fs = _filesystem_files(root)
            if fs:
                why = "프로젝트 폴더가 상위 git 저장소에서 무시됨" if ignored_root else "git 목록이 비어 있음"
                return fs, f"filesystem ({why})"
            return files, "git"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return _filesystem_files(root), "filesystem"


def support_role(top: str, is_file: bool) -> str:
    """최상위 폴더/파일 이름으로 보조 역할 추정. 모르면 ''."""
    if is_file:
        return "scripts"  # 루트의 .py 는 실행 스크립트로 본다 (진입점은 packages/entrypoints 로 따로 지정)
    low = top.lower()
    for role, names in SUPPORT_NAMES.items():
        if low in names:
            return role
    for suffix, role in SUPPORT_SUFFIX.items():
        if low.endswith(suffix):
            return role
    return ""


_CACHE: dict = {}


def get_scope(cfg) -> Scope:
    """한 번의 실행 안에서는 같은 결과를 재사용."""
    key = (str(cfg.root), tuple(cfg.packages), repr(cfg.support), tuple(cfg.exclude_paths))
    if key not in _CACHE:
        _CACHE[key] = discover(cfg)
    return _CACHE[key]


def support_module_prefixes(scope: Scope) -> dict:
    """role -> 모듈 접두어 (폴더면 폴더 이름, 루트 파일이면 파일 이름)."""
    return {role: [t.removesuffix(".py") for t in tops] for role, tops in scope.support_roots().items()}


def build_project_graph(cfg, include_support: bool = True):
    """제품 코드 + 보조 코드(테스트·스크립트·플러그인·마이그레이션) 임포트 그래프."""
    from audit_kit.importgraph import ImportGraph

    paths = list(cfg.package_paths())
    if not include_support:
        return ImportGraph.build(paths)
    scope = get_scope(cfg)
    for tops in scope.support_roots().values():
        paths += [cfg.root / t for t in tops]
    allow = {(cfg.root / f).resolve() for f in scope.production}
    allow |= {(cfg.root / f).resolve() for fs in scope.support.values() for f in fs}
    return ImportGraph.build(paths, allow=allow)


def discover(cfg) -> Scope:
    root = cfg.root
    files, source = list_python_files(root)
    prod_tops = {p.relative_to(root).parts[0] for p in cfg.package_paths()}
    src_layout = {p.relative_to(root).as_posix() for p in cfg.package_paths() if p.relative_to(root).parts[0] == "src"}
    explicit = {role: list(v) for role, v in (cfg.support or {}).items()}
    s = Scope(source=source)
    for rel in files:
        if any(fnmatch.fnmatch(rel, g) for g in ENV_PATTERNS):
            s.excluded.setdefault("가상환경·빌드물", []).append(rel)
            continue
        junk = next((why for g, why in JUNK_PATTERNS if fnmatch.fnmatch(rel, g)), None)
        if junk:
            s.junk.setdefault(junk, []).append(rel)
            continue
        if any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, g.rstrip("/") + "/*") for g in cfg.exclude_paths):
            s.excluded.setdefault("설정으로 제외(exclude_paths)", []).append(rel)
            continue
        parts = rel.split("/")
        top = parts[0]
        if top in prod_tops and (top != "src" or any(rel.startswith(p + "/") for p in src_layout)):
            s.production.append(rel)
            continue
        role = next((r for r, globs in explicit.items()
                     if any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, g.rstrip("/") + "/*") for g in globs)), "")
        if not role:  # 명시 설정에 없으면 폴더 이름으로 추정
            role = support_role(top, len(parts) == 1)
        if role:
            s.support.setdefault(role, []).append(rel)
        else:
            s.unassigned.append(rel)
    return s

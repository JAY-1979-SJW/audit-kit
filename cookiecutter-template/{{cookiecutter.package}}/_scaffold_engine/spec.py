# pylint: disable=duplicate-code
"""architecture.toml — 프로그램 구조 설계 파일."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

SPEC_FILE = "architecture.toml"

# (계층 이름, 설명, 이 계층으로 볼 폴더/모듈 이름) — 위가 상위 계층
LAYER_TEMPLATE = [
    (
        "interface",
        "외부 입출력: HTTP 라우터, CLI, AutoCAD 플러그인, UI. 요청 검증·응답 변환만 한다",
        [
            "api",
            "routers",
            "routes",
            "endpoints",
            "views",
            "cli",
            "plugin",
            "plugins",
            "autocad",
            "ui",
            "web",
            "gui",
            "tools",
        ],
    ),
    (
        "service",
        "유스케이스 조율: 트랜잭션 경계, 여러 도메인·저장소 호출 순서",
        ["services", "service", "usecases", "application", "workflows"],
    ),
    (
        "repository",
        "데이터 접근: DB 조회·저장 쿼리",
        ["repositories", "repository", "crud", "dao", "stores"],
    ),
    (
        "domain",
        "도메인 모델과 계산: ORM/스키마 모델, 도면(ezdxf) 해석·물량 계산 같은 순수 로직",
        [
            "models",
            "schemas",
            "domain",
            "entities",
            "dxf",
            "drawing",
            "drawings",
            "geometry",
            "calc",
            "calculation",
            "calculations",
            "engine",
            "quantity",
            "parsers",
        ],
    ),
    (
        "infra",
        "기반: 설정, DB 연결·세션 팩토리, 공용 유틸",
        [
            "core",
            "db",
            "database",
            "config",
            "settings",
            "utils",
            "util",
            "common",
            "infra",
            "infrastructure",
            "lib",
            "helpers",
        ],
    ),
]
ENTRYPOINT_NAMES = ["main", "app", "server", "wsgi", "asgi", "__main__", "run", "manage"]

# 외부 라이브러리 사용 위치 권장 (계층 이름 기준). None = 현재 사용 위치를 기준선으로
KNOWN_EXTERNAL = {
    "fastapi": ["interface"],
    "starlette": ["interface"],
    "flask": ["interface"],
    "ezdxf": None,
    "pyautocad": None,
    "win32com": None,
    "comtypes": None,
}


@dataclass
class Layer:
    name: str
    modules: list
    description: str = ""
    siblings_independent: bool = False  # 같은 계층 모듈끼리 임포트 금지
    responsibility: str = ""  # 이 계층이 하는 일 (설계 v2)
    must_not: list = field(default_factory=list)  # 이 계층이 하면 안 되는 일
    # 이 계층이 대응하는 기준서(35. 기준서 작성 체계) 문서의 doc_id/code. 있으면
    # standard-writer 가 00-master §1·§3 계층 도출 시 이 값을 그대로 쓴다 (강제 동기화).
    standard_doc_id: str = ""


@dataclass
class Forbidden:
    name: str
    source: list
    forbidden: list  # 내부 모듈 접두어 또는 외부 패키지 이름


@dataclass
class External:
    package: str
    allowed_in: list
    reason: str = ""


@dataclass
class Independent:
    name: str
    modules: list


@dataclass
class ArchSpec:
    root_packages: list
    layers: list = field(default_factory=list)
    entrypoints: list = field(default_factory=list)
    allow_skip_layers: bool = True  # 상위 계층이 두 단계 아래를 직접 임포트 허용
    ignore_type_checking: bool = True  # if TYPE_CHECKING: 임포트는 위반으로 보지 않음
    lazy_imports: str = "violation"  # violation | allow  (함수 안 지연 임포트도 계층 위반인가)
    cycles: str = "forbid"  # forbid | allow
    unassigned: str = "warn"  # warn | ignore  (어느 계층에도 속하지 않는 모듈)
    forbidden: list = field(default_factory=list)
    external: list = field(default_factory=list)
    independent: list = field(default_factory=list)
    # 보조 코드: role -> 모듈 접두어. 제품 코드는 이것들을 임포트하면 안 된다.
    support: dict = field(default_factory=dict)
    private_use: str = "warn"  # warn | ignore — 스크립트·플러그인이 제품의 _비공개 함수를 쓰는 것
    # 설계 v2: 프로젝트 유형(templates.TYPES 키)과 공통 관심사 지정 위치 {config: ["app.core.config"], ...}
    types: list = field(default_factory=list)
    concerns: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)  # 초안 생성 시 사람에게 알릴 메모 (파일에는 주석으로)
    path: Path | None = None

    # ---- 조회
    def layer_of(self, module: str):
        """(계층 인덱스, 계층) — 가장 긴 접두어 우선. 없으면 (None, None)."""
        best: tuple[int, Layer] | tuple[None, None] = (None, None)
        best_len = -1
        for i, lay in enumerate(self.layers):
            for prefix in lay.modules:
                if (module == prefix or module.startswith(prefix + ".")) and len(prefix) > best_len:
                    best, best_len = (i, lay), len(prefix)
        return best

    def is_entrypoint(self, module: str) -> bool:
        return any(module == e or module.startswith(e + ".") for e in self.entrypoints)

    def layer_index(self, name: str):
        return next((i for i, lay in enumerate(self.layers) if lay.name == name), None)

    def support_role(self, module: str):
        """보조 코드 역할(tests/scripts/plugins/migrations) 또는 None. 가장 긴 접두어 우선."""
        best, best_len = None, -1
        for role, prefixes in self.support.items():
            for p in prefixes:
                if (module == p or module.startswith(p + ".")) and len(p) > best_len:
                    best, best_len = role, len(p)
        return best

    def is_production(self, module: str) -> bool:
        return (
            self.layer_of(module)[0] is not None
            or self.is_entrypoint(module)
            or module in self.root_packages
            or module.split(".", maxsplit=1)[0] in self.root_packages
        )


def matches(module: str, prefixes: list) -> bool:
    return any(module == p or module.startswith(p + ".") for p in prefixes)


# ---------------------------------------------------------------- load
def load_spec(root: Path) -> ArchSpec | None:
    f = root / SPEC_FILE
    if not f.is_file():
        return None
    with f.open("rb") as fh:
        d = tomllib.load(fh)
    rules = d.get("rules", {})
    spec = ArchSpec(
        root_packages=d.get("project", {}).get("root_packages", []),
        layers=[
            Layer(
                x["name"],
                list(x.get("modules", [])),
                x.get("description", ""),
                bool(x.get("siblings_independent", False)),
                x.get("responsibility", ""),
                list(x.get("must_not", [])),
                x.get("standard_doc_id", ""),
            )
            for x in d.get("layers", [])
        ],
        entrypoints=d.get("project", {}).get("entrypoints", []),
        allow_skip_layers=rules.get("allow_skip_layers", True),
        ignore_type_checking=rules.get("ignore_type_checking", True),
        lazy_imports=rules.get("lazy_imports", "violation"),
        cycles=rules.get("cycles", "forbid"),
        unassigned=rules.get("unassigned", "warn"),
        forbidden=[
            Forbidden(x.get("name", "금지"), list(x["source"]), list(x["forbidden"]))
            for x in d.get("forbidden", [])
        ],
        external=[
            External(x["package"], list(x.get("allowed_in", [])), x.get("reason", ""))
            for x in d.get("external", [])
        ],
        independent=[
            Independent(x.get("name", "독립"), list(x["modules"])) for x in d.get("independent", [])
        ],
        support={role: list(v) for role, v in d.get("support", {}).items() if isinstance(v, list)},
        private_use=rules.get("private_use", "warn"),
        types=list(d.get("project", {}).get("types", [])),
        concerns={
            k: (list(v) if isinstance(v, list) else [v]) for k, v in d.get("concerns", {}).items()
        },
        path=f,
    )
    return spec


def validate_spec(spec: ArchSpec, modules: set) -> list:
    """설계 파일 자체의 문제 (존재하지 않는 모듈 등)."""
    problems = []
    names = [lay.name for lay in spec.layers]
    if len(names) != len(set(names)):
        problems.append("계층 이름 중복")
    seen: dict[str, str] = {}
    for lay in spec.layers:
        for prefix in lay.modules:
            if prefix in seen:
                problems.append(
                    f"모듈 '{prefix}' 가 계층 '{seen[prefix]}' 와 '{lay.name}' 에 중복 배정"
                )
            seen[prefix] = lay.name
            if not any(m == prefix or m.startswith(prefix + ".") for m in modules):
                problems.append(
                    f"계층 '{lay.name}' 의 모듈 '{prefix}' 가 코드에 없음 (설계와 코드 불일치)"
                )
    for e in spec.external:
        problems.extend(
            f"external '{e.package}' allowed_in '{p}' 는 계층 이름도 모듈도 아님"
            for p in e.allowed_in
            if p != "entrypoints"
            and spec.layer_index(p) is None
            and not any(m == p or m.startswith(p + ".") for m in modules)
        )
    from _scaffold_engine.templates import CONCERNS, TYPES

    problems.extend(
        f"알 수 없는 프로젝트 유형 '{t}' (가능: {', '.join(TYPES)})"
        for t in spec.types
        if t not in TYPES
    )
    for c, places in spec.concerns.items():
        if c not in CONCERNS:
            problems.append(f"알 수 없는 공통 관심사 '{c}' (가능: {', '.join(CONCERNS)})")
        problems.extend(
            f"[concerns] {c} 의 위치 '{p}' 가 코드에 없음"
            for p in places
            if p != "entrypoints" and not any(m == p or m.startswith(p + ".") for m in modules)
        )
    return problems


# ---------------------------------------------------------------- infer: 초안
def infer_spec(graph, root_packages: list, scope=None) -> tuple:
    """현재 코드에서 설계 초안을 추론. (ArchSpec, 미배정 모듈·폴더 목록)"""
    spec, unassigned = _infer_layers(graph, root_packages)
    if scope is not None:
        from audit_kit.scope import support_module_prefixes

        spec.support = support_module_prefixes(scope)
        tops = sorted({"/".join(f.split("/")[:2]) if "/" in f else f for f in scope.unassigned})
        unassigned += [f"{t} (폴더/파일 — 제품 계층 또는 [support] 역할 지정 필요)" for t in tops]
    apply_templates(spec, graph, root_packages)
    return spec, unassigned


# 공통 관심사 전용 모듈로 보이는 이름 (마지막 모듈 이름 기준)
CONCERN_MODULE_NAMES = {
    "config": ("config", "settings", "conf", "configuration"),
    "logging": ("logging_config", "log_config", "logging_setup", "logger", "logs"),
    "db": ("database", "db", "session", "db_session", "engine"),
    "com": ("com_session", "cad_com", "acad_com", "com_adapter", "com_client", "autocad"),
    "http": ("http_client", "api_client", "client", "http"),
}


def _apply_external_rules(spec: ArchSpec, usage, mg: dict) -> None:
    present = {e.package for e in spec.external}
    for pkg, allowed in mg["external"].items():
        if usage.get(pkg) and pkg not in present:
            al = [a for a in allowed if a == "entrypoints" or spec.layer_index(a) is not None]
            al_set = frozenset(al)
            if al:
                spec.external.append(
                    External(
                        pkg,
                        al + ([] if "entrypoints" in al_set else ["entrypoints"]),
                        "프로젝트 유형 템플릿 기본 규칙",
                    )
                )


def _apply_concern_locations(spec: ArchSpec, uses, prod: set, mg: dict) -> None:
    """공통 관심사 위치 초안: 템플릿이 권하는 계층 안에서 가장 많이 수행하는 모듈 → 없으면 전체에서
    가장 많은 곳(가장 많이 쓰는 곳이 잘못된 곳일 수 있으므로 — 예: 계산 엔진이 COM 을 직접 연결)."""
    from collections import Counter

    from _scaffold_engine.concerns import dominant_locations

    for c, want in mg["concerns"].items():
        ranked = [m for m, _ in Counter(u.module for u in uses if u.concern == c).most_common()]
        if not ranked or c in spec.concerns:
            continue
        # 1) 이름이 관심사 전용 모듈인 곳(logging_config, database, config …) — 사용 여부와 무관하게 우선
        named = sorted(
            (
                m
                for m in prod
                if m.split(".")[-1] in CONCERN_MODULE_NAMES.get(c, ())
                and not (spec.layer_of(m)[1] and spec.layer_of(m)[1].name == "interface")
            ),  # 라우터 settings.py 등 제외
            key=lambda m: (m not in ranked, m.count("."), m),
        )
        in_layer = [m for m in ranked if (spec.layer_of(m)[1] and spec.layer_of(m)[1].name == want)]
        if named:
            spec.concerns[c] = named[:1]
            if len(named) > 1:
                spec.notes.append(
                    f"{c}: 전용 모듈이 여러 개 ({', '.join(named[:4])}) — 하나로 합치는 것을 권장"
                )
        elif in_layer:
            spec.concerns[c] = [in_layer[0]]
        else:
            spec.concerns[c] = [ranked[0]]
            spec.notes.append(
                f"{c}: 권장 계층 '{want}' 에서 수행하는 곳이 없어 가장 많이 쓰는 {ranked[0]} 로 임시 지정 — "
                f"'{want}' 계층에 전용 모듈을 만들고 옮기는 것을 권장"
            )
    for c, m in dominant_locations(uses).items():
        spec.concerns.setdefault(c, [m])


def apply_templates(spec: ArchSpec, graph, root_packages: list, types=None):
    """프로젝트 유형 템플릿으로 계층 책임·금지, 외부 라이브러리 규칙, 공통 관심사 위치를 채운다.
    (STD-08: 원래 이 함수 하나가 복잡도 12였다 — 외부 라이브러리·공통 관심사 부분을 각각
    `_apply_external_rules`/`_apply_concern_locations`로 뽑아냈다, 2026-09-28)"""
    root_packages_set = frozenset(root_packages)
    from collections import Counter

    from _scaffold_engine.concerns import find_uses
    from _scaffold_engine.templates import detect_types, merged

    prod = {m for m in graph.modules if m.split(".")[0] in root_packages_set}
    usage = Counter(r.external for r in graph.records if r.external and r.src in prod)
    spec.types = list(types) if types else detect_types(usage)
    mg = merged(spec.types)
    for lay in spec.layers:
        info = mg["layers"].get(lay.name)
        if info:
            lay.responsibility = " / ".join(info["responsibility"])
            lay.must_not = list(info["must_not"])
    _apply_external_rules(spec, usage, mg)
    _apply_concern_locations(spec, find_uses(graph, prod), prod, mg)


def _infer_layers(graph, root_packages: list) -> tuple:
    root_packages_set = frozenset(root_packages)
    layers = [Layer(name, [], desc) for name, desc, _ in LAYER_TEMPLATE]
    entrypoints, unassigned = [], []
    for root in root_packages:
        children = sorted({
            m.split(".")[1] for m in graph.modules if m.startswith(root + ".") and m.count(".") >= 1
        })
        for child in children:
            full = f"{root}.{child}"
            idx = next(
                (i for i, (_, _, words) in enumerate(LAYER_TEMPLATE) if child.lower() in words),
                None,
            )
            if idx is not None:
                layers[idx].modules.append(full)
            elif child in ENTRYPOINT_NAMES and full in graph.modules:
                entrypoints.append(full)
            else:
                unassigned.append(full)
    layers = [lay for lay in layers if lay.modules]
    spec = ArchSpec(root_packages=list(root_packages), layers=layers, entrypoints=entrypoints)

    # 외부 라이브러리
    usage: dict = {}
    for r in graph.records:
        if (
            r.external in KNOWN_EXTERNAL and r.src.split(".")[0] in root_packages_set
        ):  # 제품 코드 사용만
            usage.setdefault(r.external, set()).add(_top_child(r.src))
    for pkg, where in sorted(usage.items()):
        rec = KNOWN_EXTERNAL[pkg]
        if rec is None:
            allowed = sorted(w for w in where if w)
            reason = "현재 사용 위치 기준 — 가능하면 한 계층(도면 처리 모듈 등)으로 좁히세요"
        else:
            allowed = [n for n in rec if spec.layer_index(n) is not None] + ["entrypoints"]
            reason = "웹 프레임워크는 인터페이스 계층에서만 (서비스가 HTTPException 을 던지는 등의 결합 방지)"
        spec.external.append(External(pkg, allowed, reason))
    return spec, unassigned


def _top_child(module: str) -> str:
    parts = module.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else parts[0]


def _q(items) -> str:
    return "[" + ", ".join(f'"{x}"' for x in items) + "]"


# ---------------------------------------------------------------- new (from scratch, 코드 없음)
def spec_from_scope(
    root_packages: list,
    types: list,
    concerns: list | None = None,
    entrypoints: list | None = None,
) -> ArchSpec:
    """코드가 아직 없는 새 프로젝트용 ArchSpec 초안. `infer_spec()` 은 ImportGraph(기존 코드)가
    필요하지만, 이 함수는 유형·관심사 선택만으로 `templates.merged()` 의 기본값을 채운다.
    `audit-kit new` 와 `/design` 스킬이 이 함수를 쓴다.
    """
    from _scaffold_engine.layout import layer_folder
    from _scaffold_engine.templates import LAYER_ORDER, merged

    mg = merged(types)
    layers = [
        Layer(
            name=name,
            modules=[],  # scaffold.generate_project() 가 실제 생성하며 채운다
            responsibility=" / ".join(mg["layers"].get(name, {}).get("responsibility", [])),
            must_not=list(mg["layers"].get(name, {}).get("must_not", [])),
        )
        for name in LAYER_ORDER
    ]
    spec = ArchSpec(
        root_packages=list(root_packages),
        layers=layers,
        entrypoints=list(entrypoints or []),
        types=list(types),
    )
    pkg = root_packages[0] if root_packages else "app"
    concern_names = concerns if concerns is not None else list(mg["concerns"])
    for c in concern_names:
        want = mg["concerns"].get(c)
        if want not in LAYER_ORDER:  # "entrypoints-only" 같은 특수값은 전용 모듈을 만들지 않음
            continue
        folder = layer_folder(want, types) or want
        base_name = CONCERN_MODULE_NAMES.get(c, (c,))[0]
        spec.concerns[c] = [f"{pkg}.{folder}.{base_name}"]
    return spec


def _layers_toml(spec: ArchSpec) -> list:
    L: list = [
        "",
        "# ---------------------------------------------------------------- 계층 (위 → 아래)",
        "# 위 계층은 아래 계층만 임포트할 수 있다. 아래 → 위 임포트는 위반.",
    ]
    for lay in spec.layers:
        L += [
            "",
            "[[layers]]",
            f'name = "{lay.name}"',
            f'description = "{lay.description}"',
            f"modules = {_q(lay.modules)}",
        ]
        if lay.responsibility:
            L.append(f'responsibility = "{lay.responsibility}"')
        if lay.must_not:
            L.append(f"must_not = {_q(lay.must_not)}")
        if lay.siblings_independent:
            L.append("siblings_independent = true")
        if lay.standard_doc_id:
            L.append(f'standard_doc_id = "{lay.standard_doc_id}"')
    return L


def _concerns_toml(spec: ArchSpec, unassigned: list) -> list:
    from _scaffold_engine.templates import CONCERNS

    L = [
        "",
        "# ---------------------------------------------------------------- 공통 관심사 위치",
        "# 이 작업을 하는 코드는 지정된 모듈(+진입점)에만 있어야 한다. 값: 모듈 접두어 목록",
        "# 초안은 '지금 가장 많이 하는 곳' — 사람이 확정한다. 다른 곳에 있으면 ARCH-CONCERN 위반.",
        "[concerns]",
    ]
    concerns_items = cast("list[tuple[str, dict]]", list(CONCERNS.items()))
    for c, info in concerns_items:
        if c in spec.concerns:
            L.append(f"{c} = {_q(spec.concerns[c])}  # {info['title']}")
        else:
            L.append(f"# {c} = []  # {info['title']} — 현재 코드에서 사용 안 함")
    L += [f"# !! {n}" for n in spec.notes]
    if unassigned:
        L += ["", "# !! 미배정 모듈 — 위 계층 중 하나의 modules 에 넣거나 entrypoints 에 넣으세요:"]
        L += [f"#    {m}" for m in unassigned]
    return L


def _external_toml(spec: ArchSpec) -> list:
    L = [
        "",
        "# ---------------------------------------------------------------- 외부 라이브러리 사용 위치",
        '# allowed_in: 계층 이름, 모듈 접두어, 또는 "entrypoints"',
    ]
    for e in spec.external:
        L += ["", "[[external]]", f'package = "{e.package}"', f"allowed_in = {_q(e.allowed_in)}"]
        if e.reason:
            L.append(f'reason = "{e.reason}"')
    L += [
        "",
        "# [[external]]",
        '# package = "sqlalchemy"',
        '# allowed_in = ["repository", "domain", "infra", "service"]',
        "",
        "# ---------------------------------------------------------------- 금지 규칙 (예시)",
        "# [[forbidden]]",
        '# name = "도면 계산 로직은 웹/플러그인을 모른다"',
        f'# source = ["{spec.root_packages[0] if spec.root_packages else "app"}.dxf"]',
        f'# forbidden = ["{spec.root_packages[0] if spec.root_packages else "app"}.api", "fastapi", "pyautocad"]',
        "",
        "# ---------------------------------------------------------------- 서로 독립 (예시)",
        "# [[independent]]",
        '# name = "플러그인끼리 독립"',
        '# modules = ["app.plugins.autocad", "app.plugins.web"]',
        "",
    ]
    return L


def render_spec(spec: ArchSpec, unassigned: list) -> str:
    """architecture.toml 텍스트를 만든다. 섹션별로 `_*_toml()` 헬퍼로 나눠뒀다(STD-08: 원래
    이 함수 하나가 복잡도 13·분기 13이었다, 2026-09-28)."""
    L = [
        "# 프로그램 구조 설계 — audit-kit 가 이 파일을 기준으로 임포트를 검사·수정한다.",
        "# 수정 후: audit-kit arch check   (위반 확인)",
        "#          audit-kit arch fix     (자동 수정 미리보기, --apply 로 적용)",
        "#          audit-kit arch doc     (ARCHITECTURE.md 갱신)",
        "",
        "[project]",
        f"root_packages = {_q(spec.root_packages)}",
        "# 진입점: 모든 계층을 조립하는 곳(main.py 등). 계층 검사에서 제외된다.",
        f"entrypoints = {_q(spec.entrypoints)}",
        "# 프로젝트 유형 (fastapi / cad / mcp / cli / desktop / library) — 계층 책임·관심사 기본값의 근거",
        f"types = {_q(spec.types)}",
        "",
        "[rules]",
        "# true: 상위 계층이 여러 단계 아래 계층을 직접 임포트해도 됨 (interface → infra 등)",
        f"allow_skip_layers = {str(spec.allow_skip_layers).lower()}",
        "# true: if TYPE_CHECKING: 블록 임포트는 실행 시 의존이 아니므로 위반에서 제외",
        f"ignore_type_checking = {str(spec.ignore_type_checking).lower()}",
        '# "violation": 함수 안 지연 임포트도 계층 위반으로 봄 / "allow": 허용',
        f'lazy_imports = "{spec.lazy_imports}"',
        '# "forbid": 모듈 최상위 순환 임포트 금지',
        f'cycles = "{spec.cycles}"',
        '# "warn": 어느 계층에도 속하지 않는 모듈을 보고',
        f'unassigned = "{spec.unassigned}"',
        '# "warn": 스크립트·플러그인이 제품 코드의 _비공개 함수를 직접 쓰면 보고 (테스트는 제외)',
        f'private_use = "{spec.private_use}"',
        "",
        "# ---------------------------------------------------------------- 보조 코드",
        "# 제품이 아닌 코드. 제품 코드(아래 계층들)는 이것들을 임포트하면 안 된다(배포에 없을 수 있음).",
        "# 값은 모듈 접두어 (폴더 이름, 루트 스크립트는 파일 이름).",
        "[support]",
    ]
    L.extend(
        f"{role} = {_q(spec.support.get(role, []))}"
        for role in ("tests", "scripts", "plugins", "migrations")
    )
    for role, v in spec.support.items():
        if role not in ("tests", "scripts", "plugins", "migrations"):
            L.append(f"{role} = {_q(v)}")
    L += _layers_toml(spec)
    L += _concerns_toml(spec, unassigned)
    L += _external_toml(spec)
    return "\n".join(L)

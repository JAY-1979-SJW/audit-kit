"""기준서 직접 구현 검사 (AST). ruff 규칙으로 잡을 수 없는 조항을 맡는다.

파일 단위 검사는 `(tree, rel) -> [Hit]`, 프로젝트 단위 검사는 별도 함수다.
Hit.check 는 rules.toml 의 `custom`/`check.name` 과 같은 이름이고, run.py 가 조항으로 바꾼다.
"""

from __future__ import annotations

import ast
import copy
import fnmatch
import hashlib
import importlib.util
import os
import re
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path

from audit_kit.textio import read_text

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

ABS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|/(?:Users|home)/)")
COPY_NAME_RE = re.compile(r"(?i)(?:_backup|_bak|_old|_copy)$|복사본|백업")
VERSION_SUFFIX_RE = re.compile(r"(?i)(?:_v\d+|_final)$")
# 반복문 안에서 열면 안 되는 것: 같은 대상을 반복해 여는 경우.
# 파일 열기는 경로가 반복마다 달라지면 정상이라 인자가 반복 변수에 의존하지 않을 때만 본다.
# HTTP 요청은 재시도·페이지 반복이 정상이라 정적으로 구분할 수 없어 검사하지 않는다.
FILE_OPENERS = {"open", "load_workbook", "read_excel", "read_csv"}
CONNECTORS = {"Dispatch", "DispatchEx", "GetActiveObject"}
IO_NAMES = FILE_OPENERS | CONNECTORS
IO_MODULES = {"psycopg2": {"connect"}, "sqlite3": {"connect"}}
LOOPS = (ast.For, ast.AsyncFor, ast.While)
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
# 스크립트가 새로 띄우는 Office 앱만 Quit 대상이다. 실행 중인 AutoCAD 에 붙는 코드는 Quit 하면 사용자 세션이 닫힌다.
OFFICE_APPS = {
    "excel.application",
    "word.application",
    "powerpoint.application",
    "outlook.application",
}
MIN_DUP_STATEMENTS = 5


@dataclass
class Hit:
    check: str
    file: str
    line: int
    message: str
    severity: str = ""  # 비우면 조항의 기본 심각도를 쓴다


# ---------------------------------------------------------------- AST 도우미
def own_nodes(node: ast.AST):
    """node 와 그 하위 노드. 중첩 함수·클래스·람다(시작 노드 포함)의 안쪽은 들어가지 않는다."""
    if isinstance(node, NESTED):
        return
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(c for c in ast.iter_child_nodes(n) if not isinstance(c, NESTED))


def loop_parts(node: ast.AST) -> list:
    """반복문·컴프리헨션에서 '반복마다 실행되는' 부분."""
    if isinstance(node, ast.While):
        return [node.test, *node.body, *node.orelse]
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return [*node.body, *node.orelse]
    if isinstance(node, ast.DictComp):
        parts: list = [node.key, node.value]
    elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        parts = [node.elt]
    else:
        return []
    return parts + [c for g in node.generators for c in g.ifs]


def loops_in(tree: ast.AST):
    return (n for n in ast.walk(tree) if isinstance(n, LOOPS + COMPREHENSIONS))


def call_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    return f.attr if isinstance(f, ast.Attribute) else ""


def import_aliases(tree: ast.AST) -> dict:
    """별칭 -> 원래 이름. `import requests as r` => {"r": "requests"}, `from requests import get as g` => {"g": "requests.get"}."""
    aliases: dict = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            aliases.update({
                (a.asname or a.name).split(".")[0]: a.name.split(".")[0] for a in n.names
            })
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            aliases.update({
                a.asname or a.name: f"{n.module.split('.')[0]}.{a.name}" for a in n.names
            })
    return aliases


def io_call(call: ast.Call, aliases: dict | None = None) -> str:
    """반복문 안에서 피해야 하는 I/O 호출이면 그 이름, 아니면 ''."""
    aliases = aliases or {}
    name = call_name(call)
    f = call.func
    if isinstance(f, ast.Name):
        origin = aliases.get(f.id, "")
        module, _, attr = origin.partition(".")
        if attr and attr in IO_MODULES.get(module, ()):
            return origin  # from requests import get -> requests.get
        return name if name in IO_NAMES else ""
    if isinstance(f, ast.Attribute):
        if isinstance(f.value, ast.Name):
            module = aliases.get(f.value.id, f.value.id)  # import requests as r -> requests
            if name in IO_MODULES.get(module, ()):
                return f"{module}.{name}"
        if name in IO_NAMES - {"open"}:
            return name
    return ""


def docstring_ids(tree: ast.AST) -> set:
    ids = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, *DEFS)) and n.body:
            first = n.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def is_list_value(node: ast.AST) -> bool:
    if isinstance(node, (ast.List, ast.ListComp)):
        return True
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "list"


# ---------------------------------------------------------------- 파일 단위 검사
def check_abs_path_literal(tree: ast.AST, rel: str) -> list:
    """STD-02: 문자열 리터럴에 박힌 절대경로."""
    skip = docstring_ids(tree)
    return [
        Hit("ABS-PATH-LITERAL", rel, n.lineno, f"절대경로 하드코딩: {n.value[:60]!r}")
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in skip
        and ABS_PATH_RE.match(n.value)
    ]


def outermost_loops(tree: ast.AST) -> list:
    """다른 반복문 안에 들어 있지 않은 반복문·컴프리헨션 (안쪽 반복은 바깥 반복의 일부로 본다)."""
    loops = list(loops_in(tree))
    inner = {
        id(n)
        for loop in loops
        for part in loop_parts(loop)
        for n in own_nodes(part)
        if isinstance(n, LOOPS + COMPREHENSIONS)
    }
    return [loop for loop in loops if id(loop) not in inner]


def stored_names(loop: ast.AST) -> set:
    """반복문 안에서 값이 바뀌는 이름 (반복 변수와 반복 안에서 대입되는 이름)."""
    return {
        n.id for n in ast.walk(loop) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
    }


def _is_retry_or_poll(loop: ast.AST) -> bool:
    """재시도·폴링·첫 성공 탐색 반복인가: sleep 호출, break, return 이 있으면 같은 것을 다시 여는 것이 의도다."""
    for n in ast.walk(loop):
        if isinstance(n, (ast.Break, ast.Return)):
            return True
        if isinstance(n, ast.Call) and call_name(n) == "sleep":
            return True
    return False


def _opens_for_write(call: ast.Call) -> bool:
    """open(path, 'w') 처럼 쓰기·추가 모드 — 반복 중 체크포인트 저장은 정상이다."""
    mode = (
        call.args[1]
        if len(call.args) > 1
        else next((k.value for k in call.keywords if k.arg == "mode"), None)
    )
    return (
        isinstance(mode, ast.Constant)
        and isinstance(mode.value, str)
        and any(m in mode.value for m in "wax")
    )


def _opens_varying_target(call: ast.Call, name: str, varying: set) -> bool:
    """파일 열기 호출이고 인자가 반복마다 바뀌는 이름에 의존하면 True (파일마다 다른 것을 여는 정상 사용)."""
    if name.rsplit(".", 1)[-1] not in FILE_OPENERS:
        return False
    if name == "open" and _opens_for_write(call):
        return True
    args = [*call.args, *(k.value for k in call.keywords)]
    return any(isinstance(n, ast.Name) and n.id in varying for a in args for n in ast.walk(a))


def check_io_in_loop(tree: ast.AST, rel: str) -> list:
    """EFF-03: 반복문 안에서 같은 파일·DB 연결·COM 을 반복해 엶."""
    hits, seen = [], set()
    aliases = import_aliases(tree)
    for loop in outermost_loops(tree):
        if _is_retry_or_poll(loop):
            continue
        varying = stored_names(loop)
        for part in loop_parts(loop):
            for n in own_nodes(part):
                name = io_call(n, aliases) if isinstance(n, ast.Call) else ""
                if name and _opens_varying_target(n, name, varying):
                    continue
                if name and n.lineno not in seen:
                    seen.add(n.lineno)
                    hits.append(
                        Hit(
                            "IO-IN-LOOP",
                            rel,
                            n.lineno,
                            f"반복문 안에서 {name}() 호출 — 밖에서 한 번에 처리",
                        )
                    )
    return hits


def _scopes(tree: ast.AST):
    yield tree
    yield from (n for n in ast.walk(tree) if isinstance(n, DEFS))


def _scope_body(scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef) -> list:
    return scope.body


def _is_list_annotation(ann) -> bool:
    """타입 힌트가 `list` 또는 `list[...]` 인지(문자열 힌트·`typing.List` 는 다루지 않음 — 최신
    코드 스타일만 우선 지원, CLAUDE.md 4장 권장 문법과 일치)."""
    if isinstance(ann, ast.Name):
        return ann.id == "list"
    return (
        isinstance(ann, ast.Subscript)
        and isinstance(ann.value, ast.Name)
        and ann.value.id == "list"
    )


def _list_param_names(scope) -> set:
    """`: list` 또는 `: list[...]` 타입 힌트가 붙은 매개변수 이름(scope 가 함수가 아니면 빈 집합).
    이름만으로 추측하지 않는다 — 명시적 타입 힌트가 있을 때만 후보로 삼아, 다른 함수의 무관한
    동명 매개변수를 오탐하지 않는다(tests/test_std_variants.py "다른 함수의 같은 이름 list" 참고)."""
    if not isinstance(scope, DEFS):
        return set()
    args = scope.args
    all_args = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    return {a.arg for a in all_args if _is_list_annotation(a.annotation)}


@dataclass
class _ListCandidate:
    """EFF-02 자동 수정(std/fix.py)에 필요한 정보: list 이름 하나가 어느 스코프에서(지역 대입 또는
    매개변수) 만들어져, 어느 `in`/`not in` 비교식들에 걸렸는지."""

    scope: ast.AST
    name: str
    source: str  # "assign" | "param"
    anchor: ast.AST | None  # source=="assign"일 때 그 대입문, param 이면 None
    compares: list


def _scope_list_names(scope, own: list) -> tuple:
    """scope 에서 list 로 만들어진 이름들: (이름 -> 대입문 dict, 매개변수까지 합친 전체 이름 집합)."""
    assigns = {
        n.targets[0].id: n
        for n in own
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Name)
        and is_list_value(n.value)
    }
    return assigns, set(assigns) | _list_param_names(scope)


def _collect_membership_matches(own: list, names: set) -> dict:
    """반복문 안에서 `in`/`not in` 으로 걸린 노드들을 이름별로 모은다. 중첩 반복문은 바깥/안쪽이
    각각 "반복문"으로 잡혀 같은 비교식이 두 번 걸릴 수 있어 (노드, 이름) 쌍으로 중복 제거한다
    (기존 check_list_membership 의 lineno 기반 seen 과 같은 목적, 2026-09-28 std/fix.py 자동
    수정에서 같은 노드가 두 번 교체되는 실제 버그로 발견함 — 연쇄 비교 `x in a in b`처럼 한 노드가
    이름 둘에 걸리는 건 그대로 살린다)."""
    by_name: dict = {n: [] for n in names}
    seen: set = set()
    for loop in (n for n in own if isinstance(n, LOOPS + COMPREHENSIONS)):
        active = names - (_appended_names(loop) & names)
        if not active:
            continue
        for part in loop_parts(loop):
            for n in own_nodes(part):
                for matched in _matched_names(n, active):
                    key = (id(n), matched)
                    if key not in seen:
                        seen.add(key)
                        by_name[matched].append(n)
    return by_name


def _list_membership_candidates(tree: ast.AST) -> list:
    """탐지(check_list_membership)와 자동 수정(std/fix.py)이 공유하는 단일 근거. 지역 대입으로 만든
    list 뿐 아니라, 함수를 추출하는 리팩터링으로 list 를 만드는 곳과 `in` 검사하는 곳이 서로 다른
    함수로 갈라진 경우(`list` 타입 힌트 매개변수)도 잡는다(2026-09-28, arch/fix.py의 strat_move 분리
    과정에서 실제로 놓친 사례를 발견함)."""
    out = []
    for scope in _scopes(tree):
        own = [n for stmt in _scope_body(scope) for n in own_nodes(stmt)]
        assigns, names = _scope_list_names(scope, own)
        if not names:
            continue
        by_name = _collect_membership_matches(own, names)
        for name, compares in by_name.items():
            if compares:
                out.append(
                    _ListCandidate(
                        scope,
                        name,
                        "assign" if name in assigns else "param",
                        assigns.get(name),
                        compares,
                    )
                )
    return out


def check_list_membership(tree: ast.AST, rel: str) -> list:
    """EFF-02: list 로 만든 이름(지역 대입 또는 `list` 타입 힌트 매개변수)에 반복문 안에서 `in` 검사."""
    hits, seen = [], set()
    for cand in _list_membership_candidates(tree):
        for n in cand.compares:
            if n.lineno not in seen:
                seen.add(n.lineno)
                hits.append(
                    Hit(
                        "LIST-MEMBERSHIP-IN-LOOP",
                        rel,
                        n.lineno,
                        "반복문 안에서 list 에 `in` 검사 — set/dict 로 바꾸면 O(1)",
                    )
                )
    return hits


def _appended_names(loop: ast.AST) -> set:
    """반복문 안에서 .append()/.add() 되는 이름 — 순서를 유지하는 중복 제거 패턴이라 list 가 정당하다."""
    return {
        n.func.value.id
        for n in ast.walk(loop)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "append"
        and isinstance(n.func.value, ast.Name)
    }


def _matched_names(node: ast.AST, names: set) -> set:
    """node 가 `x in names_set`/`x not in names_set` 비교식이면, 걸린 이름들(보통 0개 또는 1개,
    연쇄 비교 `x in a in b` 면 여러 개일 수 있음)."""
    if not isinstance(node, ast.Compare) or not any(
        isinstance(o, (ast.In, ast.NotIn)) for o in node.ops
    ):
        return set()
    return {c.id for c in node.comparators if isinstance(c, ast.Name) and c.id in names}


def _is_load_workbook(call: ast.Call, aliases: dict) -> bool:
    """load_workbook(...) 이거나 `from openpyxl import load_workbook as lw` 의 lw(...)."""
    if call_name(call) == "load_workbook":
        return True
    f = call.func
    return isinstance(f, ast.Name) and aliases.get(f.id, "").endswith(".load_workbook")


def check_openpyxl_mode(tree: ast.AST, rel: str) -> list:
    """EFF-04: 읽기만 하는 load_workbook 에 read_only 를 주지 않음 (큰 파일이면 메모리 과다)."""
    hits: list = []
    aliases = import_aliases(tree)
    if any(isinstance(n, ast.Call) and call_name(n) == "save" for n in ast.walk(tree)):
        return hits  # 워크북을 저장하는 파일은 편집용이므로 read_only 를 쓸 수 없다
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and _is_load_workbook(n, aliases):
            kw = {k.arg: k.value for k in n.keywords}
            ro = kw.get("read_only")
            if ro is None or (isinstance(ro, ast.Constant) and ro.value is False):
                hits.append(
                    Hit(
                        "OPENPYXL-MODE",
                        rel,
                        n.lineno,
                        "load_workbook 에 read_only=True 없음 — 큰 파일이면 read_only 와 wb.close() 를 쓴다",
                    )
                )
    return hits


def _written_cell_ids(loop: ast.AST) -> set:
    """반복문 안에서 값·스타일을 쓰는 데 쓰인 ws.cell(...) 호출의 id. openpyxl 에는 범위 스타일 API 가 없어 불가피하다."""
    styled, ids = set(), set()
    for n in ast.walk(loop):
        targets = (
            n.targets
            if isinstance(n, ast.Assign)
            else [n.target]
            if isinstance(n, ast.AugAssign)
            else []
        )
        for t in targets:
            if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                styled.add(t.value.id)  # cell.border = ...
            elif isinstance(t, ast.Attribute) and isinstance(t.value, ast.Call):
                ids.add(id(t.value))  # ws.cell(...).value = ...
    for n in ast.walk(loop):
        if (
            isinstance(n, ast.Assign)
            and isinstance(n.value, ast.Call)
            and _is_cell_access(n.value)
            and any(isinstance(t, ast.Name) and t.id in styled for t in n.targets)
        ):
            ids.add(id(n.value))
        if (
            isinstance(n, ast.Call)
            and _is_cell_access(n)
            and any(k.arg == "value" for k in n.keywords)
        ):
            ids.add(id(n))
    return ids


def _is_cell_access(node: ast.AST) -> bool:
    """ws.cell(row=…, column=…) 형태의 셀 접근 호출인가."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    return node.func.attr == "cell" and bool(node.keywords or len(node.args) >= 2)


def check_cell_by_cell(tree: ast.AST, rel: str) -> list:
    """EFF-05: 반복문 안에서 ws.cell(...) 로 셀 값을 하나씩 읽음 (쓰기·스타일 지정은 제외)."""
    hits, seen = [], set()
    for loop in loops_in(tree):
        written = _written_cell_ids(loop)
        for part in loop_parts(loop):
            for n in own_nodes(part):
                if _is_cell_access(n) and n.lineno not in seen and id(n) not in written:
                    seen.add(n.lineno)
                    hits.append(
                        Hit(
                            "CELL-BY-CELL",
                            rel,
                            n.lineno,
                            "반복문 안에서 셀 단위 접근 — 행·범위 단위(iter_rows, append)를 검토",
                        )
                    )
    return hits


def check_com_cleanup(tree: ast.AST, rel: str) -> list:
    """EFF-06: Excel·Word 등 Office 앱을 Dispatch 로 띄우는 코드에 try/finally 가 없음."""
    hits: list = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.Module, *DEFS))):
        own = [n for stmt in fn.body for n in own_nodes(stmt)]
        if any(isinstance(n, ast.Try) and n.finalbody for n in own):
            continue
        hits.extend(
            Hit(
                "COM-CLEANUP",
                rel,
                n.lineno,
                "COM 애플리케이션을 띄우는데 try/finally(Quit·해제)가 없음",
            )
            for n in own
            if isinstance(n, ast.Call)
            and call_name(n) in {"Dispatch", "DispatchEx"}
            and _is_application(n)
        )
    return hits


def _is_application(call: ast.Call) -> bool:
    first = call.args[0] if call.args else None
    return (
        isinstance(first, ast.Constant)
        and isinstance(first.value, str)
        and first.value.lower() in OFFICE_APPS
    )


# subprocess 는 표준 라이브러리 모듈이라 `import subprocess` 로만 들어온다고 본다
# (별칭 import 는 import_aliases 로 따라간다).
SUBPROCESS_FUNCS = {"run", "Popen", "check_output", "check_call", "call"}


def _is_subprocess_call(call: ast.Call, aliases: dict) -> bool:
    f = call.func
    if not (isinstance(f, ast.Attribute) and f.attr in SUBPROCESS_FUNCS):
        return False
    base = f.value
    return isinstance(base, ast.Name) and aliases.get(base.id, base.id) == "subprocess"


def _is_true_constant(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _encoding_is_set(node: ast.AST | None) -> bool:
    """encoding= 인자가 있고 명시적으로 None 이 아님(값이 뭐든 지정했다는 뜻)."""
    if node is None:
        return False
    return not (isinstance(node, ast.Constant) and node.value is None)


def check_subprocess_text_no_encoding(tree: ast.AST, rel: str) -> list:
    """STD-12: subprocess ... text=True(또는 universal_newlines=True) 인데 encoding 미지정.

    text 모드는 encoding 을 안 주면 로케일 기본 인코딩(Windows 는 cp949)을 쓴다 - UTF-8 을
    내보내는 자식 프로세스의 출력이 깨지거나(디코딩 실패로) stdout 이 비정상이 될 수 있다.
    """
    hits = []
    aliases = import_aliases(tree)
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and _is_subprocess_call(n, aliases)):
            continue
        kw = {k.arg: k.value for k in n.keywords if k.arg}
        text_on = _is_true_constant(kw.get("text")) or _is_true_constant(
            kw.get("universal_newlines")
        )
        if text_on and not _encoding_is_set(kw.get("encoding")):
            hits.append(
                Hit(
                    "SUBPROCESS-TEXT-NO-ENCODING",
                    rel,
                    n.lineno,
                    "text=True(또는 universal_newlines=True) 인데 encoding 미지정 — "
                    "로케일 인코딩으로 읽혀 한글이 깨지거나 출력이 None 이 될 수 있다",
                )
            )
    return hits


# ------------------------------------------------ 훅(hook) 스크립트 진입점 (ERR-06, ERR-07)
# 이 두 검사는 "Claude Code 훅 스크립트"라는 특정 관행(claude-user/hooks/ 폴더, main()+
# `if __name__ == "__main__":` 진입점)을 대상으로 한다. AST 만으로 "훅인지"·"판정으로
# 이어지는지"를 완벽히 가릴 수 없어 이름·패턴 기반 휴리스틱이다(오탐·미탐 가능, rules.toml
# ERR-06/ERR-07 에 명시된 한계와 같음).
BROAD_EXCEPTION_NAMES = {"Exception", "BaseException"}
MIN_BROAD_TUPLE_SIZE = 3
SAFE_ENTRYPOINT_WRAPPERS = {"run_guard_main"}
# Claude Code 가 이 훅에 stdin 으로 JSON 페이로드를 준다는 증거. 이게 없으면 사람이 직접
# 실행하는 CLI 스크립트(예: py_stats.py, check_frontend.py)일 뿐이라 ERR-06 대상이 아니다
# (2026-09-28 실측: hooks 폴더 안에 있다는 이유만으로 둘 다 오탐했었다).
HOOK_STDIN_CALL_NAMES = {"read_hook_input", "read_edited_file", *SAFE_ENTRYPOINT_WRAPPERS}


def _is_broad_handler(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    if isinstance(handler.type, ast.Name):
        return handler.type.id in BROAD_EXCEPTION_NAMES
    if isinstance(handler.type, ast.Attribute):
        return handler.type.attr in BROAD_EXCEPTION_NAMES
    if isinstance(handler.type, ast.Tuple):
        return len(handler.type.elts) >= MIN_BROAD_TUPLE_SIZE
    return False


def _has_broad_guard(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(stmt, ast.Try) and any(_is_broad_handler(h) for h in stmt.handlers)
        for stmt in fn.body
    )


def _delegates_to_safe_wrapper(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(n, ast.Call) and call_name(n) in SAFE_ENTRYPOINT_WRAPPERS for n in ast.walk(fn)
    )


def _reads_hook_stdin(tree: ast.AST, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """fn 이(또는 fn 이 부르는 같은 모듈 함수 한 단계가) Claude Code 훅 stdin 을 읽는 흔적이
    있는가. main() 이 로직을 `_main_impl()` 같은 헬퍼로 나눈 경우까지 한 단계만 따라간다
    (완전한 호출 그래프 추적은 아니다)."""
    called = {call_name(n) for n in ast.walk(fn) if isinstance(n, ast.Call)}
    candidates = [fn, *(n for n in ast.walk(tree) if isinstance(n, DEFS) and n.name in called)]
    for f in candidates:
        for n in ast.walk(f):
            if isinstance(n, ast.Call) and call_name(n) in HOOK_STDIN_CALL_NAMES:
                return True
            if isinstance(n, ast.Attribute) and n.attr == "stdin":
                return True
    return False


def _is_main_guard(test: ast.AST) -> bool:
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
    )


def _unwrap_sys_exit(stmt: ast.AST) -> ast.Call | None:
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return None
    call = stmt.value
    if call_name(call) == "exit" and call.args and isinstance(call.args[0], ast.Call):
        return call.args[0]
    return call


def _main_entrypoint_call_name(tree: ast.AST) -> str | None:
    """모듈 최상위 `if __name__ == "__main__":` 블록이 부르는 함수 이름(있으면)."""
    for node in getattr(tree, "body", []):
        if not (isinstance(node, ast.If) and _is_main_guard(node.test)):
            continue
        for stmt in node.body:
            call = _unwrap_sys_exit(stmt)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                return call.func.id
    return None


def check_hook_entrypoint_guarded(tree: ast.AST, rel: str) -> list:
    """ERR-06: claude-user/hooks 아래 훅 진입점이 넓은 예외로 감싸여 있는지.

    대상: `hooks` 폴더 안에서 `if __name__ == "__main__":` 이 부르는 함수 — 그중에서도
    read_hook_input/read_edited_file/run_guard_main 호출이나 `sys.stdin` 참조로 Claude
    Code 가 stdin 으로 JSON 을 주는 진짜 훅임을 확인한 것만(사람이 직접 돌리는 CLI 스크립트,
    예: py_stats.py·check_frontend.py 는 제외 — 2026-09-28 실측 오탐 수정). 그 함수가
    `run_guard_main` 같은 공용 래퍼에 위임하거나, 자기 안에 바깥을 감싸는 try(핸들러가
    bare except·Exception·3개 이상 타입의 튜플)가 있으면 통과. 좁은 예외 하나만 잡는
    try(예: `except subprocess.TimeoutExpired:`)는 감싼 것으로 보지 않는다.
    """
    if "hooks" not in Path(rel).parts:
        return []
    name = _main_entrypoint_call_name(tree)
    if name is None:
        return []
    fn = next(
        (n for n in ast.walk(tree) if isinstance(n, DEFS) and n.name == name),
        None,
    )
    if fn is None or not _reads_hook_stdin(tree, fn):
        return []
    if _delegates_to_safe_wrapper(fn) or _has_broad_guard(fn):
        return []
    return [
        Hit(
            "HOOK-ENTRYPOINT-UNGUARDED",
            rel,
            fn.lineno,
            f"훅 진입점 {name}() 이 넓은 예외(Exception 또는 여러 타입)로 감싸여 있지 않음 — "
            "예상 밖 입력에 트레이스백으로 죽어 검사가 조용히 안 돌 수 있다",
        )
    ]


EXISTENCE_CALLS = {"exists", "is_file", "is_dir"}
CONTENT_CALLS = {"read_text", "read_bytes", "open", "load_workbook", "read", "readlines"}
DECISION_LOOKING_NAMES = {"emit", "deny", "block", "allow", "ask"}
DECISION_STRINGS = {"deny", "ask", "block", "allow", "pass"}


def _is_existence_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in EXISTENCE_CALLS
        and not node.args
    )


def _is_content_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in CONTENT_CALLS
    )


def _condition_is_existence_only(test: ast.AST) -> bool:
    calls = [n for n in ast.walk(test) if isinstance(n, ast.Call)]
    return any(_is_existence_call(c) for c in calls) and not any(_is_content_call(c) for c in calls)


def _looks_like_gating_decision(node: ast.AST) -> bool:
    if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant):
        v = node.value.value
        return isinstance(v, bool) or v in DECISION_STRINGS
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and call_name(node.value) in DECISION_LOOKING_NAMES
    )


def check_existence_only_gating(tree: ast.AST, rel: str) -> list:
    """ERR-07: 파일 '존재'만 보고(내용은 확인하지 않고) 차단·허용 판정으로 바로 이어짐.

    ERR-06 과 같은 이유로 `hooks` 폴더의 훅 스크립트로 범위를 좁힌다 — "mkdocs.yml 이
    있는가"처럼 설정 파일의 존재 자체가 곧 신호인 일반 코드(예: OPS-* 존재 검사)까지
    걸리면 이 검사 파일 자신도 오탐이 난다(실측으로 확인, 2026-09-28). 훅이 "이 경로가
    맞는 대상인지"를 존재만으로 판단해 차단·허용으로 바로 잇는 경우만 남긴다.

    `if path.exists():` 조건 안에 read_text 등 내용 검사가 전혀 없으면서, 그 분기 본문이
    True/False·"deny"/"ask"/"block"/"allow"/"pass" 를 반환하거나 emit/deny/block 류 이름의
    함수를 부르면 잡는다. AST 로는 "판정으로 이어지는가"까지만 볼 수 있어 오탐·미탐이 있을
    수 있다(rules.toml ERR-07 명시).
    """
    if "hooks" not in Path(rel).parts:
        return []
    hits = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.If) and _condition_is_existence_only(n.test)):
            continue
        if any(_is_content_call(c) for stmt in n.body for c in ast.walk(stmt)):
            continue
        if any(_looks_like_gating_decision(stmt) for stmt in n.body):
            hits.append(
                Hit(
                    "EXISTENCE-ONLY-GATING",
                    rel,
                    n.lineno,
                    "파일 존재만으로 판정 — 내용의 고유 서명까지 확인 필요"
                    "(무관한 파일이 같은 이름으로 있으면 오판할 수 있다)",
                )
            )
    return hits


def _is_len_call(node: ast.expr) -> ast.expr | None:
    """node 가 len(X) 형태면 X, 아니면 None."""
    if (
        isinstance(node, ast.Call)
        and call_name(node) == "len"
        and len(node.args) == 1
        and not node.keywords
    ):
        return node.args[0]
    return None


def _assert_falsy_target(test: ast.expr) -> ast.expr | None:
    """test 가 '컬렉션이 비어있어야 정상' 형태(assert not X / len(X)==0 / X==[])면 X 를 반환."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return test.operand
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq):
        left, right = test.left, test.comparators[0]
        inner = _is_len_call(left)
        if inner is not None and isinstance(right, ast.Constant) and right.value == 0:
            return inner
        if isinstance(right, (ast.List, ast.Set)) and not right.elts:
            return left
    return None


def _comprehension_source(expr: ast.expr) -> ast.expr | None:
    """expr 가 리스트/셋/제너레이터 컴프리헨션이면 첫 for 의 iter(필터링 대상 원본)을 반환."""
    if isinstance(expr, (ast.ListComp, ast.SetComp, ast.GeneratorExp)) and expr.generators:
        return expr.generators[0].iter
    return None


_COLLECTION_WRAPPERS = {"list", "tuple", "set", "frozenset", "sorted"}


_MAPPING_VIEWS = {"items", "keys", "values"}


def _unwrap_collection(node: ast.expr) -> ast.expr:
    """list(X)/tuple(X)/set(X)/frozenset(X)/sorted(X) 단순 래핑(인자 1개, 키워드 없음)과
    X.items()/X.keys()/X.values() 뷰 호출을 벗긴다. 이터레이터/제너레이터 SRC 는 list() 로
    감싸야 len()/truthy 검사가 가능하고, 매핑과 그 뷰는 비어 있음 여부가 같으므로 같은 SRC 로 본다."""
    while isinstance(node, ast.Call) and not node.keywords:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in _COLLECTION_WRAPPERS
            and len(node.args) == 1
        ):
            node = node.args[0]
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in _MAPPING_VIEWS
            and not node.args
        ):
            node = node.func.value
        else:
            break
    return node


def _test_guarantees_nonempty(test: ast.expr, source_dump: str) -> bool:
    """단언식 test 가 source_dump 와 같은 식의 '비어있지 않음'을 보장하는가.
    `X`, `list(X)`, `len(X) > N`/`>= N`/`!= 0`, 반대 방향 `N < len(X)`/`N <= len(X)`/`0 != len(X)`,
    그리고 `and` 로 이어진 항 중 하나라도 해당하면 True."""
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        return any(_test_guarantees_nonempty(v, source_dump) for v in test.values)
    if ast.dump(_unwrap_collection(test)) == source_dump:  # assert X / assert list(X)
        return True
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op, left, right = test.ops[0], test.left, test.comparators[0]
        if _is_len_call(left) is None and _is_len_call(right) is not None:
            # N < len(X) -> len(X) > N 로 뒤집는다
            flip = {ast.Lt: ast.Gt, ast.LtE: ast.GtE, ast.NotEq: ast.NotEq}.get(type(op))
            if flip is None:
                return False
            op, left, right = flip(), right, left
        inner = _is_len_call(left)
        if (
            inner is not None
            and ast.dump(_unwrap_collection(inner)) == source_dump
            and isinstance(right, ast.Constant)
        ):
            if isinstance(op, (ast.Gt, ast.GtE)) and isinstance(right.value, (int, float)):
                return True
            if isinstance(op, ast.NotEq) and right.value == 0:
                return True
    return False


def _sanity_assert_covers(fn: ast.AST, source_dump: str) -> bool:
    """fn 안에 source_dump 와 같은 식이 '비어있지 않음'을 보장하는 assert 가 있는지.
    source_dump 와 단언 양쪽의 list()/tuple() 등 단순 래핑은 벗겨서 비교한다."""
    return any(
        isinstance(node, ast.Assert) and _test_guarantees_nonempty(node.test, source_dump)
        for node in ast.walk(fn)
    )


def _bound_comprehensions(fn: ast.AST) -> dict[str, ast.expr]:
    """fn 안에서 `name = <컴프리헨션>` 형태로 대입된 이름 -> 컴프리헨션 매핑."""
    bound: dict[str, ast.expr] = {}
    for stmt in ast.walk(fn):
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, COMPREHENSIONS)
        ):
            bound[stmt.targets[0].id] = stmt.value
    return bound


# ---- ERR-08 원본(SRC) 종류 구분 -------------------------------------------------
# 기본은 '표시'. 아래 강한 증거 중 하나가 있을 때만 제외한다(비어 있어야 정상인 위반/결과 목록).
_VIOLATION_NAME_RE = re.compile(
    r"(?:^|_)(?:issues?|errors?|violations?|failures?|failed|offenders?|findings?|matches|hits"
    r"|results?|events?|requests_made|calls|out|extra|changed|leaks?|bad|missing|unexpected"
    r"|warnings?)(?:$|_)|^new_"
)
# 모집단(검사 대상 전체)을 만들어 내는 호출 — 이게 출처에 있으면 이름이 위반류여도 계속 표시한다.
_POPULATION_PRODUCERS = frozenset(
    {"glob", "rglob", "iterdir", "walk", "listdir", "scandir", "splitlines", "readlines",
     "read_text", "getmembers"}
)
_AUDIT_CALL_RE = re.compile(r"^_*(?:audit|check|validate|scan|find|run|verify)", re.IGNORECASE)
_REGEX_RESULT_FUNCS = frozenset({"finditer", "findall"})  # 정책: 정규식 결과는 위반 목록으로 본다
_MUTATORS = frozenset({"append", "extend", "add", "insert", "update"})
_VIEW_METHODS = frozenset({"items", "keys", "values", "copy"})


def _last_name(expr: ast.expr) -> str | None:
    """SRC 식의 대표 이름: Name.id / Attribute.attr / .get('k') 의 k / ['k'] 의 k."""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    if isinstance(expr, ast.Subscript):
        sl = expr.slice
        if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
            return sl.value
        return None
    if isinstance(expr, ast.Call):
        f = expr.func
        if (
            isinstance(f, ast.Attribute)
            and f.attr == "get"
            and expr.args
            and isinstance(expr.args[0], ast.Constant)
            and isinstance(expr.args[0].value, str)
        ):
            return expr.args[0].value
    return None


def _is_violation_name(name: str | None) -> bool:
    return bool(name) and _VIOLATION_NAME_RE.search(name.lower()) is not None


def _assignments(fn: ast.AST) -> dict[str, list]:
    """fn 안의 `name = <expr>` (단일 Name 타깃) -> [(lineno, value)] (줄 순)."""
    found: dict[str, list] = {}
    for st in ast.walk(fn):
        if (
            isinstance(st, ast.Assign)
            and len(st.targets) == 1
            and isinstance(st.targets[0], ast.Name)
        ):
            found.setdefault(st.targets[0].id, []).append((st.lineno, st.value))
    for v in found.values():
        v.sort(key=lambda t: t[0])
    return found


def _is_empty_container_init(v: ast.expr) -> bool:
    if isinstance(v, (ast.List, ast.Set, ast.Dict, ast.Tuple)):
        return not (v.elts if not isinstance(v, ast.Dict) else v.keys)
    return (
        isinstance(v, ast.Call)
        and isinstance(v.func, ast.Name)
        and v.func.id in {"list", "set", "dict"}
        and not v.args
    )


def _is_accumulated(fn: ast.AST, name: str, assigns: dict[str, list]) -> bool:
    """name = [] (또는 list()) 후 같은 함수에서 append/extend/+= 등으로 채워지는가."""
    if not any(_is_empty_container_init(v) for _, v in assigns.get(name, [])):
        return False
    for n in ast.walk(fn):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in _MUTATORS
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == name
        ):
            return True
        if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and n.target.id == name:
            return True
    return False


def _is_nonempty_literal(v: ast.expr) -> bool:
    while (
        isinstance(v, ast.Call)
        and isinstance(v.func, ast.Attribute)
        and v.func.attr in _VIEW_METHODS
        and not v.args
    ):
        v = v.func.value
    if isinstance(v, (ast.List, ast.Set, ast.Tuple)):
        return bool(v.elts)
    if isinstance(v, ast.Dict):
        return bool(v.keys)
    return False


def _top_call_name(v: ast.expr) -> str | None:
    if isinstance(v, ast.Call):
        return call_name(v).rsplit(".", 1)[-1] or None
    return None


def _chain_root(expr: ast.expr) -> ast.expr:
    """a.b(c)[d].e() 같은 호출/속성/첨자 사슬의 맨 앞 식(컴프리헨션은 첫 iter)."""
    while True:
        if isinstance(expr, ast.Call):
            expr = expr.func
        elif isinstance(expr, (ast.Attribute, ast.Subscript)):
            expr = expr.value
        elif isinstance(expr, COMPREHENSIONS) and expr.generators:
            expr = expr.generators[0].iter
        else:
            return expr


def _reads_process_output(call: ast.Call) -> bool:
    """x.stdout.splitlines() 처럼 하위 프로세스 출력(git diff 등 결과)을 쪼개는 호출인가."""
    for n in ast.walk(call.func):
        if isinstance(n, ast.Attribute) and n.attr in {"stdout", "stderr"}:
            return True
    return False


def _has_population_producer(v: ast.expr, assigns: dict[str, list], depth: int = 3) -> bool:
    """v 의 출처에 glob/rglob/splitlines/... 모집단 생산자, 또는 사슬 맨 앞이 UPPER_CASE 상수인가.
    정규식 결과(finditer/findall)의 최상위 호출은 위반 목록으로 보고, json.load(s) 로 파싱된
    데이터는 원문 읽기(read_text)와 분리해 안쪽 인자는 보지 않는다."""
    if _top_call_name(v) in _REGEX_RESULT_FUNCS:
        return False
    root = _chain_root(v)
    if isinstance(root, ast.Name) and root.id.isupper() and len(root.id) > 1:
        return True
    stack = [v]
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Call):
            nm = call_name(n).rsplit(".", 1)[-1]
            if nm in _POPULATION_PRODUCERS and not _reads_process_output(n):
                return True
            if nm in {"load", "loads"}:
                continue
        if isinstance(n, ast.Name) and depth > 0 and n.id in assigns and n.id.islower():
            inner = assigns[n.id][-1][1]
            if inner is not v and _has_population_producer(inner, assigns, depth - 1):
                return True
        stack.extend(ast.iter_child_nodes(n))
    return False


def _source_is_expected_violation_list(fn: ast.AST, source: ast.expr) -> bool:
    """SRC 가 '비어 있는 것이 정상'인 위반/결과 목록이라는 강한 증거가 있으면 True(= 제외)."""
    assigns = _assignments(fn)
    name = source.id if isinstance(source, ast.Name) else None
    value = assigns[name][-1][1] if name in assigns else source
    # ① 누적 목록 ② 비어 있지 않은 리터럴
    if name and _is_accumulated(fn, name, assigns):
        return True
    if _is_nonempty_literal(value):
        return True
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Sub):  # 차집합(신규 항목)
        return True
    # 모집단 생산자가 출처에 있으면 이름과 무관하게 표시 유지
    if _has_population_producer(value, assigns):
        return False
    # ③ 감사/검증 함수 호출 결과
    top = _top_call_name(value)
    if top and (_AUDIT_CALL_RE.match(top) or top in _REGEX_RESULT_FUNCS):
        return True
    # ④ 위반/결과류 이름 또는 키
    return _is_violation_name(_last_name(source)) or _is_violation_name(_last_name(value))


def _vacuous_assert_source(node: ast.Assert, bound: dict[str, ast.expr]) -> ast.expr | None:
    """node 가 컴프리헨션 기반 '비어있어야 정상' assert 면, 그 컴프리헨션의 필터링 대상
    원본 식을 반환한다(대상이 아니면 None)."""
    target = _assert_falsy_target(node.test)
    if target is None:
        return None
    comp = target if isinstance(target, COMPREHENSIONS) else bound.get(getattr(target, "id", None))
    if comp is None:
        return None
    return _comprehension_source(comp)


def check_vacuous_collection_assert(tree: ast.AST, rel: str) -> list:
    """ERR-08: test_* 함수에서 파생 컬렉션에 대한 '비어있어야 정상' assert 가, 그 파생 소스
    자체가 비어있지 않다는 별도 sanity assert 없이 쓰이는지.

    `bad = [a for a in SRC if COND]; assert not bad` 패턴은 SRC 가 계속 비어있어도 bad 가
    항상 빈 리스트가 돼 assert 가 영원히 통과한다 — 검사가 실제로는 아무 것도 검증하지 않는
    채로 "통과"만 계속 찍는다. 대상은 test_* 함수 안의 assert 뿐(일반 코드의 방어적 assert는
    범위 밖). AST 로는 파생 소스가 정확히 같은 식(ast.dump 문자열 비교)일 때만 sanity assert
    를 찾아내므로 오탐·미탐이 있을 수 있다(ERR-07 과 같은 종류의 한계).

    원본 종류 구분(기본은 '표시', 강한 증거가 있을 때만 제외): ① x=[] 후 append/extend/+= 로
    채워지는 누적 목록 ② 비어 있지 않은 리터럴(또는 a - b 차집합) ③ audit/check/validate/scan/find/run 호출 결과
    ④ issues/errors/findings/matches/... 같은 위반·결과류 이름 또는 .get('issues') 같은 키 —
    단 glob/rglob/splitlines/read_text/UPPER_CASE 상수 같은 모집단 생산자가 출처에 있으면 이름과
    무관하게 계속 표시한다. 정규식 결과(finditer/findall)는 위반 목록으로 보고 제외한다(정책).
    이름·함수명 기반 근사라 위반류 이름의 모집단은 놓칠 수 있다.
    """
    hits = []
    for fn in ast.walk(tree):
        if not (isinstance(fn, DEFS) and fn.name.startswith("test_")):
            continue
        bound = _bound_comprehensions(fn)
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assert):
                continue
            source = _vacuous_assert_source(node, bound)
            if source is None or _sanity_assert_covers(fn, ast.dump(_unwrap_collection(source))):
                continue
            if _source_is_expected_violation_list(fn, source):
                continue
            hits.append(
                Hit(
                    "VACUOUS-COLLECTION-ASSERT",
                    rel,
                    node.lineno,
                    "이 assert 가 검사하는 파생 컬렉션의 원본이 비어있지 않다는 별도 확인이 없다 — "
                    "원본이 항상/우연히 비면 이 assert 는 비교대상 0건으로 계속 조용히(공허하게) "
                    "통과한다",
                )
            )
    return hits


FILE_CHECKS = [
    check_abs_path_literal,
    check_io_in_loop,
    check_list_membership,
    check_openpyxl_mode,
    check_cell_by_cell,
    check_com_cleanup,
    check_subprocess_text_no_encoding,
    check_hook_entrypoint_guarded,
    check_existence_only_gating,
    check_vacuous_collection_assert,
]


def run_file_checks(tree: ast.AST, rel: str) -> list:
    return [h for check in FILE_CHECKS for h in check(tree, rel)]


# ---------------------------------------------------------------- 프로젝트 단위 검사
def _statement_count(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    return sum(isinstance(n, ast.stmt) for stmt in fn.body for n in ast.walk(stmt))


class _RenameLocals(ast.NodeTransformer):
    """함수의 인자·지역 변수 이름을 등장 순서대로 v0, v1 ... 로 바꾼다 (이름만 다른 복사본을 같게 만든다)."""

    def __init__(self, local_names: set) -> None:
        self.local_names = local_names
        self.mapping: dict = {}

    def _new(self, name: str) -> str:
        return self.mapping.setdefault(name, f"v{len(self.mapping)}")

    def visit_Name(self, node: ast.Name) -> ast.Name:
        if node.id in self.local_names:
            node.id = self._new(node.id)
        return node

    def visit_arg(self, node: ast.arg) -> ast.arg:
        if node.arg in self.local_names:
            node.arg = self._new(node.arg)
        node.annotation = None
        return node


def _local_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set:
    args = fn.args
    names = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
    names |= {a.arg for a in (args.vararg, args.kwarg) if a}
    names |= {
        n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
    }
    return names


def _body_key(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """본문의 정규화된 해시: docstring 제거, 인자·지역 변수 이름 무시."""
    clone = copy.deepcopy(fn)
    doc = (
        clone.body
        and isinstance(clone.body[0], ast.Expr)
        and isinstance(getattr(clone.body[0], "value", None), ast.Constant)
    )
    body = clone.body[1:] if doc else clone.body
    _RenameLocals(_local_names(clone)).visit(clone)
    dump = ast.dump(ast.Module(body=body, type_ignores=[]), annotate_fields=False)
    return hashlib.sha256(dump.encode("utf-8")).hexdigest()


def check_func_body_dup(parsed: dict) -> list:
    """DUP-02: 본문이 같은 함수(5문장 이상)가 여러 곳에 있음. docstring·지역 변수 이름은 무시한다."""
    groups: dict = {}
    for rel, tree in parsed.items():
        for fn in (n for n in ast.walk(tree) if isinstance(n, DEFS)):
            if fn.name.startswith(("test_", "__")) or _statement_count(fn) < MIN_DUP_STATEMENTS:
                continue
            groups.setdefault(_body_key(fn), []).append((rel, fn.lineno, fn.name))
    hits = []
    for members in groups.values():
        if len(members) < 2:
            continue
        others = ", ".join(f"{f}:{ln} {name}" for f, ln, name in members[1:4])
        first = members[0]
        hits.append(
            Hit(
                "FUNC-BODY-DUP",
                first[0],
                first[1],
                f"함수 {first[2]} 와 본문이 같은 함수 {len(members) - 1}개: {others}",
            )
        )
    return hits


def _looks_like_copy(rel: str, files: set) -> bool:
    """백업·복사본 이름이면 True. `_v2`·`_final` 은 같은 폴더에 원본(접미어 없는 파일)이 있을 때만."""
    path = Path(rel)
    if COPY_NAME_RE.search(path.stem):
        return True
    base = VERSION_SUFFIX_RE.sub("", path.stem)
    if base == path.stem:
        return False
    return path.with_name(base + path.suffix).as_posix() in files


def check_copy_filenames(files: list) -> list:
    """DUP-03: 백업·복사본 이름의 파일. `_v2`·`_final` 은 버전 관리되는 정상 파일일 수 있어 참고 등급."""
    universe = set(files)
    hits = []
    for rel in files:
        if not _looks_like_copy(rel, universe):
            continue
        versioned = not COPY_NAME_RE.search(Path(rel).stem)
        hits.append(
            Hit(
                "COPY-FILENAME",
                rel,
                1,
                "버전 접미어(_v2 등)가 붙은 파일 — 같은 폴더에 원본이 있음. 복사본이면 git 으로 관리하고 지운다"
                if versioned
                else "백업·복사본으로 보이는 파일 이름 — 이력은 git 으로 관리하고 파일을 지운다",
                severity="review" if versioned else "",
            )
        )
    return hits


def check_root_tests(files: list) -> list:
    """STD-11: 프로젝트 루트에 흩어진 test_*.py (테스트는 tests/ 아래)."""
    return [
        Hit("ROOT-TEST-SCRIPT", rel, 1, "루트의 test_*.py — tests/ 아래 pytest 로 옮긴다")
        for rel in files
        if "/" not in rel and Path(rel).name.startswith("test")
    ]


def _pytest_section(pyproject: dict) -> dict:
    tool = pyproject.get("tool", {})
    section = tool.get("pytest", {})
    ini = section.get("ini_options", section) if isinstance(section, dict) else {}
    return ini if isinstance(ini, dict) else {}


def check_pytest_settings(root: Path) -> list:
    """ERR-05: tests/ 가 있는 프로젝트의 pytest 설정에 경고 오류화·마커 엄격·타임아웃이 있는가."""
    if not (root / "tests").is_dir():
        return []
    pyproject_file = root / "pyproject.toml"
    ini: dict = {}
    if pyproject_file.is_file():
        try:
            with pyproject_file.open("rb") as fh:
                ini = _pytest_section(tomllib.load(fh))
        except (OSError, tomllib.TOMLDecodeError):
            ini = {}
    addopts = ini.get("addopts", [])
    text = " ".join(addopts) if isinstance(addopts, list) else str(addopts)
    filters = ini.get("filterwarnings", [])
    missing = []
    if "error" not in (filters if isinstance(filters, list) else [filters]):
        missing.append('filterwarnings = ["error", …] (경고를 오류로)')
    if "--strict-markers" not in text:
        missing.append("--strict-markers (미등록 마커 금지)")
    if "timeout" not in ini and "--timeout" not in text:
        missing.append("timeout (pytest-timeout: 무한 대기 방지)")
    if not missing:
        return []
    return [
        Hit(
            "PYTEST-SETTINGS",
            "pyproject.toml",
            1,
            "pytest 설정에 없음: " + ", ".join(missing),
        )
    ]


# ---------------------------------------------------------------- 비밀값 (SEC-03/04)
# 정규식은 gitleaks(github.com/gitleaks/gitleaks, config/gitleaks.toml)와
# detect-secrets(github.com/Yelp/detect-secrets, plugins/keyword.py)의 공개 규칙을
# 2026-09-27 에 직접 열어 확인해 옮겼다. Claude Code hook(py_secret_guard.py)과 출처는 같지만
# 이 도구는 다른 PC 에 단독으로 배포되는 별도 패키지라 규칙을 자체로 갖는다.
SECRET_PATTERNS = {
    "aws_access_key": re.compile(r"\b(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16}\b"),
    "github_token": re.compile(r"\bgh[ospu]_[0-9A-Za-z]{36}\b"),
    "slack_token": re.compile(r"\bxoxb-[0-9]{10,13}-[0-9]{10,13}[A-Za-z0-9-]*\b"),
    "jwt": re.compile(
        r"\bey[A-Za-z0-9_-]{17,}\.ey[A-Za-z0-9_/\\-]{17,}\.[A-Za-z0-9_/\\-]{10,}={0,2}\b"
    ),
    "anthropic_key": re.compile(r"\bsk-ant-api03-[A-Za-z0-9_-]{93}AA\b"),
    "openai_key": re.compile(
        r"\bsk-(?:proj|svcacct|admin)-(?:[\w-]{74}|[\w-]{58})T3BlbkFJ(?:[\w-]{74}|[\w-]{58})\b"
    ),
    "private_key_block": re.compile(r"(?i)-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----"),
}
SECRET_CARRYING_GLOBS = ("*.pem", "*.key", "*.secret", "*.secrets", "secrets.json")
ENV_NAME_RE = re.compile(r"^\.env(\..+)?$")
SKIP_TREE_KEYWORDS = ("venv", "__pycache__", "node_modules", ".git", "site-packages")


def check_secret_scan(root: Path, files: list) -> list:
    """SEC-03: 이미 저장소에 남아 있는 실제 서비스 키 형식·개인키 블록. 값은 담지 않는다."""
    hits = []
    for rel in files:
        try:
            text = read_text(root / rel)
        except OSError:
            continue
        for name, pattern in SECRET_PATTERNS.items():
            m = pattern.search(text)
            if m:
                line = text.count("\n", 0, m.start()) + 1
                hits.append(
                    Hit(
                        "SECRET-SCAN",
                        rel,
                        line,
                        f"비밀값으로 보이는 값이 있습니다(종류: {name}). 실제 값은 여기 담지 않습니다. "
                        "환경변수나 .env(.gitignore 등재)로 옮기세요.",
                    )
                )
    return hits


def _gitignore_patterns(root: Path) -> list:
    f = root / ".gitignore"
    try:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [ln.strip().lstrip("/") for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _is_secret_carrying_name(name: str) -> bool:
    return bool(ENV_NAME_RE.match(name)) or any(
        fnmatch.fnmatch(name, pat) for pat in SECRET_CARRYING_GLOBS
    )


def check_gitignore_secrets(root: Path) -> list:
    """SEC-04: .env·개인키·비밀값 파일이 실제로 있는데 .gitignore 에 없는가(내용은 열지 않는다).

    os.walk(onerror=...) 로 스캔한다 — rglob()은 스캔 도중 다른 프로세스가
    폴더를 지우면(예: mypy 가 .mypy_cache 를 정리) FileNotFoundError 로 전체가
    죽는다(2026-09-29 실제 CI 크래시로 확인). onerror 콜백은 그 폴더만 건너뛰고
    나머지 형제 디렉터리는 계속 스캔한다.
    """
    patterns = _gitignore_patterns(root)
    hits = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _exc: None):
        dir_parts_lower = [p.lower() for p in Path(dirpath).parts]
        if any(k in part for part in dir_parts_lower for k in SKIP_TREE_KEYWORDS):
            dirnames[:] = []
            continue
        for filename in filenames:
            path = Path(dirpath) / filename
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue  # 나열된 뒤 파일이 사라진 경합 — 그 항목만 건너뛴다
            if not _is_secret_carrying_name(path.name):
                continue
            covered = any(
                pat in (path.name, ".env", ".env*") or fnmatch.fnmatch(path.name, pat)
                for pat in patterns
            )
            if not covered:
                rel = path.relative_to(root).as_posix()
                hits.append(
                    Hit(
                        "GITIGNORE-SECRETS",
                        rel,
                        1,
                        f"{path.name} 이(가) .gitignore 에 없습니다. 실수로 커밋되지 않게 등재하세요.",
                    )
                )
    return hits


# ---------------------------------------------------------------- OPS 운영·관리
# 전부 "파일이 있는가"만 보는 존재 검사다(내용 검증 없음). 조항 심각도가 info 라
# Hit 에서 severity 를 지정하지 않는다(조항 기본값 그대로 review 로 보고됨).
CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
RELEASE_CONFIG_NAMES = (
    ".releaserc",
    ".releaserc.json",
    ".releaserc.yaml",
    ".releaserc.yml",
    ".releaserc.toml",
    "release.config.js",
    "release.config.cjs",
    "release.config.mjs",
)
MKDOCS_CONFIG_NAMES = ("mkdocs.yml", "mkdocs.yaml")
SONARQUBE_CONFIG_NAME = "sonar-project.properties"
SBOM_GLOBS = ("bom.json", "bom.xml", "sbom.json", "*.cdx.json")
SENTRY_DEPENDENCY_NAMES = {"sentry_sdk", "sentry-sdk"}


def _has_semantic_release_config(root: Path) -> bool:
    if any((root / name).is_file() for name in RELEASE_CONFIG_NAMES):
        return True
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return "semantic_release" in data.get("tool", {})


def check_release_config_present(root: Path) -> list:
    """OPS-01: 릴리스 관리(semantic-release 설정, CODEOWNERS) 미적용 여부."""
    hits = []
    if not any((root / rel).is_file() for rel in CODEOWNERS_LOCATIONS):
        hits.append(
            Hit(
                "RELEASE-CONFIG-PRESENT",
                ".",
                1,
                "CODEOWNERS 파일 없음(.github/, 루트, docs/ 어디에도 없음)",
            )
        )
    if not _has_semantic_release_config(root):
        hits.append(
            Hit(
                "RELEASE-CONFIG-PRESENT",
                ".",
                1,
                "semantic-release 설정 없음(.releaserc*/release.config.* 또는 "
                "pyproject.toml [tool.semantic_release])",
            )
        )
    return hits


def check_error_tracking_sdk_present(root: Path, declared: set) -> list:
    """OPS-03: 운영 오류 추적(Sentry SDK 연동) 미적용 여부."""
    if declared & {normalize(n) for n in SENTRY_DEPENDENCY_NAMES}:
        return []
    package_json = root / "package.json"
    if package_json.is_file():
        text = package_json.read_text(encoding="utf-8", errors="replace")
        if "@sentry/" in text:
            return []
    return [
        Hit(
            "ERROR-TRACKING-SDK-PRESENT",
            ".",
            1,
            "Sentry SDK 연동 없음(sentry-sdk 의존성 선언 또는 package.json 의 @sentry/*)",
        )
    ]


def check_mkdocs_config_present(root: Path) -> list:
    """OPS-04: 문서화(MkDocs) 설정 미적용 여부."""
    if any((root / name).is_file() for name in MKDOCS_CONFIG_NAMES):
        return []
    return [Hit("MKDOCS-CONFIG-PRESENT", ".", 1, "mkdocs.yml 없음")]


def _ci_workflow_mentions(root: Path, keywords: tuple) -> bool:
    """.github/workflows/*.yml(.yaml) 어딘가에 keywords 중 하나가(대소문자 무시) 있는가."""
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return False
    for f in (*workflows.glob("*.yml"), *workflows.glob("*.yaml")):
        text = f.read_text(encoding="utf-8", errors="replace").lower()
        if any(k in text for k in keywords):
            return True
    return False


def check_sonarqube_config_present(root: Path) -> list:
    """OPS-05: 기술부채 추적(SonarQube) 설정 미적용 여부."""
    if (root / SONARQUBE_CONFIG_NAME).is_file() or _ci_workflow_mentions(
        root, ("sonarsource", "sonarqube", "sonarcloud")
    ):
        return []
    return [
        Hit(
            "SONARQUBE-CONFIG-PRESENT",
            ".",
            1,
            "sonar-project.properties 없음, CI 워크플로에도 SonarQube 스텝 없음",
        )
    ]


ADR_DIR_CANDIDATES = ("doc/adr", "docs/adr")


def check_adr_dir_present(root: Path) -> list:
    """ADR-01: 설계 결정 기록(doc/adr 또는 docs/adr, .md 1개 이상) 미적용 여부."""
    for rel in ADR_DIR_CANDIDATES:
        d = root / rel
        if d.is_dir() and any(d.glob("*.md")):
            return []
    return [
        Hit(
            "ADR-DIR-PRESENT",
            ".",
            1,
            "doc/adr(또는 docs/adr)에 결정 기록(.md)이 없음",
        )
    ]


def check_db_migration_risk_config_present(root: Path) -> list:
    """DB-01: DB 마이그레이션 위험 린트(Squawk 등) CI 단계 미적용 여부."""
    if _ci_workflow_mentions(root, ("squawk",)):
        return []
    return [
        Hit(
            "DB-MIGRATION-RISK-CONFIG-PRESENT",
            ".",
            1,
            "CI 워크플로에 Squawk(마이그레이션 위험 린트) 단계 없음",
        )
    ]


def check_db_schema_drift_config_present(root: Path) -> list:
    """DB-02: 실제 DB 와 문서·마이그레이션 이력의 드리프트 확인(tbls/migra) 미적용 여부."""
    if _ci_workflow_mentions(root, ("tbls", "migra")):
        return []
    return [
        Hit(
            "DB-SCHEMA-DRIFT-CONFIG-PRESENT",
            ".",
            1,
            "CI/설정에 tbls(diff/lint) 또는 migra 실행 흔적 없음",
        )
    ]


def check_api_contract_test_config_present(root: Path, declared: set) -> list:
    """API-01: OpenAPI/GraphQL 계약 테스트(schemathesis) 미적용 여부."""
    if "schemathesis" in declared or _ci_workflow_mentions(root, ("schemathesis",)):
        return []
    return [
        Hit(
            "API-CONTRACT-TEST-CONFIG-PRESENT",
            ".",
            1,
            "schemathesis 계약 테스트 CI/의존성 흔적 없음",
        )
    ]


def _has_sbom(root: Path) -> bool:
    return any(list(root.glob(pat)) for pat in SBOM_GLOBS)


def check_sbom_present(root: Path) -> list:
    """OPS-06: 공급망 보안(SBOM/Trivy, 규제산업만 해당 시) 미적용 여부."""
    if _has_sbom(root):
        return []
    return [
        Hit(
            "SBOM-PRESENT",
            ".",
            1,
            "SBOM(CycloneDX bom.json/bom.xml 등) 없음 — 규제산업 등 해당 프로젝트만 적용 대상",
        )
    ]


DEPENDABOT_LOCATIONS = (".github/dependabot.yml", ".github/dependabot.yaml")
RUNBOOK_LOCATIONS = ("RUNBOOK.md", "docs/runbook.md", "docs/RUNBOOK.md")
PRIVACY_POLICY_LOCATIONS = ("PRIVACY.md", "docs/privacy.md", "docs/PRIVACY.md")
DEPLOYMENT_DOC_LOCATIONS = ("DEPLOYMENT.md", "docs/deployment.md", "docs/DEPLOYMENT.md")
STRUCTLOG_DEPENDENCY_NAMES = {"structlog"}
APM_SDK_DEPENDENCY_NAMES = {"opentelemetry-distro", "opentelemetry-api"}


def check_dependabot_config_present(root: Path) -> list:
    """OPS-07: 의존성 자동 업데이트(Dependabot) 미적용 여부."""
    if any((root / rel).is_file() for rel in DEPENDABOT_LOCATIONS):
        return []
    return [Hit("DEPENDABOT-CONFIG-PRESENT", ".", 1, ".github/dependabot.yml 없음")]


def check_runbook_present(root: Path) -> list:
    """OPS-08: 사고 대응 문서화(런북·포스트모템 템플릿) 미적용 여부."""
    if any((root / rel).is_file() for rel in RUNBOOK_LOCATIONS):
        return []
    return [Hit("RUNBOOK-PRESENT", ".", 1, "RUNBOOK.md(또는 docs/runbook.md) 없음")]


def check_structured_logging_present(declared: set) -> list:
    """OPS-10: 구조화된 로깅(structlog) 미적용 여부. 파일이 아니라 선언된 의존성만 본다."""
    if declared & {normalize(n) for n in STRUCTLOG_DEPENDENCY_NAMES}:
        return []
    return [Hit("STRUCTURED-LOGGING-PRESENT", ".", 1, "structlog 의존성 선언 없음")]


def check_privacy_policy_present(root: Path) -> list:
    """OPS-11: 개인정보 처리방침(개인정보 다루는 프로젝트만 해당 시) 미적용 여부."""
    if any((root / rel).is_file() for rel in PRIVACY_POLICY_LOCATIONS):
        return []
    return [
        Hit(
            "PRIVACY-POLICY-PRESENT",
            ".",
            1,
            "PRIVACY.md(또는 docs/privacy.md) 없음 — 개인정보 다루는 프로젝트만 해당",
        )
    ]


def check_apm_sdk_present(declared: set) -> list:
    """OPS-13: 성능 모니터링(APM, OpenTelemetry) 미적용 여부. 파일이 아니라 선언된 의존성만 본다."""
    if declared & {normalize(n) for n in APM_SDK_DEPENDENCY_NAMES}:
        return []
    return [
        Hit("APM-SDK-PRESENT", ".", 1, "OpenTelemetry(opentelemetry-distro/-api) 의존성 선언 없음")
    ]


def check_deployment_doc_present(root: Path) -> list:
    """OPS-14: 배포 전략·롤백 절차 문서화 미적용 여부."""
    if any((root / rel).is_file() for rel in DEPLOYMENT_DOC_LOCATIONS):
        return []
    return [Hit("DEPLOYMENT-DOC-PRESENT", ".", 1, "DEPLOYMENT.md(또는 docs/deployment.md) 없음")]


COMMUNITY_HEALTH_FILES = ("LICENSE", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md")


def check_community_files_present(root: Path) -> list:
    """OPS-16: GitHub 커뮤니티 표준 파일(LICENSE 등) 미적용 여부. 4개 중 없는 것마다 하나씩 보고."""
    return [
        Hit("COMMUNITY-FILES-PRESENT", ".", 1, f"{name} 없음")
        for name in COMMUNITY_HEALTH_FILES
        if not (root / name).is_file()
    ]


def check_claude_md(root: Path, limit: int) -> list:
    """DOC-01: CLAUDE.md 가 권고 줄 수를 넘음."""
    hits = []
    for name in ("CLAUDE.md", ".claude/CLAUDE.md"):
        f = root / name
        if f.is_file():
            n = len(f.read_text(encoding="utf-8", errors="replace").splitlines())
            if n >= limit:
                hits.append(
                    Hit(
                        "CLAUDE-MD-LINES",
                        name,
                        1,
                        f"CLAUDE.md {n}줄 — {limit}줄 미만으로, 세부는 docs/ 로 분리",
                    )
                )
    return hits


# ---------------------------------------------------------------- import 검사
def guarded_imports(tree: ast.AST) -> list:
    """(모듈 최상위 이름, 줄). try/except ImportError 와 TYPE_CHECKING 안의 선택 import 는 뺀다."""
    found: list = []

    def visit(node: ast.AST, guarded: bool) -> None:
        if isinstance(node, ast.Import) and not guarded:
            found.extend((a.name.split(".")[0], node.lineno) for a in node.names)
        elif isinstance(node, ast.ImportFrom) and not guarded and node.level == 0 and node.module:
            found.append((node.module.split(".")[0], node.lineno))
        for field, value in ast.iter_fields(node):
            for child in value if isinstance(value, list) else [value]:
                if isinstance(child, ast.AST):
                    visit(child, guarded or _guards(node, field))

    visit(tree, False)
    return found


def _guards(node: ast.AST, field: str) -> bool:
    if isinstance(node, ast.Try) and field == "body":
        names = {getattr(h.type, "id", "") for h in node.handlers} | {
            getattr(e, "id", "")
            for h in node.handlers
            if isinstance(h.type, ast.Tuple)
            for e in h.type.elts
        }
        return bool(names & {"ImportError", "ModuleNotFoundError"})
    if isinstance(node, ast.If):
        dump = ast.dump(node.test)
        if "version_info" in dump or "platform" in dump:  # 파이썬 버전·OS 에 따라 갈리는 import
            return field in {"body", "orelse"}
        return field == "body" and "TYPE_CHECKING" in dump
    return False


def is_stdlib(top: str) -> bool:
    names = getattr(sys, "stdlib_module_names", None)
    if names is not None:
        return top in names
    spec = _find_spec(top)
    return bool(
        spec and spec.origin and str(spec.origin).startswith(sysconfig.get_paths()["stdlib"])
    )


def _find_spec(top: str):
    try:
        return importlib.util.find_spec(top)
    except (ImportError, ValueError, AttributeError):
        return None


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def declared_dependencies(pyproject: dict) -> set:
    """pyproject.toml 에 선언된 의존성 이름(정규화). dependencies 와 optional-dependencies 전체."""
    project = pyproject.get("project", {})
    specs = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        specs.extend(group)
    names = (re.match(r"[A-Za-z0-9_.-]+", str(spec)) for spec in specs)
    return {normalize(m.group(0)) for m in names if m}


def declared_requirements(root: Path) -> set:
    """requirements*.txt 에 선언된 의존성 이름(정규화). 주석·옵션(-r, -e)·URL 줄은 건너뛴다."""
    names: set = set()
    for f in sorted(root.glob("requirements*.txt")):
        for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"[A-Za-z0-9][A-Za-z0-9_.-]*", raw.strip())
            if m and not raw.strip().startswith(("-", "#")):
                names.add(normalize(m.group(0)))
    return names


def _not_found_hit(rel: str, line: int, top: str, declared: bool, known: bool = False) -> Hit:
    if known and not declared:
        return Hit(
            "IMPORT-RESOLVE",
            rel,
            line,
            f"알려진 도구 '{top}'(등록부) 이 이 파이썬 환경에 설치되지 않음 — 대상 프로젝트의 환경에서 실행해야 정확히 검사됨",
            severity="review",
        )
    if declared:
        return Hit(
            "IMPORT-RESOLVE",
            rel,
            line,
            f"선언된 의존성 '{top}' 이 이 파이썬 환경에 설치되지 않음 — 프로젝트 venv 에서 실행해야 정확히 검사됨",
            severity="review",
        )
    return Hit(
        "IMPORT-RESOLVE",
        rel,
        line,
        f"'{top}' 모듈이 없음 — 설치되지 않았거나 존재하지 않는 이름(환각 import). "
        "감사 도구가 설치된 파이썬 환경 기준",
    )


def check_imports(
    parsed: dict, local: set, registry_imports: set, declared: set | None = None
) -> tuple:
    """(ERR-01 Hit 목록, DOC-02 등록부 누락 Hit 목록). 검사는 감사 도구가 실행되는 파이썬 환경 기준.

    declared: 프로젝트가 선언한 의존성(정규화된 이름). import 이름과 같으면 '환경 미설치'로 등급을 낮춘다.
    """
    declared = declared or set()
    missing: list = []
    gaps: list = []
    seen_gap: set = set()
    for rel, tree in parsed.items():
        for top, line in guarded_imports(tree):
            if top in local or is_stdlib(top):
                continue
            if _find_spec(top) is None:
                missing.append(
                    _not_found_hit(
                        rel, line, top, normalize(top) in declared, top in registry_imports
                    )
                )
            elif top not in registry_imports and top not in seen_gap:
                seen_gap.add(top)
                gaps.append(
                    Hit(
                        "REGISTRY-GAP",
                        rel,
                        line,
                        f"등록부에 없는 외부 도구 '{top}' — 공식 개발자 센터를 확인하고 docs_registry.toml 에 추가",
                    )
                )
    return missing, gaps

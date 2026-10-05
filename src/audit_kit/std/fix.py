"""개발 기준서(STD-xx 등) 자동 수정: `audit-kit std --fix | --apply | --undo`.

지금은 EFF-02(반복문 안 list 멤버십 검사)만 자동 수정한다. `structfix.py`를 그대로 본떠서
`Editor`/`Workspace`/`import_failures`/`undo`(전부 `arch.fix`에서 재사용)만 갖다 쓰고, 그 파일에
바로 결합된 `Ctx`/`Verifier`/`FixAction`은 새로 만들지 않는다(그 셋은 ArchSpec·순환 임포트 전용이라
이 도메인엔 안 맞음).

안전 가드(자동 수정은 탐지보다 더 보수적으로, 2026-09-28 사용자 요청으로 강화):
  1. 후보 이름이 그 스코프 안에서 다시 대입/삭제/재바인딩되지 않아야 한다.
  2. `list`로만 힌트된 이름이 다른 함수 호출의 인자로 그대로 넘어가면 자동 수정하지 않는다 — 그 호출이
     안에서 리스트를 변형하는지 정적으로 증명할 수 없기 때문(파이썬은 동적 언어라 일반적으로 결정
     불가능한 문제, PyFlow/CodeQL 같은 전문 도구도 이 지점은 사람 확인에 맡김). 다만 `Sequence`/
     `tuple`/`frozenset`처럼 애초에 변형 메서드가 없는 타입으로 힌트돼 있으면 mypy가 이미 변형을
     막아주므로 이 가드를 건너뛴다(2026-09-28, 사용자와 상의해 결정).
  3. `f"{name}_set"`이 이미 그 스코프에 있는 이름과 겹치면 건너뛴다.
가드에 걸리면 고치지 않고 `failed`에 사유를 남긴다 — 애매하면 고치지 않고 보고만 한다는 이 프로젝트의
원칙(CLAUDE.md 5장)과 같다.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from audit_kit.arch.edit import Editor
from audit_kit.arch.fix import Workspace, import_failures
from audit_kit.config import AuditConfig
from audit_kit.scope import build_project_graph, get_scope
from audit_kit.std import checks
from audit_kit.std.run import project_files

# 변형 메서드가 없는(또는 tuple 처럼 애초에 불변인) 타입 힌트 — mypy 가 이미 변형을 막아주므로
# "다른 함수에 인자로 전달" 가드를 건너뛰어도 안전하다(2026-09-28, IT 생태계 조사 후 사용자와
# 상의해 결정: list -> Sequence/tuple 로 좁히는 게 정적 분석으로 완전히 증명 못 하는 문제를 애초에
# 없애는 정공법). Collection/Iterable 처럼 in 검사 성능이 애매한(이미 set 일 수도 있는) 타입은
# 넣지 않는다 — 지금 탐지(checks._is_list_annotation)는 list 만 후보로 삼아 이 분기가 실제로는
# 아직 안 걸리지만(파라미터가 Sequence/tuple 이면 애초에 EFF-02 후보가 안 됨), 탐지 범위가 나중에
# 넓어져도 바로 맞물리게 미리 정확히 만들어 둔다.
IMMUTABLE_SHAPED = {"Sequence", "tuple", "frozenset"}

# 인자를 바꾸지 않는다고 공식 문서로 보장된 내장 함수만(자세한 근거는 _passed_as_call_arg 참고).
SAFE_READONLY_BUILTINS = {
    "len", "print", "str", "repr", "sorted", "list", "tuple", "set", "frozenset",
    "min", "max", "sum", "any", "all", "iter", "enumerate", "reversed", "bool", "hash",
}  # fmt: skip


@dataclass
class StdFixResult:
    workspace: Workspace
    applied: list = field(default_factory=list)  # (rule, 설명)
    failed: list = field(default_factory=list)  # (rule, 설명, 사유)
    before: int = 0
    after: int = 0


def _is_immutable_shaped(ann) -> bool:
    """`Sequence`/`tuple`/`frozenset` 류(변형 메서드 없음, 자세한 근거는 모듈 docstring)인지."""
    if isinstance(ann, ast.Name):
        return ann.id in IMMUTABLE_SHAPED
    return (
        isinstance(ann, ast.Subscript)
        and isinstance(ann.value, ast.Name)
        and ann.value.id in IMMUTABLE_SHAPED
    )


def _own_nodes_of_scope(scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef):
    """scope(Module 또는 FunctionDef) 본문 전체의 노드(중첩 함수·클래스·람다 안쪽은 제외).
    `checks.own_nodes`는 최상위 문장 단위로 호출해야 한다 — scope 자체(FunctionDef 등)에 바로
    쓰면 그 노드가 NESTED 타입이라 빈 채로 반환된다(2026-09-28 실측으로 확인한 버그, checks.py의
    기존 호출 패턴 `own_nodes(stmt) for stmt in _scope_body(scope)`을 그대로 따른다)."""
    return (n for stmt in checks._scope_body(scope) for n in checks.own_nodes(stmt))


def _rebound_elsewhere(
    scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef, name: str, exclude: set
) -> bool:
    """scope 안 어디선가(exclude 에 있는 노드 제외) name 이 다시 대입·삭제·재바인딩되는지."""
    for n in _own_nodes_of_scope(scope):
        if id(n) in exclude:
            continue
        if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in n.targets
        ):
            return True
        if (
            isinstance(n, (ast.AugAssign, ast.AnnAssign))
            and isinstance(n.target, ast.Name)
            and n.target.id == name
        ):
            return True
        if isinstance(n, ast.Delete) and any(
            isinstance(t, ast.Name) and t.id == name for t in n.targets
        ):
            return True
        if (
            isinstance(n, (ast.For, ast.AsyncFor))
            and isinstance(n.target, ast.Name)
            and n.target.id == name
        ):
            return True
    return False


def _passed_as_call_arg(
    scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef, name: str
) -> bool:
    """scope 안 어디선가 name 이 (변형 안 하는 게 확실한 내장 함수가 아닌) 함수 호출의 인자로
    그대로 넘어가는지(자세한 근거는 모듈 docstring). `len(x)`/`print(x)`처럼 인자를 바꾸지 않는다고
    공식 문서로 보장된 극히 일부 내장 함수만 예외로 둔다 — 그 밖의 호출(사용자 정의 함수 포함)은
    안에서 뭘 하는지 알 수 없으므로 전부 보수적으로 취급한다."""
    for n in _own_nodes_of_scope(scope):
        if not isinstance(n, ast.Call):
            continue
        if isinstance(n.func, ast.Name) and n.func.id in SAFE_READONLY_BUILTINS:
            continue
        if any(isinstance(a, ast.Name) and a.id == name for a in n.args):
            return True
        if any(isinstance(kw.value, ast.Name) and kw.value.id == name for kw in n.keywords):
            return True
    return False


def _safety_reason(cand, param_ann) -> str | None:
    """자동 수정하면 안 되는 이유(문자열) 또는 안전하면 None. 매개변수가 Sequence/tuple/frozenset
    류로 타입힌트돼 있으면(`param_ann`) "다른 함수에 인자로 전달" 가드를 건너뛴다 — mypy 가 이미
    변형을 막아주므로(모듈 docstring 참고). 지역 대입은 `param_ann=None`이라 이 예외를 못 받고
    그대로 검사된다(타입힌트가 없어 판단할 근거가 없으므로)."""
    anchor_ids = {id(cand.anchor)} if cand.anchor is not None else set()
    if _rebound_elsewhere(cand.scope, cand.name, anchor_ids):
        return f"'{cand.name}' 이 스코프 안에서 다시 대입/삭제돼 자동 수정 불가"
    if not _is_immutable_shaped(param_ann) and _passed_as_call_arg(cand.scope, cand.name):
        return (
            f"'{cand.name}' 이 다른 함수 호출에 인자로 그대로 넘어가 안전하게 자동 수정 불가"
            " — 수동 확인 필요(또는 Sequence/tuple 로 타입힌트를 좁히면 자동 수정 가능)"
        )
    return None


def _param_annotation(cand):
    if cand.source != "param" or not isinstance(
        cand.scope, (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        return None
    args = cand.scope.args
    all_args = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    return next((a.annotation for a in all_args if a.arg == cand.name), None)


def _apply_candidate(ed: Editor, text: str, cand) -> str | None:
    """`ed`에 파생 set 삽입 + 비교식 교체 반영. 실패 사유(str) 또는 성공하면 None."""
    set_name = f"{cand.name}_set"
    existing = {n.id for n in ast.walk(cand.scope) if isinstance(n, ast.Name)}
    if set_name in existing:
        return f"'{set_name}' 이름이 이미 쓰이고 있어 자동 수정 불가"
    for cmp_node in cand.compares:
        for comparator in cmp_node.comparators:
            if isinstance(comparator, ast.Name) and comparator.id == cand.name:
                ed.replace_node(comparator, set_name)
    line = f"{set_name} = frozenset({cand.name})\n"
    if cand.source == "assign":
        assert cand.anchor.end_lineno is not None  # 실제 소스를 파싱한 노드라 항상 있다
        ed.insert_after_line(cand.anchor.end_lineno, _line_indent(text, cand.anchor.lineno) + line)
    else:
        body = cand.scope.body
        k = (
            1
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            else 0
        )
        first = body[k] if len(body) > k else body[0]
        indent = _line_indent(text, first.lineno)
        ed.insert_after_line(first.lineno - 1, indent + line)
    return None


def _line_indent(text: str, lineno: int) -> str:
    line = text.splitlines()[lineno - 1]
    return line[: len(line) - len(line.lstrip())]


def _apply_file_candidates(
    ed: Editor, text: str, candidates: list, rel: str, res: StdFixResult
) -> bool:
    """이 파일의 후보마다 안전 가드 확인 후 반영. 하나라도 반영됐으면 True."""
    touched = False
    for cand in candidates:
        reason = _safety_reason(cand, _param_annotation(cand))
        if reason:
            res.failed.append(("EFF-02", f"{rel}:{cand.compares[0].lineno}", reason))
            continue
        err = _apply_candidate(ed, text, cand)
        if err:
            res.failed.append(("EFF-02", f"{rel}:{cand.compares[0].lineno}", err))
            continue
        touched = True
        res.applied.append(("EFF-02", f"{rel}: '{cand.name}' 멤버십 검사를 set 기반으로"))
    return touched


def _verify_and_write(
    ws: Workspace, rel: str, new_text: str, production: set, mods: list, res: StdFixResult
) -> None:
    """문법 확인 → (제품 코드면) 임포트 확인 → 통과한 것만 작업공간에 반영."""
    try:
        compile(new_text, rel, "exec")
    except SyntaxError as e:
        res.failed.append(("EFF-02", rel, f"문법 오류 {e.msg}"))
        return
    if rel not in production:
        ws.write(rel, new_text)
        return
    before_write = ws.read(rel)
    ws.write(rel, new_text)
    bad = import_failures(ws.tmp, mods) if mods else {}
    if bad:
        ws.write(rel, before_write)
        res.failed.append(("EFF-02", rel, f"임포트 실패: {next(iter(bad.values()))}"))


def fix_list_membership(ws: Workspace, cfg: AuditConfig, files: list, res: StdFixResult) -> None:
    production = set(get_scope(cfg).production)
    graph = build_project_graph(ws.cfg)  # ws.cfg 기준(작업공간 경로)이어야 rel 이 맞물림
    rel_to_mod = {ws.cfg.rel(f): m for m, f in graph.modules.items()}
    for rel in files:
        text = ws.read(rel)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        candidates = checks._list_membership_candidates(tree)
        if not candidates:
            continue
        ed = Editor(text)
        if not _apply_file_candidates(ed, text, candidates, rel, res):
            continue
        mod = rel_to_mod.get(rel)
        _verify_and_write(ws, rel, ed.apply(), production, [mod] if mod else [], res)


def _count_list_membership(read_fn, files: list) -> int:
    total = 0
    for rel in files:
        try:
            tree = ast.parse(read_fn(rel) or "")
        except SyntaxError:
            continue
        total += len(checks.check_list_membership(tree, rel))
    return total


def run_std_fix(cfg: AuditConfig) -> StdFixResult:
    files = project_files(cfg)
    ws = Workspace(cfg)
    res = StdFixResult(ws, before=_count_list_membership(ws.original, files))
    fix_list_membership(ws, cfg, files, res)
    res.after = _count_list_membership(ws.read, files)
    return res

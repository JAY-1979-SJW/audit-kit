"""AI 설계 리뷰(9단계)의 범위를 좁히기 위한 AST 휴리스틱.

여기서 나온 항목은 확정이 아니라 '후보'(REVIEW)다. /audit 스킬에서 Claude가 코드를 읽고
치명/개선/무시로 최종 판정한다.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from audit_kit.config import AuditConfig
from audit_kit.importgraph import iter_py_files
from audit_kit.models import REVIEW, Finding, ToolResult
from audit_kit.textio import read_text

SQL_RE = re.compile(
    r"\b(select\b.+\bfrom|insert\s+into|update\b.+\bset|delete\s+from|where\b|order\s+by|drop\s+table)\b",
    re.IGNORECASE | re.DOTALL,
)
ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "api_route", "route", "websocket"}
DB_METHODS = {"query", "add", "add_all", "commit", "execute", "delete", "flush", "scalars", "scalar"}


def _call_name(node: ast.AST):
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _walk_own(fn):
    """함수 본문 노드(중첩 함수·클래스·람다 내부 제외)."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        yield n
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(n))


def _is_sql_building(node: ast.AST) -> bool:
    """f-string / % / + / .format() 로 SQL 문자열을 조합하는지."""
    if isinstance(node, ast.JoinedStr):
        has_value = any(isinstance(v, ast.FormattedValue) for v in node.values)
        literal = "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
        return has_value and bool(SQL_RE.search(literal))
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        consts = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        non_const = any(isinstance(n, (ast.Name, ast.Attribute, ast.Call, ast.Subscript))
                        for n in (node.left, node.right))
        return non_const and bool(SQL_RE.search(" ".join(consts)))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        base = node.func.value
        return isinstance(base, ast.Constant) and isinstance(base.value, str) and bool(SQL_RE.search(base.value))
    return False


class _Visitor(ast.NodeVisitor):
    def __init__(self, cfg: AuditConfig, file: str, is_router: bool):
        self.cfg = cfg
        self.file = file
        self.is_router = is_router
        self.findings: list = []
        self.func_stack: list = []

    def add(self, category, rule, line, msg, evidence):
        self.findings.append(Finding(tool="heuristic", category=category, rule=rule, message=msg,
                                     file=self.file, line=line, severity=REVIEW, evidence=evidence))

    # ---- 함수 단위
    def visit_FunctionDef(self, node):
        self._check_function(node)
        self.func_stack.append(node)
        self.generic_visit(node)
        self.func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _check_function(self, fn):
        own = list(_walk_own(fn))
        is_generator = any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in own)
        calls = {_call_name(n) for n in own if isinstance(n, ast.Call)}
        with_exprs = {id(item.context_expr) for n in own if isinstance(n, (ast.With, ast.AsyncWith))
                      for item in n.items}
        created = [n for n in own
                   if isinstance(n, ast.Call) and _call_name(n) in self.cfg.session_factories
                   and id(n) not in with_exprs
                   and _call_name(n) not in ("sessionmaker", "async_sessionmaker")]
        if created and not is_generator:
            missing = [m for m in ("commit", "close") if m not in calls]
            detail = f" / 함수 안에 {', '.join(missing)}() 호출 없음" if missing else ""
            self.add("DB", "SESSION-IN-FUNC", created[0].lineno,
                     f"{fn.name}() 안에서 DB 세션을 직접 생성(with 블록/의존성 주입 아님){detail}",
                     "SQLAlchemy Session Basics — 세션 수명주기는 함수 밖(요청 단위 의존성 등)에서 관리, "
                     "트랜잭션은 명시적 commit/rollback 필요")

        if self.is_router and self._is_route(fn):
            stmts = sum(isinstance(n, ast.stmt) for n in own)
            loops = any(isinstance(n, (ast.For, ast.While, ast.AsyncFor)) for n in own)
            db_calls = sorted(c for c in calls if c in DB_METHODS)
            reasons = []
            if stmts > self.cfg.router_max_statements:
                reasons.append(f"문장 {stmts}개 > {self.cfg.router_max_statements}")
            if loops:
                reasons.append("반복문 포함")
            if db_calls:
                reasons.append("DB 직접 호출(" + ", ".join(db_calls) + ")")
            if reasons:
                self.add("구조", "ROUTER-LOGIC", fn.lineno,
                         f"라우터 {fn.name}()에 비즈니스 로직 의심: {'; '.join(reasons)} → services/ 계층 이동 검토",
                         "계층 분리 원칙 — 라우터는 입출력 변환만, 로직·DB 접근은 서비스/리포지토리 계층")

    def _is_route(self, fn) -> bool:
        for d in fn.decorator_list:
            target = d.func if isinstance(d, ast.Call) else d
            if isinstance(target, ast.Attribute) and target.attr in ROUTE_METHODS:
                return True
        return False

    # ---- SQL 문자열 조합
    def visit_Call(self, node):
        name = _call_name(node)
        if name in ("execute", "text", "exec_driver_sql", "read_sql", "read_sql_query", "executemany",
                    "raw", "executescript") and node.args and _is_sql_building(node.args[0]):
            self.add("보안", "SQL-STRING", node.lineno,
                     f"{name}()에 문자열 조합(f-string/+/%/format)으로 만든 SQL 전달",
                     "SQL 인젝션 — 바인드 파라미터(:name) 사용 원칙. bandit B608이 못 잡는 변형 포함")
        self.generic_visit(node)

    def visit_Assign(self, node):
        if _is_sql_building(node.value):
            self.add("보안", "SQL-STRING", node.lineno,
                     "문자열 조합으로 SQL 작성 후 변수에 저장 — 실행 지점에서 파라미터 바인딩 여부 확인 필요",
                     "SQL 인젝션 — 바인드 파라미터 사용 원칙")
        if not self.func_stack:
            for n in ast.walk(node.value):
                if isinstance(n, ast.Call) and _call_name(n) in self.cfg.session_factories \
                        and _call_name(n) not in ("sessionmaker", "async_sessionmaker", "scoped_session"):
                    self.add("DB", "GLOBAL-SESSION", node.lineno,
                             "모듈 전역에서 DB 세션 인스턴스 생성 — 요청 간 세션 공유 위험",
                             "SQLAlchemy — Session은 스레드/요청 간 공유 금지")
                    break
        self.generic_visit(node)


def _glob_re(pattern: str) -> re.Pattern:
    """`**/` = 0개 이상 디렉터리, `*` = 슬래시 제외 임의 문자, `?` = 한 글자."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def _matches(rel: str, globs: list) -> bool:
    return any(_glob_re(g).match(rel) for g in globs)


def run_heuristics(cfg: AuditConfig, only_files=None) -> ToolResult:
    out = []
    for pkg in cfg.package_paths():
        for f in iter_py_files(pkg):
            rel = cfg.rel(f)
            if only_files is not None and rel not in only_files:
                continue
            try:
                tree = ast.parse(read_text(f), filename=str(f))
            except SyntaxError:
                continue
            v = _Visitor(cfg, rel, _matches(rel, cfg.router_globs))
            v.visit(tree)
            out += v.findings
    return ToolResult("heuristic", "findings" if out else "ok", out)


def router_files(cfg: AuditConfig) -> list:
    return [cfg.rel(f) for pkg in cfg.package_paths() for f in iter_py_files(Path(pkg))
            if _matches(cfg.rel(f), cfg.router_globs)]

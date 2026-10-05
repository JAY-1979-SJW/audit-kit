"""audit-kit std 변형 패턴 시험: 같은 위반이 다른 모양일 때도 잡고, 비슷하지만 위반이 아닌 것은 잡지 않는다.

검증 중 발견한 결함(중첩 함수 오탐, 스코프 오탐, 별칭 import·모듈 최상위 COM·변수 이름만 다른 복사 미탐)의
회귀를 막는다. 코드 조각은 줄 단위 튜플로 적어 이스케이프 없이 읽을 수 있게 했다.
"""

from __future__ import annotations

import ast

import pytest
from audit_kit.std import checks as c

BS = chr(92)
Q3 = chr(34) * 3


def code(*rows: str) -> str:
    return "\n".join(rows) + "\n"


def run(fn, src: str) -> bool:
    return bool(fn(ast.parse(src), "m.py"))


IO, LST, XLS, CELL, COM, ABS = (
    c.check_io_in_loop,
    c.check_list_membership,
    c.check_openpyxl_mode,
    c.check_cell_by_cell,
    c.check_com_cleanup,
    c.check_abs_path_literal,
)

CASES = [
    # ---- EFF-03 같은 파일·DB 연결·COM 을 반복해 열기
    (IO, "고정 경로 open 반복", code("for i in range(3):", "    with open('fixed.txt') as f:", "        f.read()"), True),
    (IO, "고정 경로 load_workbook 반복", code("for r in rows:", "    wb = load_workbook(FIXED)"), True),
    (IO, "while 안 고정 경로 open", code("while True:", "    open('a.txt')"), True),
    (IO, "컴프리헨션 안 고정 경로 open", code("data = [open('a.txt').read() for _ in range(3)]"), True),
    (IO, "psycopg2.connect 반복", code("import psycopg2", "for x in xs:", "    psycopg2.connect(dsn)"), True),
    (IO, "import psycopg2 as pg 별칭", code("import psycopg2 as pg", "for x in xs:", "    pg.connect(dsn)"), True),
    (IO, "from sqlite3 import connect", code("from sqlite3 import connect", "for x in xs:", "    connect(path)"), True),
    (IO, "반복마다 Dispatch", code("for x in xs:", "    app = win32com.client.Dispatch('Excel.Application')"), True),
    (IO, "파일마다 다른 경로 open(정상)", code("for p in paths:", "    with open(p) as f:", "        f.read()"), False),
    (IO, "컴프리헨션에서 파일마다 open(정상)", code("data = [open(p).read() for p in paths]"), False),
    (IO, "바깥 반복 변수에 의존한 open(정상)", code("for d in dirs:", "    for n in names:", "        open(d + n)"), False),
    (IO, "체크포인트 쓰기 open('w') 는 정상", code("for i in range(3):", "    with open('out.json', 'w') as f:", "        f.write('x')"), False),
    (IO, "재시도 반복 (break 있음)", code("for i in range(3):", "    with open('a.txt') as f:", "        return f.read()"), False),
    (IO, "폴링 반복 (sleep 있음)", code("while True:", "    time.sleep(1)", "    open('result.json')"), False),
    (IO, "재시도 반복의 requests.get(정상)", code("for attempt in range(3):", "    requests.get(URL)"), False),
    (IO, "async 반복의 httpx.get(정상)", code("async def f():", "    async for x in gen():", "        httpx.get(x)"), False),
    (IO, "with open 밖, 반복은 안쪽", code("with open(p) as f:", "    for line in f:", "        pass"), False),
    (IO, "for 대상 iter 의 open", code("for line in open(p):", "    pass"), False),
    (IO, "반복 안 중첩 함수 정의 속 open", code("for x in xs:", "    def g():", "        return open('a')"), False),
    (IO, "door.open()", code("for x in xs:", "    door.open()"), False),
    # ---- EFF-02 반복 안 list 멤버십
    (LST, "list 리터럴", code("def f(xs):", "    allowed = ['a', 'b']", "    for x in xs:", "        if x in allowed:", "            pass"), True),
    (LST, "list() + not in", code("def f(xs):", "    banned = list()", "    for x in xs:", "        if x not in banned:", "            pass"), True),
    (LST, "컴프리헨션 조건", code("def f(xs):", "    bad = [1, 2]", "    return [x for x in xs if x in bad]"), True),
    (LST, "순서 유지 중복 제거(append)는 정당", code("def f(xs):", "    seen = []", "    for x in xs:", "        if x in seen:", "            continue", "        seen.append(x)"), False),
    (LST, "set 이면 통과", code("def f(xs):", "    seen = set()", "    for x in xs:", "        if x in seen:", "            pass"), False),
    (LST, "반복 밖 in 한 번", code("def f(x):", "    seen = [1, 2]", "    return x in seen"), False),
    (LST, "다른 함수의 같은 이름 list", code("def a():", "    seen = []", "", "", "def b(xs, seen):", "    for x in xs:", "        if x in seen:", "            pass"), False),
    (LST, "list 타입힌트 매개변수(함수 분리로 갈라진 경우)", code("def a():", "    return []", "", "", "def b(xs, seen: list):", "    for x in xs:", "        if x in seen:", "            pass"), True),
    (LST, "Sequence 타입힌트는 대상 아님(list 리터럴 아님)", code("def b(xs, seen: Sequence):", "    for x in xs:", "        if x in seen:", "            pass"), False),
    # ---- EFF-04 openpyxl 모드
    (XLS, "load_workbook(p)", code("wb = load_workbook(p)"), True),
    (XLS, "openpyxl.load_workbook(p)", code("wb = openpyxl.load_workbook(p)"), True),
    (XLS, "read_only=False", code("wb = load_workbook(p, read_only=False)"), True),
    (XLS, "별칭 lw", code("from openpyxl import load_workbook as lw", "wb = lw(p)"), True),
    (XLS, "저장하는 편집용 워크북은 제외", code("wb = load_workbook(p)", "wb.save(out)"), False),
    (XLS, "read_only=True", code("wb = load_workbook(p, read_only=True)"), False),
    (XLS, "read_only=변수(판단 불가는 잡지 않음)", code("wb = load_workbook(p, read_only=flag)"), False),
    # ---- EFF-05 셀 단위 접근
    (CELL, "for 안 cell(row=,column=)", code("for i in r:", "    ws.cell(row=i, column=1)"), True),
    (CELL, "위치 인자 cell(i, 1)", code("for i in r:", "    ws.cell(i, 1)"), True),
    (CELL, "값 쓰기 cell(value=) 는 불가피", code("for i in r:", "    ws.cell(row=i, column=1, value=i)"), False),
    (CELL, "스타일 지정에 쓰인 cell 은 불가피", code("for i in r:", "    c = ws.cell(row=i, column=1)", "    c.border = thin"), False),
    (CELL, "ws.cell(...).value = 쓰기", code("for i in r:", "    ws.cell(row=i, column=1).value = i"), False),
    (CELL, "값을 읽는 cell", code("for i in r:", "    v = ws.cell(row=i, column=1).value", "    print(v)"), True),
    (CELL, "반복 밖 cell", code("ws.cell(row=1, column=1)"), False),
    (CELL, "인자 없는 cell()", code("for i in r:", "    obj.cell()"), False),
    # ---- EFF-06 COM 정리
    (COM, "Dispatch Excel, finally 없음", code("def f():", "    app = win32com.client.Dispatch('Excel.Application')"), True),
    (COM, "DispatchEx", code("def f():", "    app = DispatchEx('Excel.Application')"), True),
    (COM, "모듈 최상위 Dispatch", code("app = Dispatch('Excel.Application')"), True),
    (COM, "실행 중인 AutoCAD 에 붙는 코드는 Quit 대상 아님", code("def f():", "    acad = win32com.client.Dispatch('AutoCAD.Application')", "    return acad.ActiveDocument"), False),
    (COM, "Word 도 대상", code("def f():", "    w = Dispatch('Word.Application')"), True),
    (COM, "try/finally 있음", code("def f():", "    app = Dispatch('Excel.Application')", "    try:", "        pass", "    finally:", "        app.Quit()"), False),
    (COM, "컨텍스트 매니저", code("def f():", "    with session('Excel.Application') as app:", "        pass"), False),
    (COM, "Application 이 아닌 COM", code("def f():", "    fso = Dispatch('Scripting.FileSystemObject')"), False),
    # ---- STD-02 절대경로
    (ABS, "Windows 경로", code("p = " + chr(34) + "C:" + BS * 2 + "Users" + BS * 2 + "a.txt" + chr(34)), True),
    (ABS, "슬래시 C 드라이브 경로", code("p = 'C:/data/a.txt'"), True),
    (ABS, "유닉스 홈 경로", code("p = '/home/me/a.txt'"), True),
    (ABS, "f-string 안 경로", code("p = f'C:/data/{name}.txt'"), True),
    (ABS, "docstring 안 예시", code("def f():", "    " + Q3 + "예: C:/data/x" + Q3), False),
    (ABS, "URL", code("u = 'https://example.com/a'"), False),
    (ABS, "상대경로", code("p = 'data/a.txt'"), False),
]  # fmt: skip

BODY = code(
    "def calc(a):",
    "    x = a + 1",
    "    x = x * 2",
    "    x = x - 3",
    "    x = x / 4",
    "    return x",
)
DOC1 = code(
    "def calc(a):",
    "    " + Q3 + "설명1" + Q3,
    "    x = a + 1",
    "    x = x * 2",
    "    x = x - 3",
    "    x = x / 4",
    "    return x",
)
DOC2 = DOC1.replace("설명1", "설명2").replace("calc", "c2")
SHORT = code("def f(a):", "    a = 1", "    return a")

PROJ = [
    ("본문 동일, 다른 파일", {"a.py": BODY, "b.py": BODY.replace("calc", "calc2")}, True),
    ("docstring 만 다름", {"a.py": DOC1, "b.py": DOC2}, True),
    ("변수 이름만 다름", {"a.py": BODY, "b.py": BODY.replace("x", "y").replace("calc", "other")}, True),
    ("4문장 이하", {"a.py": SHORT, "b.py": SHORT.replace("def f", "def g")}, False),
    ("test_ 함수 제외", {"a.py": BODY.replace("calc", "test_a"), "b.py": BODY.replace("calc", "test_b")}, False),
    ("본문이 서로 다름", {"a.py": BODY, "b.py": BODY.replace("+ 1", "+ 9").replace("calc", "c3")}, False),
]  # fmt: skip


@pytest.mark.parametrize(
    ("fn", "name", "src", "expect"),
    CASES,
    ids=[f"{case[0].__name__[6:]}-{case[1]}" for case in CASES],
)
def test_variant(fn, name, src, expect):
    assert run(fn, src) is expect, f"{name}: 기대={'잡기' if expect else '통과'}"


@pytest.mark.parametrize(("label", "files", "expect"), PROJ, ids=[p[0] for p in PROJ])
def test_function_duplicates(label, files, expect):
    parsed = {k: ast.parse(v) for k, v in files.items()}
    assert bool(c.check_func_body_dup(parsed)) is expect, label


def test_copy_filenames():
    names = [
        "a_old.py", "a.py", "b_backup.py", "c_copy.py", "d_v2.py", "d.py", "e_v3.py",
        "report_final.py", "report.py", "f_bak.py", "복사본.py", "g.py", "sub/h_old.py",
    ]  # fmt: skip
    want = [
        "a_old.py",
        "b_backup.py",
        "c_copy.py",
        "d_v2.py",
        "f_bak.py",
        "report_final.py",
        "sub/h_old.py",
        "복사본.py",
    ]
    assert sorted(h.file for h in c.check_copy_filenames(names)) == want

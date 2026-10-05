"""재현율 측정: 실제 파일 뒤에 조항별 결함을 심고, audit-kit std 가 그 조항을 (심은 위치에서) 보고하는지 센다."""

import ast
import collections
import random
import shutil
import sys
from pathlib import Path

from audit_kit.config import load_config
from audit_kit.std.run import run_std

sys.path.insert(0, str(Path(__file__).parent))
from _paths import load_paths

sys.stdout.reconfigure(encoding="utf-8")
random.seed(11)
HOSTS_FROM = [Path(p) for p in load_paths("hosts_from.txt")]
OUT = Path(sys.argv[1])
N_HOSTS = int(sys.argv[2])
BR = "\n"

MANY_IFS = BR.join(
    ["def t_complex(a):"]
    + [f"    if a == {i}:" + BR + f"        return {i}" for i in range(14)]
    + ["    return -1"]
)
DUP_BODY = BR.join([
    "def {n}(a):",
    "    x = a + 1",
    "    x = x * {k}",
    "    x = x - 3",
    "    x = x / 4",
    "    return x",
])

# 조항 -> (결함 코드, 기대하는 조항 ID). 코드는 파일 끝에 붙는다.
TEMPLATES = {
    "DUP-02": "",
    "STD-01": BR.join(["def t_enc(p):", "    return open(p).read()"]),
    "STD-02": "T_PATH = 'C:/Users/someone/data/input.txt'",
    "STD-02b": BR.join([
        "import os as t_os",
        "def t_join(a):",
        "    return t_os.path.join(a, 'b')",
    ]),
    "STD-03": BR.join(["def t_print():", "    print('debug')"]),
    "STD-04": BR.join(["def t_bare():", "    try:", "        pass", "    except:", "        pass"]),
    "STD-05": BR.join(["def t_default(a, b=[]):", "    return b"]),
    "STD-06": BR.join([
        "T_COUNTER = 0",
        "def t_global():",
        "    global T_COUNTER",
        "    T_COUNTER = 1",
    ]),
    "STD-07": BR.join([
        "from typing import Optional",
        "def t_opt(x: Optional[int]) -> Optional[int]:",
        "    return x",
    ]),
    "STD-08": MANY_IFS,
    "STD-09": BR.join(["def t_secret():", "    password = 'hunter2abc'", "    return password"]),
    "STD-10": "import json as t_unused_json",
    "DUP-01": BR.join(["def t_same():", "    return 1", "", "", "def t_same():", "    return 2"]),
    "EFF-01": BR.join([
        "def t_loop(xs):",
        "    out = []",
        "    for x in xs:",
        "        out.append(x * 2)",
        "    return out",
    ]),
    "EFF-02": BR.join([
        "def t_member(xs):",
        "    allowed = ['a', 'b']",
        "    for x in xs:",
        "        if x in allowed:",
        "            pass",
    ]),
    "EFF-03": BR.join([
        "def t_io(rows):",
        "    for r in rows:",
        "        with open('fixed_cfg.txt', encoding='utf-8') as f:",
        "            f.read()",
    ]),
    "EFF-04": BR.join([
        "def t_wb(p):",
        "    from openpyxl import load_workbook",
        "    return load_workbook(p)",
    ]),
    "EFF-05": BR.join([
        "def t_cell(ws, rows):",
        "    total = 0",
        "    for i in rows:",
        "        total += ws.cell(row=i, column=1).value",
        "    return total",
    ]),
    "EFF-06": BR.join([
        "def t_com():",
        "    import win32com.client",
        "    return win32com.client.Dispatch('Excel.Application')",
    ]),
    "ERR-01": "import totally_fake_pkg_zz",
    "ERR-03": BR.join([
        "import subprocess as t_sp",
        "def t_shell(c):",
        "    t_sp.run(c, shell=True)",
    ]),
}
EXPECT = {k: k[:6] for k in TEMPLATES}  # STD-02b -> STD-02

# 호스트 파일 고르기: 구문 오류 없고 너무 크지 않은 것
cands = []
for base in HOSTS_FROM:
    for f in base.rglob("*.py"):
        if any(x in f.parts for x in ("venv", ".venv", "node_modules", "__pycache__", "backup")):
            continue
        try:
            text = f.read_text(encoding="utf-8")
            ast.parse(text)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        if 20 <= len(text.splitlines()) <= 500:
            cands.append((f, text))
hosts = random.sample(cands, N_HOSTS)

shutil.rmtree(OUT, ignore_errors=True)
(OUT / "pkg").mkdir(parents=True)
(OUT / "pyproject.toml").write_text('[project]\nname = "mut"\nversion = "0"\n', encoding="utf-8")
(OUT / "pkg" / "__init__.py").write_text("", encoding="utf-8")
meta = {}
for hi, (_f, text) in enumerate(hosts):
    if not text.endswith(BR):
        text += BR
    n = len(text.splitlines())
    (OUT / "pkg" / f"host{hi}.py").write_text(text, encoding="utf-8")  # 기준선(심지 않은 원본)
    for name, code in TEMPLATES.items():
        rel = f"pkg/m{hi}_{name.replace('-', '_')}.py"
        if name == "DUP-02":  # 변이마다 본문이 달라야 그룹이 따로 만들어진다
            code = (
                DUP_BODY.format(n="t_dup_a", k=hi + 2)
                + BR
                + BR
                + DUP_BODY.format(n="t_dup_b", k=hi + 2)
            )
        (OUT / rel).write_text(text + BR + BR + code + BR, encoding="utf-8")
        meta[rel] = (name, n)
# 파일 단위 조항
(OUT / "test_mut.py").write_text("VALUE = 1\n", encoding="utf-8")
(OUT / "pkg" / "report_old.py").write_text("VALUE = 1\n", encoding="utf-8")

cfg = load_config(OUT)
found = [f for r in run_std(cfg, with_mypy=False) for f in r.findings]
by_file = collections.defaultdict(list)
for f in found:
    by_file[f.file].append(f)

hit = collections.Counter()
tot = collections.Counter()
miss = collections.defaultdict(list)
noise = collections.Counter()
for rel, (name, n) in meta.items():
    exp = EXPECT[name]
    tot[name] += 1
    new = [x for x in by_file.get(rel, []) if (x.line or 0) > n and x.severity != "ignore"]
    if any(x.rule == exp for x in new):
        hit[name] += 1
    else:
        miss[name].append(rel)
    for x in new:
        if x.rule != exp:
            noise[name, x.rule] += 1
print(f"호스트 파일 {len(hosts)}개 × 결함 {len(TEMPLATES)}종 = 변이 {len(meta)}개\n")
print(f"{'조항':8} {'심음':>4} {'찾음':>4} {'재현율':>6}")
for name in TEMPLATES:
    print(f"{name:8} {tot[name]:4} {hit[name]:4} {hit[name] / tot[name]:6.0%}")
allrec = sum(hit.values()) / sum(tot.values())
print(f"\n전체 재현율 {sum(hit.values())}/{sum(tot.values())} = {allrec:.0%}")
print(
    "파일 단위:",
    "STD-11",
    any(f.rule == "STD-11" for f in by_file.get("test_mut.py", [])),
    "| DUP-03",
    any(f.rule == "DUP-03" for f in by_file.get("pkg/report_old.py", [])),
)
print("\n놓친 것:", {k: v[:2] for k, v in miss.items() if v})
extra = collections.Counter()
for (name, rule), c in noise.items():
    extra[name, rule] += c
print("심은 위치에서 기대 외 조항이 함께 나온 것(부가 검출):", dict(list(extra.items())[:12]))

import collections
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")  # STD-14: 오류 출력도 한글이 깨지지 않게
from audit_kit.config import load_config
from audit_kit.std.run import project_files, run_std

sys.path.insert(0, str(Path(__file__).parent))
from _paths import (
    load_paths,  # ruff: ignore[module-import-not-at-top-of-file] — sys.path 조정 뒤에 와야 함
)

ROOTS = load_paths("roots.txt")
out: list[dict] = []
files_total = 0
for r in ROOTS:
    root = Path(r)
    if not root.is_dir():
        print("없음:", r)
        continue
    cfg = load_config(root)
    t0 = time.perf_counter()
    try:
        results = run_std(cfg, with_mypy=False)
    except Exception as e:  # ruff: ignore[blind-except] — 임의의 실제 프로젝트를 검사하는 검증 스크립트라
        # 무엇이 터질지 미리 알 수 없다. 죽은 프로젝트는 기록만 하고 나머지는 계속 검사한다.
        print("실패:", r, repr(e)[:200])
        continue
    n = len(project_files(cfg))
    files_total += n
    fs = [f for res in results for f in res.findings if f.severity != "ignore"]
    print(f"{root.name[:34]:34} 파일 {n:4}  발견 {len(fs):4}  {time.perf_counter() - t0:5.1f}s")
    out.extend(
        {
            "project": r,
            "rule": f.rule,
            "sev": f.severity,
            "file": f.file,
            "line": f.line,
            "msg": f.message[:200],
            "tool_rule": f.extra.get("tool_rule"),
        }
        for f in fs
    )
json.dump(out, Path(sys.argv[1]).open("w", encoding="utf-8"), ensure_ascii=False)
c = collections.Counter(o["rule"] for o in out)
print(f"\n검사 파일 {files_total}개, 발견 {len(out)}건")
for k, v in sorted(c.items()):
    print(f"  {k}: {v}")

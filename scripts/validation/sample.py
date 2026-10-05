import json
import random
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
random.seed(7)
d = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
clause, n = sys.argv[2], int(sys.argv[3])
items = [x for x in d if x["rule"] == clause]
print(f"=== {clause}: 전체 {len(items)}건 중 {min(n, len(items))}건 표본")
for x in random.sample(items, min(n, len(items))):
    path = Path(x["project"]) / x["file"]
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        continue
    ln = x["line"] or 1
    lo, hi = max(0, ln - 3), min(len(lines), ln + 2)
    print(
        f"\n--- {Path(x['project']).name[:20]}/{x['file']}:{ln} [{x['tool_rule']}] {x['msg'][:80]}"
    )
    for i in range(lo, hi):
        print(f"{'>>' if i + 1 == ln else '  '} {i + 1:4} {lines[i][:110]}")

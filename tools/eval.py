"""Local evaluation: run selector on the sample, then compare with answers.

usage: python tools/eval.py [sample_dir] [output.jsonl]
Only this script reads answers.json; the selector never does.
"""

import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sample = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "sample")
outp = sys.argv[2] if len(sys.argv) > 2 else None

tests = json.load(open(os.path.join(sample, "tests.json")))["tests"]
dur = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
ids = {t["number"]: t["id"] for t in tests}
ans = json.load(open(os.path.join(sample, "answers.json")))["commits"]

if outp:
    lines = open(outp).read().splitlines()
else:
    env = dict(os.environ, BR_DATA=sample)
    env.setdefault("BR_SRC", os.path.join(os.path.dirname(ROOT), "sqlglot-src"))
    t0 = time.process_time()
    p = subprocess.run([sys.executable, os.path.join(ROOT, "selector.py")], env=env, capture_output=True, text=True)
    if p.returncode:
        print(p.stderr)
        sys.exit(1)
    lines = p.stdout.splitlines()
    if os.environ.get("BR_DEBUG") == "1":
        print(p.stderr)


def best_cost(nums):
    if not nums:
        return 0
    d = sorted((dur[i] for i in nums), reverse=True)
    best = None
    for k in range(1, 9):
        L = [0] * k
        for x in d:
            j = L.index(min(L))
            L[j] += x
        c = max(L) + 2000 * k
        best = c if best is None else min(best, c)
    return best


total = 0
oracle = 0
misses = 0
for line in lines:
    o = json.loads(line)
    cid = o["commit"]
    ws = [w for w in o["workers"] if w]
    sel = {x for w in ws for x in w}
    cost = (max(sum(dur[x] for x in w) for w in ws) + 2000 * len(ws)) if ws else 0
    fail = set(ans[cid]["failing"])
    miss = fail - sel
    oc = best_cost(fail)
    oracle += oc
    total += cost + (300000 if miss else 0)
    misses += bool(miss)
    flag = "MISS" if miss else ""
    print(f"{cid} {ans[cid]['kind']:8s} sel={len(sel):4d} fail={len(fail):3d} cost={cost/1000:7.3f} oracle={oc/1000:7.3f} {flag}")
    for x in sorted(miss):
        print("     missed", ids[x])
print(f"TOTAL {total/1000:.3f}  oracle {oracle/1000:.3f}  misses {misses}")

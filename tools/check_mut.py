"""Collect labels from work/mut*/labels into an evaluation set and score it.

usage: python tools/check_mut.py <out_dir> <label_dir>...
"""

import glob
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
out = sys.argv[1]
tests = json.load(open(os.path.join(ROOT, "sample", "tests.json")))["tests"]
num = {t["id"]: t["number"] for t in tests}
by_file = {}
for t in tests:
    by_file.setdefault(t["file"], []).append(t["number"])

shutil.rmtree(out, ignore_errors=True)
os.makedirs(os.path.join(out, "commits"))
shutil.copy(os.path.join(ROOT, "sample", "tests.json"), out)
ans = {}
for d in sys.argv[2:]:
    for f in glob.glob(os.path.join(d, "labels", "*.json")):
        cid = os.path.basename(f)[:-5]
        L = json.load(open(f))
        if L.get("apply_failed"):
            continue
        failing = {num[n] for n in L["failed"] if n in num}
        for bf in L.get("bad_files", []):
            failing.update(by_file.get(bf.split("::")[0], []))
        ans[cid] = {"failing": sorted(failing), "kind": {"c": "code", "m": "multi", "f": "fixture"}.get(cid[-1], "?")}
        shutil.copy(os.path.join(d, "commits", cid + ".patch"), os.path.join(out, "commits"))
json.dump({"commits": ans}, open(os.path.join(out, "answers.json"), "w"))
print("labeled", len(ans))
subprocess.run([sys.executable, os.path.join(ROOT, "tools", "eval.py"), out])

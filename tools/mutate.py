"""Generate random validation mutations of the pinned sqlglot (local testing only).

usage: python tools/mutate.py <sqlglot_src> <out_dir> <count> <seed>
"""

import ast
import difflib
import os
import random
import re
import sys

SRC, OUT, COUNT, SEED = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rnd = random.Random(SEED)
os.makedirs(os.path.join(OUT, "commits"), exist_ok=True)

py = []
for dp, dn, fn in os.walk(os.path.join(SRC, "sqlglot")):
    for f in fn:
        if f.endswith(".py"):
            py.append(os.path.relpath(os.path.join(dp, f), SRC).replace(os.sep, "/"))
py.sort()
if os.environ.get("MUT_FILES"):
    py = [x for x in py if any(x.startswith(pre) for pre in os.environ["MUT_FILES"].split(","))]
fixtures = sorted(
    os.path.relpath(os.path.join(dp, f), SRC).replace(os.sep, "/")
    for dp, dn, fn in os.walk(os.path.join(SRC, "tests", "fixtures", "optimizer"))
    for f in fn
    if f.endswith(".sql") and "tpc" not in dp
) + ["tests/fixtures/identity.sql", "tests/fixtures/pretty.sql"]

OPS = [
    (" == ", " != "), (" != ", " == "), (" < ", " <= "), (" <= ", " < "), (" > ", " >= "),
    (" >= ", " > "), (" and ", " or "), (" or ", " and "), ("True", "False"), ("False", "True"),
    (" is not ", " is "), (" is None", " == None"), ("not ", ""), (" + 1", " - 1"), (" - 1", " + 1"),
    ("copy=False", "copy=True"), (" in ", " not in "),
]
COMMENTS = ["# Kept for backwards compatibility", "# Cheap check first", "# TODO: revisit",
            "# Mirrors the behavior of the reference implementation", "# Intentionally not cached"]


def read(rel):
    return open(os.path.join(SRC, rel), encoding="utf-8").read().split("\n")


def diff(rel, old, new):
    d = "\n".join(difflib.unified_diff(old, new, "a/" + rel, "b/" + rel, lineterm="", n=3))
    return f"diff --git a/{rel} b/{rel}\nindex 1111111..2222222 100644\n" + d + "\n"


def code_mut(rel):
    L = read(rel)
    for _ in range(200):
        i = rnd.randrange(len(L))
        line = L[i]
        s = line.strip()
        if not s or s.startswith(("#", '"', "'", "def ", "class ", "@", "import ", "from ")):
            continue
        cands = [(a, b) for a, b in OPS if a in line]
        m = re.search(r"(?<![\w.])(\d+)(?![\w.])", line)
        if m:
            cands.append(("NUM", m))
        if not cands:
            continue
        a, b = rnd.choice(cands)
        if a == "NUM":
            v = int(b.group(1))
            nl = line[: b.start(1)] + str(v + rnd.choice([1, -1] if v else [1])) + line[b.end(1):]
        else:
            idx = [k.start() for k in re.finditer(re.escape(a), line)]
            k = rnd.choice(idx)
            nl = line[:k] + b + line[k + len(a):]
        new = list(L)
        new[i] = nl
        try:
            ast.parse("\n".join(new))
        except SyntaxError:
            continue
        return rel, L, new
    return None


def comment_mut(rel):
    L = read(rel)
    for _ in range(100):
        i = rnd.randrange(1, len(L))
        if L[i].strip() and not L[i].strip().startswith(("'", '"', ")", "]", "}")):
            ind = re.match(r"\s*", L[i]).group(0)
            new = list(L)
            new.insert(i, ind + rnd.choice(COMMENTS))
            try:
                ast.parse("\n".join(new))
            except SyntaxError:
                continue
            return rel, L, new
    return None


def fixture_mut(rel):
    L = read(rel)
    for _ in range(100):
        i = rnd.randrange(len(L))
        if L[i].strip() and not L[i].startswith(("#", "--")):
            words = L[i].split(" ")
            k = rnd.randrange(len(words))
            words.insert(k, rnd.choice(["42", "x", "0", "NULL", ""]))
            new = list(L)
            new[i] = " ".join(words)
            if new[i] != L[i]:
                return rel, L, new
    return None


if os.environ.get("MUT_FIXTURES"):
    fixtures = os.environ["MUT_FIXTURES"].split(",")
kinds = os.environ.get("MUT_KINDS", "code,code,code,code,code,code,comment,fixture,fixture,multi").split(",")
n = 0
while n < COUNT:
    k = rnd.choice(kinds)
    parts = []
    if k == "code":
        r = code_mut(rnd.choice(py))
        parts = [r] if r else []
    elif k == "comment":
        r = comment_mut(rnd.choice(py))
        parts = [r] if r else []
    elif k == "fixture":
        r = fixture_mut(rnd.choice(fixtures))
        parts = [r] if r else []
    else:
        a = comment_mut(rnd.choice(py))
        b = code_mut(rnd.choice(py)) if rnd.random() < 0.5 else fixture_mut(rnd.choice(fixtures))
        parts = [x for x in (a, b) if x]
        if len({p[0] for p in parts}) != len(parts):
            parts = parts[:1]
    if not parts:
        continue
    text = "".join(diff(*p) for p in sorted(parts))
    cid = f"m{SEED}x{n:03d}{k[0]}"
    open(os.path.join(OUT, "commits", cid + ".patch"), "w", newline="\n", encoding="utf-8").write(text)
    n += 1
print("wrote", n)

"""Lines/names the full test_max_nodes runs but its proxy does not.

usage: python tools/proxy_lines.py <sqlglot_src> map/blast.map <out.json> [<proxy module> <test id>]
"""

import dis
import json
import os
import sys
import types
import zlib

SRC, MAP, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
PROXY = sys.argv[4] if len(sys.argv) > 4 else "maxnodes_proxy"
TEST = sys.argv[5] if len(sys.argv) > 5 else "tests/test_parser.py::TestParser::test_max_nodes"
SRC = os.path.abspath(SRC)
sys.path.insert(0, SRC)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gate"))

mon = sys.monitoring
seen = set()


def on_line(code, ln):
    fn = code.co_filename.replace("\\", "/")
    root = SRC.replace("\\", "/") + "/"
    if fn.startswith(root + "sqlglot/"):
        seen.add((fn[len(root):], ln))
    return mon.DISABLE


import importlib  # noqa: E402

proxy = importlib.import_module(PROXY)
import sqlglot  # noqa: E402,F401  (import-time lines are not part of either test)

mon.use_tool_id(mon.COVERAGE_ID, "proxy")
mon.register_callback(mon.COVERAGE_ID, mon.events.LINE, on_line)
mon.set_events(mon.COVERAGE_ID, mon.events.LINE)
proxy.body()
mon.set_events(mon.COVERAGE_ID, 0)

d = json.loads(zlib.decompress(open(MAP, "rb").read()))
t = d["tests"].index(TEST)
pool = d["pool"]
real = set()
for f, lines in d["lines"].items():
    if not f.startswith("sqlglot/"):
        continue
    for ln, r in lines.items():
        if r >= 0 and (int(pool[r], 16) >> t) & 1:
            real.add((f, int(ln)))
only = real - seen
LOAD = {"LOAD_ATTR", "LOAD_GLOBAL", "LOAD_NAME", "LOAD_METHOD", "LOAD_DEREF", "LOAD_SUPER_ATTR"}


def iter_code(co):
    yield co
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            yield from iter_code(c)


names = set()
by_file = {}
for f, ln in only:
    by_file.setdefault(f, set()).add(ln)
for f, lns in by_file.items():
    for c in iter_code(compile(open(os.path.join(SRC, f), encoding="utf-8").read(), f, "exec")):
        for ins in dis.get_instructions(c):
            if ins.positions and ins.positions.lineno in lns and ins.opname in LOAD and isinstance(ins.argval, str):
                names.add(ins.argval)
json.dump({"only_full": {f: sorted(v) for f, v in by_file.items()}, "only_full_names": sorted(names),
           "real": len(real), "proxy": len(seen)}, open(OUT, "w"))
print("real", len(real), "proxy", len(seen), "only-full", len(only), sorted(by_file.items())[:5])

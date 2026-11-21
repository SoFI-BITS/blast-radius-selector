"""Per-query line coverage of the TPC-DS gate's pre-execution pipeline.

For every tpc-ds fixture query, record the sqlglot lines run by
gate/tpcds_gate.py's components() for that query (parse, pickle, optimize,
duckdb transpile). A patch confined to function bodies can only change the
fingerprint of queries that run one of its lines.

usage: python tools/tpcds_query_cov.py <sqlglot_src> map/tpcds_qcov.json
"""

import json
import os
import sys

SRC = os.path.abspath(sys.argv[1])
OUT = sys.argv[2]
sys.path.insert(0, SRC)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gate"))
os.environ["PYTHONHASHSEED"] = "0"

import tpcds_gate as g  # noqa: E402
import sqlglot  # noqa: E402,F401
import sqlglot.optimizer  # noqa: E402,F401
from tests.helpers import TPCDS_SCHEMA  # noqa: E402,F401

mon = sys.monitoring
root = SRC.replace("\\", "/") + "/"
cur = set()


def on_line(code, ln):
    fn = code.co_filename.replace("\\", "/")
    if fn.startswith(root + "sqlglot/"):
        cur.add((fn[len(root):], ln))
    return mon.DISABLE


mon.use_tool_id(mon.COVERAGE_ID, "qcov")
mon.register_callback(mon.COVERAGE_ID, mon.events.LINE, on_line)
lines = {}  # file -> line -> bitmask of query indices
gen = g.components(os.path.join(SRC, "tests", "fixtures", "optimizer", "tpc-ds", "tpc-ds.sql"))
i = 0
while True:
    cur = set()
    mon.restart_events()
    mon.set_events(mon.COVERAGE_ID, mon.events.LINE)
    try:
        next(gen)
    except StopIteration:
        break
    finally:
        mon.set_events(mon.COVERAGE_ID, 0)
    for f, ln in cur:
        d = lines.setdefault(f, {})
        d[ln] = d.get(ln, 0) | (1 << i)
    i += 1
json.dump({"queries": i, "lines": {f: {str(k): v for k, v in d.items()} for f, d in lines.items()}}, open(OUT, "w"))
print("queries", i, "files", len(lines))

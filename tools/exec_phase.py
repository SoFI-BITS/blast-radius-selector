"""Record sqlglot lines that run in the executor tests *outside* the gated pipeline.

The gate (gate/tpcds_gate.py) fingerprints parse + pickle + optimize + duckdb
transpile. Everything else test_execute_tpcds/tpch runs (table loading, schema
setup, planning, Python execution, result pickling) is recorded here; a patch
touching any of these lines is never gated.

run inside the coverage image:  cd /src && python /tools/exec_phase.py /out/exec_phase.json
"""

import json
import os
import pickle
import sys

mon = sys.monitoring
ROOT = os.getcwd().rstrip("/") + "/"
phase = ["off"]
lines = {"tpcds": set(), "tpch": set()}
suite = ["tpcds"]
_fc = {}


def rel(fn):
    r = _fc.get(fn)
    if r is None:
        r = fn[len(ROOT):] if fn.startswith(ROOT + "sqlglot/") else ""
        _fc[fn] = r
    return r


def on_line(code, ln):
    if phase[0] != "exec":
        return None
    r = rel(code.co_filename)
    if r:
        lines[suite[0]].add((r, ln))
    return mon.DISABLE


mon.use_tool_id(mon.COVERAGE_ID, "phase")
mon.register_callback(mon.COVERAGE_ID, mon.events.LINE, on_line)
mon.set_events(mon.COVERAGE_ID, mon.events.LINE)

import concurrent.futures as cf  # noqa: E402


class Sync:
    def __init__(self, *a, initializer=None, initargs=(), **k):
        if initializer:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *a, **k):
        f = cf.Future()
        a = pickle.loads(pickle.dumps(a))
        prev = phase[0]
        phase[0] = "exec"
        try:
            r = fn(*a, **k)
            r = pickle.loads(pickle.dumps(r))
            f.set_result(r)
        except BaseException as e:  # noqa
            f.set_exception(e)
        finally:
            phase[0] = prev
        return f

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def shutdown(self, *a, **k):
        pass


cf.ProcessPoolExecutor = Sync

import sqlglot.executor as ex  # noqa: E402

_orig_opt = ex.optimize


def gated_optimize(*a, **k):
    prev = phase[0]
    phase[0] = "pre"
    try:
        return _orig_opt(*a, **k)
    finally:
        phase[0] = prev
        mon.restart_events()


ex.optimize = gated_optimize

import tests.test_executor as te  # noqa: E402

te.ProcessPoolExecutor = Sync

# table loading in setUpClass builds executor Tables: exec phase for both suites
phase[0] = "exec"
suite[0] = "tpcds"
te.TestExecutor.setUpClass()
lines["tpch"] |= lines["tpcds"]
mon.restart_events()
phase[0] = "off"

t = te.TestExecutor("test_execute_tpcds")
t.subtestHelper = lambda *a, **k: None  # comparison is pandas-only
for name, s in (("tpcds", "test_execute_tpcds"), ("tpch", "test_execute_tpch")):
    suite[0] = name
    mon.restart_events()
    getattr(te.TestExecutor(s), s).__func__(t)

out = {k: {} for k in lines}
for k, v in lines.items():
    for f, ln in v:
        out[k].setdefault(f, []).append(ln)
    for f in out[k]:
        out[k][f].sort()
json.dump(out, open(sys.argv[1], "w"))
print({k: len(v) for k, v in lines.items()}, file=sys.stderr)

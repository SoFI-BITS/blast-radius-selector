"""pytest plugin: per-fixture-case line coverage and CPU time for TestOptimizer.check_file.

Installs the sequential twin from pairs_exec.py (so running the suite with it also
checks that the twin is faithful: every test must still pass), and records for every
(test, fixture file, case number) the sqlglot lines it runs. Output: $CASES_OUT
{"<test>|<file>": {"n": cases, "ms": {case: ms}, "lines": {file: {line: case-bitmask}}}}
"""

import importlib
import json
import os
import pkgutil
import sys
import time

mon = sys.monitoring
ROOT = os.environ.get("COV_ROOT", os.getcwd()).rstrip("/") + "/"
_cur = [None]
_t0 = [0.0]
_data = {}
_fc = {}


def _rel(fn):
    r = _fc.get(fn)
    if r is None:
        r = fn[len(ROOT):] if fn.startswith(ROOT + "sqlglot/") else ""
        _fc[fn] = r
    return r


def _on_line(code, ln):
    if _cur[0] is not None:
        r = _rel(code.co_filename)
        if r:
            _cur[0].add((r, ln))
    return mon.DISABLE


def _on_case(test, file, i, what):
    key = f"{test}|{file}"
    if what == "start":
        _cur[0] = set()
        mon.restart_events()
        _t0[0] = time.process_time()
        return
    d = _data.setdefault(key, {"n": 0, "ms": {}, "lines": {}})
    d["n"] = max(d["n"], i)
    d["ms"][str(i)] = round((time.process_time() - _t0[0]) * 1000, 2)
    for f, ln in _cur[0]:
        fl = d["lines"].setdefault(f, {})
        fl[str(ln)] = fl.get(str(ln), 0) | (1 << i)
    _cur[0] = None


def pytest_configure(config):
    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name != "sqlglot.__main__":
            try:
                importlib.import_module(info.name)
            except Exception:  # noqa
                pass
    sys.path.insert(0, os.environ.get("PAIRS_DIR", "/app"))
    import pairs_exec
    import tests.test_optimizer as to

    pairs_exec.install(to, None, _on_case)
    mon.use_tool_id(mon.COVERAGE_ID, "cases")
    mon.register_callback(mon.COVERAGE_ID, mon.events.LINE, _on_line)
    mon.set_events(mon.COVERAGE_ID, mon.events.LINE)


def pytest_unconfigure(config):
    mon.set_events(mon.COVERAGE_ID, 0)
    with open(os.environ.get("CASES_OUT", "/out/cases.json"), "w") as fh:
        json.dump(_data, fh)

"""Gate for tests/test_executor.py::TestExecutor::test_execute_tpcds (21 s).

Mirrors what that test does with every tpc-ds fixture query:
  * parse_one(sql), pickled across the spawn pool               -> "pre"
  * "execute: true" queries: optimize(expr, schema, leave_tables_isolated=True),
    which is what sqlglot.executor.execute runs first           -> "pre"
  * transpile(sql, write="duckdb"), the test's expected side     -> "tr"
  * the executed result table                                    -> "res" (baseline only,
    and at runtime only for the few queries whose "pre" changed)
The selector calls this only for patches that touch no line run while planning /
executing (map/exec_phase.json). With "pre" and "tr" unchanged the executor
sees identical input; if a handful of queries changed, they are executed and
their result tables compared with the unpatched results.

usage:
  python tpcds_gate.py <tpc-ds dir> --baseline > baseline.json
  python tpcds_gate.py <tpc-ds dir> <baseline.json> < overrides.json   -> SAME | DIFF
"""

import ast
import csv
import gzip
import hashlib
import importlib.abc
import importlib.util
import json
import os
import pickle
import sys

MAX_EXECUTE = 12


class _Override(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, sources, files):
        self.sources = sources
        self.files = files

    def find_spec(self, fullname, path, target=None):
        if fullname in self.sources:
            is_pkg = self.files[fullname].endswith("__init__.py")
            spec = importlib.util.spec_from_loader(fullname, self, is_package=is_pkg)
            spec.origin = self.files[fullname]
            spec.has_location = True
            if is_pkg:
                spec.submodule_search_locations = [self.files[fullname].rsplit("/", 1)[0]]
            return spec
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        module.__file__ = self.files[module.__name__]
        exec(compile(self.sources[module.__name__], module.__file__, "exec"), module.__dict__)


_FIXTURE_TEXT = [None]


def pairs(path):
    if _FIXTURE_TEXT[0] is not None:
        raw = _FIXTURE_TEXT[0]
    else:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    if True:
        text = "\n".join(line for line in raw.splitlines() if line and not line.startswith("--"))
    statements = text.split(";")
    for i in range(0, len(statements), 2):
        if i + 1 < len(statements):
            sql = statements[i].strip()
            lines = sql.split("\n")
            meta = {}
            j = 0
            while j < len(lines) and lines[j].startswith("#"):
                k, v = lines[j].split(":", maxsplit=1)
                meta[k.lstrip("#").strip()] = v.strip()
                j += 1
            yield meta, "\n".join(lines[j:])


def _h(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode() if isinstance(p, str) else p)
    return h.hexdigest()


def components(path, only=None):
    from sqlglot import parse_one, serde, transpile
    from sqlglot.optimizer import optimize
    from tests.helpers import TPCDS_SCHEMA

    for qi, (meta, sql) in enumerate(pairs(path)):
        ex = bool(meta.get("execute"))
        if only is not None and qi not in only:
            yield None, sql
            continue
        try:
            e = pickle.loads(pickle.dumps(parse_one(sql)))
            parts = [json.dumps(serde.dump(e), sort_keys=True, default=str)]
            if ex:
                o = optimize(e, TPCDS_SCHEMA, leave_tables_isolated=True, dialect=None)
                parts += [json.dumps(serde.dump(o), sort_keys=True, default=str), o.sql(pretty=True)]
            pre = _h(*parts)
        except BaseException as err:
            pre = "ERR" + _h(type(err).__name__, str(err))
        tr = ""
        if ex:
            try:
                tr = _h(transpile(sql, write="duckdb")[0])
            except BaseException as err:
                tr = "ERR" + _h(type(err).__name__, str(err))
        yield {"execute": ex, "pre": pre, "tr": tr}, sql


def load_tables(directory, names):
    """Same table construction as TestExecutor.setUpClass."""
    from sqlglot.executor.table import Table
    from tests.helpers import TPCDS_SCHEMA

    tables = {}
    for table in names:
        columns = TPCDS_SCHEMA[table]
        rows, ctypes = [], []
        with gzip.open(os.path.join(directory, f"{table}.csv.gz"), "rt", encoding="utf-8") as fh:
            reader = csv.reader(fh, delimiter="|")
            next(reader)
            for row in reader:
                if not ctypes:
                    for v in row:
                        try:
                            ctypes.append(type(ast.literal_eval(v)))
                        except (ValueError, SyntaxError):
                            ctypes.append(str)
                rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
        tables[table] = Table(columns=columns, rows=rows)
    return tables


_TABLES = {}


def result_hash(directory, sql):
    """Like mp_execute: tables restricted to those the query names (find_tables)."""
    from sqlglot import find_tables, parse_one
    from sqlglot.executor import execute
    from tests.helpers import TPCDS_SCHEMA

    try:
        expression = pickle.loads(pickle.dumps(parse_one(sql)))
        names = sorted({t.name for t in find_tables(expression)})
        missing = [x for x in names if x not in _TABLES]
        if missing:
            _TABLES.update(load_tables(directory, missing))
        table = execute(expression, schema=TPCDS_SCHEMA, tables={x: _TABLES[x] for x in names})
        table = pickle.loads(pickle.dumps(table))
        return _h(repr(table.columns), repr(table.rows))
    except BaseException as err:
        return "ERR" + _h(type(err).__name__, str(err))


def main():
    directory = sys.argv[1]
    path = os.path.join(directory, "tpc-ds.sql")
    if sys.argv[2] == "--baseline":
        out = []
        for comp, sql in components(path):
            if comp["execute"]:
                comp["res"] = result_hash(directory, sql)
            out.append(comp)
        print(json.dumps(out))
        return
    base = json.load(open(sys.argv[2]))
    ov = json.load(sys.stdin)
    sys.meta_path.insert(0, _Override(ov["sources"], ov["files"]))
    if ov.get("fixture") is not None:
        _FIXTURE_TEXT[0] = ov["fixture"]  # patched tpc-ds.sql: the test reads its queries from it
    only = set(ov["only"]) if ov.get("only") is not None else None
    queue = []
    n = 0
    for (comp, sql), b in zip(components(path, only), base):
        n += 1
        if comp is None:
            continue  # this query runs none of the changed lines
        if comp["pre"] == b["pre"] and comp["tr"] == b["tr"]:
            continue
        if comp["tr"] != b["tr"] or comp["pre"].startswith("ERR"):
            print("DIFF")
            return
        if not b["execute"]:
            continue  # only parsed and pickled by the test: no crash, no effect
        queue.append((sql, b["res"]))
        if len(queue) > MAX_EXECUTE:
            print("DIFF")
            return
    if n != len(base):
        print("DIFF")
        return
    for sql, want in queue:
        if result_hash(directory, sql) != want:
            print("DIFF")
            return
    print("SAME")


if __name__ == "__main__":
    main()

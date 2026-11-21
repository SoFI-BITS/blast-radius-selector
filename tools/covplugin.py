"""pytest plugin: per-test line coverage + file reads, using sys.monitoring.

Contexts:
  <nodeid>                      lines run during a test's setup/call/teardown
  fixture:<scope>:<nodeid>      lines run while a non-function-scoped fixture is set up
  collect:<nodeid>              lines run while a test module is imported/collected
Writes JSON to $COV_OUT.
"""

import json
import os
import sys
import time

import pytest

mon = sys.monitoring
TOOL = mon.COVERAGE_ID
ROOT = os.environ.get("COV_ROOT", os.getcwd()).rstrip("/") + "/"

_fcache: dict = {}
_stack: list = []  # list of (ctx, lineset, fileset)
_data: dict = {}  # ctx -> [lineset, fileset]
_outcomes: dict = {}
_durations: dict = {}
_cur = [None, None]  # lineset, fileset


def _rel(fn):
    r = _fcache.get(fn)
    if r is None:
        if fn.startswith(ROOT):
            rel = fn[len(ROOT):]
            r = rel if (rel.startswith("sqlglot/") or rel.startswith("tests/")) else ""
        else:
            r = ""
        _fcache[fn] = r
    return r


def _on_line(code, line):
    rel = _rel(code.co_filename)
    if rel:
        _cur[0].add((rel, line))
    return mon.DISABLE


def _audit(event, args):
    if event == "subprocess.Popen" and _cur[1] is not None:
        _cur[1].add("<subprocess>")
        return
    if event in ("open", "os.listdir", "os.scandir") and _cur[1] is not None:
        p = args[0]
        if isinstance(p, (str, bytes, os.PathLike)):
            try:
                p = os.fsdecode(p)
                p = os.path.abspath(p)
            except Exception:
                return
            if p.startswith(ROOT):
                _cur[1].add(p[len(ROOT):])


def _push(ctx):
    entry = _data.setdefault(ctx, [set(), set()])
    _stack.append((entry, _classes.setdefault(ctx, set())))
    (_cur[0], _cur[1]), _cls_cur[0] = _stack[-1]
    mon.restart_events()


def _pop():
    _stack.pop()
    (_cur[0], _cur[1]), _cls_cur[0] = _stack[-1]
    mon.restart_events()


_classes: dict = {}  # ctx -> set of "module:qualname" instantiated
_cls_cur = [None]
_type_names: dict = {}


def _note(tp):
    names = _type_names.get(tp)
    if names is None:
        names = tuple(f"{c.__module__}:{c.__qualname__}" for c in tp.__mro__ if c.__module__.startswith("sqlglot"))
        _type_names[tp] = names
    s = _cls_cur[0]
    if s is not None:
        s.update(names)


def _wrap_init(cls, note_dialect=False):
    orig = cls.__init__

    def __init__(self, *a, **k):
        _note(type(self))
        r = orig(self, *a, **k)
        if note_dialect:
            d = getattr(self, "dialect", None)
            if d is not None:
                _note(type(d))
        return r

    cls.__init__ = __init__


class _SyncExecutor:
    """In-process stand-in for ProcessPoolExecutor so coverage sees the work."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        import pickle

        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *a, **k):
        import concurrent.futures as cf

        import pickle

        f = cf.Future()
        try:
            a, k = pickle.loads(pickle.dumps((a, k)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:  # noqa
            f.set_exception(e)
        return f

    def map(self, fn, *its, **kw):
        return [fn(*x) for x in zip(*its)]

    def shutdown(self, wait=True, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def pytest_configure(config):
    if os.environ.get("COV_SYNC_POOL", "1") == "1":
        import concurrent.futures as cf

        cf.ProcessPoolExecutor = _SyncExecutor
    mon.use_tool_id(TOOL, "blastcov")
    mon.register_callback(TOOL, mon.events.LINE, _on_line)
    mon.set_events(TOOL, mon.events.LINE)
    sys.addaudithook(_audit)
    _push("<session>")
    if os.environ.get("COV_PREIMPORT", "1") == "1":
        import importlib
        import pkgutil

        import sqlglot

        for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
            if info.name == "sqlglot.__main__":
                continue
            try:
                importlib.import_module(info.name)
            except Exception as e:  # pragma: no cover
                print("preimport failed", info.name, e, file=sys.stderr)
    if os.environ.get("COV_CLASSES", "1") == "1":
        import sqlglot.expressions.core as core
        from sqlglot.dialects.dialect import Dialect
        from sqlglot.generator import Generator
        from sqlglot.parser import Parser
        from sqlglot.tokens import Tokenizer

        _wrap_init(Dialect)
        _wrap_init(Tokenizer, True)
        _wrap_init(Parser, True)
        _wrap_init(Generator, True)
        for name in dir(core):
            obj = getattr(core, name)
            if isinstance(obj, type) and "__init__" in obj.__dict__ and obj.__module__ == core.__name__:
                _wrap_init(obj)


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    _push("collect:" + collector.nodeid)
    try:
        yield
    finally:
        _pop()


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    if fixturedef.scope != "function":
        _push(f"fixture:{fixturedef.scope}:{request.node.nodeid}")
        try:
            yield
        finally:
            _pop()
    else:
        yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _push(item.nodeid)
    t0 = time.perf_counter()
    try:
        yield
    finally:
        _durations[item.nodeid] = time.perf_counter() - t0
        _pop()


def pytest_runtest_logreport(report):
    prev = _outcomes.get(report.nodeid, "passed")
    if report.outcome == "failed" or prev == "failed":
        _outcomes[report.nodeid] = "failed"
    elif report.when == "call" or report.outcome == "skipped":
        _outcomes[report.nodeid] = report.outcome


def pytest_unconfigure(config):
    mon.set_events(TOOL, 0)
    files = {}
    out = {"contexts": {}, "files": [], "outcomes": _outcomes, "durations": _durations}

    def fid(f):
        i = files.get(f)
        if i is None:
            i = files[f] = len(out["files"])
            out["files"].append(f)
        return i

    for ctx, (lines, opened) in _data.items():
        by = {}
        for f, ln in lines:
            by.setdefault(fid(f), []).append(ln)
        out["contexts"][ctx] = {
            "lines": {str(k): sorted(v) for k, v in by.items()},
            "opened": sorted(opened),
            "classes": sorted(_classes.get(ctx, ())),
        }
    with open(os.environ.get("COV_OUT", "/out/cov.json"), "w") as fh:
        json.dump(out, fh)

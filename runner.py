"""Run cheap selected tests against mutated code, one forked child per commit.

The parent imports sqlglot (every module), the needed test modules and their
third-party dependencies once. For each commit a child is forked; it swaps the
__code__ of every function whose body the patch changed (so all existing
references run the mutated code, exactly as if the file had been edited), then
runs the requested unittest test methods and reports which failed.

The selector only sends commits whose edits are confined to function bodies
that do not run at import time; anything else keeps its static selection.

stdin : {"jobs": [{"id": str, "files": {rel: new_source}, "tests": [nodeid, ...]}]}
stdout: {"<id>": {"failed": [nodeid, ...]} | {"error": str}}
"""

import concurrent.futures as cf
import gc
import importlib
import json
import os
import pickle
import pkgutil
import select
import signal
import sys
import types
import unittest

SRC = os.environ.get("BR_SRC", "/app/src")
CHILD_TIMEOUT = float(os.environ.get("BR_CHILD_TIMEOUT", "60"))


class SyncExecutor:
    """In-process ProcessPoolExecutor stand-in that still pickles arguments and results.
    (The whole suite passes with it on the pinned commit.)"""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *a, **k):
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


def iter_code(co):
    yield co
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            yield from iter_code(c)


def code_index(top):
    """qualname -> list of code objects in definition order (skips the module body)."""
    idx = {}
    for co in iter_code(top):
        if co is top:
            continue
        idx.setdefault(co.co_qualname, []).append(co)
    return idx


def swap(files):
    """Give every live function defined in `files` its mutated code object."""
    plan = {}
    for rel, new_src in files.items():
        path = os.path.join(SRC, rel)
        old_src = open(path, encoding="utf-8").read()
        old_idx = code_index(compile(old_src, path, "exec"))
        new_idx = code_index(compile(new_src, path, "exec"))
        if {k: len(v) for k, v in old_idx.items()} != {k: len(v) for k, v in new_idx.items()}:
            raise RuntimeError("function structure changed in " + rel)
        for q, olds in old_idx.items():
            for o, n in zip(olds, new_idx[q]):
                if o.co_freevars != n.co_freevars:
                    raise RuntimeError("closure changed: " + q)
                plan[(path, q, o.co_firstlineno)] = n
    swapped = 0
    import functools

    for obj in gc.get_objects():
        if isinstance(obj, functools._lru_cache_wrapper):
            obj.cache_clear()  # nothing computed with the old code may survive
        elif isinstance(obj, types.FunctionType):
            co = obj.__code__
            n = plan.get((co.co_filename, co.co_qualname, co.co_firstlineno))
            if n is not None:
                obj.__code__ = n
                swapped += 1
    return swapped


def run_tests(nodeids):
    failed = []
    done_setup = {}
    for nid in nodeids:
        parts = nid.split("::")
        if len(parts) != 3:
            failed.append(nid)
            continue
        mod = importlib.import_module(parts[0][:-3].replace("/", "."))
        cls = getattr(mod, parts[1])
        ok = done_setup.get(cls)
        if ok is None:
            try:
                if hasattr(cls, "setUpClass"):
                    cls.setUpClass()
                ok = True
            except BaseException:  # noqa
                ok = False
            done_setup[cls] = ok
        if not ok:
            failed.append(nid)
            continue
        result = unittest.TestResult()
        try:
            cls(parts[2]).run(result)
        except BaseException:  # noqa
            failed.append(nid)
            continue
        if result.failures or result.errors or result.unexpectedSuccesses:
            failed.append(nid)
    return failed


_timed_out = [False]


def child(job, wfd):
    def on_alarm(*a):
        _timed_out[0] = True
        raise TimeoutError("child timeout")

    signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, job.get("timeout", CHILD_TIMEOUT))
    try:
        swap(job["files"])
        out = {"failed": run_tests(job["tests"])}
        if _timed_out[0]:
            out = {"error": "timeout"}  # a hang: keep the static selection
    except BaseException as e:  # noqa
        out = {"error": f"{type(e).__name__}: {e}"[:300]}
    os.write(wfd, json.dumps(out).encode())
    os.close(wfd)
    os._exit(0)


def main():
    req = json.load(sys.stdin)
    jobs = req["jobs"]
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, SRC)
    os.chdir(SRC)
    cf.ProcessPoolExecutor = SyncExecutor
    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name != "sqlglot.__main__":
            try:
                importlib.import_module(info.name)
            except Exception:  # noqa
                pass
    for m in sorted({n.split("::")[0][:-3].replace("/", ".") for j in jobs for n in j["tests"]}):
        try:
            importlib.import_module(m)
        except Exception:  # noqa
            pass
    gc.collect()

    import resource

    ru = resource.getrusage(resource.RUSAGE_SELF)
    print("preload cpu", round(ru.ru_utime + ru.ru_stime, 2), file=sys.stderr)
    results = {}
    for job in jobs:
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            child(job, w)
        os.close(w)
        buf = b""
        deadline = job.get("timeout", CHILD_TIMEOUT) + 3
        while True:
            ready, _, _ = select.select([r], [], [], deadline)
            if not ready:
                os.kill(pid, signal.SIGKILL)
                break
            chunk = os.read(r, 65536)
            if not chunk:
                break
            buf += chunk
        os.close(r)
        _, _, ru = os.wait4(pid, 0)
        print(job["id"], "child cpu", round(ru.ru_utime + ru.ru_stime, 2), len(job["tests"]), file=sys.stderr)
        try:
            results[job["id"]] = json.loads(buf.decode())
        except Exception:  # noqa
            results[job["id"]] = {"error": "child died"}
    sys.stdout.write(json.dumps(results))


if __name__ == "__main__":
    main()

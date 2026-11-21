"""Scaled-down tests/test_generator.py::TestGenerator::test_generator_memory (6.7 s).

Same loop over every dialect as the real test (get_or_raise, warm-up generator()
calls, gc, tracemalloc around repeated generator() calls), with 1/5 of the
iterations and the leak threshold scaled by the same factor. Any exception or a
measured growth over the scaled threshold is reported as DIFF (the selector
then keeps the real test).

usage: python genmem_proxy.py < overrides.json   |   python genmem_proxy.py --plain
"""

import gc
import json
import os
import sys
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WARM, MEASURED, REAL_MEASURED, REAL_LIMIT = 20, 200, 1000, 1024


def body():
    from sqlglot.dialects.dialect import Dialect, Dialects

    limit = REAL_LIMIT * MEASURED / REAL_MEASURED
    for name in Dialects:
        dialect = Dialect.get_or_raise(name)
        for _ in range(WARM):
            dialect.generator()
        gc.collect()
        tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[0]
            for _ in range(MEASURED):
                dialect.generator()
            gc.collect()
            after = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        if after - before >= limit:
            raise AssertionError(f"{name}: {after - before} >= {limit}")


def main():
    if "--plain" not in sys.argv:
        from tpcds_gate import _Override

        ov = json.load(sys.stdin)
        sys.meta_path.insert(0, _Override(ov["sources"], ov["files"]))
    try:
        body()
    except BaseException as e:  # noqa
        print("DIFF", type(e).__name__, str(e)[:200])
        return
    print("SAME")


if __name__ == "__main__":
    main()

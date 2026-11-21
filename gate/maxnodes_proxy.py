"""Scaled-down tests/test_parser.py::TestParser::test_max_nodes (28.8 s).

Same statements and assertions as the real test; the one expensive input
(10000 columns of depth-100 `x=x=...`, limit 1e6 = width*depth) is replaced by
60 columns of the same depth with limit 60*100, the same boundary, so the same parser paths,
node counting and error formatting run. The selector only trusts this proxy for
patches that touch no line (or name read on a line) that only the full-size
test runs (map/proxy_maxnodes.json).

usage: python maxnodes_proxy.py [<baseline-ignored>] < overrides.json  -> SAME | DIFF
       python maxnodes_proxy.py --plain                                  (no overrides)
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def body():
    from sqlglot import exp, parse_one
    from sqlglot.errors import ParseError

    assert isinstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)

    cols = [f"col{i}" for i in range(500)]
    sql = f"SELECT {','.join(cols)} FROM t"
    assert isinstance(parse_one(sql, max_nodes=1_000_000), exp.Select)

    width = 60
    depth = 100
    expr = "x" + "=x" * depth
    sql = f"SELECT {','.join([expr for _ in range(width)])}"

    # the real test sits exactly on the boundary: width * depth == limit (1e6),
    # and the extra root node pushes it over; keep that relation at small scale
    for limit, needle in ((width * depth, "Maximum number of AST nodes"), (5_000, "(5000)")):
        try:
            parse_one(sql, max_nodes=limit)
        except ParseError as e:
            assert needle in str(e), str(e)
        else:
            raise AssertionError("no ParseError")

    try:
        parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
    except ParseError:
        pass
    else:
        raise AssertionError("no ParseError")
    assert isinstance(parse_one("SELECT 1, 2, 3"), exp.Select)


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

"""A sequential, case-filterable twin of tests/test_optimizer.py TestOptimizer.check_file.

The original submits every fixture case to a spawn process pool (arguments and
results are pickled), then checks each result in a subTest: the optimized SQL
must equal the expected SQL and, for "execute" cases, duckdb must return the
same frame for the original and the optimized query. This twin does exactly
the same per case, one case at a time, so that

  * the map builder can record which sqlglot lines each case runs, and
  * the runner can re-check only the cases a patch can reach.

Cases are independent: each is optimized in a fresh pool worker in the real
test, and nothing a case does is visible to another case.
"""

import json
import pickle
import sys

_state = {"allowed": None, "on_case": None}


def install(test_optimizer_module, allowed=None, on_case=None):
    """Replace TestOptimizer.check_file. allowed: {(test_name, file): set(case_numbers)} or
    None for all cases; tests/files missing from `allowed` run every case."""
    _state["allowed"] = allowed
    _state["on_case"] = on_case
    test_optimizer_module.TestOptimizer.check_file = _make(test_optimizer_module)


def _make(mod):
    g = mod.__dict__
    load_sql_fixture_pairs = g["load_sql_fixture_pairs"]
    parse_and_optimize = g["parse_and_optimize"]
    string_to_bool = g["string_to_bool"]
    exp = g["exp"]
    parse_one = g["parse_one"]
    assert_frame_equal = g["assert_frame_equal"]

    def check_file(self, file, func, pretty=False, execute=None, only=None, **kwargs):
        test_name = getattr(self, "_testMethodName", "")
        allowed = _state["allowed"]
        keep = None
        if allowed is not None:
            keep = allowed.get((test_name, file))
        for i, (meta, sql, expected) in enumerate(load_sql_fixture_pairs(f"optimizer/{file}.sql"), start=1):
            title = meta.get("title") or f"{i}, {sql}"
            if only and title != only:
                continue
            if keep is not None and i not in keep:
                continue

            dialect = meta.get("dialect")
            leave_tables_isolated = meta.get("leave_tables_isolated")
            validate_qualify_columns = meta.get("validate_qualify_columns")
            canonicalize_table_aliases = meta.get("canonicalize_table_aliases")
            func_kwargs = kwargs.copy()

            if schema := meta.get("schema"):
                func_kwargs["schema"] = json.loads(schema)

            if leave_tables_isolated is not None:
                func_kwargs["leave_tables_isolated"] = string_to_bool(leave_tables_isolated)

            if validate_qualify_columns is not None:
                func_kwargs["validate_qualify_columns"] = string_to_bool(validate_qualify_columns)
            if dialect:
                func_kwargs["dialect"] = dialect
            if canonicalize_table_aliases is not None:
                func_kwargs["canonicalize_table_aliases"] = string_to_bool(canonicalize_table_aliases)

            fixture_execute = False if execute is False else meta.get("execute", execute)
            if min_python := meta.get("execute_min_python"):
                fixture_execute = string_to_bool(fixture_execute) and sys.version_info >= tuple(
                    map(int, min_python.split("."))
                )

            cb = _state["on_case"]
            if cb:
                cb(test_name, file, i, "start")
            try:
                with self.subTest(title):
                    # what the pool does: pickle the call, run it, pickle the result back
                    args = pickle.loads(pickle.dumps((func, sql, dialect, func_kwargs)))
                    optimized = parse_and_optimize(args[0], args[1], args[2], **args[3])
                    optimized = pickle.loads(pickle.dumps(optimized))
                    actual = optimized.sql(pretty=pretty, dialect=dialect)
                    self.assertEqual(expected, actual)
                    if string_to_bool(fixture_execute):
                        with self.subTest(f"(execute) {title}"):
                            dfs = []
                            for expression in (parse_one(sql, read=dialect), optimized):
                                if not isinstance(expression, (exp.Query, exp.Values)):
                                    expression = exp.select(exp.alias_(expression, "_col_0"))
                                dfs.append(self.conn.execute(expression.sql(dialect="duckdb")).df())
                            assert_frame_equal(*dfs)
            finally:
                if cb:
                    cb(test_name, file, i, "end")

    return check_file

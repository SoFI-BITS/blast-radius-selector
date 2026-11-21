# Blast Radius: coverage-driven test selection for sqlglot

For each patch against sqlglot `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, the
selector prints the tests that can fail and splits them over workers.

The scored container never runs sqlglot's tests. It reads the patches and a
prebuilt map (`map/blast.map`, about 0.65 MB), then answers in well under a CPU second.

## How it decides

1. **Parse and apply the patch** to the vendored pinned sources (`vendor/`).
2. **Semantic no-op check.** The selector compares the old and new AST, but only
   for the statements that overlap the hunks. These count as no change:
   - comments and whitespace,
   - docstrings and other bare string statements (the suite collects no doctests),
   - `is` ↔ `==` against `None` or against an enum member (every sqlglot `__eq__` returns False for `None`).
3. **Map each changed line to an execution unit** in the old AST:
   - Function body statement: the tests whose per-test line coverage hits that
     statement's own lines (the whole span, so short-circuit changes are caught).
     An insertion uses the coverage of the block it lands in.
   - Lambda inside a class/module literal (such as the `FUNCTIONS` dicts): coverage of the lambda.
   - Function header (signature, defaults, decorators): coverage of the function body, which means its callers.
   - Class-body attribute: the tests that read that attribute name, intersected with the
     tests that instantiated the class or a subclass. Readers come from bytecode names on
     covered lines, plus a taint closure through metaclass and `__init_subclass__` code.
   - Module-level statement: readers of the names it binds. A changed import uses the module's users.
   - Code that runs at import time (factories, metaclasses): coverage of the closures it
     creates, plus readers of the attributes it stores.
   - Syntax error, deleted module or renamed module: every test that imports the module,
     using the static import closure. Anything in `import sqlglot`'s closure means all tests.
4. **Fixtures and data files:** every test that opened the file, using an audit hook.
   Class-level `setUpClass` reads count for the whole class.
5. **Config files** (`pyproject.toml`, `setup.cfg`, `conftest.py`, and similar): the full suite.
   Anything the analysis cannot understand falls back to module users or the full suite,
   never to an empty selection.
6. **Scheduling:** LPT over 1–8 workers with a move-improvement pass, picking the worker
   count with the lowest `makespan + 2 s × workers`. Times are integer milliseconds,
   the same as the scorer.

## Building the map (offline, once)

`tools/covplugin.py` is a pytest plugin that records the following for every test:
- line coverage via `sys.monitoring`,
- files opened or listed,
- sqlglot classes instantiated,
- separate contexts for collection, class fixtures, and import time.

It pre-imports every sqlglot module so that import-time lines stay apart from runtime
lines. It also swaps the suite's spawn `ProcessPoolExecutor` for an in-process
executor that still pickles arguments and results, so coverage sees that work too.

```sh
docker build -f tools/Dockerfile.cov -t br-cov .
docker run --rm -v "$PWD/tools:/tools:ro" -v "$PWD/work:/out" -e PYTHONPATH=/src:/tools \
  -e COV_OUT=/out/cov.json br-cov python -m pytest -p covplugin -q -p no:cacheprovider tests
docker run --rm -v "$PWD:/w" br-cov sh -c "cd /w && python tools/build_map.py /src work/cov.json sample/tests.json map/blast.map"
```

Everything in the map comes from the pinned commit. Nothing comes from patches or answers.

## Local evaluation

```sh
make sample                 # official flow: build, run under the scorer's limits, score
python tools/eval.py        # per-commit cost, misses, and the oracle gap
python tools/mutate.py ...  # random validation mutations, labeled by tools/label.sh
```

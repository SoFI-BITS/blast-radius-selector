"""Blast radius selector.

For every patch in /data/commits, decide which sqlglot tests can fail and split
them over workers. Decisions come from a map built from the pinned commit
(per-test line coverage, import-time lines, bytecode name readers, class
instantiations, file reads, import graph) plus static analysis of the patch.
"""

import ast
import json
import os
import re
import sys
import zlib

APP = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("BR_DATA", "/data")
SRC = os.path.abspath(os.environ.get("BR_SRC", os.path.join(APP, "src")))
MAP = os.environ.get("BR_MAP", os.path.join(APP, "map", "blast.map"))
DEBUG = os.environ.get("BR_DEBUG") == "1"
GATE = os.path.join(APP, "gate", "tpcds_gate.py")
GATE_FIXTURE = os.path.abspath(os.environ.get("BR_GATE_FIXTURE", os.path.join(SRC, "tests", "fixtures", "optimizer", "tpc-ds")))
GATE_BASELINE = os.path.abspath(os.environ.get("BR_GATE_BASELINE", os.path.join(APP, "gate", "baseline.json")))
GATE_TEST_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"

WORKER_MS = 2000
MAX_WORKERS = 8

_STDLIB_OK = set(getattr(sys, "stdlib_module_names", ())) | {"typing_extensions"}

CONFIG_FILES = {"pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py", "setup.py"}


def log(*a):
    if DEBUG:
        print(*a, file=sys.stderr)


# ---------------------------------------------------------------- the map
class Map:
    def __init__(self, path):
        d = json.loads(zlib.decompress(open(path, "rb").read()))
        self.d = d
        self.tests = d["tests"]
        self.N = len(self.tests)
        self.ALL = (1 << self.N) - 1
        self._pool = d["pool"]
        self._cache = {}
        self.lines = d["lines"]
        self.import_lines = {f: set(v) for f, v in d["import_lines"].items()}
        self.names = d["names"]
        self.edges = d["edges"]
        self.opened = d["opened"]
        self.classes = d["classes"]
        self.file_rt = d["file_rt"]
        self.module_users_ = d["module_users"]
        self.test_file_bits = d["test_file_bits"]
        self.plain_enums = set(d.get("plain_enums", ()))
        self.defs = d.get("defs", {})
        self.top_names = d.get("top_names", {})
        self.gates = []
        here = os.path.dirname(path)

        def tid(x):
            return self.tests.index(x) if x in self.tests else None

        try:
            epj = json.load(open(os.path.join(here, "exec_phase.json")))
            if os.path.exists(GATE_BASELINE):
                self.gates.append({
                    "name": "tpcds", "bit": tid(GATE_TEST_ID),
                    "lines": {f: set(v) for f, v in epj["tpcds"].items()}, "names": set(epj["tpcds_names"]),
                    "argv": [GATE, GATE_FIXTURE, GATE_BASELINE]})
        except Exception:
            pass
        try:
            pj = json.load(open(os.path.join(here, "proxy_maxnodes.json")))
            self.gates.append({
                "name": "max_nodes", "bit": tid("tests/test_parser.py::TestParser::test_max_nodes"),
                "lines": {f: set(v) for f, v in pj["only_full"].items()}, "names": set(pj["only_full_names"]),
                "argv": [os.path.join(APP, "gate", "maxnodes_proxy.py")], "always": True})
        except Exception:
            pass
        try:
            pj = json.load(open(os.path.join(here, "proxy_genmem.json")))
            self.gates.append({
                "name": "generator_memory", "bit": tid("tests/test_generator.py::TestGenerator::test_generator_memory"),
                "lines": {f: set(v) for f, v in pj["only_full"].items()}, "names": set(pj["only_full_names"]),
                "argv": [os.path.join(APP, "gate", "genmem_proxy.py")], "always": True})
        except Exception:
            pass
        self.tpcds_exec_qcov = None
        self.tpcds_executed = set()
        try:
            self.tpcds_exec_qcov = json.load(open(os.path.join(here, "tpcds_exec_qcov.json")))["lines"]
            base = json.load(open(GATE_BASELINE)) if os.path.exists(GATE_BASELINE) else []
            self.tpcds_executed = {i for i, b in enumerate(base) if b.get("execute")}
        except Exception:
            self.tpcds_exec_qcov = None
        self.attr_reads = {}
        try:
            self.attr_reads = json.load(open(os.path.join(here, "attr_reads.json")))
        except Exception:
            pass
        self.tpcds_qcov = None
        try:
            self.tpcds_qcov = json.load(open(os.path.join(here, "tpcds_qcov.json")))["lines"]
        except Exception:
            pass
        # most expensive test first: a later check only runs if dropping its test still pays
        order = {"max_nodes": 0, "tpcds": 1, "generator_memory": 2}
        self.gates.sort(key=lambda g: order.get(g["name"], 9))
        for g in self.gates:
            g["min_saving"] = {"max_nodes": 1500, "tpcds": 7000, "generator_memory": 2500}.get(g["name"], 2000)

    def bits(self, r):
        if r is None or r < 0:
            return 0
        b = self._cache.get(r)
        if b is None:
            b = self._cache[r] = int(self._pool[r], 16)
        return b

    def line(self, f, ln):
        d = self.lines.get(f)
        if not d:
            return 0
        return self.bits(d.get(str(ln), -1))

    def lines_bits(self, f, lns):
        d = self.lines.get(f)
        if not d:
            return 0
        b = 0
        for ln in lns:
            r = d.get(str(ln))
            if r is not None:
                b |= self.bits(r)
        return b

    def module_users(self, f):
        r = self.module_users_.get(f)
        if r is None:
            return self.ALL
        return self.bits(r)

    def file_tests(self, f):
        return self.bits(self.test_file_bits.get(f, -1))

    def readers(self, names, depth=3):
        seen = set(names)
        frontier = list(names)
        for _ in range(depth):
            nxt = []
            for n in frontier:
                for m in self.edges.get(n, ()):
                    if m not in seen:
                        seen.add(m)
                        nxt.append(m)
            frontier = nxt
        b = 0
        for n in seen:
            b |= self.bits(self.names.get(n, -1))
        return b, seen

    def class_users(self, qual):
        return self.bits(self.classes.get(qual, -1))

    def opened_bits(self, path):
        return self.bits(self.opened.get(path, -1))

    def under_dir(self, d):
        """Tests that listed or opened anything under directory d."""
        b = 0
        pre = d.rstrip("/") + "/"
        for p, r in self.opened.items():
            if p == d.rstrip("/") or p.startswith(pre):
                b |= self.bits(r)
        return b


# ------------------------------------------------------------ patch parse
def _unquote(p):
    p = p.strip()
    if p.startswith('"') and p.endswith('"'):
        try:
            p = ast.literal_eval(p).encode("latin-1").decode("utf-8")
        except Exception:
            p = p[1:-1]
    return p


def _strip_prefix(p):
    if p == "/dev/null":
        return None
    p = p.split("\t")[0]
    p = _unquote(p)
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    if "\r\n" in text:
        text = text.replace("\r\n", "\n")
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        l = lines[i]
        if l.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "new_file": False, "deleted": False,
                   "binary": False, "rename": False, "mode_only": False}
            m = re.match(r'^diff --git (".*?"|\S+) (".*?"|\S+)$', l)
            if m:
                cur["old"] = _strip_prefix(m.group(1))
                cur["new"] = _strip_prefix(m.group(2))
            files.append(cur)
        elif l.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            if cur is None or cur["hunks"]:
                cur = {"old": None, "new": None, "hunks": [], "new_file": False, "deleted": False,
                       "binary": False, "rename": False, "mode_only": False}
                files.append(cur)
            o = _strip_prefix(l[4:])
            nw = _strip_prefix(lines[i + 1][4:])
            cur["old"] = o
            cur["new"] = nw
            if o is None:
                cur["new_file"] = True
            if nw is None:
                cur["deleted"] = True
            i += 2
            continue
        elif l.startswith("@@") and cur is not None:
            m = HUNK_RE.match(l)
            if not m:
                i += 1
                continue
            os_, ol, ns, nl = int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1)
            body = []
            ro, rn = ol, nl
            i += 1
            while i < n and (ro > 0 or rn > 0):
                x = lines[i]
                if x.startswith("\\"):
                    i += 1
                    continue
                tag = x[:1] if x else " "
                if tag == " ":
                    ro -= 1
                    rn -= 1
                elif tag == "-":
                    ro -= 1
                elif tag == "+":
                    rn -= 1
                else:
                    break
                body.append((tag, x[1:]))
                i += 1
            while i < n and lines[i].startswith("\\"):
                i += 1
            cur["hunks"].append((os_, ol, ns, nl, body))
            continue
        elif cur is not None:
            if l.startswith("new file mode"):
                cur["new_file"] = True
            elif l.startswith("deleted file mode"):
                cur["deleted"] = True
            elif l.startswith("rename from "):
                cur["rename"] = True
                cur["old"] = _unquote(l[len("rename from "):])
            elif l.startswith("rename to "):
                cur["rename"] = True
                cur["new"] = _unquote(l[len("rename to "):])
            elif l.startswith("copy from "):
                cur["old"] = _unquote(l[len("copy from "):])
            elif l.startswith("Binary files") or l.startswith("GIT binary patch"):
                cur["binary"] = True
        i += 1
    for f in files:
        if f["new_file"]:
            f["old_path_exists"] = False
    return files


def apply_hunks(old_text, hunks):
    """Return (new_text, changed_old_lines, insert_points, changed_new_lines) or None."""
    old = old_text.split("\n")
    out = []
    pos = 0  # index into old (0-based)
    changed_old = set()
    inserts = set()  # insertion after old line p (1-based), p may be 0
    changed_new = set()
    for os_, ol, ns, nl, body in hunks:
        want = [t for tag, t in body if tag in " -"]
        start = os_ - 1 if ol > 0 else os_
        # locate the hunk, allowing an offset
        found = None
        for off in [0] + [d for k in range(1, 200) for d in (k, -k)]:
            s = start + off
            if s < pos or s < 0 or s + len(want) > len(old):
                continue
            if old[s:s + len(want)] == want:
                found = s
                break
        if found is None:
            return None
        out.extend(old[pos:found])
        oi = found
        for tag, t in body:
            if tag == " ":
                out.append(old[oi])
                oi += 1
            elif tag == "-":
                changed_old.add(oi + 1)
                oi += 1
            else:
                inserts.add(oi)  # after old line oi (1-based index oi)
                out.append(t)
                changed_new.add(len(out))
        pos = oi
    out.extend(old[pos:])
    return "\n".join(out), changed_old, inserts, changed_new


# --------------------------------------------------------------- AST units
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try,
            ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Match)
if hasattr(ast, "TryStar"):
    COMPOUND = COMPOUND + (ast.TryStar,)


def header_range(s):
    """Lines of a statement that belong to it and not to a child block."""
    if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        first = min([d.lineno for d in s.decorator_list] + [s.lineno])
        return first, s.body[0].lineno - 1 if s.body[0].lineno > s.lineno else s.lineno
    if isinstance(s, (ast.Try,) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())):
        return s.lineno, s.lineno
    if isinstance(s, ast.Match):
        return s.lineno, s.cases[0].pattern.lineno - 1 if s.cases else s.end_lineno
    if isinstance(s, COMPOUND):
        b0 = s.body[0].lineno
        return s.lineno, max(s.lineno, b0 - 1) if b0 > s.lineno else s.lineno
    return s.lineno, s.end_lineno or s.lineno


_STMT_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")


class Units:
    """Statement index of a module (parents for statements only: fast)."""

    def __init__(self, tree):
        self.tree = tree
        self.parent = {}
        self.stmts = []
        stack = [tree]
        while stack:
            p = stack.pop()
            for fld in _STMT_FIELDS:
                blk = getattr(p, fld, None)
                if not isinstance(blk, list):
                    continue
                for c in blk:
                    if isinstance(c, (ast.stmt, ast.ExceptHandler, ast.match_case)):
                        self.parent[c] = p
                        if not isinstance(c, ast.match_case):
                            self.stmts.append(c)
                        stack.append(c)

    def innermost_stmt(self, ln):
        best = None
        for s in self.stmts:
            e = s.end_lineno or s.lineno
            if s.lineno <= ln <= e:
                if best is None or (s.lineno >= best.lineno and e <= (best.end_lineno or best.lineno)):
                    best = s
        return best

    def lambda_at(self, ln, stmt):
        """Innermost lambda containing line ln inside stmt (not in nested statements)."""
        if stmt is None:
            return None
        best = None
        todo = [stmt]
        while todo:
            n = todo.pop()
            for c in ast.iter_child_nodes(n):
                if isinstance(c, ast.stmt):
                    continue
                if isinstance(c, ast.Lambda) and c.lineno <= ln <= (c.end_lineno or c.lineno):
                    if best is None or c.lineno >= best.lineno:
                        best = c
                todo.append(c)
        return best

    def scope_of(self, node):
        p = self.parent.get(node)
        while p is not None and not isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            p = self.parent.get(p)
        return p

    def qualname(self, node):
        parts = []
        p = node
        while p is not None and not isinstance(p, ast.Module):
            if isinstance(p, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                parts.append(p.name)
            p = self.parent.get(p)
        return ".".join(reversed(parts))

    def enclosing_function(self, node):
        p = self.parent.get(node)
        while p is not None:
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return p
            p = self.parent.get(p)
        return None


def all_lines(node):
    return range(node.lineno, (node.end_lineno or node.lineno) + 1)


def body_lines(fn):
    if isinstance(fn, ast.Lambda):
        return all_lines(fn)
    return range(fn.body[0].lineno, (fn.end_lineno or fn.lineno) + 1)


def stored_names(s):
    out = set()
    tg = []
    if isinstance(s, ast.Assign):
        tg = s.targets
    elif isinstance(s, (ast.AugAssign, ast.AnnAssign)):
        tg = [s.target]
    elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.add(s.name)
    elif isinstance(s, (ast.Import, ast.ImportFrom)):
        for a in s.names:
            out.add((a.asname or a.name).split(".")[0])
    elif isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
        f = s.value.func
        if isinstance(f, ast.Attribute):
            for n in ast.walk(f.value):
                if isinstance(n, ast.Attribute):
                    out.add(n.attr)
                elif isinstance(n, ast.Name):
                    out.add(n.id)
    for t_ in tg:
        for n in ast.walk(t_):
            if isinstance(n, ast.Attribute):
                out.add(n.attr)
            elif isinstance(n, ast.Name):
                out.add(n.id)
    return out


def _fixture_pairs(text):
    """tests/helpers.py load_sql_fixture_pairs, on a string (raises where it raises)."""
    statements = "\n".join(line for line in text.splitlines() if line and not line.startswith("--")).split(";")
    for i in range(0, len(statements), 2):
        if i + 1 < len(statements):
            sql = statements[i].strip()
            lines = sql.split("\n")
            meta = {}
            j = 0
            while lines[j].startswith("#"):
                key, val = lines[j].split(":", maxsplit=1)
                meta[key.lstrip("#").strip()] = val.strip()
                j += 1
            yield meta, "\n".join(lines[j:]), statements[i + 1].strip()


def mod_name(rel):
    m = rel[:-3].replace("/", ".")
    return m[: -len(".__init__")] if m.endswith(".__init__") else m


def in_type_checking(units, node):
    p = units.parent.get(node)
    while p is not None:
        if isinstance(p, ast.If):
            try:
                if "TYPE_CHECKING" in ast.unparse(p.test):
                    return True
            except Exception:
                pass
        p = units.parent.get(p)
    return False


def _identity_safe(node, plain_enums):
    """True when `x is node` and `x == node` provably agree in sqlglot:
    None (every sqlglot __eq__ returns False for None) or a member of a plain Enum."""
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    if isinstance(node, ast.Attribute) and isinstance(node.value, (ast.Name, ast.Attribute)):
        owner = node.value.attr if isinstance(node.value, ast.Attribute) else node.value.id
        return owner in plain_enums
    return False


class _Normalize(ast.NodeTransformer):
    def __init__(self, plain_enums):
        self.pe = plain_enums

    def visit_Expr(self, node):
        # bare string statements (docstrings) have no runtime effect beyond __doc__,
        # and the suite collects no doctests
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            node.value = ast.Constant(value="")
            return node
        self.generic_visit(node)
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        ops = []
        left = node.left
        for op, right in zip(node.ops, node.comparators):
            if isinstance(op, (ast.Eq, ast.NotEq)) and (
                _identity_safe(right, self.pe) or _identity_safe(left, self.pe)
            ):
                op = ast.Is() if isinstance(op, ast.Eq) else ast.IsNot()
            ops.append(op)
            left = right
        node.ops = ops
        return node


def _span(st):
    lo = st.lineno
    for d in getattr(st, "decorator_list", ()) or ():
        lo = min(lo, d.lineno)
    return lo, st.end_lineno or st.lineno


def _overlaps(st, ranges):
    lo, hi = _span(st)
    return any(lo <= b and hi >= a for a, b in ranges)


_HDR_FIELDS = {
    ast.ClassDef: ("name", "bases", "keywords", "decorator_list", "type_params"),
    ast.FunctionDef: ("name", "args", "returns", "decorator_list", "type_params"),
    ast.AsyncFunctionDef: ("name", "args", "returns", "decorator_list", "type_params"),
}


def region_equal(olds, news, oranges, nranges, pe):
    """Compare two statement lists that only differ inside the given line ranges."""
    o_sel = [x for x in olds if _overlaps(x, oranges)]
    n_sel = [x for x in news if _overlaps(x, nranges)]
    if len(o_sel) != len(n_sel) or len(olds) - len(o_sel) != len(news) - len(n_sel):
        return False
    norm = _Normalize(pe)
    for a, b in zip(o_sel, n_sel):
        if type(a) is not type(b):
            return False
        flds = _HDR_FIELDS.get(type(a))
        if flds and (a.end_lineno - a.lineno) > 40:
            for f in flds:
                va, vb = getattr(a, f, None), getattr(b, f, None)
                if isinstance(va, list):
                    if len(va) != len(vb) or any(ast.dump(norm.visit(x)) != ast.dump(norm.visit(y)) for x, y in zip(va, vb)):
                        return False
                elif isinstance(va, ast.AST) or isinstance(vb, ast.AST):
                    if va is None or vb is None or ast.dump(norm.visit(va)) != ast.dump(norm.visit(vb)):
                        return False
                elif va != vb:
                    return False
            if not region_equal(a.body, b.body, oranges, nranges, pe):
                return False
        else:
            if ast.dump(norm.visit(a)) != ast.dump(norm.visit(b)):
                return False
    return True


def hunk_ranges(hunks):
    o, n = [], []
    for os_, ol, ns, nl, _ in hunks:
        o.append((os_, os_ + max(ol, 1)))
        n.append((ns, ns + max(nl, 1)))
    return o, n


def normalized_dump(tree, plain_enums):
    """Normalizes `tree` in place (only Compare ops change) and dumps it."""
    return ast.dump(_Normalize(plain_enums).visit(tree))


# ------------------------------------------------------------ analysis
class Analyzer:
    def __init__(self, mp):
        self.m = mp
        self._old_cache = {}
        self.cost_of = None  # bits -> scheduled cost in ms, set by main()

    def read_src(self, rel):
        p = os.path.join(SRC, rel)
        try:
            with open(p, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None

    def py_change(self, rel, fp):
        """A change in module M can only reach tests that import M or run its code."""
        bits = self._py_change(rel, fp)
        if rel.startswith("sqlglot/"):
            m = self.m
            mask = m.module_users(rel) | m.under_dir(rel.rsplit("/", 1)[0]) | m.opened_bits("<subprocess>")
            if bits & ~mask:
                log("  masked to module users", bin(bits).count("1"), "->", bin(bits & mask).count("1"))
            bits &= mask
        return bits

    def _py_change(self, rel, fp):
        """Bits for a modification of an existing .py file."""
        m = self.m
        old = self.read_src(rel)
        if old is None:
            log("  missing source", rel)
            return self.unknown_py(rel)
        res = apply_hunks(old, fp["hunks"])
        if res is None:
            log("  patch did not apply", rel)
            return self.unknown_py(rel)
        new, ch_old, ins, ch_new = res
        try:
            new_tree = ast.parse(new)
        except SyntaxError:
            log("  syntax error ->module users", rel)
            self.gate_ok = False
            self.proxy_ok = False
            return m.module_users(rel)
        cached = self._old_cache.get(rel)
        if cached is None:
            old_tree = ast.parse(old)
            cached = self._old_cache[rel] = (old_tree, Units(old_tree))
        old_tree, u = cached
        orng, nrng = hunk_ranges(fp["hunks"])
        if region_equal(old_tree.body, new_tree.body, orng, nrng, m.plain_enums):
            log("  AST unchanged", rel)
            return 0
        un = Units(new_tree)
        self.overrides[mod_name(rel)] = (os.path.join(SRC, rel), new)
        bits = 0
        targets = set(ch_old)
        for p in ins:
            targets.add(p)
            targets.add(p + 1)
        # Only consider lines whose statement actually differs: compare the
        # innermost statements of old and new by dump to skip comment lines.
        new_simple = {}
        for x in un.stmts:
            if not isinstance(x, COMPOUND) and _overlaps(x, nrng):
                new_simple.setdefault(ast.dump(x), []).append(x)
        old_simple = {}
        for x in u.stmts:
            if not isinstance(x, COMPOUND) and _overlaps(x, orng):
                old_simple.setdefault(ast.dump(x), []).append(x)
        same_old = set()
        for dmp, xs in old_simple.items():
            if len(new_simple.get(dmp, ())) == len(xs):
                same_old.update(id(x) for x in xs)
        same_new = set()
        for dmp, xs in new_simple.items():
            if len(old_simple.get(dmp, ())) == len(xs):
                same_new.update(id(x) for x in xs)
        for ln in sorted(targets):
            st = u.innermost_stmt(ln)
            if st is not None and id(st) in same_old and ln in ch_old:
                continue  # statement text changed only in comments/whitespace
            b = self.unit_bits(rel, u, ln, is_insert_neighbor=(ln not in ch_old))
            bits |= b
        # Import-time effects seen from the new tree (e.g. an inserted
        # class attribute): analyse new statements in class/module scope.
        new_lines = new.split("\n")
        for ln in sorted(ch_new):
            txt = new_lines[ln - 1].strip() if 0 < ln <= len(new_lines) else ""
            if not txt or txt.startswith("#"):
                continue  # blank or comment-only added line
            s = un.innermost_stmt(ln)
            if s is None or id(s) in same_new:
                continue
            if isinstance(s, ast.ClassDef) and not (header_range(s)[0] <= ln <= header_range(s)[1]):
                continue  # inside the class body but between statements
            sc = un.scope_of(s)
            fn = un.enclosing_function(s)
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and isinstance(sc, ast.ClassDef)                     and header_range(s)[0] <= ln <= header_range(s)[1]                     and not self._old_has_method(rel, u, un.qualname(sc), s.name):
                # a new or re-declared method may override a parent's method of that name
                self.gate_ok = False
                b = m.bits(m.defs.get(s.name, -1))
                users = m.class_users(self.class_qual(rel, un, sc))
                bits |= (b & users) if users else b
                log("  method def", s.name, bin(b).count("1"), "users", bin(users).count("1"))
            elif fn is None and not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and not un.lambda_at(ln, s):
                bits |= self.static_effect(rel, un, s, sc)
        return bits

    def unknown_py(self, rel):
        self.gate_ok = False
        self.proxy_ok = False
        return self.m.module_users(rel) if rel.startswith("sqlglot/") else self.tests_file_scope(rel)

    def tests_file_scope(self, rel):
        return self.m.module_users(rel) | self.m.file_tests(rel)

    def unit_bits(self, rel, u, ln, is_insert_neighbor=False):
        m = self.m
        s = u.innermost_stmt(ln)
        if s is None:
            # blank/comment line at module level near an insertion
            return 0
        lam = u.lambda_at(ln, s)
        fn = u.enclosing_function(s)
        il = m.import_lines.get(rel, set())
        if lam is not None and fn is None:
            # lambda inside a class/module level statement: runtime part
            b = m.lines_bits(rel, all_lines(lam))
            self.touched.setdefault(rel, set()).update(all_lines(lam))
            if not is_insert_neighbor and ln == lam.lineno:
                b |= self.static_effect(rel, u, s, u.scope_of(s))
            return b
        if fn is not None:
            # statement inside a function body
            lo, hi = header_range(s)
            if lo <= ln <= hi or not isinstance(s, COMPOUND):
                lines = range(lo, hi + 1)
            else:
                # line inside compound but between children (else:, finally:)
                lines = list(range(lo, hi + 1)) + [ln, ln + 1]
            if is_insert_neighbor:
                # insertion: block that holds the neighbour statement
                parent = u.parent.get(s)
                sib = []
                for fld in ("body", "orelse", "finalbody", "handlers"):
                    blk = getattr(parent, fld, None)
                    if isinstance(blk, list) and s in blk:
                        sib = blk
                lines = list(lines)
                for x in sib:
                    a, bb = header_range(x)
                    lines.extend(range(a, bb + 1))
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    lines.extend(body_lines(parent))
            b = m.lines_bits(rel, lines)
            self.touched.setdefault(rel, set()).update(lines)
            if any(x in il for x in lines):
                # runs at import time (factory / metaclass / decorator body)
                self.gate_ok = False
                b |= self.import_exec_effect(rel, u, s, fn)
            if is_insert_neighbor:
                return b
            return b
        # class or module scope statement
        sc = u.scope_of(s)
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lo, hi = header_range(s)
            if lo <= ln <= hi:
                if is_insert_neighbor:
                    return m.lines_bits(rel, [ln])
                # signature/defaults: only callers see it, i.e. whoever runs the body
                self.header_change = True
                self.touched.setdefault(rel, set()).update(body_lines(s))
                if s.decorator_list:
                    self.gate_ok = False
                b = m.lines_bits(rel, body_lines(s))
                b |= self.static_effect(rel, u, s, sc) if any(d for d in s.decorator_list) else 0
                return b
            return 0
        if isinstance(s, ast.ClassDef):
            lo, hi = header_range(s)
            if lo <= ln <= hi and not is_insert_neighbor:
                self.gate_ok = False
                return self.class_scope_users(rel, u, s) | m.lines_bits(rel, all_lines(s))
            return 0
        if is_insert_neighbor:
            return 0
        return self.static_effect(rel, u, s, sc) | m.lines_bits(rel, all_lines(s))

    def _old_has_method(self, rel, u, cls_qual, name):
        for x in u.stmts:
            if isinstance(x, ast.ClassDef) and u.qualname(x) == cls_qual:
                return any(isinstance(y, (ast.FunctionDef, ast.AsyncFunctionDef)) and y.name == name for y in x.body)
        return False

    def import_resolves(self, rel, s):
        """Every module/name the import statement needs exists in the pinned sources."""
        tn = self.m.top_names
        if isinstance(s, ast.Import):
            return all(a.name in tn or not a.name.startswith("sqlglot") and a.name.split(".")[0] in _STDLIB_OK
                       for a in s.names)
        base = s.module or ""
        if s.level:
            pkg = mod_name(rel) if rel.endswith("__init__.py") else mod_name(rel).rpartition(".")[0]
            parts = pkg.split(".")
            if s.level > 1:
                parts = parts[: len(parts) - (s.level - 1)]
            base = ".".join(parts + ([s.module] if s.module else []))
        if base not in tn:
            return base.split(".")[0] in _STDLIB_OK and not base.startswith("sqlglot")
        names = set(tn[base])
        for a in s.names:
            if a.name == "*":
                return False
            if a.name not in names and f"{base}.{a.name}" not in tn:
                return False
        return True

    def class_qual(self, rel, u, cls_node):
        return f"{mod_name(rel)}:{u.qualname(cls_node)}"

    def class_scope_users(self, rel, u, cls_node):
        m = self.m
        q = self.class_qual(rel, u, cls_node)
        b = m.class_users(q)
        # methods of the class
        b |= m.lines_bits(rel, [x for x in all_lines(cls_node)])
        return b

    def import_exec_effect(self, rel, u, s, fn):
        """A statement in a function that runs at import time."""
        m = self.m
        b = 0
        nested = [n for n in ast.walk(fn) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and n is not fn]
        for nd in nested:
            b |= m.lines_bits(rel, body_lines(nd))
        names = stored_names(s) - {"self", "cls", "klass"}
        if names:
            rb, _ = m.readers(names)
            b |= rb
        if not b:
            b = m.module_users(rel)
        return b

    def static_effect(self, rel, u, s, sc):
        """Import-time statement in class or module scope."""
        if isinstance(s, (ast.Import, ast.ImportFrom)) or not rel.startswith("sqlglot/"):
            self.gate_ok = False
        else:
            names = stored_names(s)
            if not names:
                self.gate_ok = False
            else:
                _, seen = self.m.readers(names)
                self.static_names |= seen
        b = self._static_effect(rel, u, s, sc)
        if rel.startswith("sqlglot/") and self.m.module_users(rel) == self.m.ALL and not in_type_checking(u, s):
            # tests that import sqlglot in a fresh subprocess see import-time effects
            b |= self.m.opened_bits("<subprocess>")
        return b

    def _static_effect(self, rel, u, s, sc):
        m = self.m
        if in_type_checking(u, s):
            return 0
        if isinstance(s, (ast.Import, ast.ImportFrom)):
            if rel.startswith("sqlglot/") and self.import_resolves(rel, s):
                # an import only binds a module global: what changes is code in
                # this module that references the bound names
                names = {(a.asname or a.name).split(".")[0] for a in s.names}
                tree = self._old_cache.get(rel, (None,))[0]
                lines = set()
                if tree is not None:
                    for n in ast.walk(tree):
                        if isinstance(n, ast.Name) and n.id in names and isinstance(n.ctx, ast.Load):
                            lines.add(n.lineno)
                        elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in names:
                            lines.add(n.lineno)
                rb = m.lines_bits(rel, lines)
                # module-level uses run at import: their effects are the readers of what they bind
                for st in tree.body if tree is not None else ():
                    if not isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)):
                        if any(isinstance(n, ast.Name) and n.id in names for n in ast.walk(st)):
                            rb |= self._static_effect(rel, u, st, u.scope_of(st)) if u.scope_of(st) is not None else 0
                log("  import (resolvable)", names, bin(rb).count("1"))
                return rb
            return m.module_users(rel) if rel.startswith("sqlglot/") else self.tests_file_scope(rel)
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant):
            return 0  # docstring
        names = stored_names(s)
        if rel.startswith("tests/"):
            b = m.file_tests(rel)
            if isinstance(sc, ast.Module):
                rb, _ = m.readers(names) if names else (0, None)
                b |= rb
                if not names:
                    b |= m.module_users(rel)
            return b
        rb, seen = m.readers(names) if names else (0, set())
        if isinstance(sc, ast.ClassDef):
            users = 0
            p = sc
            while isinstance(p, ast.ClassDef):
                users |= m.class_users(self.class_qual(rel, u, p))
                p = u.scope_of(p)
            if users:
                # direct readers first; the derived-name closure only when the
                # attribute is never read directly by a user of the class
                direct, _ = m.readers(names, depth=0)
                sel = direct & users
                if not sel:
                    sel = rb & users
                log("  class attr", names, "readers", bin(rb).count("1"), "users", bin(users).count("1"), "sel", bin(sel).count("1"))
                return sel if sel else 0
            log("  class attr (no users)", names, bin(rb).count("1"))
            return rb if rb else m.module_users(rel)
        # module scope
        log("  module stmt", names, bin(rb).count("1"))
        if not names:
            return m.module_users(rel)
        return rb

    def file_change(self, fp):
        m = self.m
        old, new = fp.get("old"), fp.get("new")
        plain = old and old == new and not (fp["deleted"] or fp["new_file"] or fp["rename"] or fp["binary"])
        if plain and (old.startswith("tests/fixtures/") or (
                old.startswith("tests/") and old.endswith(".py")
                and old not in ("tests/helpers.py", "tests/__init__.py") and not old.endswith("conftest.py"))):
            # an edit to a fixture or test file beside the code change: it only blocks the
            # checks of tests that read that file (see apply_gate)
            self.side_edits.add(old)
        elif not (plain and old.startswith("sqlglot/") and old.endswith(".py")):
            self.gate_ok = False
            self.proxy_ok = False
        bits = 0
        paths = [p for p in (old, new) if p]
        if not paths:
            return m.ALL
        for p in set(paths):
            base = p.rsplit("/", 1)[-1]
            if base in CONFIG_FILES and (base != "setup.py" or p == "setup.py"):
                log("  config file", p)
                return m.ALL
        if fp["binary"]:
            for p in set(paths):
                bits |= m.opened_bits(p) | m.under_dir(p.rsplit("/", 1)[0]) if p.startswith("tests/") else m.opened_bits(p)
            return bits
        is_py = any(p.endswith(".py") for p in paths)
        deleted = fp["deleted"]
        new_file = fp["new_file"]
        rename = fp["rename"] and old != new
        if is_py and (old or "").startswith(("sqlglot/", "tests/")) or (new or "").startswith(("sqlglot/", "tests/")) and is_py:
            if deleted or rename:
                if old and old.endswith(".py"):
                    bits |= m.module_users(old) if old.startswith("sqlglot/") else self.tests_file_scope(old)
                if rename and new:
                    bits |= m.under_dir(new.rsplit("/", 1)[0])
                if rename and not fp["hunks"] and old and new and old.endswith(".py"):
                    return bits
            if new_file and not (old and self.read_src(old) is not None):
                target = new
                # new module: whoever lists that package, and anything walking it
                bits |= m.under_dir(target.rsplit("/", 1)[0])
                log("  new py file", target, bin(bits).count("1"))
                return bits
            if not deleted and not new_file:
                tgt = old if old and self.read_src(old) is not None else new
                if rename and fp["hunks"]:
                    bits |= self.py_change(old, fp)
                elif tgt and self.read_src(tgt) is not None:
                    bits |= self.py_change(tgt, fp)
                else:
                    bits |= m.module_users(tgt) if (tgt or "").startswith("sqlglot/") else 0
            return bits
        # non-python file
        for p in set(paths):
            b = m.opened_bits(p)
            if new_file or deleted or rename:
                b |= m.under_dir(p.rsplit("/", 1)[0]) if "/" in p else 0
            elif fp["hunks"] and p.endswith(".sql") and p.startswith("tests/fixtures/"):
                b = self.refine_sql_fixture(p, fp, b)
            log("  data file", p, bin(b).count("1"))
            bits |= b
        return bits

    # ------------------------------------------------------------ fixtures
    def refine_sql_fixture(self, p, fp, b):
        """Fixture read in a class's setUpClass: only the tests that use the attribute it is
        stored in can see the change, as long as the class setup still succeeds."""
        m = self.m
        old = self.read_src(p)
        if old is None:
            return b
        res = apply_hunks(old, fp["hunks"])
        if res is None:
            return b
        new = res[0]
        self.fixture_edits[p] = new
        changed = [t for tag, t in (x for h in fp["hunks"] for x in h[4]) if tag in "+-"]
        if changed and all(t.startswith("--") for t in changed):
            log("  fixture: only '--' comment lines changed (the loaders drop them)")
            return 0
        try:
            list(_fixture_pairs(new))  # what setUpClass does: must still load
        except Exception:
            log("  fixture no longer loads: whole consumer classes kept")
            return b
        by_class = {}
        for i, nid in enumerate(m.tests):
            if (b >> i) & 1:
                parts = nid.split("::")
                if len(parts) == 3:
                    by_class.setdefault((parts[0], parts[1]), []).append(i)
        out = b
        for (tfile, cname), idxs in by_class.items():
            all_idx = [i for i, nid in enumerate(m.tests) if nid.startswith(f"{tfile}::{cname}::")]
            if len(idxs) != len(all_idx) or len(all_idx) < 3:
                continue  # not a class-wide (setUpClass) consumer
            users = self.class_attr_users(tfile, cname, p)
            if users is None:
                continue
            keep = 0
            for i in all_idx:
                if m.tests[i].split("::")[2] in users:
                    keep |= 1 << i
            cls_bits = 0
            for i in all_idx:
                cls_bits |= 1 << i
            log("  fixture via", cname, "setUpClass: users", sorted(users))
            out = (out & ~cls_bits) | keep
        return out

    def class_attr_users(self, tfile, cname, p):
        src = self.read_src(tfile)
        if src is None:
            return None
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return None
        cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cname), None)
        if cls is None:
            return None
        methods = {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        setup = methods.get("setUpClass")
        if setup is None:
            return None
        rel = p[len("tests/fixtures/"):]
        attrs = set()
        for n in ast.walk(setup):
            if isinstance(n, ast.Assign):
                consts = [c.value for c in ast.walk(n.value) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
                if any(c and (rel == c or rel.endswith("/" + c) or c.endswith(rel)) for c in consts):
                    for t_ in n.targets:
                        if isinstance(t_, ast.Attribute):
                            attrs.add(t_.attr)
        if not attrs:
            return None  # the file reaches the class some other way: keep all
        measured = self.m.attr_reads.get(f"{tfile}::{cname}")
        if measured is not None:
            # measured on the pinned commit: which tests read each setUpClass attribute
            return {t for a_ in attrs for t in measured.get(a_, ())}

        def refs(fn):
            a = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
            return a

        direct = {name for name, fn in methods.items() if refs(fn) & attrs and name != "setUpClass"}
        users = set(direct)
        changed = True
        while changed:  # tests calling helpers that use the attribute
            changed = False
            for name, fn in methods.items():
                if name not in users and refs(fn) & users:
                    users.add(name)
                    changed = True
        return {u for u in users if u.startswith("test")}

    def commit(self, text):
        self.gate_ok = True
        self.proxy_ok = True
        self.header_change = False
        self.fixture_edits = {}
        self.side_edits = set()
        self.touched = {}
        self.overrides = {}
        self.static_names = set()
        bits = self._commit(text)
        return self.apply_gate(bits)

    @property
    def swap_ok(self):
        """Every edit is inside bodies of functions that never run at import time:
        swapping their __code__ in a preloaded process reproduces the patch exactly."""
        return bool(self.overrides) and self.gate_ok and self.proxy_ok and not self.header_change \
            and not self.static_names and not self.side_edits

    def apply_gate(self, bits):
        """Drop an expensive selected test when a faithful check proves it unaffected."""
        tpcds_fixture = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"
        if not self.overrides and set(self.fixture_edits) == {tpcds_fixture} and self.only_fixture_edits:
            g = next((x for x in self.m.gates if x["name"] == "tpcds"), None)
            if g is not None and g["bit"] is not None and (bits >> g["bit"]) & 1:
                verdict = self.run_gate(g, {"sources": {}, "files": {}, "fixture": self.fixture_edits[tpcds_fixture]})
                log("  gate tpcds (fixture edit) verdict", verdict[:100])
                if verdict == "SAME":
                    bits &= ~(1 << g["bit"])
            return bits
        if not self.overrides:
            return bits
        payload = None
        for g in self.m.gates:
            t = g["bit"]
            if t is None or not (bits >> t) & 1:
                continue
            tid_ = self.m.tests[t]
            if self.side_edits and g["name"] != "tpcds":
                continue  # side-edit commits: only the 21 s TPC-DS check is worth its CPU
            if any(((self.m.opened_bits(p) >> t) & 1) or tid_.startswith(p + "::") for p in self.side_edits):
                log("  gate", g["name"], ": a side edit is read by the real test")
                continue
            if self.cost_of is not None:
                saving = self.cost_of(bits) - self.cost_of(bits & ~(1 << t))
                if saving < g.get("min_saving", 1500):
                    log("  gate", g["name"], ": dropping it would save only", saving, "ms")
                    continue
            if g.get("always"):
                # scaled replica of the real test, run on the patched code: trusted for
                # any patch made only of edits to existing, parseable sqlglot modules
                if not self.proxy_ok:
                    continue
            elif not self.gate_ok:
                continue
            else:
                hit = self.static_names & g["names"]
                if hit:
                    log("  gate", g["name"], ": names read only by the real test", sorted(hit)[:5])
                    continue
                if any(g["lines"].get(rel) and not g["lines"][rel].isdisjoint(lines) for rel, lines in self.touched.items()):
                    log("  gate", g["name"], ": touches lines only the real test runs")
                    if g["name"] == "tpcds":
                        bits = self.tpcds_partial_replica(g, bits)
                    continue
            req = {
                "sources": {k: v[1] for k, v in self.overrides.items()},
                "files": {k: v[0] for k, v in self.overrides.items()},
            }
            if g["name"] == "tpcds" and self.m.tpcds_qcov is not None and not self.static_names:
                # function-body edits: only queries that run a changed line can change
                mask = 0
                for rel, lines in self.touched.items():
                    d = self.m.tpcds_qcov.get(rel)
                    if d:
                        for ln in lines:
                            mask |= d.get(str(ln), 0)
                req["only"] = [i for i in range(mask.bit_length()) if (mask >> i) & 1]
                log("  gate tpcds: queries to check", len(req["only"]))
                if not req["only"]:
                    bits &= ~(1 << t)
                    continue
            payload = json.dumps(req)
            try:
                import subprocess

                env = {"PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0",
                       "PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp")}
                r = subprocess.run([sys.executable, "-S"] + g["argv"], input=payload, capture_output=True,
                                   text=True, env=env, timeout=90, cwd=SRC)
                out = r.stdout.strip().splitlines()
                verdict = out[-1] if out else "ERR"
            except Exception as e:  # never drop a test on a gate problem
                log("  gate error", g["name"], repr(e))
                continue
            log("  gate", g["name"], "verdict", verdict[:120])
            if verdict == "SAME":
                bits &= ~(1 << t)
        return bits

    def tpcds_partial_replica(self, g, bits):
        """Executor-phase lines touched: run the exact test logic on the patched code, but
        only for the queries that run a touched line (parse/optimize or execution)."""
        m = self.m
        if m.tpcds_qcov is None or m.tpcds_exec_qcov is None or self.cost_of is None:
            return bits
        t = g["bit"]
        saving = self.cost_of(bits) - self.cost_of(bits & ~(1 << t))
        mask = 0
        for rel, lines in self.touched.items():
            pre = m.tpcds_qcov.get(rel, {})
            exe = m.tpcds_exec_qcov.get(rel, {})
            ex_phase = g["lines"].get(rel, set())
            for ln in lines:
                mask |= pre.get(str(ln), 0) | exe.get(str(ln), 0)
                if ln in ex_phase and str(ln) not in exe:
                    mask = (1 << 99) - 1  # table loading / shared setup: every query
        only = [i for i in range(mask.bit_length()) if (mask >> i) & 1]
        executed = sum(1 for i in only if i in m.tpcds_executed)
        est = 1500 + 350 * executed + 20 * len(only)
        log("  gate tpcds partial replica:", len(only), "queries,", executed, "executed, est", est, "ms, saving", saving)
        if not only:
            return bits & ~(1 << t)
        if est > 0.6 * saving:
            return bits
        verdict = self.run_gate(g, {"sources": {k: v[1] for k, v in self.overrides.items()},
                                    "files": {k: v[0] for k, v in self.overrides.items()},
                                    "full": True, "only": only})
        log("  gate tpcds partial replica verdict", verdict[:60])
        return bits & ~(1 << t) if verdict == "SAME" else bits

    def run_gate(self, g, req):
        import subprocess

        env = {"PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0",
               "PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp")}
        try:
            r = subprocess.run([sys.executable, "-S"] + g["argv"], input=json.dumps(req), capture_output=True,
                               text=True, env=env, timeout=90, cwd=SRC)
            out = r.stdout.strip().splitlines()
            return out[-1] if out else "ERR"
        except Exception as e:
            return "ERR " + repr(e)

    def _commit(self, text):
        files = parse_patch(text)
        self.only_fixture_edits = all(
            (f.get("old") or "").startswith("tests/fixtures/") and f.get("old") == f.get("new")
            and not (f["deleted"] or f["new_file"] or f["rename"] or f["binary"]) for f in files) if files else False
        if not files:
            log("  no files parsed")
            return 0 if not text.strip() else self.m.ALL
        bits = 0
        for fp in files:
            log(" file", fp.get("old"), "->", fp.get("new"))
            try:
                bits |= self.file_change(fp)
            except Exception as e:  # never let a parse problem become an empty selection
                log("  error", repr(e))
                self.gate_ok = self.proxy_ok = False
                p = fp.get("new") or fp.get("old") or ""
                bits |= self.m.module_users(p) if p.startswith("sqlglot/") and p.endswith(".py") else self.m.ALL
        return bits


# ------------------------------------------------------------- scheduling
def schedule(items):
    """items: list of (ms, number). Returns best list of workers."""
    if not items:
        return []
    items = sorted(items, key=lambda x: (-x[0], x[1]))
    total = sum(d for d, _ in items)
    best = None
    for k in range(1, MAX_WORKERS + 1):
        if k > len(items):
            break
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for d, num in items:
            j = min(range(k), key=loads.__getitem__)
            loads[j] += d
            ws[j].append(num)
        # local improvement: move/swap from the busiest worker
        for _ in range(200):
            hi = max(range(k), key=loads.__getitem__)
            lo = min(range(k), key=loads.__getitem__)
            if hi == lo:
                break
            gap = loads[hi] - loads[lo]
            moved = False
            # best single move
            cand = None
            for idx, num in enumerate(ws[hi]):
                d = dur_of[num]
                if 0 < d < gap:
                    newmax = max(loads[hi] - d, loads[lo] + d)
                    if newmax < loads[hi] and (cand is None or newmax < cand[0]):
                        cand = (newmax, idx)
            if cand is not None:
                num = ws[hi].pop(cand[1])
                ws[lo].append(num)
                loads[hi] -= dur_of[num]
                loads[lo] += dur_of[num]
                moved = True
            if not moved:
                break
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, [w for w in ws if w])
        if max(loads) <= items[0][0]:
            break  # adding workers cannot reduce the makespan below the longest test
    return best[1]


dur_of = {}


RUN_TESTS = os.environ.get("BR_RUN", "1") == "1"
RUN_MAX_TEST_MS = 1500
RUN_BUDGET_MS = 4000
RUN_SKIP_FILES = ("tests/test_executor.py", "tests/test_sqlglotc.py", "tests/test_build.py")


def run_jobs(jobs):
    import subprocess

    env = {"PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0", "BR_SRC": SRC,
           "PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"), "SKIP_INTEGRATION": "0"}
    try:
        r = subprocess.run([sys.executable, os.path.join(APP, "runner.py")], input=json.dumps({"jobs": jobs}),
                           capture_output=True, text=True, env=env, timeout=150, cwd=SRC)
        if DEBUG and r.stderr:
            log("runner stderr:", r.stderr[-800:])
        return json.loads(r.stdout) if r.stdout.strip() else {}
    except Exception as e:  # any failure: keep every static selection
        log("runner failed", repr(e))
        return {}


def main():
    t_json = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    for t in t_json:
        dur_of[t["number"]] = int(t["seconds"] * 1000 + 0.5)
    mp = Map(MAP)
    num_by_id = {t["id"]: t["number"] for t in t_json}
    map_to_num = [num_by_id.get(n) for n in mp.tests]
    unknown = [t["number"] for t in t_json if t["id"] not in set(mp.tests)]
    file_of = {t["number"]: t["file"] for t in t_json}
    an = Analyzer(mp)

    def cost_of(bits):
        nums = []
        i = 0
        b = bits
        while b:
            if b & 1 and map_to_num[i] is not None:
                nums.append(map_to_num[i])
            b >>= 1
            i += 1
        ws = schedule([(dur_of[x], x) for x in nums])
        return (max(sum(dur_of[x] for x in w) for w in ws) + WORKER_MS * len(ws)) if ws else 0

    an.cost_of = cost_of

    def cost_of_nums(nums):
        ws = schedule([(dur_of[x], x) for x in nums])
        return (max(sum(dur_of[x] for x in w) for w in ws) + WORKER_MS * len(ws)) if ws else 0
    cdir = os.path.join(DATA, "commits")
    test_id_of = {t["number"]: t["id"] for t in t_json}
    num_of_id = {t["id"]: t["number"] for t in t_json}
    decided = []
    jobs = []
    expected_net = [0.0]
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".patch"):
            continue
        cid = name[: -len(".patch")]
        try:
            with open(os.path.join(cdir, name), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            log("commit", cid)
            bits = an.commit(text)
        except Exception as e:
            log("commit failed", cid, repr(e))
            bits = mp.ALL
            an.overrides = {}
            an.gate_ok = False
        nums = set()
        b = bits
        i = 0
        while b:
            if b & 1:
                nm = map_to_num[i]
                if nm is not None:
                    nums.add(nm)
            b >>= 1
            i += 1
        if bits == mp.ALL:
            nums.update(unknown)
        elif unknown and nums:
            files = {file_of[x] for x in nums}
            nums.update(x for x in unknown if file_of[x] in files)
        run = set()
        mode = None
        try:
            if RUN_TESTS and nums and an.overrides and an.proxy_ok and not an.side_edits:
                mode = "swap" if an.swap_ok else "reimport"
                overhead = 150 if mode == "swap" else 300
                cand = {x for x in nums if dur_of[x] <= RUN_MAX_TEST_MS and not file_of[x].startswith(RUN_SKIP_FILES)}
                spend = 1.2 * sum(dur_of[x] for x in cand) + overhead
                gain = cost_of_nums(nums) - cost_of_nums(nums - cand)
                # run when it pays even if only ~60% of such checks find nothing failing
                if cand and (spend < 0.8 * gain or os.environ.get("BR_FORCE_RUN") == "1"):
                    run = cand
                    expected_net[0] += 0.8 * gain - spend
        except Exception:
            run = set()
        if run:
            jobs.append({"id": cid, "mode": mode,
                         "files": {os.path.relpath(v[0], SRC).replace(os.sep, "/"): v[1] for v in an.overrides.values()},
                         "tests": sorted(test_id_of[x] for x in run),
                         "timeout": max(3.0, 4 * sum(dur_of[x] for x in run) / 1000 + 1)})
            log("  runtime check of", len(run), "tests")
        decided.append((cid, nums, run))
    # one preload (~1.5 s CPU) serves the whole batch: only worth it if the batch gains more
    if jobs and expected_net[0] < 2500 and os.environ.get("BR_FORCE_RUN") != "1":
        log("runner skipped: expected net gain", round(expected_net[0]))
        jobs = []
        decided = [(c, n, set()) for c, n, _ in decided]
    results = run_jobs(jobs) if jobs else {}
    out = []
    for cid, nums, run in decided:
        if run:
            res = results.get(cid) or {"error": "no result"}
            if "failed" in res:
                failed = {num_of_id[n] for n in res["failed"] if n in num_of_id}
                nums = (nums - run) | failed
                log(cid, "runtime:", len(run), "run,", len(failed), "failed")
            else:
                log(cid, "runtime error, keeping static selection:", res.get("error"))
        ws = schedule([(dur_of[x], x) for x in nums])
        out.append(json.dumps({"commit": cid, "workers": ws}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))


if __name__ == "__main__":
    main()

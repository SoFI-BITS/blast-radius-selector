"""Build the committed impact map from a coverage run of the pinned sqlglot.

usage: python build_map.py <sqlglot_src> <cov.json> <tests.json> <out.map>

Everything here is derived from the pinned commit only (coverage, imports,
bytecode names, file reads). Nothing is derived from any patch or answer.
"""

import ast
import dis
import json
import os
import sys
import types
import zlib
from collections import defaultdict

SRC, COV, TESTS, OUT = sys.argv[1:5]

cov = json.load(open(COV))
cfiles = cov["files"]
C = cov["contexts"]
tests = [t["id"] for t in json.load(open(TESTS))["tests"]]
tid = {n: i for i, n in enumerate(tests)}
N = len(tests)
ALL = (1 << N) - 1


def prefix_bits(node):
    if node in tid:
        return 1 << tid[node]
    b = 0
    for n, i in tid.items():
        if node == "" or n.startswith(node + "::") or n.startswith(node + "/"):
            b |= 1 << i
    return b


def ctx_bits(ctx):
    if ctx in tid:
        return 1 << tid[ctx]
    if ctx.startswith("collect:"):
        return prefix_bits(ctx[len("collect:"):])
    if ctx.startswith("fixture:"):
        _, _scope, node = ctx.split(":", 2)
        return prefix_bits(node)
    return None  # <session>: import time


line_bits = defaultdict(int)  # (file, line) -> runtime test bits
import_lines = defaultdict(set)  # file -> lines run at import (session)
opened_bits = defaultdict(int)  # repo path -> bits
class_bits = defaultdict(int)  # "module:qualname" -> bits
file_rt_bits = defaultdict(int)  # file -> bits of tests running any line in it

for ctx, d in C.items():
    b = ctx_bits(ctx)
    for fidx, lines in d["lines"].items():
        f = cfiles[int(fidx)]
        if b is None:
            import_lines[f].update(lines)
            continue
        for ln in lines:
            line_bits[(f, ln)] |= b
        file_rt_bits[f] |= b
    if b is None:
        continue
    for p in d["opened"]:
        opened_bits[p] |= b
    for c in d.get("classes", ()):
        class_bits[c] |= b

# ---------------------------------------------------------------- names
GENERIC = {
    "self", "cls", "klass", "t", "exp", "k", "v", "key", "value", "i", "x", "e", "_", "args",
    "kwargs", "super", "isinstance", "len", "getattr", "setattr", "hasattr", "list", "dict",
    "set", "tuple", "str", "int", "bool", "type", "items", "get", "append", "update", "name",
    "this", "expression", "expressions", "copy", "format", "join", "split", "lower", "upper",
}

LOAD_OPS = {
    "LOAD_ATTR", "LOAD_GLOBAL", "LOAD_NAME", "LOAD_METHOD", "LOAD_DEREF", "LOAD_SUPER_ATTR",
    "IMPORT_FROM", "IMPORT_NAME", "LOAD_FROM_DICT_OR_GLOBALS", "LOAD_FROM_DICT_OR_DEREF",
    "LOAD_CLASSDEREF",
}


def iter_code(co):
    yield co
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            yield from iter_code(c)


names_bits = defaultdict(int)
py_files = []
for root in ("sqlglot", "tests"):
    for dp, dn, fn in os.walk(os.path.join(SRC, root)):
        dn[:] = [d for d in dn if d not in ("__pycache__", "fixtures")]
        for f in fn:
            if f.endswith(".py"):
                py_files.append(os.path.relpath(os.path.join(dp, f), SRC).replace(os.sep, "/"))
py_files.sort()

sources = {}
for rel in py_files:
    src = open(os.path.join(SRC, rel), encoding="utf-8").read()
    sources[rel] = src
    try:
        top = compile(src, rel, "exec")
    except SyntaxError:
        continue
    for co in iter_code(top):
        per_line = defaultdict(set)
        for ins in dis.get_instructions(co):
            ln = ins.positions.lineno if ins.positions else None
            if ln is None:
                continue
            if ins.opname in LOAD_OPS and isinstance(ins.argval, str):
                per_line[ln].add(ins.argval.split(".")[-1])
                if ins.opname == "IMPORT_NAME":
                    per_line[ln].add(ins.argval)
            elif ins.opname == "LOAD_CONST" and isinstance(ins.argval, str) and ins.argval.isidentifier():
                per_line[ln].add(ins.argval)
        for ln, ns in per_line.items():
            b = line_bits.get((rel, ln), 0)
            if b:
                for n in ns:
                    names_bits[n] |= b

# ------------------------------------------------- derived attribute edges
# Inside code that runs at import (metaclasses, __init_subclass__, class
# bodies), record which attribute names flow into which stored attributes.


def loaded_names(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
            out.add(n.value)
    return out


def store_names(target):
    out = set()
    for n in ast.walk(target):
        if isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Name):
            out.add(n.id)
    return out


edges = defaultdict(set)


def taint_function(fn_node):
    """Within one function: name -> set of attribute names it flows into."""
    local = defaultdict(set)  # local/attr name -> sources

    def visit(stmts, ctrl):
        for s in stmts:
            if isinstance(s, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                tg = s.targets if isinstance(s, ast.Assign) else [s.target]
                src = loaded_names(s.value) if s.value is not None else set()
                src |= ctrl
                for t_ in tg:
                    for nm in store_names(t_):
                        local[nm] |= src
                        if isinstance(t_, ast.Subscript):
                            local[nm] |= loaded_names(t_.slice)
            elif isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
                f = s.value.func
                if isinstance(f, ast.Attribute):
                    obj = f.value
                    tgt = store_names(obj)
                    src = set()
                    for a in s.value.args:
                        src |= loaded_names(a)
                    for kw in s.value.keywords:
                        src |= loaded_names(kw.value)
                    src |= ctrl
                    for nm in tgt:
                        local[nm] |= src
                if isinstance(f, ast.Name) and f.id == "setattr" and len(s.value.args) >= 3:
                    a1 = s.value.args[1]
                    src = loaded_names(s.value.args[2]) | ctrl
                    if isinstance(a1, ast.Constant) and isinstance(a1.value, str):
                        local[a1.value] |= src
            elif isinstance(s, (ast.For, ast.AsyncFor)):
                src = loaded_names(s.iter) | ctrl
                for nm in store_names(s.target):
                    local[nm] |= src
                visit(s.body, src)
                visit(s.orelse, ctrl)
            elif isinstance(s, (ast.If, ast.While)):
                c2 = ctrl | loaded_names(s.test)
                visit(s.body, c2)
                visit(s.orelse, c2)
            elif isinstance(s, (ast.With, ast.AsyncWith)):
                visit(s.body, ctrl)
            elif isinstance(s, ast.Try):
                visit(s.body, ctrl)
                for h in s.handlers:
                    visit(h.body, ctrl)
                visit(s.orelse, ctrl)
                visit(s.finalbody, ctrl)
            elif isinstance(s, ast.Return) and s.value is not None:
                local["<return>"] |= loaded_names(s.value) | ctrl

    visit(fn_node.body, set())
    # transitive closure inside the function
    changed = True
    it = 0
    while changed and it < 6:
        changed = False
        it += 1
        for nm, srcs in list(local.items()):
            extra = set()
            for s_ in srcs:
                if s_ in local and s_ != nm:
                    extra |= local[s_]
            if not extra <= srcs:
                srcs |= extra
                changed = True
    for nm, srcs in local.items():
        for s_ in srcs:
            if s_ != nm and s_ not in GENERIC:
                edges[s_].add(nm)


for rel, src in sources.items():
    if not rel.startswith("sqlglot/"):
        continue
    try:
        tree = ast.parse(src)
    except SyntaxError:
        continue
    il = import_lines.get(rel, set())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body_lines = range(node.body[0].lineno, (node.end_lineno or node.lineno) + 1)
            if any(ln in il for ln in body_lines):
                taint_function(node)
        elif isinstance(node, ast.ClassDef):
            # class-body assignments deriving from other class attributes
            fake = ast.FunctionDef(name="<cls>", args=None, body=node.body, decorator_list=[])
            taint_function(fake)

# --------------------------------------------------------- module users
def mod_of(rel):
    m = rel[:-3].replace("/", ".")
    return m[: -len(".__init__")] if m.endswith(".__init__") else m


modules = {mod_of(r): r for r in py_files}


def top_imports(rel):
    """Module-level imports (not under TYPE_CHECKING, not inside functions)."""
    try:
        tree = ast.parse(sources[rel])
    except SyntaxError:
        return set()
    out = set()
    pkg = mod_of(rel) if rel.endswith("__init__.py") else mod_of(rel).rpartition(".")[0]

    def resolve(level, module):
        if level:
            base = pkg.split(".")
            base = base[: len(base) - (level - 1)] if level > 1 else base
            return ".".join(base + ([module] if module else []))
        return module

    def walk(stmts):
        for s in stmts:
            if isinstance(s, ast.Import):
                for a in s.names:
                    parts = a.name.split(".")
                    for k in range(1, len(parts) + 1):
                        out.add(".".join(parts[:k]))
            elif isinstance(s, ast.ImportFrom):
                base = resolve(s.level, s.module)
                if base:
                    parts = base.split(".")
                    for k in range(1, len(parts) + 1):
                        out.add(".".join(parts[:k]))
                    for a in s.names:
                        out.add(base + "." + a.name)
            elif isinstance(s, ast.If):
                tst = ast.unparse(s.test)
                if "TYPE_CHECKING" in tst:
                    walk(s.orelse)
                else:
                    walk(s.body)
                    walk(s.orelse)
            elif isinstance(s, ast.Try):
                walk(s.body)
                for h in s.handlers:
                    walk(h.body)
                walk(s.orelse)
                walk(s.finalbody)
            elif isinstance(s, ast.ClassDef):
                walk(s.body)

    walk(tree.body)
    return {m for m in out if m in modules}


imp_graph = {m: top_imports(r) for m, r in modules.items()}
rev = defaultdict(set)
for m, deps in imp_graph.items():
    for d in deps:
        rev[d].add(m)


def importers(m):
    seen = {m}
    stack = [m]
    while stack:
        x = stack.pop()
        for y in rev.get(x, ()):
            if y not in seen:
                seen.add(y)
                stack.append(y)
    return seen


sqlglot_closure = set()
stack = ["sqlglot"]
while stack:
    x = stack.pop()
    if x in sqlglot_closure:
        continue
    sqlglot_closure.add(x)
    stack.extend(imp_graph.get(x, ()))

test_file_bits = defaultdict(int)
for n, i in tid.items():
    test_file_bits[n.split("::")[0]] |= 1 << i

module_users = {}
for m, rel in modules.items():
    if not rel.startswith("sqlglot/"):
        continue
    if m in sqlglot_closure:
        module_users[rel] = ALL
        continue
    b = 0
    for imp in importers(m):
        r2 = modules[imp]
        if r2.startswith("tests/"):
            b |= test_file_bits.get(r2, 0)
        b |= file_rt_bits.get(r2, 0)
        mod2 = imp
        for cname, cb in class_bits.items():
            if cname.split(":")[0] == mod2:
                b |= cb
    module_users[rel] = b
for rel in py_files:
    if rel.startswith("tests/"):
        m = mod_of(rel)
        b = 0
        for imp in importers(m):
            b |= test_file_bits.get(modules[imp], 0)
        module_users[rel] = b

# ------------------------------------- method name -> tests running it
# (a method added to a subclass can override any same-named method)
defs_bits = defaultdict(int)
top_names = {}  # module -> top-level names it defines (for import checks)
for rel, src in sources.items():
    try:
        tree = ast.parse(src)
    except SyntaxError:
        continue
    names = set()
    for st in tree.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(st.name)
        elif isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for t_ in (st.targets if isinstance(st, ast.Assign) else [st.target]):
                for n in ast.walk(t_):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
        elif isinstance(st, (ast.Import, ast.ImportFrom)):
            for a in st.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(st, (ast.If, ast.Try)):
            for n in ast.walk(st):
                if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                    names.add(n.name)
                elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    names.add(n.id)
                elif isinstance(n, ast.alias):
                    names.add((n.asname or n.name).split(".")[0])
    top_names[mod_of(rel)] = sorted(names)
    if not rel.startswith("sqlglot/"):
        continue
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            b = 0
            for ln in range(node.body[0].lineno, (node.end_lineno or node.lineno) + 1):
                b |= line_bits.get((rel, ln), 0)
            defs_bits[node.name] |= b

# ------------------------------------------------- plain (non-mixin) enums
enum_bases = {}
for rel, src in sources.items():
    if not rel.startswith("sqlglot/"):
        continue
    try:
        tree = ast.parse(src)
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            enum_bases[node.name] = [ast.unparse(b).split(".")[-1] for b in node.bases]


def enum_kind(name, seen=()):
    if name in ("Enum", "Flag"):
        return "plain"
    if name in ("IntEnum", "StrEnum", "IntFlag", "str", "int", "float", "bytes"):
        return "mixin"
    if name in seen or name not in enum_bases:
        return None
    kinds = {enum_kind(b, seen + (name,)) for b in enum_bases[name]}
    if "mixin" in kinds:
        return "mixin"
    return "plain" if "plain" in kinds else None


# identity comparisons against members of any sqlglot enum: the compared
# value is always an enum member in sqlglot code, so `is` and `==` agree.
plain_enums = sorted(n for n in enum_bases if enum_kind(n) in ("plain", "mixin"))
print("plain enums", plain_enums, file=sys.stderr)

# ------------------------------------------------------------- serialize
pool = {}
pool_list = []


def ref(b):
    if not b:
        return -1
    r = pool.get(b)
    if r is None:
        r = pool[b] = len(pool_list)
        pool_list.append(b)
    return r


lines_out = defaultdict(dict)
for (f, ln), b in line_bits.items():
    lines_out[f][ln] = ref(b)

out = {
    "tests": tests,
    "lines": {f: {str(k): v for k, v in d.items()} for f, d in lines_out.items()},
    "import_lines": {f: sorted(v) for f, v in import_lines.items()},
    "names": {n: ref(b) for n, b in names_bits.items()},
    "edges": {k: sorted(v) for k, v in edges.items()},
    "opened": {p: ref(b) for p, b in opened_bits.items()},
    "classes": {c: ref(b) for c, b in class_bits.items()},
    "file_rt": {f: ref(b) for f, b in file_rt_bits.items()},
    "module_users": {f: ref(b) for f, b in module_users.items()},
    "test_file_bits": {f: ref(b) for f, b in test_file_bits.items()},
    "plain_enums": plain_enums,
    "defs": {n: ref(b) for n, b in defs_bits.items()},
    "top_names": top_names,
    "pool": [format(b, "x") for b in pool_list],
}
raw = json.dumps(out, separators=(",", ":")).encode()
open(OUT, "wb").write(zlib.compress(raw, 9))
print("pool", len(pool_list), "raw MB", len(raw) / 1e6, "out MB", os.path.getsize(OUT) / 1e6, file=sys.stderr)
print("closure", len(sqlglot_closure), "edges", len(edges), "names", len(names_bits), file=sys.stderr)

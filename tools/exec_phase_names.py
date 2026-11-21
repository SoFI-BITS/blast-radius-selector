"""Add "tpcds_names" (names read on executor-phase lines) to map/exec_phase.json.

usage: python tools/exec_phase_names.py <sqlglot_src> map/exec_phase.json
"""

import dis
import json
import sys
import types

SRC, PATH = sys.argv[1].rstrip("/") + "/", sys.argv[2]
LOAD = {"LOAD_ATTR", "LOAD_GLOBAL", "LOAD_NAME", "LOAD_METHOD", "LOAD_DEREF", "LOAD_SUPER_ATTR",
        "IMPORT_FROM", "LOAD_FROM_DICT_OR_GLOBALS", "LOAD_FROM_DICT_OR_DEREF"}


def iter_code(co):
    yield co
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            yield from iter_code(c)


ep = json.load(open(PATH))
names = set()
for f, lines in ep["tpcds"].items():
    wanted = set(lines)
    for c in iter_code(compile(open(SRC + f, encoding="utf-8").read(), f, "exec")):
        for ins in dis.get_instructions(c):
            ln = ins.positions.lineno if ins.positions else None
            if ln in wanted and isinstance(ins.argval, str) and (
                ins.opname in LOAD or (ins.opname == "LOAD_CONST" and ins.argval.isidentifier())
            ):
                names.add(ins.argval.split(".")[-1])
ep["tpcds_names"] = sorted(names)
json.dump(ep, open(PATH, "w"))
print(len(names), "names")

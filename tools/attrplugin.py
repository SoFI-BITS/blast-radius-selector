"""pytest plugin: which class attributes set in setUpClass does each test read?

Every attribute a unittest class's setUpClass stores is replaced by a data
descriptor that records reads per running test. Output ($ATTR_OUT):
{"<file>::<Class>": {"<attr>": ["<test name>", ...]}}
"""

import json
import os

import pytest

_cur = [None]
_reads = {}


class _Recorder:
    def __init__(self, key, name, value):
        self.key, self.name, self.value = key, name, value

    def __get__(self, obj, objtype=None):
        if _cur[0] is not None:
            _reads.setdefault(self.key, {}).setdefault(self.name, set()).add(_cur[0])
        return self.value

    def __set__(self, obj, value):
        self.value = value


_snap = {}


def pytest_collection_modifyitems(items):
    for item in items:
        cls = getattr(item, "cls", None)
        if cls is not None and cls not in _snap:
            _snap[cls] = dict(cls.__dict__)


def _instrument(item):
    cls = getattr(item, "cls", None)
    if cls is None or cls not in _snap:
        return
    key = item.nodeid.rsplit("::", 1)[0]
    before = _snap[cls]
    for name, value in list(cls.__dict__.items()):
        if name.startswith("__") or isinstance(value, _Recorder):
            continue
        if name not in before or before[name] is not value:
            setattr(cls, name, _Recorder(key, name, value))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    yield
    _instrument(item)  # setUpClass has run by now


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _cur[0] = item.nodeid.rsplit("::", 1)[1]
    try:
        yield
    finally:
        _cur[0] = None


def pytest_unconfigure(config):
    out = {k: {a: sorted(v) for a, v in d.items()} for k, d in _reads.items()}
    with open(os.environ.get("ATTR_OUT", "/out/attr_reads.json"), "w") as fh:
        json.dump(out, fh, indent=0)

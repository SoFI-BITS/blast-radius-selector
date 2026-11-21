"""Record failing test nodeids (failures, errors, subtest failures, collection errors)."""

import json
import os

_failed = set()
_bad_files = set()


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        _bad_files.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    if hasattr(session.config, "workerinput"):
        return
    with open(os.environ["LABEL_OUT"], "w") as fh:
        json.dump({"failed": sorted(_failed), "bad_files": sorted(_bad_files)}, fh)


def pytest_configure(config):
    # cap the suite's process pools: os.cpu_count() pools per xdist worker exhaust memory
    import concurrent.futures as cf

    orig = cf.ProcessPoolExecutor

    class Capped(orig):
        def __init__(self, max_workers=None, *a, **k):
            super().__init__(max_workers or 2, *a, **k)

    cf.ProcessPoolExecutor = Capped

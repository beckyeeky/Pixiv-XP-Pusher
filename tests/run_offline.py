#!/usr/bin/env python3
"""Run the candidate unittest suites without pytest and without any network.

This runner exists for the VPS regression box, where pytest is intentionally not
installed.  It reproduces the isolation that ``tests/conftest.py`` gives pytest
users:

* ``database.DB_PATH`` points at a throw-away file that is recreated per test;
* every AF_INET/AF_INET6 socket connection raises instead of reaching a network;
* the process working directory is a temporary directory, so relative state or
  quota files can never touch the repository or a production data directory.

Usage:
    python3 tests/run_offline.py [module ...]

Modules are given as names under ``tests/`` (default: the Native Jev backend and
the modules it changes).  No package installation, no credentials, no network.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_MODULES = (
    "test_native_jev_judge",
    "test_native_jev_round4_fixes",
    "test_native_jev_review_fixes",
    "test_search_grounded_judge",
    "test_classification_maintenance",
    "test_config",
    "test_provider_model_graph",
    "test_tag_classifier",
    "test_database",
)


def _isolate_network() -> None:
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def offline_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError(f"Unexpected network connection in offline regression: {address!r}")
        return original_connect(sock, address)

    def offline_connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError(f"Unexpected network connection in offline regression: {address!r}")
        return original_connect_ex(sock, address)

    socket.socket.connect = offline_connect
    socket.socket.connect_ex = offline_connect_ex


class IsolatedResult(unittest.TextTestResult):
    """Recreate an empty schema before every test, like conftest's fixture."""

    def startTest(self, test):  # noqa: N802 - unittest API
        import database

        path = database.DB_PATH
        if "production" in str(path) or str(path).startswith("/opt/pixiv"):
            raise AssertionError(f"Refusing to run against {path}")
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass
        database._init_db_sync()
        super().startTest(test)


def main(argv: list[str]) -> int:
    modules = argv or list(DEFAULT_MODULES)
    workdir = tempfile.mkdtemp(prefix="native-jev-offline-")
    db_path = Path(workdir) / "offline.sqlite3"
    os.chdir(workdir)
    os.environ.setdefault("TZ", "UTC")

    import database

    database.DB_PATH = db_path
    _isolate_network()
    #: The round-4 transport tests drive a real 127.0.0.1 socket and say so;
    #: this runner forbids sockets, so they skip themselves instead of failing
    #: the suite or being silently dropped from the default run.
    os.environ["JEV_OFFLINE_RUNNER"] = "1"

    import importlib.util

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in modules:
        spec = importlib.util.spec_from_file_location(
            f"offline_{name}", ROOT / "tests" / f"{name}.py",
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        suite.addTests(loader.loadTestsFromModule(module))

    runner = unittest.TextTestRunner(
        verbosity=1, resultclass=IsolatedResult, stream=sys.stdout,
    )
    result = runner.run(suite)
    print(
        f"offline-run: tests={result.testsRun} failures={len(result.failures)} "
        f"errors={len(result.errors)} skipped={len(result.skipped)} "
        f"db={db_path} workdir={workdir}"
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

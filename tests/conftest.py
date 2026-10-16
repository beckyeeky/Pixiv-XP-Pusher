import sys
import socket

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_database_and_network(tmp_path, monkeypatch):
    """Use the real schema per test; never touch the default DB or business network."""
    import database

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.sqlite3")
    database._init_db_sync()

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def offline_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError(f"Unexpected network connection in test: {address!r}")
        return original_connect(sock, address)

    def offline_connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError(f"Unexpected network connection in test: {address!r}")
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", offline_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", offline_connect_ex)

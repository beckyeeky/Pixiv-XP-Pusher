"""Web /api/feedback must update the real preference ledger from local data only.

These tests use the real schema, the real persistent ledger and FastAPI's
TestClient. No Pixiv, Telegram or network client is created or awaited.
"""
import json
import logging
import sqlite3

import pytest
import yaml
from fastapi.testclient import TestClient

import database as db
import persistent_profile as ledger
import web.app as web_app_module
from web.app import app as canonical_app


def write_config(tmp_path, monkeypatch, config=None):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config or {}, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(web_app_module, "CONFIG_PATH", path)
    return path


def seed_profile():
    """Create a real 2:1 projection through the ledger, not a mocked return."""
    with sqlite3.connect(db.DB_PATH) as connection:
        connection.executemany(
            "INSERT INTO xp_bookmarks(user_id, illust_id, tags) VALUES (1, ?, ?)",
            [
                (1, json.dumps(["alpha", "beta"])),
                (2, json.dumps(["alpha"])),
            ],
        )
    assert ledger.build(db.DB_PATH, 1) == {"alpha": 2.0, "beta": 1.0}


def cache_illust(illust_id, tags, user_id=7, user_name="artist"):
    with sqlite3.connect(db.DB_PATH) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO illust_cache"
            "(illust_id, tags, user_id, user_name, source) VALUES (?, ?, ?, ?, 'test')",
            (illust_id, json.dumps(tags), user_id, user_name),
        )


def cache_raw(illust_id, raw_tags, user_id=0, user_name=""):
    with sqlite3.connect(db.DB_PATH) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO illust_cache"
            "(illust_id, tags, user_id, user_name, source) VALUES (?, ?, ?, ?, 'test')",
            (illust_id, raw_tags, user_id, user_name),
        )


def add_metadata(illust_id, tags, user_id=0, user_name=""):
    with sqlite3.connect(db.DB_PATH) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO feedback_metadata"
            "(illust_id, tags, user_id, user_name, source) VALUES (?, ?, ?, ?, 'test')",
            (illust_id, json.dumps(tags), user_id, user_name),
        )


def profile():
    with sqlite3.connect(db.DB_PATH) as connection:
        return dict(connection.execute("SELECT tag, weight FROM xp_profile"))


def tables():
    with sqlite3.connect(db.DB_PATH) as connection:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {name: list(connection.execute(f"SELECT * FROM {name}")) for name in names}


@pytest.fixture
def client():
    async def authenticated():
        return None

    canonical_app.dependency_overrides[web_app_module.require_auth] = authenticated
    try:
        with TestClient(canonical_app) as test_client:
            yield test_client
    finally:
        canonical_app.dependency_overrides.pop(web_app_module.require_auth, None)


def post_feedback(client, illust_id, action):
    return client.post("/api/feedback", json={"illust_id": illust_id, "action": action})


def test_first_like_updates_real_profile_and_ledger(tmp_path, monkeypatch, client):
    write_config(tmp_path, monkeypatch)
    seed_profile()
    cache_illust(1001, ["alpha", "beta"])

    response = post_feedback(client, 1001, "like")

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "message": "已记录对作品 1001 的 like",
    }
    assert profile() == {"alpha": 2.5, "beta": 1.5}
    with sqlite3.connect(db.DB_PATH) as connection:
        row = connection.execute(
            "SELECT action, reversible FROM preference_feedback WHERE illust_id=1001"
        ).fetchone()
        feedback_row = connection.execute(
            "SELECT action FROM feedback WHERE illust_id=1001"
        ).fetchone()
    assert row == ("like", 1)
    assert feedback_row == ("like",)


def test_duplicate_like_leaves_every_business_table_unchanged(tmp_path, monkeypatch, client):
    write_config(tmp_path, monkeypatch)
    seed_profile()
    cache_illust(1002, ["alpha"])
    assert post_feedback(client, 1002, "like").status_code == 200
    before = tables()

    response = post_feedback(client, 1002, "like")

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert tables() == before


def test_like_dislike_like_restores_exact_profile_values(tmp_path, monkeypatch, client):
    write_config(tmp_path, monkeypatch)
    seed_profile()
    cache_illust(1003, ["alpha", "beta"])
    assert post_feedback(client, 1003, "like").status_code == 200
    after_like = profile()
    with sqlite3.connect(db.DB_PATH) as connection:
        like_contributions = json.loads(
            connection.execute(
                "SELECT contributions FROM preference_feedback WHERE illust_id=1003"
            ).fetchone()[0]
        )

    assert post_feedback(client, 1003, "dislike").status_code == 200
    with sqlite3.connect(db.DB_PATH) as connection:
        contributions = json.loads(
            connection.execute(
                "SELECT contributions FROM preference_feedback WHERE illust_id=1003"
            ).fetchone()[0]
        )
    expected_dislike = dict(after_like)
    for tag, values in contributions.items():
        assert values[0] < 0
        # Switching drops the exact recorded like contribution and adds the
        # real profile-dependent dislike penalty.
        expected_dislike[tag] = (
            after_like[tag] + values[0] - like_contributions[tag][0]
        )
    assert profile() == pytest.approx(expected_dislike)

    assert post_feedback(client, 1003, "like").status_code == 200
    assert profile() == after_like
    with sqlite3.connect(db.DB_PATH) as connection:
        assert dict(connection.execute("SELECT tag, weight FROM preference_projection")) == after_like


def test_metadata_only_feedback_and_duplicate_after_cache_clear(tmp_path, monkeypatch, client):
    write_config(tmp_path, monkeypatch)
    seed_profile()
    add_metadata(1004, ["alpha"], user_id=7, user_name="artist")

    first = post_feedback(client, 1004, "like")

    assert first.status_code == 200
    assert profile() == {"alpha": 2.5, "beta": 1.0}
    with sqlite3.connect(db.DB_PATH) as connection:
        assert connection.execute(
            "SELECT action FROM preference_feedback WHERE illust_id=1004"
        ).fetchone() == ("like",)
    before = tables()

    duplicate = post_feedback(client, 1004, "like")

    assert duplicate.status_code == 200
    assert tables() == before


def test_duplicate_like_after_cache_clear_uses_retained_metadata(
    tmp_path, monkeypatch, client
):
    write_config(tmp_path, monkeypatch)
    seed_profile()
    cache_illust(1013, ["alpha", "beta"])
    assert post_feedback(client, 1013, "like").status_code == 200

    with sqlite3.connect(db.DB_PATH) as connection:
        connection.execute("DELETE FROM illust_cache WHERE illust_id=1013")
        retained = connection.execute(
            "SELECT tags FROM feedback_metadata WHERE illust_id=1013"
        ).fetchone()
    assert retained is not None and json.loads(retained[0]) == ["alpha", "beta"]
    before = tables()

    response = post_feedback(client, 1013, "like")

    assert response.status_code == 200
    assert tables() == before


def test_corrupt_cache_falls_back_to_valid_metadata(tmp_path, monkeypatch, client):
    write_config(tmp_path, monkeypatch)
    seed_profile()
    cache_raw(1005, "{not-json")
    add_metadata(1005, ["beta"], user_id=9, user_name="metadata-artist")

    response = post_feedback(client, 1005, "like")

    assert response.status_code == 200
    with sqlite3.connect(db.DB_PATH) as connection:
        contributions = json.loads(
            connection.execute(
                "SELECT contributions FROM preference_feedback WHERE illust_id=1005"
            ).fetchone()[0]
        )
        assert connection.execute(
            "SELECT score FROM artist_profile WHERE artist_id=9"
        ).fetchone() == (1.0,)
    assert list(contributions) == ["beta"]
    assert profile() == {"alpha": 2.0, "beta": 1.5}


def test_no_usable_tags_is_422_and_writes_nothing(tmp_path, monkeypatch, client):
    write_config(tmp_path, monkeypatch)
    seed_profile()
    before = tables()

    response = post_feedback(client, 1006, "like")

    assert response.status_code == 422
    assert tables() == before


def test_unauthenticated_feedback_is_401_and_writes_nothing(tmp_path, monkeypatch):
    write_config(
        tmp_path,
        monkeypatch,
        {"web": {"require_login_password": True, "password": "hashed-secret"}},
    )
    web_app_module.sessions.clear()
    before = tables()
    with TestClient(canonical_app) as client:
        response = post_feedback(client, 1007, "like")
    assert response.status_code == 401
    assert tables() == before


def test_invalid_action_is_400_and_writes_nothing(tmp_path, monkeypatch, client):
    write_config(tmp_path, monkeypatch)
    seed_profile()
    cache_illust(1008, ["alpha"])
    before = tables()

    response = post_feedback(client, 1008, "meh")

    assert response.status_code == 400
    assert tables() == before


def test_historical_feedback_switches_with_retained_baseline(tmp_path, monkeypatch, client):
    write_config(tmp_path, monkeypatch)
    seed_profile()
    add_metadata(1009, ["alpha"], user_id=7, user_name="artist")
    with sqlite3.connect(db.DB_PATH) as connection:
        connection.execute(
            "INSERT INTO preference_feedback"
            "(illust_id, action, contributions, artist_id, artist_delta, reversible)"
            " VALUES (1009, 'like', '{}', 7, 1.0, 0)"
        )
        base_before = dict(connection.execute("SELECT tag, weight FROM preference_base"))

    response = post_feedback(client, 1009, "dislike")

    assert response.status_code == 200
    with sqlite3.connect(db.DB_PATH) as connection:
        assert dict(connection.execute("SELECT tag, weight FROM preference_base")) == base_before
        # The opaque historical like is never reversed; only the new action counts.
        assert connection.execute(
            "SELECT score FROM artist_profile WHERE artist_id=7"
        ).fetchone() == (-1.0,)
        assert connection.execute(
            "SELECT action, reversible FROM preference_feedback WHERE illust_id=1009"
        ).fetchone() == ("dislike", 1)


def test_invalid_profile_mode_is_rejected_without_writes(tmp_path, monkeypatch, client):
    write_config(tmp_path, monkeypatch, {"profiler": {"profile_mode": "auto"}})
    seed_profile()
    cache_illust(1010, ["alpha"])
    before = tables()

    response = post_feedback(client, 1010, "like")

    assert response.status_code == 500
    assert "profile_mode" in response.json()["detail"]
    assert tables() == before


def test_legacy_rejection_on_persistent_database_is_clear_409(tmp_path, monkeypatch, client):
    write_config(tmp_path, monkeypatch, {"profiler": {"profile_mode": "legacy"}})
    seed_profile()  # build marks the persistent ledger as used
    cache_illust(1011, ["alpha"])
    before = tables()

    response = post_feedback(client, 1011, "like")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "备份" in detail
    assert "Traceback" not in detail and "sqlite3" not in detail
    assert tables() == before


def test_legacy_mode_keeps_existing_profiler_policy_on_clean_database(
    tmp_path, monkeypatch, client
):
    write_config(tmp_path, monkeypatch, {"profiler": {"profile_mode": "legacy"}})
    with sqlite3.connect(db.DB_PATH) as connection:
        connection.execute("INSERT INTO xp_profile(tag, weight) VALUES ('alpha', 1.0)")
    with sqlite3.connect(db.DB_PATH) as connection:
        connection.execute(
            "INSERT INTO preference_projection(tag, weight) VALUES ('alpha', 1.0)"
        )
    cache_illust(1012, ["alpha"])

    response = post_feedback(client, 1012, "like")

    assert response.status_code == 200
    assert profile() == {"alpha": pytest.approx(1.5)}
    with sqlite3.connect(db.DB_PATH) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM preference_feedback WHERE illust_id=1012"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT action FROM feedback WHERE illust_id=1012"
        ).fetchone() == ("like",)


def test_index_info_logs_do_not_leak_fake_secrets(tmp_path, monkeypatch, caplog):
    secret = "fake-secret-value-7b1f"
    password_hash = web_app_module.hash_password("fake-password")
    write_config(
        tmp_path,
        monkeypatch,
        {
            "web": {"require_login_password": False, "password": password_hash},
            "pixiv": {"refresh_token": secret},
            "notifier": {"telegram": {"bot_token": secret}},
        },
    )
    caplog.set_level(logging.INFO, logger="web.app")

    with TestClient(canonical_app) as client:
        response = client.get("/", follow_redirects=False)

    assert response.status_code in (302, 307)
    assert secret not in caplog.text
    assert password_hash not in caplog.text

"""SQLite integration tests, all databases created under pytest tmp_path."""
import json
import sqlite3
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
import database as db
import persistent_profile as ledger
from pixiv_client import Illust
from profiler import XPProfiler


@pytest.fixture
def path(tmp_path, monkeypatch):
    path = tmp_path / 'test.sqlite3'
    monkeypatch.setattr(db, 'DB_PATH', path)
    db._init_db_sync()
    return path


def work(id=10, tags=None):
    return Illust(id=id, title='test', user_id=7, user_name='artist',
                  tags=tags or ['trait_raw', 'trait_raw'], bookmark_count=0, view_count=0,
                  page_count=1, image_urls=[], is_r18=False, ai_type=0,
                  create_date=datetime(2000, 1, 1))


def snapshot(path):
    with sqlite3.connect(path) as c:
        return dict(c.execute('SELECT tag,weight FROM xp_profile'))


def add_bookmark(path, id=10):
    with sqlite3.connect(path) as c:
        c.execute('INSERT INTO xp_bookmarks(user_id,illust_id,tags,illust_create_date) VALUES (?,?,?,?)',
                  (1, id, json.dumps(['trait_raw', 'trait_raw']), '2000-01-01'))


def test_new_bookmark_once_no_decay_or_saturation(path):
    add_bookmark(path)
    assert ledger.build(path, 1) == {'trait_raw': 1.0}
    assert ledger.build(path, 1) == {'trait_raw': 1.0}
    with sqlite3.connect(path) as c:
        c.execute('DELETE FROM xp_bookmarks')
    assert ledger.build(path, 1) == {'trait_raw': 1.0}
    add_bookmark(path)
    assert ledger.build(path, 1) == {'trait_raw': 1.0}


def test_feedback_duplicate_switch_and_rebuild(path):
    add_bookmark(path)
    ledger.build(path, 1)
    ledger.feedback(path, work(), 'like', {})
    assert snapshot(path) == {'trait_raw': 1.5}
    ledger.feedback(path, work(), 'like', {})
    assert snapshot(path) == {'trait_raw': 1.5}
    ledger.feedback(path, work(), 'dislike', {})
    assert snapshot(path)['trait_raw'] == pytest.approx(0.85)
    ledger.feedback(path, work(), 'dislike', {})
    assert snapshot(path)['trait_raw'] == pytest.approx(0.85)
    ledger.feedback(path, work(), 'like', {})
    assert snapshot(path)['trait_raw'] == pytest.approx(1.5)
    assert ledger.build(path, 1)['trait_raw'] == pytest.approx(1.5)
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT score FROM artist_profile WHERE artist_id=7').fetchone()[0] == 1
        assert c.execute('SELECT weight FROM negative_profile').fetchone()[0] == pytest.approx(0)
        assert c.execute('SELECT dislike_count FROM tag_blacklist').fetchone()[0] == 0


def test_boost_blacklist_alias_changes_are_projection_only(path):
    add_bookmark(path)
    assert ledger.build(path, 1, boosts={'trait_raw': 2}) == {'trait_raw': 2}
    assert ledger.build(path, 1, boosts={'trait_raw': 2}) == {'trait_raw': 2}
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO tag_aliases(original_tag,normalized_tag,kind) VALUES ('trait_raw','white_hair','equivalent')")
    assert ledger.build(path, 1, boosts={'white_hair': 3}) == {'white_hair': 3}
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO blocked_tags(tag) VALUES ('white_hair')")
    assert ledger.build(path, 1) == {}
    with sqlite3.connect(path) as c:
        c.execute('DELETE FROM blocked_tags')
        c.execute('DELETE FROM tag_aliases')
    assert ledger.build(path, 1) == {'trait_raw': 1}


def test_manual_reset_pauses_instead_of_silent_reweight(path):
    add_bookmark(path)
    ledger.build(path, 1)
    with sqlite3.connect(path) as c:
        c.execute('DELETE FROM xp_profile')
    with pytest.raises(RuntimeError, match='outside its ledger'):
        ledger.build(path, 1)
    assert snapshot(path) == {}


def test_feedback_atomic_rollback(path):
    with sqlite3.connect(path) as c:
        c.execute("CREATE TRIGGER fail_feedback BEFORE INSERT ON feedback BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        ledger.feedback(path, work(), 'dislike', {})
    with sqlite3.connect(path) as c:
        for table in ('preference_feedback', 'feedback', 'negative_profile', 'artist_profile', 'tag_blacklist'):
            assert c.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def test_import_weights_once_and_backup_wal(tmp_path, monkeypatch):
    path = tmp_path / 'legacy.db'
    monkeypatch.setattr(db, 'DB_PATH', path)
    with sqlite3.connect(path) as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('CREATE TABLE xp_profile(tag TEXT PRIMARY KEY, weight REAL, updated_at TIMESTAMP)')
        c.execute("INSERT INTO xp_profile VALUES ('trait_raw',7.125,CURRENT_TIMESTAMP)")
        c.commit()
        # Connection remains open to ensure the WAL is present during backup.
        db._init_db_sync()
        backups = list((tmp_path / 'backups').glob('*.preference-v1.*.sqlite3'))
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as backup:
            assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert dict(backup.execute('SELECT tag,weight FROM xp_profile')) == {'trait_raw': 7.125}
    assert ledger.build(path, 1) == {'trait_raw': 7.125}
    db._init_db_sync()
    assert ledger.build(path, 1) == {'trait_raw': 7.125}
    assert len(list((tmp_path / 'backups').glob('*.preference-v1.*.sqlite3'))) == 1


def test_migration_failure_rolls_back(path):
    with sqlite3.connect(path) as c:
        c.execute('DROP TABLE preference_migrations')
        for table in ('preference_base', 'preference_bookmarks', 'preference_feedback', 'preference_projection'):
            c.execute(f'DROP TABLE {table}')
        c.execute('ALTER TABLE feedback RENAME TO missing_feedback')
        c.commit()
        with pytest.raises(sqlite3.OperationalError):
            ledger.migrate(c)
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='preference_base'").fetchone()
        c.execute('ALTER TABLE missing_feedback RENAME TO feedback')
        c.commit()
        ledger.migrate(c)
        ledger.migrate(c)
        assert c.execute('SELECT count(*) FROM preference_migrations').fetchone()[0] == 1


def test_imported_feedback_duplicate_is_noop_switch_starts_new_contribution(path):
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO preference_feedback VALUES (10,'like','{}',NULL,0,0)")
    ledger.feedback(path, work(), 'like', {})
    assert snapshot(path) == {}
    result = ledger.feedback(path, work(), 'dislike', {})
    assert 'retained' in result['migration_policy']
    assert snapshot(path) == {'trait_raw': pytest.approx(-.3)}
    ledger.feedback(path, work(), 'dislike', {})
    assert snapshot(path) == {'trait_raw': pytest.approx(-.3)}
    ledger.feedback(path, work(), 'like', {})
    assert snapshot(path) == {'trait_raw': pytest.approx(.5)}


@pytest.mark.asyncio
async def test_profiler_build_round_preserves_feedback(path):
    add_bookmark(path)
    await db.set_state('sync_completed_1', 'true')
    client = AsyncMock()
    client._logged_in = False
    client.get_bookmarks.return_value = []
    profiler = XPProfiler(client=client)
    first = await profiler.build_profile(1)
    assert first == {'trait_raw': 1}
    await profiler.apply_feedback(work(), 'like', {})
    assert await profiler.build_profile(1) == {'trait_raw': 1.5}


def test_legacy_bookmarks_are_not_added_to_imported_weights(tmp_path, monkeypatch):
    path = tmp_path / 'old.db'
    monkeypatch.setattr(db, 'DB_PATH', path)
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE xp_profile(tag TEXT PRIMARY KEY,weight REAL,updated_at TIMESTAMP)')
        c.execute("INSERT INTO xp_profile VALUES ('trait_raw',8.75,CURRENT_TIMESTAMP)")
        c.execute('CREATE TABLE xp_bookmarks(user_id INTEGER,illust_id INTEGER,tags TEXT,illust_create_date TIMESTAMP,synced_at TIMESTAMP,PRIMARY KEY(user_id,illust_id))')
        c.execute("INSERT INTO xp_bookmarks VALUES (1,10,'[\"trait_raw\"]','2000-01-01',CURRENT_TIMESTAMP)")
    db._init_db_sync()
    assert snapshot(path) == {'trait_raw': 8.75}
    assert ledger.build(path, 1) == {'trait_raw': 8.75}
    assert ledger.build(path, 1) == {'trait_raw': 8.75}
    add_bookmark(path, id=11)
    assert ledger.build(path, 1) == {'trait_raw': 9.75}
    backup = next((tmp_path / 'backups').glob('*.sqlite3'))
    restored = tmp_path / 'restored.db'
    with sqlite3.connect(backup) as source, sqlite3.connect(restored) as destination:
        source.backup(destination)
    monkeypatch.setattr(db, 'DB_PATH', restored)
    db._init_db_sync()
    assert ledger.build(restored, 1) == {'trait_raw': 8.75}


def test_feedback_duplicate_concurrent_requests_are_serialized(path):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: ledger.feedback(path, work(), 'like', {}), range(8)))
    assert snapshot(path) == {'trait_raw': 0.5}
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT score FROM artist_profile WHERE artist_id=7').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_daily_blocked_tag_sync_does_not_invalidate_ledger(path):
    add_bookmark(path)
    ledger.build(path, 1)
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO blocked_tags(tag) VALUES ('trait_raw')")
    assert await db.sync_blocked_tags_to_xp() == 1
    assert ledger.build(path, 1) == {}
    with sqlite3.connect(path) as c:
        c.execute('DELETE FROM blocked_tags')
    assert ledger.build(path, 1) == {'trait_raw': 1}


@pytest.mark.parametrize('boost,discount', [(2, .3), (1, 1)])
def test_imported_anchor_keeps_first_and_repeated_config(tmp_path, monkeypatch, boost, discount):
    path = tmp_path / 'weighted-legacy.db'
    monkeypatch.setattr(db, 'DB_PATH', path)
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE xp_profile(tag TEXT PRIMARY KEY, weight REAL, updated_at TIMESTAMP)')
        c.executemany('INSERT INTO xp_profile VALUES (?,?,CURRENT_TIMESTAMP)', [('trait_raw', 8), ('series', 3)])
    db._init_db_sync()
    options = dict(boosts={'trait_raw': boost}, ip_tags={'series'}, ip_discount=discount)
    assert ledger.build(path, 1, **options) == {'trait_raw': 8, 'series': 3}
    assert ledger.build(path, 1, **options) == {'trait_raw': 8, 'series': 3}
    changed = ledger.build(path, 1, boosts={'trait_raw': 4}, ip_tags={'series'}, ip_discount=.6)
    assert changed == pytest.approx({'trait_raw': 8 * 4 / boost, 'series': 3 * .6 / discount})
    assert ledger.build(path, 1, **options) == pytest.approx({'trait_raw': 8, 'series': 3})


@pytest.mark.asyncio
async def test_legacy_direct_writes_are_recorded_and_rebuild_safe(path):
    add_bookmark(path)
    ledger.build(path, 1)
    await db.adjust_tag_weight('trait_raw', 2)
    assert snapshot(path) == {'trait_raw': 3}
    assert ledger.build(path, 1) == {'trait_raw': 3}
    await db.update_xp_profile({'manual': 7})
    assert snapshot(path) == {'manual': 7}
    assert ledger.build(path, 1) == {'manual': 7}


@pytest.mark.asyncio
async def test_controlled_reset_retains_feedback_rebuilds_cached_bookmarks(path):
    add_bookmark(path)
    ledger.build(path, 1)
    ledger.feedback(path, work(), 'dislike', {})
    await db.reset_xp_data()
    assert snapshot(path) == {}
    assert ledger.build(path, 1) == {'trait_raw': 1}
    assert ledger.build(path, 1) == {'trait_raw': 1}
    result = ledger.feedback(path, work(), 'like', {})
    assert 'retained' in result['migration_policy']
    assert snapshot(path) == {'trait_raw': 1.5}
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT weight FROM negative_profile').fetchone()[0] > 0


@pytest.mark.asyncio
async def test_controlled_reset_rolls_back_all_tables_on_failure(path):
    add_bookmark(path)
    ledger.build(path, 1)
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO strategy_stats(strategy) VALUES ('test')")
        c.execute("CREATE TRIGGER fail_reset BEFORE DELETE ON strategy_stats BEGIN SELECT RAISE(ABORT,'reset injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match='reset injected'):
        await db.reset_xp_data()
    assert snapshot(path) == {'trait_raw': 1}
    assert ledger.build(path, 1) == {'trait_raw': 1}


@pytest.mark.asyncio
async def test_negative_alias_projection_preserves_raw_reversal_and_unmerge(path):
    ledger.feedback(path, work(), 'dislike', {})
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO tag_aliases(original_tag,normalized_tag,kind) VALUES ('trait_raw','canonical','equivalent')")
    assert await db.get_negative_profile() == {'canonical': pytest.approx(.3)}
    assert await db.get_top_negative_tags() == [('canonical', pytest.approx(.3))]
    ledger.feedback(path, work(), 'like', {})
    assert await db.get_negative_profile() == {'canonical': pytest.approx(0)}
    with sqlite3.connect(path) as c:
        c.execute('DELETE FROM tag_aliases')
    assert await db.get_negative_profile() == {'trait_raw': pytest.approx(0)}


def test_review_b1_new_boost_uses_opaque_base_average(tmp_path, monkeypatch):
    p = tmp_path / 'review-old.db'
    monkeypatch.setattr(db, 'DB_PATH', p)
    with sqlite3.connect(p) as c:
        c.execute('CREATE TABLE xp_profile(tag TEXT PRIMARY KEY, weight REAL, updated_at TIMESTAMP)')
        c.execute("INSERT INTO xp_profile VALUES ('existing',8,CURRENT_TIMESTAMP)")
    db._init_db_sync()
    assert ledger.build(p, 1) == {'existing': 8}
    assert ledger.build(p, 1, boosts={'new_interest': 2}) == {'existing': 8, 'new_interest': 8}
    assert ledger.build(p, 1, boosts={'existing': 3, 'new_interest': 2}) == {'existing': 24, 'new_interest': 8}


@pytest.mark.asyncio
async def test_review_b2_feedback_keeps_raw_observation_after_unalias(path):
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO tag_aliases(original_tag,normalized_tag,kind) VALUES ('trait_raw','canonical','equivalent')")
    ledger.feedback(path, work(), 'like', {})
    assert snapshot(path) == {'canonical': .5}
    assert await db.delete_tag_alias('trait_raw')
    assert ledger.build(path, 1) == {'trait_raw': .5}


@pytest.mark.asyncio
async def test_review_b2_negative_raw_and_exact_cross_alias_reversal(path):
    ledger.feedback(path, work(), 'like', {})
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO tag_aliases(original_tag,normalized_tag,kind) VALUES ('trait_raw','canonical','equivalent')")
    ledger.feedback(path, work(), 'dislike', {})
    assert await db.delete_tag_alias('trait_raw')
    assert await db.get_negative_profile() == {'trait_raw': pytest.approx(.3)}
    assert ledger.build(path, 1) == {'trait_raw': pytest.approx(-.3)}
    ledger.feedback(path, work(), 'like', {})
    assert ledger.build(path, 1) == {'trait_raw': .5}
    with sqlite3.connect(path) as c:
        assert dict(c.execute('SELECT tag,weight FROM negative_profile')) == {'trait_raw': pytest.approx(0)}
        assert dict(c.execute('SELECT tag,dislike_count FROM tag_blacklist')) == {'trait_raw': 0}
        assert c.execute('SELECT score FROM artist_profile').fetchall() == [(1.0,)]


@pytest.mark.parametrize('action,expected', [('like', .5), ('dislike', -.3)])
def test_review_b2_synonyms_deduplicated_per_work(path, action, expected):
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO tag_aliases(original_tag,normalized_tag,kind) VALUES ('trait_raw','canonical','equivalent')")
    ledger.feedback(path, work(tags=['trait_raw', 'canonical', 'trait_raw']), action, {})
    assert ledger.build(path, 1) == {'canonical': pytest.approx(expected)}
    with sqlite3.connect(path) as c:
        if action == 'dislike':
            assert c.execute('SELECT SUM(weight) FROM negative_profile').fetchone()[0] == pytest.approx(.3)
            assert c.execute('SELECT SUM(dislike_count) FROM tag_blacklist').fetchone()[0] == 1


def test_review_b2_existing_raw_observations_merge_and_unmerge(path):
    ledger.feedback(path, work(tags=['trait_raw', 'canonical']), 'like', {})
    assert ledger.build(path, 1) == {'trait_raw': .5, 'canonical': .5}
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO tag_aliases(original_tag,normalized_tag,kind) VALUES ('trait_raw','canonical','equivalent')")
    assert ledger.build(path, 1) == {'canonical': .5}
    with sqlite3.connect(path) as c:
        c.execute('DELETE FROM tag_aliases')
    assert ledger.build(path, 1) == {'trait_raw': .5, 'canonical': .5}


def test_review_b2_observations_rollback_with_feedback(path):
    with sqlite3.connect(path) as c:
        c.execute("CREATE TRIGGER fail_review BEFORE INSERT ON feedback BEGIN SELECT RAISE(ABORT,'review injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match='review injected'):
        ledger.feedback(path, work(), 'dislike', {})
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT name FROM sqlite_master WHERE name='preference_feedback_observations'").fetchall() == []
        for table in ('preference_feedback', 'negative_profile', 'tag_blacklist', 'artist_profile'):
            assert c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('contents', [None, '{broken'])
async def test_r1_deprecated_bad_file_does_not_block_offline_build(path, tmp_path, contents):
    p = tmp_path / 'ip.json'
    if contents is not None:
        p.write_text(contents)
    client = AsyncMock()
    profiler = XPProfiler(client, ip_tags=str(p), ip_weight_discount=.5)
    assert await profiler.build_profile(1, cache_only=True) == {}
    assert client.mock_calls == []
    with sqlite3.connect(path) as c:
        assert ledger.persistent_used(c)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['build', 'feedback'])
@pytest.mark.parametrize('used', ['build', 'feedback', 'reset'])
async def test_r2_legacy_rejected_after_persistent(path, operation, used):
    if used == 'feedback':
        ledger.feedback(path, work(), 'like', {})
    else:
        ledger.build(path, 1)
    if used == 'reset':
        await db.reset_xp_data()
    before = snapshot(path)
    client = AsyncMock()
    profiler = XPProfiler(client, profile_mode='legacy')
    with pytest.raises(RuntimeError, match='backup'):
        if operation == 'build':
            await profiler.build_profile(1)
        else:
            await profiler.apply_feedback(work(), 'like', {})
    assert client.mock_calls == []
    assert snapshot(path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['disabled', 'empty_file', 'already_used', 'legacy'])
async def test_r1_compatible_paths(path, tmp_path, source):
    p = tmp_path / 'ip.json'
    if source == 'empty_file':
        p.write_text('[]')
    if source == 'already_used':
        ledger.build(path, 1)
    client = AsyncMock()
    client._logged_in = False
    client.get_bookmarks.return_value = []
    profiler = XPProfiler(client, ip_tags=str(p),
                          ip_weight_discount=1 if source == 'disabled' else .5,
                          profile_mode='legacy' if source == 'legacy' else 'persistent')
    before = snapshot(path)
    await profiler.build_profile(1, cache_only=True)
    assert snapshot(path) == before
    assert client.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('old_profile', [False, True])
async def test_r2_init_only_legacy_remains_usable(path, old_profile):
    if old_profile:
        await db.update_xp_profile({'old': 8})
    client = AsyncMock()
    client._logged_in = False
    client.get_bookmarks.return_value = []
    profiler = XPProfiler(client, profile_mode='legacy')
    await profiler.build_profile(1)
    await profiler.apply_feedback(work(), 'like', {})
    with sqlite3.connect(path) as c:
        assert not ledger.persistent_used(c)


@pytest.mark.parametrize('table', ['preference_anchor', 'preference_feedback_observations'])
def test_r2_pre_marker_v1_evidence_survives_empty_rows(path, table):
    ledger.build(path, 1)
    with sqlite3.connect(path) as c:
        c.execute('DROP TABLE preference_usage')
        other = 'preference_anchor' if table.endswith('observations') else 'preference_feedback_observations'
        c.execute(f'DROP TABLE {other}')
    with pytest.raises(RuntimeError, match='backup'):
        ledger.preflight(path, 'legacy')


def test_r2_usage_marker_rolls_back_on_failure(path):
    with sqlite3.connect(path) as c:
        c.execute("CREATE TRIGGER fail_marker BEFORE INSERT ON feedback BEGIN SELECT RAISE(ABORT,'marker injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match='marker injected'):
        ledger.feedback(path, work(), 'like', {})
    with sqlite3.connect(path) as c:
        assert not ledger.persistent_used(c)
        assert c.execute("SELECT name FROM sqlite_master WHERE name='preference_usage'").fetchall() == []


@pytest.mark.asyncio
async def test_r1_missing_deprecated_default_path_is_ignored(path, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    profiler = XPProfiler(AsyncMock(), ip_tags='data/ip_tags.json', ip_weight_discount=.5)
    assert await profiler.build_profile(1, cache_only=True) == {}
    await profiler._profile_preflight()


@pytest.mark.asyncio
async def test_r2_imported_old_database_not_locked_by_init(tmp_path, monkeypatch):
    p = tmp_path / 'pre-upgrade.sqlite3'
    monkeypatch.setattr(db, 'DB_PATH', p)
    with sqlite3.connect(p) as c:
        c.execute('CREATE TABLE xp_profile(tag TEXT PRIMARY KEY, weight REAL, updated_at TIMESTAMP)')
        c.execute("INSERT INTO xp_profile VALUES ('old',8,CURRENT_TIMESTAMP)")
    db._init_db_sync()
    client = AsyncMock()
    client._logged_in = False
    client.get_bookmarks.return_value = []
    profiler = XPProfiler(client, profile_mode='legacy')
    await profiler.build_profile(1)
    await profiler.apply_feedback(work(), 'like', {})
    with sqlite3.connect(p) as c:
        assert not ledger.persistent_used(c)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['build', 'feedback'])
@pytest.mark.parametrize('reset', [False, True])
async def test_final_ignored_ip_failure_preserves_migrated_anchor(tmp_path, monkeypatch, operation, reset):
    p = tmp_path / 'legacy.sqlite3'
    monkeypatch.setattr(db, 'DB_PATH', p)
    with sqlite3.connect(p) as c:
        c.execute('CREATE TABLE xp_profile(tag TEXT PRIMARY KEY,weight REAL,updated_at TIMESTAMP)')
        c.execute("INSERT INTO xp_profile VALUES ('series',8,CURRENT_TIMESTAMP)")
    db._init_db_sync()
    ip = tmp_path / 'ip.json'
    ip.write_text('["series"]')
    client = AsyncMock()
    client._logged_in = False
    client.get_bookmarks.return_value = []
    assert await XPProfiler(client, ip_tags=str(ip), ip_weight_discount=.5).build_profile(1) == {'series': 8}
    if reset:
        await db.reset_xp_data()
    before = snapshot(p)
    client.reset_mock()
    ip.unlink()
    profiler = XPProfiler(client, ip_tags=str(ip), ip_weight_discount=.5)
    if operation == 'build':
        assert await profiler.build_profile(1, cache_only=True) == before
    else:
        await profiler.apply_feedback(work(), 'like', {})
        before = {**before, 'trait_raw': .5}
    assert snapshot(p) == before
    assert client.mock_calls == []
    ip.write_text('["series"]')
    assert await profiler.build_profile(1, cache_only=True) == before


@pytest.mark.asyncio
async def test_final_manual_before_first_build_and_new_contributions(path):
    profiler = XPProfiler(None, boost_tags={'manual': 2})
    await db.update_xp_profile({'manual': 7})
    assert ledger.build(path, 1, boosts=profiler.boost_tags) == {'manual': 7}
    assert ledger.build(path, 1, boosts=profiler.boost_tags) == {'manual': 7}
    ledger.feedback(path, work(tags=['manual']), 'like', {}, boosts=profiler.boost_tags)
    assert snapshot(path) == {'manual': 8}
    assert ledger.build(path, 1, boosts={'manual': 3})['manual'] > 8


@pytest.mark.asyncio
async def test_final_manual_deleted_seed_alias_and_new_boost(path):
    assert ledger.build(path, 1, boosts={'old': 2}) == {'old': 1}
    await db.update_xp_profile({'manual': 7})
    assert ledger.build(path, 1, boosts={'old': 2}) == {'manual': 7}
    assert ledger.build(path, 1, boosts={'old': 2, 'fresh': 2}) == {'manual': 7, 'fresh': 1}
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO tag_aliases(original_tag,normalized_tag,kind) VALUES ('manual','canonical','equivalent')")
    assert ledger.build(path, 1, boosts={'old': 2, 'fresh': 2, 'canonical': 2}) == {'canonical': 7, 'fresh': 1}
    await db.update_xp_profile({})
    assert ledger.build(path, 1, boosts={'old': 2, 'fresh': 2, 'canonical': 2}) == {}
    assert 'old' in ledger.build(path, 1, boosts={'old': 3, 'fresh': 2, 'canonical': 2})


@pytest.mark.asyncio
async def test_final_deleted_seed_survives_boost_removal_and_real_contribution(path):
    ledger.build(path, 1, boosts={'old': 2})
    await db.update_xp_profile({})
    assert ledger.build(path, 1) == {}
    ledger.feedback(path, work(tags=['old']), 'like', {}, boosts={'old': 2})
    assert snapshot(path) == {'old': 1}
    assert ledger.build(path, 1, boosts={'old': 2}) == {'old': 1}


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['build', 'feedback'])
async def test_final_same_profiler_ignores_deprecated_ip_file_changes(path, tmp_path, operation):
    ip = tmp_path / 'ip.json'
    ip.write_text('["series"]')
    client = AsyncMock()
    profiler = XPProfiler(client, ip_tags=str(ip), ip_weight_discount=.5)
    assert await profiler.build_profile(1, cache_only=True) == {}
    ip.unlink()
    if operation == 'build':
        assert await profiler.build_profile(1, cache_only=True) == {}
    else:
        await profiler.apply_feedback(work(), 'like', {})
        assert snapshot(path) == {'trait_raw': .5}
    assert client.mock_calls == []
    ip.write_text('[]')
    await profiler._profile_preflight()
    assert snapshot(path) == ({} if operation == 'build' else {'trait_raw': .5})


@pytest.mark.asyncio
@pytest.mark.parametrize('transition',['alias','block'])
async def test_independent_frozen_seed_identity_or_visibility_roundtrip(path, transition):
    assert ledger.build(path,1,boosts={'old':2})=={'old':1}
    await db.update_xp_profile({})
    assert ledger.build(path,1,boosts={'old':2})=={}
    if transition=='alias':
        with sqlite3.connect(path) as c:
            c.execute("INSERT INTO tag_aliases(original_tag,normalized_tag,kind) VALUES ('old','canon','equivalent')")
        during=ledger.build(path,1,boosts={'old':2})
        with sqlite3.connect(path) as c:c.execute('DELETE FROM tag_aliases')
    else:
        with sqlite3.connect(path) as c:c.execute("INSERT INTO blocked_tags(tag) VALUES ('old')")
        during=ledger.build(path,1,boosts={'old':2})
        with sqlite3.connect(path) as c:c.execute('DELETE FROM blocked_tags')
    after=ledger.build(path,1,boosts={'old':2})
    with sqlite3.connect(path) as c:
        seeds=c.execute('SELECT * FROM preference_boost_seed').fetchall()
        manual=c.execute('SELECT * FROM preference_manual').fetchall()
    print({'transition':transition,'during':during,'after':after,'seeds':seeds,'manual':manual})
    assert during=={}
    assert after=={}, 'No observation or boost factor change: cleared synthetic seed must remain cleared'


async def accept_frozen_test_alias(path, raw='old', canonical='canon'):
    assert await db.save_tag_mapping_candidates([{
        'original_tag': raw, 'proposed_normalized_tag': canonical,
        'kind': 'equivalent',
    }]) == 1
    with sqlite3.connect(path) as c:
        candidate_id = c.execute('SELECT id FROM tag_mapping_candidates WHERE original_tag=?', (raw,)).fetchone()[0]
    await db.review_tag_mapping_candidate(candidate_id, 'accept')
    assert (await db.get_accepted_tag_aliases())[raw] == canonical


@pytest.mark.asyncio
@pytest.mark.parametrize('initial_alias', [False, True])
async def test_frozen_public_roundtrip_preserves_raw_seed(path, initial_alias):
    if initial_alias:
        await accept_frozen_test_alias(path)
    assert ledger.build(path, 1, boosts={'old': 2}) == ({'canon': 1} if initial_alias else {'old': 1})
    await db.update_xp_profile({})
    with sqlite3.connect(path) as c:
        baseline = c.execute('SELECT * FROM preference_boost_seed').fetchall()
    assert baseline == [('old', 2, 1, 1)]
    if not initial_alias:
        await accept_frozen_test_alias(path)
    for _ in range(3):
        assert ledger.build(path, 1, boosts={'old': 2}) == {}
        await db.block_tag('old')
        assert ledger.build(path, 1, boosts={'old': 2}) == {}
        assert await db.unblock_tag('old')
        assert ledger.build(path, 1, stop_words=['old'], boosts={'old': 2}) == {}
        assert ledger.build(path, 1, boosts={}) == {}
        assert ledger.build(path, 1, boosts={'old': 2}) == {}
        with sqlite3.connect(path) as c:
            assert c.execute('SELECT * FROM preference_boost_seed').fetchall() == baseline
    assert await db.delete_tag_alias('old')
    for _ in range(3):
        assert ledger.build(path, 1, boosts={'old': 2}) == {}
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT * FROM preference_boost_seed').fetchall() == baseline
    ledger.feedback(path, work(tags=['old']), 'like', {}, boosts={'old': 2})
    assert snapshot(path) == {'old': 1}
    # Explicit factor change releases the old seed; .5 * 3 - 1 manual.
    assert ledger.build(path, 1, boosts={'old': 3}) == {'old': .5}

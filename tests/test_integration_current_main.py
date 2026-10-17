"""Application policy regressions: real SQLite, no Pixiv/network calls."""
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest
import database as db
from profiler import XPProfiler
from test_persistent_profile import work


@pytest.mark.asyncio
async def test_deprecated_ip_config_never_changes_persistent_projection(tmp_path):
    with sqlite3.connect(db.DB_PATH) as c:
        c.execute("INSERT INTO xp_bookmarks(user_id,illust_id,tags) VALUES (1,10,?)", (json.dumps(['series']),))
    profiler = XPProfiler(AsyncMock(), ip_tags=['series'], ip_weight_discount=.3)
    assert await profiler.build_profile(1, cache_only=True) == {'series': 1}
    assert await profiler.build_profile(1, cache_only=True) == {'series': 1}
    await profiler.apply_feedback(work(tags=['series']), 'like', {})
    await profiler.apply_feedback(work(tags=['series']), 'like', {})
    assert await profiler.build_profile(1, cache_only=True) == {'series': 1.5}
    await profiler.apply_feedback(work(tags=['series']), 'dislike', {})
    assert await profiler.build_profile(1, cache_only=True) == {'series': pytest.approx(.85)}
    profiler.client.get_bookmarks.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('content', [None, '{bad', '{}'])
async def test_missing_or_invalid_deprecated_ip_file_is_not_a_write_gate(tmp_path, content):
    path = tmp_path / 'deprecated.json'
    if content is not None:
        path.write_text(content)
    profiler = XPProfiler(AsyncMock(), ip_tags=str(path), ip_weight_discount=.3)
    assert await profiler.build_profile(1, cache_only=True) == {}
    await profiler.apply_feedback(work(tags=['series']), 'like', {})
    assert await profiler.build_profile(1, cache_only=True) == {'series': .5}


def test_real_current_main_schema9_migrates_idempotently(tmp_path, monkeypatch):
    """Load the pinned main initializer, not a guessed schema-version fixture."""
    import subprocess
    from pathlib import Path
    source = subprocess.check_output(
        ['git', 'show', '43ec3ad22d249778c0f0961453b78fdf6439f07d:database.py'],
        cwd=Path(__file__).resolve().parents[1], text=True,
    )
    namespace = {'__file__': str(Path(db.__file__)), '__name__': 'main_schema_fixture'}
    exec(compile(source, 'pinned-main:database.py', 'exec'), namespace)
    path = tmp_path / 'current-main9.sqlite3'
    namespace['DB_PATH'] = path
    namespace['_init_db_sync']()
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT value FROM system_state WHERE key='schema_version'").fetchone() == ('9',)
        assert not c.execute("SELECT name FROM sqlite_master WHERE name='preference_migrations'").fetchall()
        c.execute("INSERT INTO xp_profile(tag,weight) VALUES ('series',8),('trait_raw',4)")
        c.execute("INSERT INTO tag_aliases(original_tag,normalized_tag,kind) VALUES ('raw','canonical','search')")
        c.execute("INSERT INTO push_history(illust_id) VALUES (99)")
    monkeypatch.setattr(db, 'DB_PATH', path)
    db._init_db_sync()
    with sqlite3.connect(path) as c:
        assert dict(c.execute('SELECT tag,weight FROM preference_base')) == {'series':8, 'trait_raw':4}
        for table in ('preference_migrations', 'preference_bookmarks', 'preference_feedback',
                      'preference_projection', 'exposure_ids', 'feedback_metadata'):
            assert c.execute('SELECT name FROM sqlite_master WHERE name=?', (table,)).fetchone()
        assert c.execute('SELECT illust_id FROM exposure_ids').fetchall() == [(99,)]
        tables = [row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='system_state'")]
        first = {table: list(c.execute(f'SELECT * FROM {table}')) for table in tables}
    db._init_db_sync()
    with sqlite3.connect(path) as c:
        # updated_at timestamps are an existing metadata refresh, not ledger changes.
        for table in tables:
            assert list(c.execute(f'SELECT * FROM {table}')) == first[table], table
        assert dict(c.execute('SELECT tag,weight FROM preference_base')) == {'series':8, 'trait_raw':4}
        assert c.execute('SELECT COUNT(*) FROM preference_migrations').fetchone() == (1,)
        assert c.execute('SELECT illust_id FROM exposure_ids').fetchall() == [(99,)]
    assert list((path.parent/'backups').glob('*.preference-v1.*'))


@pytest.mark.asyncio
async def test_old_weights_effective_boost_anchor_ignores_point_three_ip(tmp_path, monkeypatch):
    path = tmp_path / 'weighted-main.db'
    monkeypatch.setattr(db, 'DB_PATH', path)
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE xp_profile(tag TEXT PRIMARY KEY, weight REAL, updated_at TIMESTAMP)')
        c.execute("INSERT INTO xp_profile VALUES ('series',8,CURRENT_TIMESTAMP)")
    db._init_db_sync()
    profiler = XPProfiler(AsyncMock(), ip_tags=['series'], ip_weight_discount=.3, boost_tags={'series':2})
    for _ in range(3):
        assert await profiler.build_profile(1, cache_only=True) == {'series':8}
    profiler.ip_weight_discount = .8
    assert await profiler.build_profile(1, cache_only=True) == {'series':8}
    profiler.boost_tags = {'series':4}
    assert await profiler.build_profile(1, cache_only=True) == {'series':16}


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['search', 'equivalent'])
async def test_alias_review_protection_keeps_persistent_raw_ledger(kind):
    import persistent_profile as ledger
    with sqlite3.connect(db.DB_PATH) as c:
        c.execute("INSERT INTO xp_bookmarks(user_id,illust_id,tags) VALUES (1,10,?)", (json.dumps(['raw']),))
    assert ledger.build(db.DB_PATH, 1) == {'raw':1}
    await db.save_tag_mapping_candidates([dict(original_tag='raw', proposed_normalized_tag='target')])
    candidate = (await db.get_tag_mapping_candidates())[0]
    await db.save_tag_mapping_ai_recommendation(candidate['id'], dict(
        relation='equivalent', confidence=1, rationale='same', canonical_tag='target',
        risk_flags=[], principle_checks={}), model='test', principles_version='test', evidence={})
    with sqlite3.connect(db.DB_PATH) as c:
        rid = c.execute('SELECT id FROM tag_mapping_ai_recommendations').fetchone()[0]
        c.execute("INSERT INTO tag_aliases(original_tag,normalized_tag,kind,source) VALUES ('raw','existing',?,'manual')", (kind,))
        aliases = c.execute('SELECT * FROM tag_aliases').fetchall()
        raw = c.execute('SELECT * FROM preference_bookmarks').fetchall()
    with pytest.raises(ValueError, match='alias_conflict'):
        db.apply_tag_mapping_ai_batch_sync([dict(candidate_id=candidate['id'], recommendation_id=rid, decision='accept_equivalent')])
    expected = {'existing':1} if kind == 'equivalent' else {'raw':1}
    assert ledger.build(db.DB_PATH, 1) == expected
    assert ledger.build(db.DB_PATH, 1) == expected
    with sqlite3.connect(db.DB_PATH) as c:
        assert c.execute('SELECT * FROM tag_aliases').fetchall() == aliases
        assert c.execute('SELECT * FROM preference_bookmarks').fetchall() == raw
        assert c.execute('SELECT status FROM tag_mapping_candidates').fetchone() == ('pending',)


@pytest.mark.asyncio
async def test_fallback_migration_offline_projection_does_not_replace_ledger():
    from scripts.migrate_fallback_tag_classifications import _rebuild_cached_profile
    import persistent_profile as ledger
    with sqlite3.connect(db.DB_PATH) as c:
        c.execute("INSERT INTO xp_bookmarks(user_id,illust_id,tags) VALUES (1,10,?)", (json.dumps(['raw']),))
    assert ledger.build(db.DB_PATH, 1) == {'raw':1}
    ledger.feedback(db.DB_PATH, work(tags=['raw']), 'like', {})
    with sqlite3.connect(db.DB_PATH) as c:
        c.execute('DELETE FROM xp_bookmarks')
    result = await _rebuild_cached_profile(db.DB_PATH, {'profiler':{'ip_weight_discount':.3}}, 1)
    assert result['cache_only'] is True
    assert ledger.build(db.DB_PATH, 1) == {'raw':1.5}
    assert await db.get_state('sync_completed_1') is None


@pytest.mark.asyncio
async def test_cleanup_dedup_keeps_feedback_and_bookmark_contribution():
    from datetime import datetime
    profiler = XPProfiler(AsyncMock())
    await db.cache_illust(10, ['series'], user_id=7)
    await db.mark_pushed(10, 'test')
    with sqlite3.connect(db.DB_PATH) as c:
        c.execute("INSERT INTO xp_bookmarks(user_id,illust_id,tags) VALUES (1,10,?)", (json.dumps(['series']),))
    assert await profiler.build_profile(1, cache_only=True) == {'series':1}
    await profiler.apply_feedback(work(tags=['series']), 'like', {})
    await db.cleanup_old_records(days=-1)
    assert await db.is_pushed(10)
    assert await db.get_pushed_ids_batch([10,11]) == {10}
    await profiler.apply_feedback(work(tags=['series']), 'like', {})
    assert await profiler.build_profile(1, cache_only=True) == {'series':1.5}
    with sqlite3.connect(db.DB_PATH) as c:
        assert c.execute('SELECT COUNT(*) FROM preference_feedback').fetchone() == (1,)
        assert c.execute('SELECT COUNT(*) FROM preference_bookmarks').fetchone() == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize('factor', [.3, 2.0])
@pytest.mark.parametrize('operation', ['build', 'feedback'])
async def test_real_old_used_anchor_refused_before_any_write(tmp_path, monkeypatch, factor, operation):
    import subprocess
    from pathlib import Path
    sha = 'db6663087eadc559a85b4a813b936b667e0b7793'
    def old_module(name):
        source = subprocess.check_output(['git', 'show', f'{sha}:{name}.py'], cwd=Path(__file__).resolve().parents[1], text=True)
        ns = {'__name__': 'old_fixture', '__file__': db.__file__}
        exec(compile(source, f'{sha}:{name}.py', 'exec'), ns)
        return ns
    path = tmp_path / 'old-used.sqlite3'
    old_db = old_module('database')
    old_db['DB_PATH'] = path
    old_db['_init_db_sync']()
    monkeypatch.setattr(db, 'DB_PATH', path)
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO xp_profile(tag,weight) VALUES ('series',8)")
        c.execute("INSERT INTO preference_base VALUES ('series',8)")
        c.execute("INSERT INTO preference_projection VALUES ('series',8)")
    old_client = AsyncMock()
    old = old_module('profiler')['XPProfiler'](old_client, ip_tags=['series'], ip_weight_discount=factor if factor == .3 else 1, boost_tags={'series':2} if factor == 2 else {})
    old_client.get_bookmarks.return_value = []
    assert await old.build_profile(1) == {'series':8}
    db._init_db_sync()
    def dump():
        with sqlite3.connect(path) as c:
            return '\n'.join(c.iterdump())
    before = dump()
    client = AsyncMock()
    current = XPProfiler(client, boost_tags={'series':4})
    with pytest.raises(RuntimeError, match='专门兼容处理'):
        if operation == 'build':
            await current.build_profile(1)
        else:
            await current.apply_feedback(work(tags=['series']), 'like', {})
    assert dump() == before
    assert client.mock_calls == []


@pytest.mark.asyncio
async def test_new_policy_boost_anchor_survives_restart(tmp_path, monkeypatch):
    path = tmp_path / 'new-policy.sqlite3'
    import subprocess
    from pathlib import Path
    source = subprocess.check_output(['git', 'show', '43ec3ad22d249778c0f0961453b78fdf6439f07d:database.py'], cwd=Path(__file__).resolve().parents[1], text=True)
    ns = {'__name__': 'main9_boost_fixture', '__file__': db.__file__}
    exec(compile(source, 'pinned-main:database.py', 'exec'), ns)
    ns['DB_PATH'] = path
    ns['_init_db_sync']()
    with sqlite3.connect(path) as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='preference_migrations'").fetchone()
    monkeypatch.setattr(db, 'DB_PATH', path)
    db._init_db_sync()
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO preference_base VALUES ('series',8)")
        c.execute("INSERT INTO preference_projection VALUES ('series',8)")
        c.execute("INSERT INTO xp_profile(tag,weight) VALUES ('series',8)")
    assert await XPProfiler(AsyncMock(), boost_tags={'series':2}).build_profile(1, cache_only=True) == {'series':8}
    db._init_db_sync()
    assert await XPProfiler(AsyncMock(), boost_tags={'series':4}).build_profile(1, cache_only=True) == {'series':16}


@pytest.mark.asyncio
async def test_application_policy_marker_rolls_back_with_projection_failure(monkeypatch):
    import persistent_profile as ledger
    with sqlite3.connect(db.DB_PATH) as c:
        before = '\n'.join(c.iterdump())
    def fail(*args, **kwargs):
        raise RuntimeError('injected projection failure')
    monkeypatch.setattr(ledger, '_project', fail)
    with pytest.raises(RuntimeError, match='injected projection failure'):
        await XPProfiler(AsyncMock()).build_profile(1, cache_only=True)
    with sqlite3.connect(db.DB_PATH) as c:
        assert '\n'.join(c.iterdump()) == before

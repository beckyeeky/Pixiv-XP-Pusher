import asyncio
import sqlite3
import pytest
import database as db
from classification_maintenance import ClassificationMaintenance

@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.sqlite3')
    asyncio.run(db.init_db())
    return db.DB_PATH

@pytest.mark.asyncio
async def test_cleanup_preserves_exposure_and_feedback(isolated):
    await db.cache_illust(712, ['sample'], user_id=19)
    await db.mark_pushed(712, 'search')
    await db.record_feedback(712, 'like')
    with sqlite3.connect(isolated) as conn:
        conn.execute("UPDATE push_history SET pushed_at='2000-01-01'")
        conn.execute("UPDATE illust_cache SET created_at='2000-01-01'")
    await db.cleanup_old_sent_history(30)
    await db.cleanup_old_illust_cache(60)
    assert await db.is_pushed(712)
    assert await db.get_pushed_ids_batch([712, 713]) == {712}
    assert await db.get_recent_liked_tags() == ['sample']
    assert await db.get_recent_liked_illusts_for_artist(19) == [712]
    assert await db.get_recent_liked_illusts_for_tag('sample') == [712]

@pytest.mark.asyncio
async def test_default_automatic_maintenance_never_searches(isolated):
    async def forbidden(*args):
        pytest.fail('default automatic maintenance reached paid classifier')
    service = ClassificationMaintenance({}, classify=forbidden)
    result = await service.run_profile({'sample': 5})
    assert result['attempted'] == 0
    assert result['automatic_external_search'] is False

@pytest.mark.asyncio
async def test_direct_feedback_and_cleanup_entrypoints(isolated):
    await db.cache_illust(801, ['negative'], user_id=42)
    with sqlite3.connect(isolated) as conn:
        conn.execute("INSERT INTO feedback (illust_id,action) VALUES (801,'dislike')")
    await db.delete_cached_illust(801)
    assert await db.get_recent_disliked_tags() == ['negative']
    assert await db.is_pushed(801)

@pytest.mark.asyncio
async def test_mark_pushed_rolls_back_history_on_exposure_failure(isolated):
    with sqlite3.connect(isolated) as conn:
        conn.execute("""CREATE TRIGGER reject_exposure BEFORE INSERT ON exposure_ids
                     BEGIN SELECT RAISE(ABORT,'injected retention failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match='injected retention failure'):
        await db.mark_pushed(802, 'search')
    with sqlite3.connect(isolated) as conn:
        assert conn.execute('SELECT 1 FROM push_history WHERE illust_id=802').fetchone() is None

@pytest.mark.asyncio
async def test_migration_backfills_legacy_and_online_backup(isolated):
    with sqlite3.connect(isolated) as conn:
        for table in ('feedback', 'illust_cache'):
            for event in ('insert', 'update'):
                conn.execute(f'DROP TRIGGER retain_{table}_{event}')
        conn.execute('DROP TABLE retention_migrations')
        conn.execute('DROP TABLE exposure_ids')
        conn.execute('DROP TABLE feedback_metadata')
        conn.execute("INSERT INTO push_history(illust_id) VALUES (901)")
        conn.execute("INSERT INTO feedback(illust_id,action) VALUES (902,'like')")
        conn.execute("INSERT INTO illust_cache(illust_id,tags,user_id) VALUES (902,'[\"legacy\"]',55)")
        conn.execute("INSERT INTO illust_cache(illust_id,tags) VALUES (903,'[]')")
    await db.init_db()
    backups = list((isolated.parent / 'backups').glob('*.retention-v1.*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute('SELECT COUNT(*) FROM push_history').fetchone()[0] == 1
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='exposure_ids'").fetchone() is None
    await db.init_db()
    assert list((isolated.parent / 'backups').glob('*.retention-v1.*.sqlite3')) == backups
    assert await db.get_pushed_ids_batch([901,902,903]) == {901,902}
    await db.cleanup_old_records(days=-1)
    assert await db.get_recent_liked_tags() == ['legacy']
    assert await db.get_recent_liked_illusts_for_artist(55) == [902]

@pytest.mark.asyncio
async def test_permanent_confirmed_and_unresolved_cooldown(isolated):
    from classification_maintenance import ClassificationMaintenance
    await db.save_tag_classifications([
        ('human', 'feature', 'manual'), ('accepted', 'feature', 'ai'),
        ('cooling', 'unresolved', 'ai_unresolved'), ('retry', 'unresolved', 'ai_unresolved'),
    ])
    with sqlite3.connect(isolated) as conn:
        conn.execute("UPDATE tag_classification_cache SET updated_at='2000-01-01' WHERE normalized_tag!='cooling'")
    fresh = await db.get_tag_classifications(['human','accepted','cooling','retry'])
    assert set(fresh) == {'human','accepted','cooling'}
    manager = ClassificationMaintenance({'tag_classifier': {'maintenance': {'allow_automatic_external_search': True}}})
    assert await manager._select_profile_tags({'human':10,'accepted':9,'cooling':8,'retry':7,'new':6}) == ['retry','new']

@pytest.mark.asyncio
async def test_cache_only_never_becomes_exposed_or_metadata(isolated):
    await db.cache_illust(1001, ['candidate'], user_id=7)
    await db.init_db()
    assert not await db.is_pushed(1001)
    await db.cleanup_old_records(days=-1)
    assert not await db.is_pushed(1001)
    with sqlite3.connect(isolated) as conn:
        assert conn.execute('SELECT 1 FROM feedback_metadata WHERE illust_id=1001').fetchone() is None

@pytest.mark.asyncio
async def test_missing_metadata_not_fabricated_and_late_cache_retained(isolated):
    await db.record_feedback(1002, 'like')
    assert await db.is_pushed(1002)
    with sqlite3.connect(isolated) as conn:
        assert conn.execute('SELECT 1 FROM feedback_metadata WHERE illust_id=1002').fetchone() is None
    await db.cache_illust(1002, ['late'], user_id=8)
    await db.delete_cached_illust(1002)
    assert await db.get_recent_liked_tags() == ['late']
    assert await db.get_recent_liked_illusts_for_artist(8) == [1002]

@pytest.mark.asyncio
async def test_failed_feedback_retention_rolls_back_feedback(isolated):
    with sqlite3.connect(isolated) as conn:
        conn.execute("""CREATE TRIGGER reject_feedback_exposure BEFORE INSERT ON exposure_ids
                     BEGIN SELECT RAISE(ABORT,'feedback retention failure'); END""")
        with pytest.raises(sqlite3.IntegrityError, match='feedback retention failure'):
            conn.execute("INSERT INTO feedback(illust_id,action) VALUES (1003,'like')")
        assert conn.execute('SELECT 1 FROM feedback WHERE illust_id=1003').fetchone() is None

@pytest.mark.asyncio
async def test_independent_history_cleanup_keeps_only_real_exposure(isolated):
    await db.cache_illust(1004, ['candidate'])
    with sqlite3.connect(isolated) as conn:
        conn.execute("INSERT INTO push_history(illust_id,pushed_at) VALUES (1005,'2000-01-01')")
    await db.cleanup_old_sent_history(1)
    await db.cleanup_old_illust_cache(-1)
    assert await db.get_pushed_ids_batch([1004,1005]) == {1005}

"""Versioned persistent preference ledger; never uses publication time as decay.

xp_profile is the compatibility projection, not the source of observations.
Migration keeps opaque legacy weights intact: they cannot be decomposed into
historical feedback or undone safely when its original metadata is missing.
"""
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from tag_mapping import TagIdentityResolver

VERSION = 1


def backup_before_upgrade(path):
    """SQLite online backup includes committed WAL pages; never copy DB files."""
    path = Path(path)
    if not path.exists() or not path.stat().st_size:
        return None
    with sqlite3.connect(path) as source:
        exists = source.execute("SELECT 1 FROM sqlite_master WHERE name='preference_migrations'").fetchone()
        if exists and source.execute("SELECT 1 FROM preference_migrations WHERE version=?", (VERSION,)).fetchone():
            return None
        directory = path.parent / 'backups'
        directory.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        target = directory / f'{path.name}.preference-v{VERSION}.{stamp}.sqlite3'
        with sqlite3.connect(target) as destination:
            source.backup(destination)
        return target


def migrate(connection):
    """All ledger DDL, import and version marker are one rollback-able transaction."""
    connection.execute('BEGIN IMMEDIATE')
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS preference_migrations (version INTEGER PRIMARY KEY)')
        if connection.execute('SELECT 1 FROM preference_migrations WHERE version=?', (VERSION,)).fetchone():
            connection.commit()
            return
        for statement in (
            'CREATE TABLE preference_base (tag TEXT PRIMARY KEY, weight REAL NOT NULL)',
            'CREATE TABLE preference_bookmarks (user_id INTEGER, illust_id INTEGER, tags TEXT NOT NULL, imported INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id, illust_id))',
            'CREATE TABLE preference_feedback (illust_id INTEGER PRIMARY KEY, action TEXT NOT NULL, contributions TEXT NOT NULL, artist_id INTEGER, artist_delta REAL NOT NULL DEFAULT 0, reversible INTEGER NOT NULL DEFAULT 1)',
            'CREATE TABLE preference_projection (tag TEXT PRIMARY KEY, weight REAL NOT NULL)',
        ):
            connection.execute(statement)
        connection.execute('INSERT INTO preference_base SELECT tag, weight FROM xp_profile')
        connection.execute('INSERT INTO preference_projection SELECT tag, weight FROM xp_profile')
        # Historical bookmarks already represented by the opaque imported profile
        # must not be counted twice; retain their raw metadata for audit.
        has_base = connection.execute('SELECT 1 FROM preference_base LIMIT 1').fetchone()
        if has_base:
            connection.execute('INSERT INTO preference_bookmarks SELECT user_id, illust_id, tags, 1 FROM xp_bookmarks')
        connection.execute("INSERT INTO preference_feedback (illust_id,action,contributions,reversible) SELECT illust_id,action,'{}',0 FROM feedback")
        connection.execute('INSERT INTO preference_migrations VALUES (?)', (VERSION,))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _resolver(connection):
    rows = connection.execute("SELECT original_tag, normalized_tag FROM tag_aliases WHERE kind='equivalent'").fetchall()
    return TagIdentityResolver(dict(rows))


def _check_projection(connection):
    expected = dict(connection.execute('SELECT tag,weight FROM preference_projection'))
    actual = dict(connection.execute('SELECT tag,weight FROM xp_profile'))
    if actual != expected:
        raise RuntimeError('Persistent profile was changed outside its ledger (legacy/reset/manual write). Restore the projection or explicitly migrate/reset before rebuilding; refusing to double count.')


def _project(connection, stop_words=(), boosts=None, ip_tags=(), ip_discount=1.0):
    resolver = _resolver(connection)
    weights = defaultdict(float)
    for tag, weight in connection.execute('SELECT tag,weight FROM preference_base'):
        weights[resolver.resolve(tag)] += weight
    for (tags,) in connection.execute('SELECT tags FROM preference_bookmarks WHERE imported=0'):
        for tag in {resolver.resolve(t) for t in json.loads(tags)} - {''}:
            weights[tag] += 1.0
    # Raw observations are independent of exact undo entries in contributions.
    # Lazy DDL keeps existing v1 databases compatible and transaction rollback safe.
    connection.execute('CREATE TABLE IF NOT EXISTS preference_feedback_observations (illust_id INTEGER PRIMARY KEY, observations TEXT NOT NULL)')
    for contributions, observations in connection.execute('SELECT f.contributions,o.observations FROM preference_feedback f LEFT JOIN preference_feedback_observations o ON o.illust_id=f.illust_id'):
        if observations is None:
            # Earlier v1 rows have no recoverable raw identity.
            entries = {t: v[0] for t, v in json.loads(contributions).items()}
        else:
            entries = json.loads(observations)
        seen = set()
        for tag, delta in entries.items():
            canonical = resolver.resolve(tag)
            if canonical not in seen:
                weights[canonical] += delta
                seen.add(canonical)
    blocked = {resolver.resolve(t) for (t,) in connection.execute('SELECT tag FROM blocked_tags')}
    stopped = {resolver.resolve(t) for t in stop_words} | blocked | {''}
    raw_boosts = dict(boosts or {})
    boosts = {resolver.resolve(t): float(v) for t, v in raw_boosts.items()}
    ip_tags = {resolver.resolve(t) for t in ip_tags}
    # Opaque legacy weights already include unknown historical multipliers.
    # Anchor current factors once; future changes apply a ratio, not a second boost.
    connection.execute('CREATE TABLE IF NOT EXISTS preference_anchor (tag TEXT PRIMARY KEY, factor REAL NOT NULL)')
    for tag, _ in connection.execute('SELECT tag,weight FROM preference_base').fetchall():
        canonical = resolver.resolve(tag)
        factor = boosts.get(canonical, ip_discount if canonical in ip_tags else 1.0)
        connection.execute('INSERT OR IGNORE INTO preference_anchor VALUES (?,?)', (tag, factor))
    # Average the observation units, including opaque imported weights, before
    # removing base units for separately anchored multiplier application.
    visible = {t: w for t, w in weights.items() if t not in stopped}
    average = sum(visible.values()) / len(visible) if visible else 1.0
    base = defaultdict(float)
    for tag, weight, factor in connection.execute('SELECT b.tag,b.weight,a.factor FROM preference_base b JOIN preference_anchor a ON a.tag=b.tag'):
        canonical = resolver.resolve(tag)
        current = boosts.get(canonical, ip_discount if canonical in ip_tags else 1.0)
        # A zero historical factor cannot be inverted: retain the opaque baseline.
        base[canonical] += weight * (current / factor if factor else 1.0)
        weights[canonical] -= weight
    profile = {t: w for t, w in weights.items() if t not in stopped}
    for tag in list(profile):
        if tag in boosts:
            profile[tag] *= boosts[tag]
        elif tag in ip_tags:
            profile[tag] *= ip_discount
    for tag, weight in base.items():
        if tag not in stopped:
            profile[tag] = profile.get(tag, 0) + weight
    connection.execute('CREATE TABLE IF NOT EXISTS preference_manual (tag TEXT PRIMARY KEY, weight REAL NOT NULL)')
    manual = list(connection.execute('SELECT tag,weight FROM preference_manual'))
    manual_tags = {resolver.resolve(t) for t, _ in manual}
    connection.execute('CREATE TABLE IF NOT EXISTS preference_boost_seed (tag TEXT PRIMARY KEY, factor REAL NOT NULL, weight REAL NOT NULL, frozen INTEGER NOT NULL)')
    seeds = defaultdict(list)
    for raw, factor, weight, frozen in connection.execute('SELECT tag,factor,weight,frozen FROM preference_boost_seed'):
        seeds[resolver.resolve(raw)].append((raw, factor, weight, frozen))
    # Resolve only for projection/config lookup. Never rename a stored seed:
    # manual cancellation and later unaliasing still belong to its raw identity.
    raw_keys = {resolver.resolve(raw): raw for raw in raw_boosts}
    for tag in seeds.keys() | boosts.keys():
        previous = seeds.get(tag, [])
        multiplier = boosts.get(tag)
        locked = [(raw, f, w, frozen) for raw, f, w, frozen in previous
                  if frozen and (multiplier is None or f == multiplier)]
        for _, _, weight, _ in locked:
            if tag not in stopped and weight:
                profile[tag] = profile.get(tag, 0) + weight
        pending = [row for row in previous if row not in locked]
        if multiplier is None:
            continue
        if not pending and previous:
            continue
        frozen = not previous and tag in manual_tags
        weight = 0.0 if frozen else average * 0.5 * multiplier
        if tag not in stopped and tag not in profile:
            if weight:
                profile[tag] = weight
        elif not frozen:
            weight = 0.0
        # Existing rows retain their raw keys. A new row uses the raw config
        # spelling even when its first build occurs under an accepted alias.
        keys = [row[0] for row in pending] or [raw_keys[tag]]
        for index, raw in enumerate(keys):
            connection.execute('INSERT OR REPLACE INTO preference_boost_seed VALUES (?,?,?,?)',
                               (raw, multiplier, weight if index == 0 else 0.0, int(frozen)))
    for tag, weight in manual:
        tag = resolver.resolve(tag)
        if tag not in stopped:
            profile[tag] = profile.get(tag, 0) + weight
            if profile[tag] == 0:
                profile.pop(tag)
    connection.execute('DELETE FROM xp_profile')
    connection.execute('DELETE FROM preference_projection')
    connection.executemany('INSERT INTO xp_profile(tag,weight) VALUES (?,?)', profile.items())
    connection.executemany('INSERT INTO preference_projection VALUES (?,?)', profile.items())
    return profile


def persistent_used(connection):
    """Read-only detection: migration alone does not mean persistent was used.

    Anchor/observation table existence is evidence even with zero rows (empty
    build/reset). Pre-marker v1 without either is ambiguous only when it has
    non-imported ledger entries; imported history alone is safe for legacy.
    """
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables & {'preference_usage', 'preference_anchor', 'preference_feedback_observations'}:
        return True
    for table, condition in (('preference_bookmarks', 'imported=0'),
                             ('preference_feedback', 'reversible=1')):
        if table in tables and connection.execute(f'SELECT 1 FROM {table} WHERE {condition} LIMIT 1').fetchone():
            return True
    return False


def preflight(path, mode, ip_error=None, ip_discount=1.0):
    # Open read-only: validation must not initialize or modify a database.
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as connection:
        used = persistent_used(connection)
        if mode == 'legacy' and used:
            raise RuntimeError('Persistent profile has been used; legacy build/feedback is unsafe. Restore the pre-upgrade backup before running legacy (恢复升级前备份). This is not a lossless round trip.')
        if mode == 'persistent' and ip_error and ip_discount != 1.0:
            raise RuntimeError(f'Cannot safely project persistent IP multipliers: {ip_error}. 修复 IP 文件（有效空列表 [] 也可），或显式设置 ip_weight_discount=1 后重试。')


def _mark_used(connection):
    # Table existence survives reset, even when the profile is empty.
    connection.execute('CREATE TABLE IF NOT EXISTS preference_usage (version INTEGER PRIMARY KEY)')
    connection.execute('INSERT OR IGNORE INTO preference_usage VALUES (?)', (VERSION,))


def build(path, user_id, stop_words=(), boosts=None, ip_tags=(), ip_discount=1.0):
    with sqlite3.connect(path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        _check_projection(connection)
        _mark_used(connection)
        connection.execute('INSERT OR IGNORE INTO preference_bookmarks SELECT user_id,illust_id,tags,0 FROM xp_bookmarks WHERE user_id=?', (user_id,))
        return _project(connection, stop_words, boosts, ip_tags, ip_discount)


def feedback(path, illust, action, config, stop_words=(), boosts=None, ip_tags=(), ip_discount=1.0):
    if action not in {'like', 'dislike'}:
        raise ValueError('feedback action must be like or dislike')
    with sqlite3.connect(path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        _check_projection(connection)
        _mark_used(connection)
        old = connection.execute('SELECT action,contributions,artist_id,artist_delta,reversible FROM preference_feedback WHERE illust_id=?', (illust.id,)).fetchone()
        result = {'action': action, 'illust_id': illust.id, 'disliked_tags': [], 'auto_blocked_artists': []}
        if old and old[0] == action:
            return result
        if old and not old[4]:
            # Imported history remains an opaque baseline. Switching starts a new
            # reversible contribution; never claim to undo historical effects.
            result['migration_policy'] = 'Historical contribution retained; new action applies only a post-migration contribution.'
            old = None
        # Reverse exact recorded deltas, never recompute from current weights.
        if old:
            for tag, values in json.loads(old[1]).items():
                connection.execute('UPDATE negative_profile SET weight=weight-? WHERE tag=?', (values[1], tag))
                connection.execute('UPDATE tag_blacklist SET dislike_count=MAX(0,dislike_count-?) WHERE tag=?', (values[2], tag))
            if old[2]:
                connection.execute('UPDATE artist_profile SET score=score-? WHERE artist_id=?', (old[3], old[2]))
        resolver = _resolver(connection)
        groups = defaultdict(list)
        blocked = {resolver.resolve(t) for (t,) in connection.execute('SELECT tag FROM blocked_tags')}
        stopped = {resolver.resolve(t) for t in stop_words} | blocked | {''}
        for raw in dict.fromkeys(illust.tags):
            canonical = resolver.resolve(raw)
            if canonical not in stopped:
                groups[canonical].append(raw)
        profile = dict(connection.execute('SELECT tag,weight FROM xp_profile'))
        max_weight = max(profile.values(), default=1.0)
        contributions = {}
        observations = {}
        for canonical, raw_tags in groups.items():
            # One negative accounting entry per equivalent identity, keyed by
            # an observed raw spelling; reversal uses this exact key forever.
            tag = raw_tags[0]
            if action == 'like':
                contributions[tag] = [float(config.get('like_boost', 0.5)), 0, 0]
            else:
                ratio = profile.get(canonical, 0) / max_weight if max_weight > 0 else 0
                penalty = float(config.get('dislike_penalty', 0.3)) * (1 - ratio * 0.5)
                contributions[tag] = [-penalty, penalty, 1]
                connection.execute('INSERT INTO negative_profile(tag,weight) VALUES (?,?) ON CONFLICT(tag) DO UPDATE SET weight=weight+excluded.weight', (tag, penalty))
                connection.execute('INSERT INTO tag_blacklist(tag,dislike_count) VALUES (?,1) ON CONFLICT(tag) DO UPDATE SET dislike_count=dislike_count+1', (tag,))
                result['disliked_tags'].append(canonical)
            for raw in raw_tags:
                observations[raw] = contributions[tag][0]
        connection.execute('CREATE TABLE IF NOT EXISTS preference_feedback_observations (illust_id INTEGER PRIMARY KEY, observations TEXT NOT NULL)')
        connection.execute('INSERT OR REPLACE INTO preference_feedback_observations VALUES (?,?)', (illust.id, json.dumps(observations)))
        artist_delta = 1.0 if action == 'like' else -1.0
        if illust.user_id:
            connection.execute('INSERT INTO artist_profile(artist_id,score) VALUES (?,?) ON CONFLICT(artist_id) DO UPDATE SET score=score+excluded.score', (illust.user_id, artist_delta))
            score = connection.execute('SELECT score FROM artist_profile WHERE artist_id=?', (illust.user_id,)).fetchone()[0]
            if action == 'dislike' and score <= -3:
                inserted = connection.execute('INSERT OR IGNORE INTO blocked_artists(artist_id,artist_name) VALUES (?,?)', (illust.user_id, illust.user_name)).rowcount
                if inserted:
                    result['auto_blocked_artists'].append({'artist_id': illust.user_id, 'artist_name': illust.user_name})
        connection.execute('INSERT OR REPLACE INTO preference_feedback VALUES (?,?,?,?,?,1)', (illust.id, action, json.dumps(contributions), illust.user_id, artist_delta))
        connection.execute('INSERT OR REPLACE INTO feedback(illust_id,action,created_at) VALUES (?,?,CURRENT_TIMESTAMP)', (illust.id, action))
        _project(connection, stop_words, boosts, ip_tags, ip_discount)
        return result


def replace_projection(path, profile=None, tag=None, delta=0):
    """Legacy direct writes become fixed-unit manual corrections, atomically.

    They do not rewrite or pretend to recover raw historical observations.
    """
    with sqlite3.connect(path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        _check_projection(connection)
        current = dict(connection.execute('SELECT tag,weight FROM xp_profile'))
        resolver = _resolver(connection)
        if profile is not None:
            # Full replacement is expressed in canonical identities, just like
            # projection; equivalent input spellings contribute only once each.
            canonical = defaultdict(float)
            for key, value in profile.items():
                canonical[resolver.resolve(key)] += value
            profile = dict(canonical)
        target = dict(profile) if profile is not None else dict(current)
        corrections = defaultdict(float)
        if profile is not None:
            connection.execute('CREATE TABLE IF NOT EXISTS preference_boost_seed (tag TEXT PRIMARY KEY, factor REAL NOT NULL, weight REAL NOT NULL, frozen INTEGER NOT NULL)')
            # Freeze cancellation in the same raw units as the seed, including
            # seeds first created while an alias was already active. Rebalance
            # only newly frozen rows; repeated replacement must not replay it.
            for raw, weight in connection.execute('SELECT tag,weight FROM preference_boost_seed WHERE frozen=0'):
                canonical = resolver.resolve(raw)
                if canonical in current:
                    corrections[raw] -= weight
                    corrections[canonical] += weight
            connection.execute('UPDATE preference_boost_seed SET frozen=1')
        if profile is None:
            tag = _resolver(connection).resolve(tag)
            target[tag] = target.get(tag, 0) + delta
        connection.execute('CREATE TABLE IF NOT EXISTS preference_manual (tag TEXT PRIMARY KEY, weight REAL NOT NULL)')
        for key in current.keys() | target.keys():
            corrections[key] += target.get(key, 0) - current.get(key, 0)
        for key, change in corrections.items():
            connection.execute('INSERT INTO preference_manual VALUES (?,?) ON CONFLICT(tag) DO UPDATE SET weight=weight+excluded.weight', (key, change))
        for table in ('xp_profile', 'preference_projection'):
            connection.execute(f'DELETE FROM {table}')
            connection.executemany(f'INSERT INTO {table}(tag,weight) VALUES (?,?)', target.items())


def reset(connection):
    """Reset positive observations; retain negative/artist/feedback history.

    Cached bookmarks will be ingested once on the next build. Feedback is
    retained as opaque history so a later switch starts a new contribution.
    Must run inside the caller's transaction with all other reset operations.
    """
    for table in ('preference_base', 'preference_projection', 'preference_bookmarks'):
        connection.execute(f'DELETE FROM {table}')
    for table in ('preference_anchor', 'preference_manual'):
        connection.execute(f'CREATE TABLE IF NOT EXISTS {table} (tag TEXT PRIMARY KEY, {"factor" if table == "preference_anchor" else "weight"} REAL NOT NULL)')
        connection.execute(f'DELETE FROM {table}')
    connection.execute('CREATE TABLE IF NOT EXISTS preference_boost_seed (tag TEXT PRIMARY KEY, factor REAL NOT NULL, weight REAL NOT NULL, frozen INTEGER NOT NULL)')
    connection.execute('DELETE FROM preference_boost_seed')
    connection.execute('CREATE TABLE IF NOT EXISTS preference_feedback_observations (illust_id INTEGER PRIMARY KEY, observations TEXT NOT NULL)')
    connection.execute('DELETE FROM preference_feedback_observations')
    connection.execute("UPDATE preference_feedback SET contributions='{}',artist_id=NULL,artist_delta=0,reversible=0")


def reset_path(path):
    with sqlite3.connect(path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        reset(connection)
        for table in ('xp_profile', 'xp_tag_pairs', 'ai_error_logs', 'strategy_stats'):
            connection.execute(f'DELETE FROM {table}')

#!/usr/bin/env python3
"""Export human-labelled tags as a weighted, non-mutating shadow input file."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def export_manual_labels(db_path: Path, output_path: Path, *, priority_limit: int) -> int:
    """Export manual labels ordered by Preference Profile impact without changing the DB."""
    if priority_limit < 1:
        raise ValueError("priority_limit 必须至少为 1")
    resolved_db_path = db_path.resolve()
    if not resolved_db_path.exists():
        raise FileNotFoundError(f"数据库不存在: {resolved_db_path}")
    with sqlite3.connect(f"file:{resolved_db_path}?mode=ro", uri=True) as db:
        rows = db.execute("""
            SELECT c.normalized_tag, t.translated_name, c.classification,
                   COALESCE(p.weight, 0)
            FROM tag_classification_cache AS c
            LEFT JOIN xp_profile AS p ON p.tag = c.normalized_tag
            LEFT JOIN tag_translations AS t ON t.name = c.normalized_tag
            WHERE c.source = 'manual'
            ORDER BY ABS(COALESCE(p.weight, 0)) DESC, c.normalized_tag ASC
        """).fetchall()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output:
        for rank, (tag, translation, classification, profile_weight) in enumerate(rows, start=1):
            weight = float(profile_weight)
            output.write(json.dumps({
                "tag": tag,
                "translation": translation,
                "expected_classification": classification,
                "profile_weight": weight,
                "priority": rank <= priority_limit and abs(weight) > 0,
            }, ensure_ascii=False) + "\n")
    return len(rows)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/pixiv_xp.db"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--priority-limit", type=int, default=50,
                        help="Mark the highest-weighted manual tags as priority (default: 50)")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        count = export_manual_labels(args.db, args.output, priority_limit=args.priority_limit)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"shadow input export failed: {exc}")
        return 2
    print(f"exported {count} human-labelled tags to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

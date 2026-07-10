"""Review unresolved tags without editing the SQLite database by hand."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database as db
from tag_categories import TAG_CATEGORY_UNRESOLVED, normalize_tag_category


async def list_reviews(limit: int) -> list[dict]:
    await db.init_db()
    return await db.get_tag_review_queue(limit)


async def submit_review(tag: str, classification: str) -> None:
    category = normalize_tag_category(classification)
    if category == TAG_CATEGORY_UNRESOLVED:
        raise ValueError("classification must be feature, character, copyright, artist, or non_preference")
    await db.init_db()
    await db.review_tag_classification(tag, category)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    list_command = commands.add_parser("list", help="list unresolved tags by recommendation impact")
    list_command.add_argument("--limit", type=int, default=100)
    review_command = commands.add_parser("review", help="record a human Tag Category decision")
    review_command.add_argument("tag")
    review_command.add_argument("classification")
    args = parser.parse_args(argv)
    if args.command == "list":
        print(json.dumps(asyncio.run(list_reviews(args.limit)), ensure_ascii=False, indent=2, default=str))
    else:
        asyncio.run(submit_review(args.tag, args.classification))
        print(f"reviewed {args.tag} as {args.classification}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

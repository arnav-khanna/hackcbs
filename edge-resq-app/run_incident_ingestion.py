"""Load a JSONL file of pre-computed embeddings into YugabyteDB.

Run from the directory that CONTAINS the `app` package:

    python -m app.run_incident_ingestion /path/to/incidents_labeled.jsonl
    python -m app.run_incident_ingestion /path/to/other.jsonl --dataset other

Use a different --dataset per file. Row ids are derived from the row number, so
two files ingested without one would overwrite-skip each other's rows.
"""
import argparse
import asyncio
import sys
from pathlib import Path

from app.models.database import close_db
from app.rag.ingestion import ingest_jsonl


async def main(jsonl_path: Path, dataset: str | None) -> None:
    try:
        print(await ingest_jsonl(jsonl_path=jsonl_path, dataset=dataset))
    finally:
        await close_db()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("jsonl_path", type=Path)
    parser.add_argument(
        "--dataset", default=None,
        help="Name mixed into row ids so different files cannot collide (default: legacy ids).",
    )
    args = parser.parse_args()
    if not args.jsonl_path.is_file():
        sys.exit(f"File not found: {args.jsonl_path}")
    asyncio.run(main(args.jsonl_path, args.dataset))

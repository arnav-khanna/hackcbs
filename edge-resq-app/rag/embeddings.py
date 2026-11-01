"""Local embeddings via Ollama (Nomic).

`ollama_embed` keeps the request/validation logic of the original CSV script
(Ollama `/api/embed`, count + emptiness checks) but is async and config-driven.
The original CSV -> JSONL command line is retained at the bottom of the file.
"""
import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from app.config import (
    EMBED_BATCH_SIZE,
    EMBED_DOC_PREFIX,
    EMBED_MODEL,
    EMBED_QUERY_PREFIX,
    OLLAMA_BASE_URL,
    OLLAMA_TIMEOUT,
    VECTOR_DIMENSION,
)
from app.rag.errors import DimensionMismatchError, EmbeddingError

DEFAULT_MODEL = EMBED_MODEL
DEFAULT_OLLAMA_URL = OLLAMA_BASE_URL
DEFAULT_TEXT_COLUMNS = ["user", "assistant", "source", "topic"]


async def ollama_embed(
    texts: list[str],
    ollama_url: str | None = None,
    model: str | None = None,
    timeout: float | None = None,
) -> list[list[float]]:
    """Generate embeddings using Ollama's /api/embed endpoint."""
    ollama_url = ollama_url or OLLAMA_BASE_URL
    model = model or EMBED_MODEL
    timeout = timeout or OLLAMA_TIMEOUT
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{ollama_url.rstrip('/')}/api/embed",
                json={"model": model, "input": texts},
            )
    except httpx.HTTPError as exc:
        raise EmbeddingError(
            f"Could not reach Ollama at {ollama_url}: {exc!r}. Is it running?"
        ) from exc

    if response.status_code == 404:
        raise EmbeddingError(
            f"Embedding model '{model}' is not available in Ollama. "
            f"Pull it with: ollama pull {model}"
        )
    if response.is_error:
        raise EmbeddingError(
            f"Ollama embed failed ({response.status_code}): {response.text[:300]}"
        )

    embeddings = response.json().get("embeddings")

    if not isinstance(embeddings, list) or len(embeddings) != len(texts):
        raise EmbeddingError(
            "Unexpected Ollama response: number of embeddings does not "
            "match number of input texts."
        )

    for index, vector in enumerate(embeddings):
        if not isinstance(vector, list) or not vector:
            raise EmbeddingError(f"Empty or invalid embedding at batch index {index}.")
        if len(vector) != VECTOR_DIMENSION:
            raise DimensionMismatchError(
                f"Model '{model}' returned {len(vector)}-dim vectors but the "
                f"database expects {VECTOR_DIMENSION}. Fix VECTOR_DIMENSION and "
                "db/schema.sql together (this requires re-embedding all data)."
            )
        if not all(math.isfinite(v) for v in vector):
            raise EmbeddingError(f"Non-finite values in embedding at index {index}.")

    return embeddings


async def embed_documents(texts: list[str]) -> list[list[float]]:
    """Embed document chunks (batched), applying the document task prefix."""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = [EMBED_DOC_PREFIX + t for t in texts[start : start + EMBED_BATCH_SIZE]]
        vectors.extend(await ollama_embed(batch))
    return vectors


async def embed_query(query: str) -> list[float]:
    """Embed a user query with the same model, using the query task prefix."""
    return (await ollama_embed([EMBED_QUERY_PREFIX + query]))[0]


async def probe_embedding_dimension() -> int:
    """Embed a probe string and return the model's actual output size.

    Used by /rag/health so the dimension is observed, not assumed.
    """
    async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
        try:
            response = await client.post(
                f"{OLLAMA_BASE_URL.rstrip('/')}/api/embed",
                json={"model": EMBED_MODEL, "input": ["dimension probe"]},
            )
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"Could not reach Ollama: {exc!r}") from exc
    if response.status_code == 404:
        raise EmbeddingError(f"Embedding model '{EMBED_MODEL}' is not pulled in Ollama.")
    if response.is_error:
        raise EmbeddingError(f"Ollama embed failed ({response.status_code}).")
    return len(response.json()["embeddings"][0])


# --------------------------------------------------------------------------
# Original CSV -> JSONL command line (unchanged behaviour; needs pandas, which
# is a dev-only dependency and not part of the API image).
# --------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate local nomic-embed-text embeddings for CSV rows."
    )
    parser.add_argument("--input", required=True, help="Input CSV path")
    parser.add_argument(
        "--output",
        default="embeddings.jsonl",
        help="Output JSONL path (default: embeddings.jsonl)",
    )
    parser.add_argument(
        "--ollama-url",
        default=DEFAULT_OLLAMA_URL,
        help=f"Ollama base URL (default: {DEFAULT_OLLAMA_URL})",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Ollama embedding model (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--text-columns",
        nargs="+",
        default=DEFAULT_TEXT_COLUMNS,
        help="CSV columns to combine into each embedding",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=32,
        help="Number of rows to send per Ollama embedding request (default: 32)",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append to the output instead of overwriting it",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of rows to process, useful for testing",
    )
    return parser.parse_args()


def clean_value(value: Any) -> str:
    """Convert a CSV value to clean text, treating missing values as empty."""
    import pandas as pd

    if pd.isna(value):
        return ""
    return str(value).strip()


def build_embedding_text(row, columns: list[str]) -> str:
    """
    Build meaningful text for semantic search.

    `system` is intentionally excluded by default because it repeats the same
    instruction in every row. Add it to --text-columns only if you need it.
    """
    parts = []
    for column in columns:
        value = clean_value(row.get(column, ""))
        if value:
            parts.append(f"{column}: {value}")
    return "\n".join(parts)


def main() -> int:
    import pandas as pd

    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.is_file():
        print(f"ERROR: Input CSV not found: {input_path}", file=sys.stderr)
        return 1

    if args.batch_size < 1:
        print("ERROR: --batch-size must be at least 1.", file=sys.stderr)
        return 1

    try:
        df = pd.read_csv(input_path)
    except Exception as exc:
        print(f"ERROR: Could not read CSV: {exc}", file=sys.stderr)
        return 1

    missing_columns = [
        column for column in args.text_columns if column not in df.columns
    ]
    if missing_columns:
        print(
            f"ERROR: Columns not found: {missing_columns}\n"
            f"Available columns: {list(df.columns)}",
            file=sys.stderr,
        )
        return 1

    if args.limit is not None:
        df = df.head(max(0, args.limit))

    if df.empty:
        print("No rows to process.")
        return 0

    texts = [build_embedding_text(row, args.text_columns) for _, row in df.iterrows()]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.append else "w"
    total = len(texts)
    written = 0
    skipped = 0

    print(f"Input rows: {total}")
    print(f"Embedding model: {args.model}")
    print(f"Ollama URL: {args.ollama_url}")
    print(f"Text columns: {args.text_columns}")
    print(f"Output: {output_path}")

    try:
        with output_path.open(mode, encoding="utf-8") as out_file:
            for start in range(0, total, args.batch_size):
                end = min(start + args.batch_size, total)
                batch_texts = texts[start:end]

                # Empty rows cannot provide useful semantic search vectors.
                nonempty_positions = [
                    i for i, text in enumerate(batch_texts) if text.strip()
                ]
                nonempty_texts = [batch_texts[i] for i in nonempty_positions]
                vectors_by_position: dict[int, list[float]] = {}

                if nonempty_texts:
                    # Same document prefix as embed_documents(); queries use the
                    # query prefix, so unprefixed document vectors rank worse.
                    vectors = asyncio.run(
                        ollama_embed(
                            [EMBED_DOC_PREFIX + t for t in nonempty_texts],
                            args.ollama_url,
                            args.model,
                        )
                    )
                    for position, vector in zip(nonempty_positions, vectors):
                        vectors_by_position[position] = vector

                for local_index, text in enumerate(batch_texts):
                    row_index = start + local_index
                    row = df.iloc[row_index]
                    if local_index not in vectors_by_position:
                        # ingest_jsonl rejects records without text/embedding,
                        # so one empty row would abort the whole load.
                        skipped += 1
                        continue

                    metadata = {}
                    for column in ["source", "topic", "chunk_id"]:
                        if column in df.columns:
                            value = clean_value(row[column])
                            if value:
                                metadata[column] = value

                    record = {
                        "row_id": int(row_index),
                        "text": text,
                        "metadata": metadata,
                        "embedding": vectors_by_position.get(local_index),
                    }
                    out_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                    written += 1

                out_file.flush()
                print(f"Processed {end}/{total} rows")
                time.sleep(0.05)

    except EmbeddingError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: Embedding generation failed: {exc}", file=sys.stderr)
        return 1

    print(f"Done. Wrote {written} records to {output_path} ({skipped} empty rows skipped)")
    print("Insert them into YugabyteDB with app.rag.ingestion.ingest_jsonl().")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# run (from the repo root, with the app package importable):
#   python -m app.rag.embeddings --input "dataset(1).csv" --output "embeddings.jsonl"

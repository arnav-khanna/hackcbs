import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests

DEFAULT_MODEL = "nomic-embed-text"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_TEXT_COLUMNS = ["user", "assistant", "source", "topic"]


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
    if pd.isna(value):
        return ""
    return str(value).strip()


def build_embedding_text(row: pd.Series, columns: list[str]) -> str:
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


def ollama_embed(
    texts: list[str],
    ollama_url: str,
    model: str,
    timeout: int = 180,
) -> list[list[float]]:
    """Generate embeddings using Ollama's /api/embed endpoint."""
    response = requests.post(
        f"{ollama_url.rstrip('/')}/api/embed",
        json={"model": model, "input": texts},
        timeout=timeout,
    )
    response.raise_for_status()

    payload = response.json()
    embeddings = payload.get("embeddings")

    if not isinstance(embeddings, list) or len(embeddings) != len(texts):
        raise RuntimeError(
            "Unexpected Ollama response: number of embeddings does not "
            "match number of input texts."
        )

    for index, vector in enumerate(embeddings):
        if not isinstance(vector, list) or not vector:
            raise RuntimeError(f"Empty or invalid embedding at batch index {index}.")

    return embeddings


def main() -> int:
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

    # For this dataset, source/topic are metadata, while user + assistant
    # capture the question and its answer. Repeated system instructions are
    # excluded from embeddings by default.
    texts = [build_embedding_text(row, args.text_columns) for _, row in df.iterrows()]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.append else "w"
    total = len(texts)
    written = 0

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
                    vectors = ollama_embed(
                        nonempty_texts,
                        args.ollama_url,
                        args.model,
                    )
                    for position, vector in zip(nonempty_positions, vectors):
                        vectors_by_position[position] = vector

                for local_index, text in enumerate(batch_texts):
                    row_index = start + local_index
                    row = df.iloc[row_index]

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

    except requests.RequestException as exc:
        print(
            f"ERROR: Could not reach Ollama or generate embeddings: {exc}\n"
            "Check that Ollama is running and the model is available. "
            f"Pull it with: ollama pull {args.model}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"ERROR: Embedding generation failed: {exc}", file=sys.stderr)
        return 1

    print(f"Done. Wrote {written} records to {output_path}")
    print(
        "Embeddings are stored in JSONL; they have not been inserted into YugabyteDB."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# run python embeddings.py --input "dataset(1).csv" --output "embeddings.jsonl"

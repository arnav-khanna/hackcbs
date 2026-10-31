import json
from pathlib import Path
from typing import Iterator

from app.config import VECTOR_DIMENSION


def load_jsonl(path: str | Path) -> Iterator[dict]:
    file_path = Path(path)

    if not file_path.is_file():
        raise FileNotFoundError(f"JSONL file not found: {file_path}")

    with file_path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at line {line_number}: {exc}") from exc

            text = record.get("text")
            embedding = record.get("embedding")

            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"Missing text at line {line_number}")

            if not isinstance(embedding, list):
                raise ValueError(f"Missing embedding at line {line_number}")

            if len(embedding) != VECTOR_DIMENSION:
                raise ValueError(
                    f"Expected {VECTOR_DIMENSION} dimensions at "
                    f"line {line_number}, got {len(embedding)}"
                )

            if not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and __import__("math").isfinite(value)
                for value in embedding
            ):
                raise ValueError(f"Invalid embedding values at line {line_number}")

            yield {
                "row_id": record.get("row_id", line_number - 1),
                "content": text,
                "metadata": record.get("metadata") or {},
                "embedding": embedding,
            }

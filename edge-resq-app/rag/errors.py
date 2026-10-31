"""Domain errors for the RAG pipeline.

`status_code` is the HTTP status the API layer should answer with; the
processing code itself never imports FastAPI.
"""


class RagError(Exception):
    status_code = 500


class InvalidPdfError(RagError):
    status_code = 422


class EmptyPdfError(RagError):
    """PDF is valid but yielded no extractable text (e.g. scanned images)."""

    status_code = 422


class FileTooLargeError(RagError):
    status_code = 413


class EmbeddingError(RagError):
    status_code = 502


class DimensionMismatchError(RagError):
    status_code = 500


class GenerationError(RagError):
    status_code = 502


class DatabaseUnavailableError(RagError):
    status_code = 503

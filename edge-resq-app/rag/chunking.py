"""Character-window chunking with overlap, per page.

Chunks never span pages, so every chunk has exactly one page number for
citation. A procedure that continues across a page break is split there.
"""
from dataclasses import dataclass

from app.rag.pdf import Page

_SEPARATORS = ("\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ")


@dataclass(frozen=True)
class Chunk:
    page: int
    index: int  # 0-based, unique within the document
    text: str


def split_text(text: str, size: int, overlap: int) -> list[str]:
    if size < 1 or not 0 <= overlap < size:
        raise ValueError("Require chunk size >= 1 and 0 <= overlap < size")
    if len(text) <= size:
        return [text.strip()] if text.strip() else []

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            floor = start + int(size * 0.6)  # only cut in the last 40% of the window
            for sep in _SEPARATORS:
                idx = text.rfind(sep, floor, end)
                if idx != -1:
                    end = idx + len(sep)
                    break
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        nxt = max(end - overlap, start + 1)
        if nxt > 0 and not text[nxt - 1].isspace():  # start on a word boundary
            sp = text.find(" ", nxt, end)
            if sp != -1:
                nxt = sp + 1
        start = nxt
    return chunks


def chunk_pages(pages: list[Page], size: int, overlap: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    for page in pages:
        for piece in split_text(page.text, size, overlap):
            chunks.append(Chunk(page=page.number, index=len(chunks), text=piece))
    return chunks

"""Split report chunks into complete, quotable sentences.

The guidance step asks the model to *pick numbered sentences* instead of
copying text. Small models paraphrase when asked to quote, which used to fail
validation and throw the whole answer away; picking a number cannot paraphrase.
For that to be safe, every candidate must be a complete sentence or list item,
so this module is deliberately conservative:

* a list item that follows a lead-in line ("If you are indoors:") keeps the
  lead-in, so the condition is never dropped;
* text that starts mid-sentence (chunk overlap) or ends without terminal
  punctuation (truncated chunk end) is not offered.
"""
import re
import unicodedata
from dataclasses import dataclass

MIN_WORDS = 3
MAX_CHARS = 500

_LIST_ITEM = re.compile(
    r"^\s*(?:[-•●▪◦‣–*]\s+|\(?\d{1,3}[.)]\s+|\(?[A-Za-z][.)]\s+|step\s*\d+[.:)]?\s+)",
    re.IGNORECASE,
)
_MARKER_ONLY = re.compile(r"^\(?(?:\d{1,3}|[A-Za-z])[.)]$")
_SENTENCE_END = re.compile(r"""[.!?]+["')\]]*\s+""")
_TERMINATED = re.compile(r"""[.!?]["')\]]*$""")
_ABBREVIATIONS = frozenset({"dr", "mr", "mrs", "ms", "no", "vs", "approx", "min", "max", "st"})


@dataclass(frozen=True)
class Sentence:
    text: str


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _split_line(line: str) -> list[str]:
    """Split one line into sentences without breaking on "1." or "approx. 5"."""
    parts: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(line):
        before = line[start:match.start() + 1].split()
        last = before[-1] if before else ""
        token = last.rstrip(".!?").lower()
        if _MARKER_ONLY.match(last) or token in _ABBREVIATIONS:
            continue
        # a lowercase continuation means this was not a sentence boundary
        rest = line[match.end():]
        if rest[:1].islower():
            continue
        parts.append(line[start:match.end()].strip())
        start = match.end()
    tail = line[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _first_letter_lower(text: str) -> bool:
    for ch in text:
        if ch.isalpha():
            return ch.islower()
    return False


def split_sentences(chunk_text: str) -> list[Sentence]:
    """Return the complete sentences / list items of one chunk, in order.

    A list item that follows a lead-in line is returned as "<lead-in> <item>".
    The two parts are verbatim in the chunk but not adjacent when several items
    share one lead-in.
    """
    lines = [ln.strip() for ln in chunk_text.split("\n")]
    nonblank = [i for i, ln in enumerate(lines) if ln]
    if not nonblank:
        return []
    first_line, last_line = nonblank[0], nonblank[-1]
    out: list[Sentence] = []
    lead = ""  # lead-in line ("If you are indoors:") applying to following items

    for index, line in enumerate(lines):
        if not line:
            lead = ""
            continue
        is_item = bool(_LIST_ITEM.match(line))
        if line.endswith(":") and not is_item:
            lead = line
            continue

        if is_item:
            # A line break is a safe boundary, so punctuation is optional,
            # except on the chunk's last line, which may be cut off.
            if index == last_line and not _TERMINATED.search(line):
                continue
            _add(out, f"{lead} {line}" if lead else line)
            continue

        lead = ""
        for n, piece in enumerate(_split_line(line)):
            # The first piece of a chunk can be the tail of a sentence that
            # started in the previous chunk (overlap starts on a word boundary).
            if n == 0 and index == first_line and _first_letter_lower(piece):
                continue
            # Unterminated text is a heading or a truncated sentence.
            if not _TERMINATED.search(piece):
                continue
            _add(out, piece)
    return out


def _add(out: list[Sentence], text: str) -> None:
    text = text.strip()
    if len(text.split()) < MIN_WORDS or len(text) > MAX_CHARS:
        return
    out.append(Sentence(text))


def contains_passage(passage: str, source: str) -> bool:
    """True if `passage` occurs verbatim in `source` (whitespace-insensitive)."""
    p = _normalize(passage)
    return bool(p) and p in _normalize(source)

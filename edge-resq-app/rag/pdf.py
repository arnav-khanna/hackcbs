"""PDF text extraction and conservative cleaning.

Cleaning only fixes extraction artefacts (ligatures, control characters,
hard-wrapped lines, hyphenation at line ends, page-number footers). It never
drops content lines: warnings, numbers, units and procedure steps are kept.
"""
import io
import logging
import re
import unicodedata
from dataclasses import dataclass

from pypdf import PdfReader

from app.rag.errors import EmptyPdfError, InvalidPdfError

logger = logging.getLogger(__name__)

_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"}
_INVISIBLE = dict.fromkeys(map(ord, "­​‌‍⁠﻿"), None)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HYPHEN_BREAK = re.compile(r"(?<=[a-z])-\n(?=[a-z])")
_LIST_START = re.compile(
    r"^\s*(?:[-•●▪◦‣–*]\s|\(?\d{1,3}[.)]\s|\(?[A-Za-z][.)]\s|step\s*\d)",
    re.IGNORECASE,
)
_PAGE_LABEL = re.compile(
    r"^\s*(?:page\s+\d{1,4}(?:\s+of\s+\d{1,4})?|\d{1,4}\s*/\s*\d{1,4}|[-–—]\s*\d{1,4}\s*[-–—])\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Page:
    number: int  # 1-based physical page number
    text: str


def _is_heading(line: str) -> bool:
    s = line.strip()
    return 0 < len(s) <= 60 and any(c.isalpha() for c in s) and s == s.upper() and not s.endswith(",")


def clean_text(raw: str, page_number: int | None = None) -> str:
    text = unicodedata.normalize("NFC", raw)  # NFC, not NFKC: keeps m², ½, °, etc.
    for src, dst in _LIGATURES.items():
        text = text.replace(src, dst)
    text = text.translate(_INVISIBLE).replace("\xa0", " ")
    text = _CONTROL.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    text = _HYPHEN_BREAK.sub("", text)

    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]

    # Drop page-number footers only when unambiguous.
    nonblank = [i for i, ln in enumerate(lines) if ln]
    for i in nonblank:
        if _PAGE_LABEL.match(lines[i]):
            lines[i] = ""
    if nonblank and page_number is not None:
        for i in (nonblank[0], nonblank[-1]):
            if lines[i] == str(page_number):
                lines[i] = ""

    # Re-join hard-wrapped lines, keep paragraph, list and heading breaks.
    out: list[str] = []
    prev = ""
    for ln in lines:
        if not ln:
            if out and out[-1] != "":
                out.append("")
            prev = ""
            continue
        if not prev:
            out.append(ln)
        elif _LIST_START.match(ln) or prev.endswith(":") or _is_heading(prev) or _is_heading(ln):
            out.append(ln)
        else:
            out[-1] = f"{out[-1]} {ln}"
        prev = ln
    return "\n".join(out).strip()


def extract_pages(data: bytes) -> tuple[list[Page], int]:
    """Return (pages that contain text, total page count).

    Raises InvalidPdfError for non-PDF / corrupt / password-protected files and
    EmptyPdfError when no page yields text (typically a scanned PDF).
    """
    if b"%PDF-" not in data[:1024]:
        raise InvalidPdfError("File is not a PDF (missing %PDF header).")

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise InvalidPdfError("PDF is password protected.")
        total = len(reader.pages)
    except InvalidPdfError:
        raise
    except Exception as exc:
        raise InvalidPdfError(f"Could not read PDF: {exc}") from exc

    pages: list[Page] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            raw = page.extract_text() or ""
        except Exception as exc:  # one bad page must not lose the document
            logger.warning("Text extraction failed on page %d: %s", number, exc)
            continue
        text = clean_text(raw, number)
        if text:
            pages.append(Page(number, text))

    if not pages:
        raise EmptyPdfError(
            "PDF contains no extractable text (it may be a scan; OCR is not supported)."
        )
    return pages, total

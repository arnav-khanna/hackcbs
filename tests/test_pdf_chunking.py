"""Unit tests: PDF extraction, cleaning, chunking. No services required."""
import io

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.rag.chunking import chunk_pages, split_text
from app.rag.errors import EmptyPdfError, InvalidPdfError
from app.rag.pdf import Page, clean_text, extract_pages


def make_pdf(pages: list[list[str]], encrypt: str | None = None) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, encrypt=encrypt)
    for lines in pages:
        y = 800
        for line in lines:
            c.drawString(50, y, line)
            y -= 14
        c.showPage()
    c.save()
    return buf.getvalue()


# ---- cleaning -------------------------------------------------------------


def test_clean_rejoins_wrapped_lines_but_keeps_steps_and_headings():
    raw = (
        "FLOOD EVACUATION\n"
        "Move to higher ground above the\nflood level immediately.\n"
        "1. Carry drinking water.\n"
        "2. Switch off the gas.\n"
        "Note:\nnever walk through moving water."
    )
    out = clean_text(raw)
    assert "Move to higher ground above the flood level immediately." in out
    assert "\n1. Carry drinking water.\n2. Switch off the gas." in out
    assert out.startswith("FLOOD EVACUATION\nMove")
    assert "Note:\nnever walk" in out


def test_clean_keeps_units_numbers_and_removes_only_artefacts():
    raw = "Keep 4 m² clear and ﬁre exits ½ open.\x00­Temp 40°C\n12\nPage 3 of 9"
    out = clean_text(raw, page_number=1)
    assert "4 m²" in out and "½" in out and "40°C" in out
    assert "fire exits" in out  # ligature expanded
    assert "\x00" not in out and "­" not in out
    assert "Page 3 of 9" not in out  # unambiguous footer dropped
    assert out.endswith("40°C 12")  # bare "12" is not page 1's number: kept


def test_clean_drops_bare_number_only_when_it_is_the_pages_own_number():
    assert clean_text("Some text here.\n7", page_number=7) == "Some text here."
    assert clean_text("Some text here.\n7", page_number=2) == "Some text here. 7"


def test_clean_dehyphenates_only_lowercase_line_breaks():
    assert clean_text("evac-\nuation plan") == "evacuation plan"
    assert "A-\nB" in clean_text("A-\nB") or "A- B" in clean_text("A-\nB")


# ---- chunking -------------------------------------------------------------


def test_split_text_respects_size_overlap_and_covers_everything():
    words = [f"w{i}" for i in range(600)]
    text = " ".join(words)
    chunks = split_text(text, 200, 40)
    assert len(chunks) > 5
    assert all(len(c) <= 200 for c in chunks)
    joined = " ".join(chunks)
    for w in words:
        assert w in joined
    # consecutive chunks overlap
    assert any(chunks[i].split()[-1] in chunks[i + 1].split() for i in range(len(chunks) - 1))


def test_split_text_terminates_on_unbroken_text_and_validates_args():
    chunks = split_text("x" * 5000, 800, 120)
    assert all(len(c) <= 800 for c in chunks) and len(chunks) >= 6
    with pytest.raises(ValueError):
        split_text("abc", 100, 100)


def test_chunk_pages_keeps_page_numbers_and_global_indexes():
    pages = [Page(2, "a " * 300), Page(5, "short page")]
    chunks = chunk_pages(pages, 200, 20)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert {c.page for c in chunks} == {2, 5}
    assert chunks[-1].page == 5 and chunks[-1].text == "short page"


# ---- extraction -----------------------------------------------------------


def test_extract_multi_page_pdf_keeps_page_numbers_and_skips_blank_pages():
    data = make_pdf([["Page one text about floods."], [], ["Third page about earthquakes."]])
    pages, total = extract_pages(data)
    assert total == 3
    assert [p.number for p in pages] == [1, 3]
    assert "floods" in pages[0].text and "earthquakes" in pages[1].text


def test_extract_rejects_non_pdf_corrupt_empty_and_encrypted():
    with pytest.raises(InvalidPdfError):
        extract_pages(b"just some text, not a pdf")
    with pytest.raises(InvalidPdfError):
        extract_pages(b"%PDF-1.4\n garbage garbage garbage")
    with pytest.raises(EmptyPdfError):
        extract_pages(make_pdf([[], []]))
    with pytest.raises(InvalidPdfError, match="password"):
        extract_pages(make_pdf([["secret"]], encrypt="pw"))

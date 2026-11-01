from app.rag.sentences import contains_passage, split_sentences


def texts(chunk):
    return [s.text for s in split_sentences(chunk)]


def test_drops_chunk_start_tail_and_truncated_end():
    chunk = "e shaking stops. Leave the building by the stairs. Never touch wires or"
    assert texts(chunk) == ["Leave the building by the stairs."]


def test_list_items_keep_their_lead_in():
    chunk = "If you are indoors:\n- Drop and take cover under a sturdy table\n- Protect your head and neck\n\nStay calm and wait for help."
    assert texts(chunk) == [
        "If you are indoors: - Drop and take cover under a sturdy table",
        "If you are indoors: - Protect your head and neck",
        "Stay calm and wait for help.",
    ]


def test_lead_in_does_not_leak_past_a_blank_line():
    chunk = "If you are indoors:\n- Drop and take cover now\n\n- Call 112 immediately please\nOK."
    assert texts(chunk)[1] == "- Call 112 immediately please"


def test_numbers_and_abbreviations_are_not_sentence_ends():
    out = texts("Approx. 15 cm of water can knock you down. Stay away from power lines.")
    assert out == ["Approx. 15 cm of water can knock you down.", "Stay away from power lines."]


def test_headings_and_short_fragments_are_not_offered():
    assert texts("DURING A FLOOD\nGo up.\nMove to higher ground immediately.") == [
        "Move to higher ground immediately."
    ]


def test_last_list_item_without_punctuation_may_be_cut_off():
    assert texts("Do this:\n- Move to higher ground now\n- Call for help and give your exact") == [
        "Do this: - Move to higher ground now"
    ]


def test_every_part_is_verbatim_in_the_chunk():
    chunk = "If you are indoors:\n- Drop and take cover under a sturdy table\n- Protect your head and neck\n"
    for s in split_sentences(chunk):
        lead, item = s.text.split(": ", 1)
        assert contains_passage(lead + ":", chunk) and contains_passage(item, chunk)

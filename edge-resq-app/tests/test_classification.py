"""Pipeline tests with the model and database replaced by fakes."""
import asyncio

import pytest
from langchain_core.runnables import RunnableLambda

from app import classification_agent as agent
from app.classification_agent import _Selection, _Triage, _TriageIncident
from app.rag.retrieval import RetrievedChunk
from app.schemas import ClassificationResponse


def run(coro):
    return asyncio.run(coro)


def chunk(n, content, sim=0.8):
    return RetrievedChunk(id=f"c{n}", content=content, filename="ndma.pdf", page=n,
                          chunk_index=n, similarity=sim)


FLOOD_REPORT = chunk(1, "Move to higher ground immediately. Do not walk through moving floodwater. "
                        "Keep an emergency kit ready before the monsoon season.")
FIRE_REPORT = chunk(2, "Get out and stay out of a burning building. Do not use lifts.")


def triage_returns(*claims, situation="x"):
    result = _Triage(situation=situation, incidents=[_TriageIncident(incident_type=t, evidence=e) for t, e in claims])
    return RunnableLambda(lambda _: result)


def select_returns(**lists):
    result = _Selection(**lists)
    return RunnableLambda(lambda _: result)


@pytest.fixture
def wire(monkeypatch):
    """Install fakes; returns a setter so each test chooses its own."""
    def install(triage=None, selection=None, chunks=None, retrieval_error=None):
        monkeypatch.setattr(agent, "_triage_chain", lambda: triage)
        monkeypatch.setattr(agent, "_selection_chain", lambda: selection)

        async def fake_retrieve(query, top_k=None):
            if retrieval_error:
                raise retrieval_error
            return list(chunks or [])
        monkeypatch.setattr(agent, "retrieve_chunks", fake_retrieve)
    return install


def failing():
    def boom(_):
        raise RuntimeError("ollama down")
    return RunnableLambda(boom)


# ---- regression: the playbook used to be silently dropped by the schema ----

def test_response_schema_carries_the_playbook():
    result = agent.classify_incident("the building is on fire")
    dumped = ClassificationResponse.model_validate(result.model_dump())
    assert dumped.emergency_guidance and dumped.emergency_guidance.incident_type == "fire"
    assert "emergency_guidance" in ClassificationResponse.model_json_schema()["properties"]


# ---- happy path ----

def test_meaning_based_flood_with_cited_guidance(wire):
    wire(
        triage=triage_returns(("flood", "Water is coming into the house")),
        selection=select_returns(actions=[1], warnings=[2]),
        chunks=[FLOOD_REPORT],
    )
    r = run(agent.classify_sos("Water is coming into the house and we are on the roof"))
    assert (r.incident_type, r.method, r.grounding_status, r.grounded) == ("flood", "rag", "grounded", True)
    assert r.recommended_actions[0].text == "Move to higher ground immediately."
    assert r.warnings[0].text == "Do not walk through moving floodwater."
    assert r.recommended_actions[0].source_id == 1
    assert r.emergency_guidance.incident_type == "flood"
    assert r.incidents[0].matched_keywords[0] == "Water is coming into the house"


def test_model_cannot_pick_text_it_was_not_shown(wire):
    wire(
        triage=triage_returns(("flood", "river has come over the road")),
        selection=select_returns(actions=[1, 99, 1], warnings=[1]),
        chunks=[FLOOD_REPORT],
    )
    r = run(agent.classify_sos("the river has come over the road"))
    assert [g.text for g in r.recommended_actions] == ["Move to higher ground immediately."]
    assert r.warnings == []  # sentence 1 already used; duplicates are dropped


# ---- verification of the model's claim ----

def test_hallucinated_incident_is_rejected_and_keywords_answer(wire):
    wire(triage=triage_returns(("earthquake", "the ground shook violently")))
    r = run(agent.classify_sos("my house is on fire"))
    assert r.incident_type == "fire" and r.method == "keyword_rules"
    assert r.grounding_status == "invalid_output"


def test_unverified_wording_is_accepted_when_keywords_agree(wire):
    wire(
        triage=triage_returns(("fire", "paraphrased wording not in message")),
        selection=select_returns(),
        chunks=[FIRE_REPORT],
    )
    r = run(agent.classify_sos("my house is on fire"))
    assert r.incident_type == "fire" and r.method == "rag"
    assert r.incidents[0].matched_keywords == ["on fire"]


def test_negation_survives_when_model_returns_nothing(wire):
    wire(triage=triage_returns())
    r = run(agent.classify_sos("there is no fire here"))
    assert r.incident_type == "unknown" and r.emergency_guidance.incident_type == "general"


# ---- degradation: a person in danger always gets the playbook ----

def test_model_down_falls_back_to_keywords_with_playbook(wire):
    wire(triage=failing())
    r = run(agent.classify_sos("flood water rising"))
    assert (r.incident_type, r.grounding_status) == ("flood", "unavailable")
    assert r.emergency_guidance.do_now


def test_retrieval_down_keeps_the_model_classification(wire):
    wire(triage=triage_returns(("flood", "Water is coming into the house")),
         retrieval_error=ConnectionError("db down"))
    r = run(agent.classify_sos("Water is coming into the house"))
    assert (r.incident_type, r.method, r.grounding_status, r.grounded) == ("flood", "rag", "unavailable", False)
    assert r.emergency_guidance.incident_type == "flood"


def test_selection_failure_keeps_the_model_classification(wire):
    wire(triage=triage_returns(("flood", "Water is coming into the house")),
         selection=failing(), chunks=[FLOOD_REPORT])
    r = run(agent.classify_sos("Water is coming into the house"))
    assert (r.incident_type, r.grounding_status) == ("flood", "unavailable")
    assert len(r.sources) == 1


def test_all_invalid_numbers_is_reported_not_trusted(wire):
    wire(triage=triage_returns(("flood", "Water is coming into the house")),
         selection=select_returns(actions=[40, 41]), chunks=[FLOOD_REPORT])
    r = run(agent.classify_sos("Water is coming into the house"))
    assert r.grounding_status == "invalid_output" and r.recommended_actions == []


def test_triage_timeout_is_a_fallback_not_a_hang(wire, monkeypatch):
    async def slow(_):
        await asyncio.sleep(5)
    monkeypatch.setattr(agent, "TRIAGE_TIMEOUT_S", 0.05)
    wire(triage=RunnableLambda(slow))
    r = run(agent.classify_sos("the market is on fire"))
    assert r.incident_type == "fire" and r.grounding_status == "unavailable"


# ---- multiple hazards, danger, context budget ----

def test_collapse_is_critical_and_keyword_only_hazards_get_their_playbook(wire):
    wire(
        triage=triage_returns(("earthquake", "The building next door collapsed")),
        selection=select_returns(), chunks=[FIRE_REPORT],
    )
    r = run(agent.classify_sos("The building next door collapsed and there is fire in the rubble"))
    assert r.incident_type == "earthquake" and r.danger_level == "critical"
    assert [g.incident_type for g in r.additional_guidance] == ["fire"]
    assert "fire" in r.alert


def test_context_budget_drops_whole_chunks_never_cuts_them():
    from app.rag.rag import fit_chunks
    big = [chunk(i, "x" * 800) for i in range(20)]
    kept = fit_chunks(big, budget=6000)
    assert 1 <= len(kept) <= 7 and all(len(c.content) == 800 for c in kept)
    assert len(fit_chunks([chunk(1, "y" * 9000)], budget=100)) == 1


def test_retrieval_interleaves_incident_types(monkeypatch):
    by_query = {
        "flood": [chunk(i, f"flood {i}", 0.9 - i / 100) for i in range(3)],
        "fire": [chunk(10 + i, f"fire {i}", 0.5) for i in range(3)],
    }

    async def fake(query, top_k=None):
        return by_query["flood" if query.startswith("flood") else "fire"]
    monkeypatch.setattr(agent, "retrieve_chunks", fake)
    merged = run(agent._retrieve_for_types(["flood", "fire"], "sos", 3))
    assert [c.content for c in merged[:4]] == ["flood 0", "fire 0", "flood 1", "fire 1"]

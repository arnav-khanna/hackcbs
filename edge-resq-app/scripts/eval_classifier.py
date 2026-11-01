"""Measure incident classification accuracy on YOUR Ollama model.

    python -m app.scripts.eval_classifier                    # uses OLLAMA_CHAT_MODEL
    python -m app.scripts.eval_classifier --model qwen2.5:7b
    python -m app.scripts.eval_classifier --keyword-only     # offline baseline

Scores exact-set matches per message. Add your own real SOS messages to CASES:
a few dozen from your actual users are worth more than these hand-written ones.
"""
import argparse
import asyncio
import importlib.util
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# (message, expected incident types)
CASES: list[tuple[str, set[str]]] = [
    # fire
    ("My kitchen is full of smoke and I can't reach the door", {"fire"}),
    ("The warehouse next to our house is burning, flames are coming towards us", {"fire"}),
    ("Our whole house burned down last night, we are sleeping outside", {"fire"}),
    ("Forest fire has reached our village, we can see it on the hill", {"fire"}),
    ("Wires are sparking and the shop ceiling is on fire", {"fire"}),
    ("The fire is out now but the roof could fall", {"fire"}),
    ("aag lag gayi hai, bahut dhuan hai", {"fire"}),
    # earthquake / collapse
    ("Strong shaking for 30 seconds, the ceiling cracked and my mother is trapped", {"earthquake"}),
    ("The ground started moving and everyone ran out, now there are aftershocks", {"earthquake"}),
    ("Our building fell down and my brother is under the concrete", {"earthquake"}),
    ("Everything was shaking, the plates fell off the shelf", {"earthquake"}),
    ("The school wall collapsed on two children", {"earthquake"}),
    ("bhukamp aaya hai, ghar ki deewar gir gayi", {"earthquake"}),
    # flood
    ("Water has entered the ground floor and is still rising", {"flood"}),
    ("The river overflowed and our street is under water", {"flood"}),
    ("We were crossing the road and the water swept our car away", {"flood"}),
    ("The dam gates were opened and the village is getting submerged", {"flood"}),
    ("Heavy rain all night, knee deep water in the lane, we cannot get out", {"flood"}),
    ("Floodwater has gone down but our house is damaged and we have no food", {"flood"}),
    ("ghar mein paani bhar gaya hai, hum chhat par hain", {"flood"}),
    # cyclone
    ("A cyclone is hitting our coast, our tin roof is flying off", {"cyclone"}),
    ("Very strong winds uprooted trees and electric poles fell on the road", {"cyclone"}),
    ("Cyclone warning is on, the sea is very rough and we are in the shelter", {"cyclone"}),
    # glacial lake outburst
    ("A glacier lake burst upstream and a wall of water and rocks is coming down the valley", {"glacial_lake_outburst"}),
    ("The glacial lake above our village broke and the river is full of ice and mud", {"glacial_lake_outburst"}),
    # drought
    ("No rain for months, our wells are dry and cattle are dying", {"drought"}),
    ("The tanker has not come for ten days and we have no drinking water", {"drought"}),
    ("Crops have failed again this year because there was no rain", {"drought"}),
    # several at once
    ("Building collapsed and now there is fire in the debris", {"earthquake", "fire"}),
    ("The cyclone pushed flood water into our house", {"cyclone", "flood"}),
    ("The shaking broke the gas pipe and the kitchen is on fire", {"earthquake", "fire"}),
    ("The ground is shaking and the river is rising fast", {"earthquake", "flood"}),
    # nothing supported / negated / hypothetical
    ("I fell and my leg is bleeding badly, please send an ambulance", set()),
    ("What should I do if an earthquake happens at night?", set()),
    ("There is no flood in our area, but we need food packets", set()),
    ("Someone is following me and I feel unsafe", set()),
    ("We had a fire drill at school today", set()),
    ("My grandfather is having chest pain", set()),
]


def _load_app():
    if importlib.util.find_spec("app") is None:
        spec = importlib.util.spec_from_file_location(
            "app", _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)]
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["app"] = module
        spec.loader.exec_module(module)


def _score(name: str, results: list[tuple[str, set, set]]) -> None:
    exact = sum(got == want for _, want, got in results)
    tp = sum(len(want & got) for _, want, got in results)
    fp = sum(len(got - want) for _, want, got in results)
    fn = sum(len(want - got) for _, want, got in results)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    print(f"{name:<22} exact {exact}/{len(results)} ({exact / len(results):.0%})  "
          f"precision {precision:.0%}  recall {recall:.0%}")


async def main(keyword_only: bool) -> None:
    from app.classification_agent import _accept_incidents, _triage_chain, classify_incident
    from app.config import CHAT_MODEL

    keyword, model_raw, pipeline, failures = [], [], [], 0
    latencies: list[float] = []
    for text, want in CASES:
        kw = classify_incident(text)
        keyword.append((text, want, {i.incident_type for i in kw.incidents}))
        if keyword_only:
            continue
        try:
            started = time.perf_counter()
            triage = await _triage_chain().ainvoke({"sos": text})
            latencies.append(time.perf_counter() - started)
        except Exception as exc:
            failures += 1
            print(f"  model call failed: {text[:50]!r}: {exc!r}")
            continue
        model_raw.append((text, want, {i.incident_type for i in triage.incidents}))
        accepted, _ = _accept_incidents(triage, text, kw)
        pipeline.append((text, want, set(accepted) if accepted else set()))

    print(f"\nModel: {'(not used)' if keyword_only else CHAT_MODEL}   cases: {len(CASES)}\n")
    _score("keyword rules only", keyword)
    if not keyword_only:
        _score("model, unverified", model_raw)
        _score("model + verification", pipeline)
        if latencies:
            latencies.sort()
            print(f"\ntriage latency: median {latencies[len(latencies) // 2]:.1f}s, "
                  f"max {latencies[-1]:.1f}s; failed calls: {failures}")
        print("\nMisses in the full pipeline (model + verification):")
        for text, want, got in pipeline:
            if got != want:
                print(f"  want {sorted(want)} got {sorted(got)}  <- {text}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", help="Ollama chat model to test (sets OLLAMA_CHAT_MODEL)")
    parser.add_argument("--keyword-only", action="store_true", help="Score the offline keyword rules only")
    args = parser.parse_args()
    if args.model:
        os.environ["OLLAMA_CHAT_MODEL"] = args.model
    _load_app()
    asyncio.run(main(args.keyword_only))

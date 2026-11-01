# Changes

## Classification (the main problem)
- **Two small LLM calls instead of one big one.** Triage sees only the SOS (no reports), so
  retrieved text can't bias it or crowd the context window. Guidance is a separate call.
- **LangChain structured output** (`ChatOllama.with_structured_output`, JSON schema). The incident
  type is an enum enforced by Ollama's constrained decoding, so the model can't invent a label.
- **Few-shot examples + type definitions** in the triage prompt (keyword-free descriptions, negation,
  multi-hazard, aftermath, "nothing supported").
- **Verification.** A model claim is kept only if its evidence appears in the SOS, or the keyword
  classifier independently agrees. Otherwise the keyword result is used.
- **Guidance by sentence number.** The model picks numbered report sentences; it never copies text, so
  quotes can't be paraphrased/invented (the old verbatim-quote validation rejected most small-model output).
- **Retrieval per identified incident type**, interleaved, within `MAX_CONTEXT_CHARS`.
- Model failure, retrieval failure and bad selections each degrade separately; a good classification
  is no longer thrown away because the guidance step failed.
- Default `OLLAMA_CHAT_MODEL` is now `qwen2.5:3b` (was `qwen2.5:0.5b`). `ollama pull qwen2.5:3b`.

## Bugs fixed
- `schemas.py`: the `emergency_guidance` / `additional_guidance` patch was never applied, so the
  playbook was dropped from every API response. Applied properly.
- Context overflow: `top_k` up to 20 x 800-char chunks exceeded `num_ctx` and Ollama silently
  truncated the prompt. Now capped by `MAX_CONTEXT_CHARS` (default 6000) in both Q&A and classification.
- `run_incident_ingestion.py` imported a function that doesn't exist (`ingest_incidents_jsonl`) and
  used a non-`app` import path. Now a CLI: `python -m app.run_incident_ingestion FILE [--dataset NAME]`.
- `rag/embeddings.py` CLI embedded documents without the `search_document:` prefix that queries expect,
  and wrote empty rows that make `ingest_jsonl` reject the whole file. Both fixed.
- `ingest_jsonl`: row ids were only the row number, so a second file silently skipped colliding rows.
  Optional `dataset=` namespaces them (default unchanged, so existing data is not duplicated).
- Added `__init__.py` files and `requirements.txt` (`python-multipart` was missing; PDF upload needs it).
- Model is warmed at startup and `keep_alive` is set (default 30m) to avoid cold-start timeouts.

## New
- `tests/` (25 tests, incl. a fake-Ollama HTTP test of the real LangChain wire format).
- `scripts/eval_classifier.py` to measure accuracy on your own Ollama model.

## Layout
Imports use `from app...`, so this folder must be named `app` (or sit inside a package called `app`).

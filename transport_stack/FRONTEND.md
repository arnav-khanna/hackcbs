# EDGE-RESQ field assistant

The mobile UI is one textbox, a send button and a conversation. Start the host with `./start.sh host` and the full tower with `./start.sh server` from the repository root. See [STARTUP.md](../STARTUP.md) for setup/device selection. The original raw JSON console remains at `/console` for diagnostics.

## Request path

```text
Textbox → mobile Pluto POST /send {"text":"..."}
→ RF → tower Pluto GET /receive
→ tower bridge → POST /safety/assess
→ tower Pluto POST /send → RF → mobile GET /receive → response view
```

Every message is assessed through **`/safety/assess`**. The bridge no longer chooses separate classification/RAG/resource endpoints. The browser calls only its existing mobile Pluto `/health`, `/send` and `/receive` endpoints. Central configuration is `json_transport/frontend/config.mjs`; no cross-origin tower connections are allowed from the browser.

Text is trimmed, nonblank and at most 2,000 characters, matching the assessment API. `POST /safety/assess` now accepts `{ "text": "..." }` without requiring location. It returns classification and available local guidance, with `location: null`, `safety_plan: null`, `location_required: true` and a `location_prompt` when nearby resources need a location. No coordinates or resource availability are invented. Existing callers supplying latitude/longitude together or `place_name` retain the location-aware safety plan behavior; malformed coordinate pairs remain HTTP 422.

To provide location without another UI field, include a line in the message:

```text
Describe the incident here.
Location: your town or landmark
```

`Location: 28.6, 77.2` also works. The bridge forwards this explicitly supplied location as `place_name` or as coordinates in the actual `/safety/assess` schema. The complete original text is preserved.

## Responses and errors

The conversation displays `calm_steps`, alerts, cited actions/cautions, returned safe locations, and location prompts from the assessment. Full JSON remains available under “Response details.” All remote content uses textContent. Actions remain proposed; no approval or dispatch functionality is claimed.

The UI distinguishes radio delivery from tower acceptance and final response receipt. Asynchronous responses update the matching outgoing message through the bridge's `edge_resq.reply_to`; stable response IDs suppress repeats after uncertain ACK delivery. Reports and unsent responses are journaled by the tower bridge in `.runtime/tower-bridge.sqlite3`. The UI retains at most 100 messages in memory, and keeps the current textbox draft in per-tab sessionStorage when available. Reload does not restore previously consumed replies.

API failures, uncertain sends and disconnected radios are visible. POST requests are never automatically repeated by the browser. Read polling reconnects automatically and is cancelled on page exit. The UI remains vanilla HTML/CSS/JavaScript and needs no build or CDN.

## One receiver per endpoint

`/receive` consumes a queue. Keep one host conversation/receive console open across all browsers; Chrome and the Codex preview otherwise divide incoming messages. Web Locks prevent duplicate same-origin conversation tabs inside one browser, but cannot coordinate separate browser profiles. The server UI disables sending/receiving because its bridge owns the server inbox.

## Model readiness

The configured model remains the one selected in `.env` (currently `qwen3:8b`). Missing models download in the background during server startup; the existing assessment fallback can provide explicitly labeled built-in guidance while the configured model is unavailable. No other model is silently selected. Use `./start.sh server --wait-for-models` to require model readiness before starting the radio. A missing/empty RAG document store is reported as ungrounded; actual manuals are required for report-grounded procedures.

## Checks

```sh
PYTHONPATH=transport_stack transport_stack/.runtime/venv/bin/python -m pytest transport_stack/tests -q
node --test transport_stack/tests/frontend.test.mjs
# In a backend environment with pytest installed:
python -m pytest edge-resq-app/tests/test_safety.py -q
```

Tests cover single-textbox payload limits, assessment response shapes, exact bridge endpoint/schema forwarding, API errors, radio transport, launcher behavior and text-only/location-aware assessment validation. Hardware tests use the existing radio transport rather than frontend mock data.

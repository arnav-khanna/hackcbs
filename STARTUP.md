# Start the two EDGE-RESQ roles

Run these commands from the project directory on the respective Macs:

```sh
# Mac mini: mobile radio, mobile API and operator dashboard only
./start.sh host

# Laptop: YugabyteDB, Ollama, tower API, MCP, tower radio and forwarding bridge
./start.sh server
```

The assignments may be swapped, but run one host and one server. A separate Pluto belongs to each role. The current ports are host **12000**, server radio **12001**, tower AI API **8000**, MCP **8001**, database **5433**, and Ollama **11434**. Each radio dashboard uses its own local API origin. Open the printed dashboard URL on that Mac; the host browser never connects directly to tower services.

If a copy loses executable permissions, `bash start.sh host` or `bash start.sh server` works. The launcher also works through an absolute path from another directory.

## Automatic startup

Both roles find a supported Python (3.9–3.13, preferring 3.12), create a private environment in `transport_stack/.runtime/venv`, and install the existing transport requirements. Subsequent launches use cached dependencies. No activation, npm or frontend build is necessary. An incompatible/interrupted environment is repaired; shared configuration and existing secure keys are preserved.

After the cached-dependencies message, startup now prints each stage immediately: entering the environment, role preparation, configuration, native driver checks, device discovery and API readiness. Native driver probing runs in a separate process with a **20-second limit**; device discovery has a **15-second limit**. Locks report the owning PID while waiting and have a deadline. Install/build commands print periodic progress even when their underlying tools are quiet. A failed stage reports its error rather than leaving a silent terminal.

A compatible native libiio is reused. If missing on macOS, the launcher installs Homebrew build prerequisites and builds the pinned, SHA-256-verified Analog Devices libiio 0.25 locally. It does not replace a global framework. If Homebrew or Apple Command Line Tools are needed but missing, finish that one-time macOS setup ([Homebrew](https://brew.sh), or `xcode-select --install`) and rerun. macOS admin/installer dialogs cannot be silently accepted by this project.

**Host** starts only its existing Pluto transport/API and opens the field dashboard. It does not start Docker or AI services.

**Server** additionally installs Docker Desktop through Homebrew if absent, starts its engine when necessary, validates the existing Compose configuration, builds the tower API/MCP images when their inputs change, starts all four Compose services, downloads missing configured Ollama models into the **container's** model store, checks database/API/MCP readiness and reports model readiness. Native Ollama's model store is separate and is not mistaken for Docker's store. First model downloads can take several minutes; real pull progress is displayed. The server then starts its radio and the tower bridge. Docker Desktop's initial license/permission setup must be completed by the user if presented.

Model names come from `.env`; the current configuration uses **`qwen3:8b`** and `nomic-embed-text`. Missing models download in the background, so radio assessments can use the existing, explicitly labeled built-in fallback until model services are ready. No substitute model is selected. Use `--wait-for-models` if startup must wait for the configured models. The embedding dimension is checked against the existing database schema.

The API applies its existing idempotent database/schema setup. Starting the system does **not** invent or import emergency manuals. With an empty document store, report-grounded procedures are unavailable and the existing classifier exposes its fallback/built-in guidance and grounding status. Upload actual documents through the existing tower `POST /rag/documents` workflow when needed.

## Full radio workflow

```text
Host UI → host /send → physical RF → server /receive
→ tower bridge → POST /safety/assess
→ server /send → physical RF → host /receive → host UI
```

The bridge is the sole server inbox consumer. The server dashboard displays telemetry and disables field reporting/receiving so it cannot steal reports from the bridge. The host UI has one textbox and a conversation. Every message is forwarded to `/safety/assess`; returned assessments and available guidance appear in the conversation. Location is optional. Include a `Location: ...` line in the same textbox when nearby resources are needed.

The bridge journals consumed requests and pending responses in `.runtime/tower-bridge.sqlite3`; restarts recover interrupted processing and unsent results. Its stable `edge_resq.response_id` lets the host UI suppress repeated responses after a lost delivery ACK. Radio-level fragmentation, ACKs, retries, encryption and device locking remain in the existing backend. Proposed guidance is never marked human-approved or dispatched; approval/dispatch services are not implemented.

The default `reliable` hardware profile enables delivery ACKs and uses **128-byte packets**, a **one-packet ACK window**, **Zstandard compression**, at least **32768 RX samples**, **4 RX kernel buffers**, and a **500 ms ACK wait**. The original 4096-sample/80 ms profile lost short ACKs during the physical two-device test; the larger receive buffer and ACK wait delivered both directions reliably in that test. It preserves higher configured receive-buffer/ACK settings and explicitly selected compression algorithms. It does not alter the wire format, frequency or modulation. Use the same profile on both peers; `--transport-profile configured` keeps the original packet/window/buffer settings for diagnostics. Generated settings are local; `configs/transport.yaml` is not rewritten. Shared RF settings must match on the peers.

## Device selection and two Plutos on one Mac

With one USB Pluto attached, the local device is detected even if a copied configuration has the other Mac's serial. Selections are remembered only after successful radio startup. An explicitly selected network URI remains preferred. With two attached, each role uses its configured/remembered serial; if one peer is identified, the other role selects the remaining Pluto. Ambiguous selections fail explicitly.

For precise selection:

```sh
./start.sh host --serial YOUR_FIRST_PLUTO_SERIAL
./start.sh server --serial YOUR_SECOND_PLUTO_SERIAL
# Or select the Pluto reachable from this Mac by network:
./start.sh host --uri ip:192.168.2.1
```

Use the Pluto's local device URI, not the remote computer's API address. `PLUTO_JSON_RADIO_LIBIIO=/absolute/library/path` selects a compatible native library explicitly. Invalid overrides fail instead of silently selecting a different driver.

## Controls and diagnostics

```sh
./start.sh host --no-open                    # Do not open a browser
./start.sh server --port 13001               # Override server radio API port
./start.sh host --config path/to/config.yaml # Existing transport configuration
./start.sh host --check                      # Dependency/config/port checks; no radio opened
./start.sh host --setup-only                 # Python/native driver setup only
./start.sh server --radio-only               # Diagnostic radio-only role; no infra or bridge
./start.sh host --ack-timeout-ms 1000         # Explicit ACK wait
./start.sh server --infra-timeout 3600        # Longer first-time model/build stage deadline
```

`--startup-timeout` controls the local radio API readiness deadline (45 seconds by default). `--infra-timeout` bounds builds and synchronous downloads under `--wait-for-models` (1800 seconds by default); background model downloads continue independently until completion or server shutdown; tower API health has a 300-second maximum after model preparation. Duplicate roles in the same checkout and occupied ports are rejected without killing their owners. For a custom tower AI API binding, pass `--tower-url` to the server launcher.

**Ctrl+C** stops that role's radio and bridge and releases the device/port. From another terminal, `./start.sh host --stop` or `./start.sh server --stop` stops only the verified matching launcher from this checkout; unrelated PIDs are never signalled. Docker infrastructure and named data/model volumes are retained for fast restarts and to avoid stopping services already running before the launcher. To stop the tower containers afterward, run `docker compose stop` from the project root. Do not run `docker compose down -v` unless you intend to delete persisted data/models.

Do not copy `.runtime` or virtual environments between Macs; they contain local caches, device choices and journals. The original `transport_stack/run.py` entry point is still available for manually managed environments.

## Without hardware

On **one computer**, use two terminals:

```sh
./start.sh host --simulate
./start.sh server --simulate
```

The server still starts the real AI infrastructure and bridge; only the radio link uses the repository's loopback UDP simulator. Add `--radio-only` to the server command for a transport-only test. Simulation does not initialize an SDR and cannot connect two separate Macs; normal two-Mac operation uses the commands without `--simulate`.

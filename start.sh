#!/bin/bash
# Run from any directory. Both Macs use this same entry point.
set -euo pipefail
EDGE_RESQ_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" || $# -eq 0 ]]; then
  cat <<'USAGE'
Usage: ./start.sh host|server [options]

  ./start.sh host                       Ground/mobile Pluto + API + dashboard
  ./start.sh server                     Full tower infrastructure + Pluto + AI forwarding bridge
  ./start.sh host --uri ip:192.168.2.1   Select a network-connected local Pluto
  ./start.sh server --serial SERIAL     Select one of several USB Plutos
  ./start.sh host --simulate            Local UDP simulation, no SDR required

Options: --port PORT, --config FILE, --no-open, --check, --setup-only, --radio-only, --stop
Server starts Docker, YugabyteDB, Ollama/models, tower API, MCP and radio bridge.
First run installs Python packages in transport_stack/.runtime/venv.
A missing native driver is built locally on macOS using Homebrew prerequisites.
Ctrl+C stops the instance. Run one role on each Mac, with one Pluto attached.
USAGE
  exit 0
fi
EDGE_RESQ_PYTHON_BIN="${EDGE_RESQ_PYTHON:-}"
if [[ -z "$EDGE_RESQ_PYTHON_BIN" ]]; then
  for candidate in python3.12 /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 python3.13 python3.11 python3.10 python3.9 python3 /usr/bin/python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(not ((3,9) <= sys.version_info[:2] < (3,14)))' 2>/dev/null; then
      EDGE_RESQ_PYTHON_BIN="$candidate"
      break
    fi
  done
fi
if [[ -z "$EDGE_RESQ_PYTHON_BIN" ]]; then
  EDGE_RESQ_BREW="$(command -v brew || true)"
  [[ -n "$EDGE_RESQ_BREW" ]] || { [[ ! -x /opt/homebrew/bin/brew ]] || EDGE_RESQ_BREW=/opt/homebrew/bin/brew; }
  [[ -n "$EDGE_RESQ_BREW" ]] || { [[ ! -x /usr/local/bin/brew ]] || EDGE_RESQ_BREW=/usr/local/bin/brew; }
  if [[ -z "$EDGE_RESQ_BREW" ]]; then
    echo 'Python 3.9–3.13 is required. Install Homebrew from https://brew.sh, then run: brew install python@3.12' >&2
    exit 1
  fi
  HOMEBREW_NO_AUTO_UPDATE=1 "$EDGE_RESQ_BREW" install python@3.12
  EDGE_RESQ_PYTHON_BIN="$("$EDGE_RESQ_BREW" --prefix python@3.12)/bin/python3.12"
fi
exec "$EDGE_RESQ_PYTHON_BIN" "$EDGE_RESQ_ROOT/scripts/launch.py" "$@"

"""Test setup.

The deployed package is `app` (Dockerfile copies edge-resq-app/ to /edge-resq-app/app),
but the folder in the repo is `edge-resq-app`, which is not importable. Alias it.

Integration tests need a PostgreSQL-compatible server with pgvector reachable at
TEST_DATABASE_URL (default: the docker-compose YugabyteDB address) and start a
stub Ollama on 127.0.0.1:11434 themselves.
"""
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DATABASE_URL", "postgresql://yugabyte:yugabyte@localhost:5433/disaster_db")
os.environ.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
os.environ.setdefault("CHUNK_SIZE", "800")
os.environ.setdefault("CHUNK_OVERLAP", "120")

_app_pkg = types.ModuleType("app")
_app_pkg.__path__ = [str(ROOT / "edge-resq-app")]
sys.modules["app"] = _app_pkg


@pytest.fixture(scope="session")
def stub_ollama():
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "stub_ollama:app", "--port", "11434", "--log-level", "warning"],
        cwd=Path(__file__).parent,
    )
    base = os.environ["OLLAMA_BASE_URL"]
    for _ in range(50):
        try:
            httpx.get(f"{base}/_state", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("stub Ollama did not start")

    class Stub:
        def control(self, **kw):
            return httpx.post(f"{base}/_control", json=kw).json()

        def state(self):
            return httpx.get(f"{base}/_state").json()

    yield Stub()
    proc.terminate()
    proc.wait(timeout=5)

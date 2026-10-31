from __future__ import annotations

import ctypes
import ctypes.util
import os
from dataclasses import dataclass
from pathlib import Path


REQUIRED_SYMBOL = "iio_get_backends_count"


@dataclass(frozen=True)
class LibiioProbe:
    path: str
    ok: bool
    detail: str


_SELECTED_LIBIIO: str | None = None


def prepare_libiio() -> LibiioProbe:
    global _SELECTED_LIBIIO

    if _SELECTED_LIBIIO:
        return LibiioProbe(_SELECTED_LIBIIO, True, "already selected")

    probes = probe_libiio_candidates()
    for probe in probes:
        if probe.ok:
            _patch_find_library(probe.path)
            _SELECTED_LIBIIO = probe.path
            return probe

    details = "; ".join(f"{probe.path}: {probe.detail}" for probe in probes)
    return LibiioProbe("", False, details or "no libiio candidates found")


def probe_libiio_candidates() -> list[LibiioProbe]:
    candidates: list[str] = []
    env_path = os.environ.get("PLUTO_JSON_RADIO_LIBIIO")
    if env_path:
        candidates.append(env_path)

    candidates.extend(
        [
            "/opt/local/lib/libiio.dylib",
            "/opt/homebrew/lib/libiio.dylib",
            "/usr/local/lib/libiio.dylib",
            "/Library/Frameworks/iio.framework/iio",
        ]
    )

    found = ctypes.util.find_library("iio")
    if found:
        candidates.append(found)

    unique_candidates = list(dict.fromkeys(candidates))
    return [_probe_candidate(candidate) for candidate in unique_candidates]


def _probe_candidate(path: str) -> LibiioProbe:
    if path.startswith("/") and not Path(path).exists():
        return LibiioProbe(path, False, "not found")

    try:
        lib = ctypes.CDLL(path, use_errno=True, use_last_error=True)
    except OSError as exc:
        return LibiioProbe(path, False, f"could not load: {exc}")

    if not hasattr(lib, REQUIRED_SYMBOL):
        return LibiioProbe(path, False, f"missing symbol {REQUIRED_SYMBOL}")

    return LibiioProbe(path, True, "ok")


def _patch_find_library(selected_path: str) -> None:
    original_find_library = ctypes.util.find_library

    def patched_find_library(name: str) -> str | None:
        if name in {"iio", "libiio"}:
            return selected_path
        return original_find_library(name)

    ctypes.util.find_library = patched_find_library

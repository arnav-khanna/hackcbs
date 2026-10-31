from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable

import numpy as np

from .config import DeviceConfig
from .config import validate_pluto_uri
from .native_libiio import prepare_libiio

if TYPE_CHECKING:
    from adi import Pluto


def make_pluto(config: DeviceConfig) -> "Pluto":
    validate_pluto_uri(config.uri)
    libiio_probe = prepare_libiio()
    if not libiio_probe.ok:
        raise RuntimeError(
            "No usable native libiio was found. Install a compatible libiio runtime "
            "and set PLUTO_JSON_RADIO_LIBIIO if needed. Details: "
            f"{libiio_probe.detail}"
        )
    try:
        import adi
    except Exception as exc:
        raise RuntimeError(
            "Could not import pyadi-iio/libiio cleanly. This usually means Python is "
            "mixing global packages with the project .venv, or iio.py is paired with "
            "an incompatible libiio dynamic library. Install the dependencies listed "
            "in requirements.txt in this application's virtual environment."
        ) from exc

    sdr = adi.Pluto(uri=config.uri)
    sdr.sample_rate = int(config.sample_rate_hz)
    sdr.rx_lo = int(config.center_frequency_hz)
    sdr.tx_lo = int(config.center_frequency_hz)
    sdr.rx_rf_bandwidth = int(config.rf_bandwidth_hz)
    sdr.tx_rf_bandwidth = int(config.rf_bandwidth_hz)
    sdr.tx_hardwaregain_chan0 = float(config.tx_gain_db)
    sdr.rx_buffer_size = int(config.rx_buffer_size)
    if config.rx_gain_db == "auto":
        sdr.gain_control_mode_chan0 = "slow_attack"
    else:
        sdr.gain_control_mode_chan0 = "manual"
        sdr.rx_hardwaregain_chan0 = float(config.rx_gain_db)

    sdr.tx_cyclic_buffer = False
    return sdr


def transmit_once(sdr: "Pluto", iq: np.ndarray) -> None:
    scaled = np.asarray(iq * (2**14), dtype=np.complex64)
    sdr.tx(scaled)
    # Let the DMA buffer drain before destroying it.  The old 50 ms fixed
    # delay dominated short ACKs; 8 ms is ample guard time after the actual
    # waveform duration while keeping the turn-around path responsive.
    time.sleep(len(iq) / float(sdr.sample_rate) + 0.008)
    try:
        sdr.tx_destroy_buffer()
    except Exception:
        pass


def prime_receiver(sdr: "Pluto") -> None:
    """Create and start the persistent RX DMA buffer before the first TX.

    libiio starts capture when this buffer is created.  Priming it before a
    request prevents a fast ACK from arriving before the sender has begun RX.
    """
    np.asarray(sdr.rx(), dtype=np.complex64)


def receive_samples(sdr: "Pluto", seconds: float) -> np.ndarray:
    sample_rate = int(sdr.sample_rate)
    target = max(int(sample_rate * seconds), int(sdr.rx_buffer_size))
    chunks: list[np.ndarray] = []
    samples_read = 0

    while samples_read < target:
        chunk = np.asarray(sdr.rx(), dtype=np.complex64)
        if chunk.size:
            chunks.append(chunk)
            samples_read += len(chunk)

    if not chunks:
        return np.array([], dtype=np.complex64)
    return np.concatenate(chunks)


def receive_chunk(sdr: "Pluto") -> np.ndarray:
    """Receive one SDR buffer for continuous low-latency processing."""
    return np.asarray(sdr.rx(), dtype=np.complex64)


def receive_until(
    sdr: "Pluto", seconds: float, predicate: Callable[[np.ndarray], bool]
) -> bool:
    """Receive until *predicate* accepts recent IQ or time expires.

    ACKs are shorter than one configured RX buffer.  Retaining only a small
    overlap handles DMA-boundary splits without re-decoding an ever-growing
    capture on every buffer, which otherwise increases ACK latency sharply.
    """
    deadline = time.monotonic() + seconds
    chunks: list[np.ndarray] = []

    while time.monotonic() < deadline:
        chunk = np.asarray(sdr.rx(), dtype=np.complex64)
        if not chunk.size:
            continue
        chunks.append(chunk)
        if len(chunks) > 3:
            chunks.pop(0)
        if predicate(np.concatenate(chunks)):
            return True
    return False

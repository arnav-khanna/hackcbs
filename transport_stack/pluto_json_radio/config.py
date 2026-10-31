from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any

import yaml
import json


MAX_RESENDS = 4


@dataclass(frozen=True)
class DeviceConfig:
    uri: str
    center_frequency_hz: int
    sample_rate_hz: int
    rf_bandwidth_hz: int
    tx_gain_db: float
    rx_gain_db: float | str
    rx_buffer_size: int


@dataclass(frozen=True)
class PhyConfig:
    preamble_hex: str
    samples_per_symbol: int
    compatibility_samples_per_symbol: tuple[int, ...]
    tone_deviation_hz: float
    max_payload_bytes: int
    initial_burst_copies: int
    ack_burst_copies: int
    ack_followup_count: int
    ack_followup_gap_seconds: float
    max_preamble_bit_errors: int
    capture_seconds: float
    rx_overlap_seconds: float
    max_resends: int
    ack_wait_seconds: float


@dataclass(frozen=True)
class LinkConfig:
    local_address: str
    default_receiver_address: str
    value_codes: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class SecurityConfig:
    encryption_method: str | None
    private_key: str | None
    peer_public_keys: dict[str, str]
    signing_private_key: str | None
    peer_signing_public_keys: dict[str, str]


@dataclass(frozen=True)
class AppConfig:
    device: DeviceConfig
    phy: PhyConfig
    link: LinkConfig
    security: SecurityConfig


def _require(mapping: dict[str, Any], key: str) -> Any:
    if key not in mapping:
        raise KeyError(f"Missing required config key: {key}")
    return mapping[key]


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    device = _require(raw, "device")
    phy = _require(raw, "phy")
    link = _require(raw, "link")
    security = raw.get("security") or {}
    dictionary = _load_metadata_dictionary(
        config_path.parent / str(_require(link, "metadata_dictionary"))
    )

    return AppConfig(
        device=DeviceConfig(
            uri=str(_require(device, "uri")),
            center_frequency_hz=int(_require(device, "center_frequency_hz")),
            sample_rate_hz=int(_require(device, "sample_rate_hz")),
            rf_bandwidth_hz=int(_require(device, "rf_bandwidth_hz")),
            tx_gain_db=float(_require(device, "tx_gain_db")),
            rx_gain_db=_parse_gain(_require(device, "rx_gain_db")),
            rx_buffer_size=int(_require(device, "rx_buffer_size")),
        ),
        phy=PhyConfig(
            preamble_hex=str(_require(phy, "preamble_hex")).replace(" ", ""),
            samples_per_symbol=int(_require(phy, "samples_per_symbol")),
            compatibility_samples_per_symbol=_parse_compatibility_samples(
                phy.get("compatibility_samples_per_symbol", [])
            ),
            tone_deviation_hz=float(_require(phy, "tone_deviation_hz")),
            max_payload_bytes=int(_require(phy, "max_payload_bytes")),
            initial_burst_copies=max(1, int(_require(phy, "initial_burst_copies"))),
            ack_burst_copies=max(1, int(_require(phy, "ack_burst_copies"))),
            ack_followup_count=max(1, int(_require(phy, "ack_followup_count"))),
            ack_followup_gap_seconds=float(_require(phy, "ack_followup_gap_seconds")),
            max_preamble_bit_errors=int(_require(phy, "max_preamble_bit_errors")),
            capture_seconds=float(_require(phy, "capture_seconds")),
            rx_overlap_seconds=float(_require(phy, "rx_overlap_seconds")),
            max_resends=_parse_max_resends(_require(phy, "max_resends")),
            ack_wait_seconds=float(_require(phy, "ack_wait_seconds")),
        ),
        link=LinkConfig(
            local_address=str(_require(link, "local_address")),
            default_receiver_address=str(_require(link, "default_receiver_address")),
            value_codes=dictionary.get("values", {}),
        ),
        security=SecurityConfig(
            encryption_method=_parse_encryption_method(security.get("encryption_method")),
            private_key=_optional_string(security.get("private_key")),
            peer_public_keys={
                str(address): str(key)
                for address, key in (security.get("peer_public_keys") or {}).items()
            },
            signing_private_key=_optional_string(security.get("signing_private_key")),
            peer_signing_public_keys={
                str(address): str(key)
                for address, key in (security.get("peer_signing_public_keys") or {}).items()
            },
        ),
    )


def _load_metadata_dictionary(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        dictionary = json.load(fh)
    values = dictionary.get("values", {})
    if not isinstance(values, dict):
        raise ValueError("metadata dictionary values must be an object")
    for required in ("receiver_id", "source_id"):
        if not isinstance(values.get(required), dict):
            raise ValueError(f"metadata dictionary must define values.{required}")
    return {"values": values}


def _parse_gain(value: Any) -> float | str:
    if isinstance(value, str) and value.lower() == "auto":
        return "auto"
    return float(value)


def _parse_max_resends(value: Any) -> int:
    resends = int(value)
    if resends < 0:
        raise ValueError("phy.max_resends cannot be negative")
    # One initial transmission plus four resends preserves the five-broadcast
    # ceiling of this protocol.
    return min(resends, MAX_RESENDS)


def _parse_compatibility_samples(value: Any) -> tuple[int, ...]:
    """Return distinct legacy waveform symbol durations, preserving order."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("phy.compatibility_samples_per_symbol must be a list")
    profiles: list[int] = []
    for item in value:
        samples = int(item)
        if samples <= 0:
            raise ValueError("compatibility waveform samples_per_symbol must be positive")
        if samples not in profiles:
            profiles.append(samples)
    return tuple(profiles)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _parse_encryption_method(value: Any) -> str | None:
    if value in (None, "none", "null", "off"):
        return None
    if value != "x25519-aesgcm":
        raise ValueError("security.encryption_method must be x25519-aesgcm or none")
    return "x25519-aesgcm"


def validate_pluto_uri(uri: str) -> None:
    if not uri.startswith("ip:"):
        return
    host = uri[3:]
    try:
        parsed = ip_address(host)
    except ValueError:
        return
    if parsed.version == 4 and str(parsed).endswith(".255"):
        raise ValueError(
            f"{uri!r} looks like an IPv4 broadcast address. "
            "Use the Pluto's device IP instead, for example ip:172.31.241.1."
        )

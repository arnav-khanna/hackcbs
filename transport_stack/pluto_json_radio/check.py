from __future__ import annotations

import argparse
import sys

from .config import load_config
from .pluto import make_pluto


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check ADALM-Pluto connection.")
    parser.add_argument("--config", default="configs/pluto.example.yaml")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    try:
        sdr = make_pluto(config.device)
    except Exception as exc:
        print(f"Pluto check failed for {config.device.uri}: {exc}", file=sys.stderr)
        return 1

    print(f"Pluto connection ok: {config.device.uri}")
    print(f"sample_rate={sdr.sample_rate}")
    print(f"rx_lo={sdr.rx_lo}")
    print(f"tx_lo={sdr.tx_lo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

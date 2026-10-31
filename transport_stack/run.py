#!/usr/bin/env python3
"""One entry point for either endpoint, with or without hardware."""
import argparse
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description='Fast JSON transport over ADALM-Pluto or UDP simulation')
    parser.add_argument('--mode', required=True, choices=['host', 'server'])
    parser.add_argument('--config', default=str(Path(__file__).resolve().parent / 'configs/transport.yaml'))
    parser.add_argument('--simulate', action='store_true', help='Use UDP simulation; never opens an SDR')
    parser.add_argument('--port', type=int, help='Override the local FastAPI port')
    args = parser.parse_args()
    try:
        from json_transport.config import Config
        from json_transport.api import create_app
        import uvicorn
        config = Config.load(args.config, args.mode)
        if args.simulate:
            config.backend = config.raw['backend'] = 'simulation'
        port = args.port or config.local['api_port']
        app = create_app(config)
    except (ValueError, KeyError, OSError, ImportError) as exc:
        parser.exit(2, f'Configuration error: {exc}\n')
    print(f'{args.mode}: {config.backend}, node {config.local["node_id"]} → {config.local["peer_id"]}', flush=True)
    print(f'API http://{config.api["bind"]}:{port}/docs', flush=True)
    uvicorn.run(app, host=config.api['bind'], port=port, workers=1, log_level='info')


if __name__ == '__main__':
    main()

"""Small YAML configuration with explicit, independent feature switches."""
from __future__ import annotations
import copy
from pathlib import Path
import yaml

DEFAULTS = {
    'backend': 'radio',
    'compression': {'algorithm': 'none', 'level': 1, 'min_bytes': 256, 'skip_if_larger': True},
    'encryption': {'algorithm': 'none', 'key_env': 'PLUTO_AES_KEY', 'key_file': None},
    'key_exchange': {'algorithm': 'disabled'},
    'session': {'enabled': False, 'timeout_ms': 1000, 'retries': 3},
    'key_distribution': {'enabled': False},
    'signing': {'algorithm': 'none'},
    'reliability': {'enabled': True, 'window_size': 4, 'ack_timeout_ms': 80, 'max_retries': 4},
    'limits': {'packet_bytes': 512, 'max_message_bytes': 8388608, 'max_pending_messages': 32,
               'max_buffer_bytes': 16777216, 'reassembly_timeout_s': 30,
               'receive_queue': 128, 'dedup_entries': 4096, 'dedup_ttl_s': 120},
    'api': {'bind': '127.0.0.1'},
    'simulation': {'host': '127.0.0.1', 'loss_rate': 0.0, 'duplicate_rate': 0.0,
                   'delay_ms': 0.0, 'jitter_ms': 0.0, 'seed': 1234},
    'radio': {'sample_rate_hz': 2000000, 'center_frequency_hz': 866500000,
              'rf_bandwidth_hz': 1000000, 'tx_gain_db': -15, 'rx_gain_db': 30,
              'rx_buffer_size': 4096, 'samples_per_symbol': 8, 'tone_deviation_hz': 125000,
              'preamble_hex': '5555555555555555d391ac6f', 'tx_guard_ms': 1,
              'interframe_gap_ms': 1, 'rx_kernel_buffers': 2, 'burst_copies': 1, 'tail_padding_ms': 1, 'tx_kernel_buffers': 4},
    'modes': {
        'host': {'node_id': 1, 'peer_id': 2, 'api_port': 8000, 'simulation_port': 19001,
                 'peer_simulation_port': 19002, 'uri': 'ip:172.31.241.1',
                 'usb_serial': '104473e6a60f000208002c00e217167b6a',
                 'identity_file': 'configs/pluto.example.yaml'},
        'server': {'node_id': 2, 'peer_id': 1, 'api_port': 8001, 'simulation_port': 19002,
                   'peer_simulation_port': 19001, 'uri': 'ip:172.31.242.1',
                   'usb_serial': None,
                   'identity_file': 'configs/pluto.example.yaml'},
    },
}


def merge(base, update):
    for key, value in update.items():
        if key not in base:
            raise ValueError(f'Unknown configuration key: {key}')
        if isinstance(base[key], dict) and isinstance(value, dict):
            merge(base[key], value)
        else:
            base[key] = value
    return base


class Config:
    def __init__(self, settings=None, mode='host', base=None):
        if mode not in ('host', 'server'):
            raise ValueError('mode must be host or server')
        self.raw = merge(copy.deepcopy(DEFAULTS), settings or {})
        self.mode = mode
        self.base = Path(base or Path.cwd()).resolve()
        for name, value in self.raw.items():
            setattr(self, name, value)
        self.local = self.modes[mode]
        self.validate()

    @classmethod
    def load(cls, filename, mode):
        path = Path(filename).resolve()
        with path.open() as stream:
            settings = yaml.safe_load(stream) or {}
        if not isinstance(settings, dict):
            raise ValueError('Transport YAML must contain a mapping')
        return cls(settings, mode, path.parent.parent)

    def path(self, value):
        path = Path(value)
        return path if path.is_absolute() else self.base / path

    def validate(self):
        choices = [('backend', self.backend, ('radio', 'simulation')),
                   ('compression', self.compression['algorithm'], ('none', 'brotli', 'zstd', 'lz4')),
                   ('encryption', self.encryption['algorithm'], ('none', 'aes256')),
                   ('key_exchange', self.key_exchange['algorithm'], ('disabled', 'x25519')),
                   ('signing', self.signing['algorithm'], ('none', 'ed25519'))]
        for label, value, allowed in choices:
            if value not in allowed:
                raise ValueError(f'{label} must be one of {allowed}')
        for section, key in [('session', 'enabled'), ('key_distribution', 'enabled'),
                             ('reliability', 'enabled'), ('compression', 'skip_if_larger')]:
            if not isinstance(self.raw[section][key], bool):
                raise ValueError(f'{section}.{key} must be true or false')
        if self.key_distribution['enabled'] and (not self.session['enabled'] or self.key_exchange['algorithm'] == 'disabled'):
            raise ValueError('Public-key distribution requires session.enabled and key_exchange: x25519')
        for key in ('node_id', 'peer_id'):
            if not 1 <= self.local[key] <= 255:
                raise ValueError(f'{key} must be 1..255')
        if self.local['node_id'] == self.local['peer_id']:
            raise ValueError('Local and peer IDs must differ')
        if not 128 <= self.limits['packet_bytes'] <= 4096:
            raise ValueError('packet_bytes must be 128..4096')
        if not 1 <= self.reliability['window_size'] <= 32:
            raise ValueError('window_size must be 1..32')
        for section, keys in [('limits', ('max_message_bytes', 'max_pending_messages', 'max_buffer_bytes', 'reassembly_timeout_s', 'receive_queue', 'dedup_entries', 'dedup_ttl_s')),
                              ('reliability', ('ack_timeout_ms',)), ('session', ('timeout_ms',))]:
            for key in keys:
                if self.raw[section][key] <= 0:
                    raise ValueError(f'{section}.{key} must be positive')
        for section, key in [('reliability', 'max_retries'), ('session', 'retries'), ('compression', 'min_bytes')]:
            if self.raw[section][key] < 0:
                raise ValueError(f'{section}.{key} must be nonnegative')
        for key in ('loss_rate', 'duplicate_rate'):
            if not 0 <= self.simulation[key] < 1:
                raise ValueError(f'simulation.{key} must be between 0 and 1 (exclusive)')
        for key in ('delay_ms', 'jitter_ms'):
            if self.simulation[key] < 0:
                raise ValueError(f'simulation.{key} must be nonnegative')
        if self.radio['samples_per_symbol'] not in (8, 16, 32):
            raise ValueError('samples_per_symbol must be 8, 16, or 32')
        if self.radio['tx_guard_ms'] < 0 or self.radio['interframe_gap_ms'] < 0:
            raise ValueError('Radio guard and gap must be nonnegative')
        if self.radio['sample_rate_hz'] <= 0 or self.radio['rx_buffer_size'] <= 0:
            raise ValueError('Radio sample rate and buffer size must be positive')
        for key in ('burst_copies', 'rx_kernel_buffers', 'tx_kernel_buffers'):
            if not isinstance(self.radio[key], int) or not 1 <= self.radio[key] <= 64:
                raise ValueError(f'radio.{key} must be an integer from 1 to 64')
        if self.radio['tail_padding_ms'] < 0:
            raise ValueError('radio.tail_padding_ms must be nonnegative')
        if not isinstance(self.compression['level'], int):
            raise ValueError('compression.level must be an integer')
        try:
            preamble = bytes.fromhex(self.radio['preamble_hex'])
        except (ValueError, TypeError) as exc:
            raise ValueError('radio.preamble_hex must be valid hex') from exc
        if not 8 <= len(preamble) <= 32:
            raise ValueError('radio preamble must contain 8..32 bytes')

"""Launcher selection, isolation and failures without touching a real SDR."""
import importlib.util
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('edge_resq_launch', Path(__file__).resolve().parents[2] / 'scripts/launch.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

def test_command_runner_operates_without_role_state():
    assert launcher.run([sys.executable, '-c', 'pass'], timeout=5).returncode == 0

def test_command_runner_bounds_hung_setup_commands():
    with pytest.raises(launcher.LaunchError, match='Timed out'):
        launcher.run([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=.05)

LOCAL = {'uri': 'ip:172.31.241.1', 'usb_serial': 'SERIAL_FROM_OTHER_COMPUTER'}
DEVICES = {'usb:1.2.3': 'Analog Devices PlutoSDR serial=LOCAL_SERIAL'}


def test_copied_config_selects_the_single_local_pluto():
    chosen = launcher.choose_device(LOCAL, DEVICES)
    assert chosen == {'uri': 'usb:1.2.3', 'usb_serial': 'LOCAL_SERIAL'}
    assert LOCAL['usb_serial'] == 'SERIAL_FROM_OTHER_COMPUTER'


def test_multiple_plutos_require_explicit_selection_unless_pinned():
    devices = {**DEVICES, 'usb:4.5.6': 'Analog Devices PlutoSDR serial=SECOND_SERIAL'}
    with pytest.raises(launcher.LaunchError, match='Multiple USB Plutos'):
        launcher.choose_device(LOCAL, devices)
    assert launcher.choose_device(LOCAL, devices, serial='SECOND_SERIAL')['usb_serial'] == 'SECOND_SERIAL'
    assert launcher.choose_device(LOCAL, devices, saved={'uri': 'usb:OLD', 'usb_serial': 'LOCAL_SERIAL'})['usb_serial'] == 'LOCAL_SERIAL'

def test_two_plutos_can_select_the_known_peers_other_device():
    devices = {**DEVICES, 'usb:4.5.6': 'Analog Devices PlutoSDR serial=SECOND_SERIAL'}
    chosen = launcher.choose_device({'uri': 'ip:local', 'usb_serial': None}, devices, peer_serial='LOCAL_SERIAL')
    assert chosen['usb_serial'] == 'SECOND_SERIAL'

@pytest.mark.parametrize('profile', ['reliable', 'configured'])
def test_physical_profile_is_local_and_configured_profile_is_preserved(tmp_path, monkeypatch, profile):
    monkeypatch.setattr(launcher, 'RUNTIME', tmp_path)
    monkeypatch.setattr(launcher, 'ensure_native', lambda: None)
    monkeypatch.setattr(launcher, 'discover_plutos', lambda: {})
    source = launcher.STACK / 'configs/transport.yaml'
    before = source.read_bytes()
    args = SimpleNamespace(config=str(source), mode='host', simulate=False, port=None, uri=None, serial=None, transport_profile=profile)
    config, _ = launcher.load_config(args)
    if profile == 'reliable':
        assert config.limits['packet_bytes'] == 128 and config.reliability['window_size'] == 1
        assert config.radio['rx_buffer_size'] >= 32768 and config.reliability['ack_timeout_ms'] >= 500
        assert config.compression['algorithm'] == 'zstd'
    else:
        from json_transport.config import Config
        original = Config.load(source, 'host')
        assert config.radio == original.radio and config.reliability == original.reliability
        assert config.limits == original.limits and config.compression == original.compression
    assert source.read_bytes() == before


def test_explicit_network_uri_disables_stale_usb_serial_and_is_remembered():
    saved = launcher.choose_device(LOCAL, DEVICES, uri='ip:192.168.2.1')
    assert saved == {'uri': 'ip:192.168.2.1', 'usb_serial': None}
    assert launcher.choose_device(LOCAL, DEVICES, saved=saved) == saved


def test_no_discovery_keeps_configured_network_fallback():
    network = {'uri': 'ip:172.31.242.1', 'usb_serial': None}
    assert launcher.choose_device(network, {}) == network


def test_broadcast_address_rejected():
    with pytest.raises(ValueError, match='broadcast'):
        launcher.choose_device(LOCAL, {}, uri='ip:192.168.2.255')


def test_in_use_port_is_reported_without_stopping_its_owner():
    with socket.socket() as owner:
        owner.bind(('127.0.0.1', 0))
        port = owner.getsockname()[1]
        with pytest.raises(launcher.LaunchError, match='--port'):
            launcher.check_port('127.0.0.1', port)
        assert owner.fileno() != -1
    launcher.check_port('127.0.0.1', port)


def test_recent_tcp_connections_do_not_block_restart():
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', 0))
        port = server.getsockname()[1]
        server.listen()
        with socket.create_connection(('127.0.0.1', port)) as client:
            connection, _ = server.accept()
            connection.close()  # Server closes first, leaving a TIME_WAIT socket.
            client.recv(1)
    launcher.check_port('127.0.0.1', port)


@pytest.mark.parametrize('port', [0, -1, 65536])
def test_invalid_ports_are_rejected(port):
    with pytest.raises(launcher.LaunchError, match='between'):
        launcher.check_port('127.0.0.1', port)


def test_duplicate_role_is_locked_and_lock_is_reusable(tmp_path):
    path = tmp_path / 'host.lock'
    with launcher.lock(path):
        with pytest.raises(launcher.LaunchError, match='Another process'):
            with launcher.lock(path):
                pytest.fail('Second instance acquired lock')
    with launcher.lock(path):
        pass


def test_simulation_keeps_role_ids_and_does_not_need_native_or_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, 'RUNTIME', tmp_path)
    monkeypatch.setattr(launcher, 'ensure_native', lambda: pytest.fail('Simulation tried native setup'))
    monkeypatch.setattr(launcher, 'discover_plutos', lambda: pytest.fail('Simulation tried device discovery'))
    args = SimpleNamespace(config=str(launcher.STACK / 'configs/transport.yaml'), mode='server', simulate=True, port=18761, uri=None, serial=None)
    config, path = launcher.load_config(args)
    assert config.backend == 'simulation'
    assert config.local['node_id'] == 2 and config.local['peer_id'] == 1
    assert config.local['api_port'] == 18761
    from json_transport.config import Config
    loaded = Config.load(path, 'server')
    assert loaded.backend == 'simulation'
    assert Path(loaded.local['identity_file']).is_absolute()


def test_bad_explicit_driver_fails_without_installing_or_falling_back(tmp_path, monkeypatch):
    monkeypatch.setenv('PLUTO_JSON_RADIO_LIBIIO', str(tmp_path / 'missing.dylib'))
    monkeypatch.setattr(launcher, 'build_native', lambda: pytest.fail('Invalid override triggered native installation'))
    with pytest.raises(launcher.LaunchError, match='Correct or unset'):
        launcher.ensure_native()

def test_stop_does_not_signal_a_reused_or_unrelated_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, 'RUNTIME', tmp_path)
    (tmp_path / 'host.lock').write_text('12345')
    monkeypatch.setattr(launcher.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='python unrelated-project.py'))
    monkeypatch.setattr(launcher.os, 'kill', lambda *a: pytest.fail('Signalled an unrelated process'))
    with pytest.raises(launcher.LaunchError, match='another process'):
        launcher.stop_instance('host')

@pytest.mark.parametrize('db_ok,embed_ok,chat_ok,pending,expected', [
    (True, True, False, {'chat'}, True),
    (False, True, False, {'chat'}, False),
    (True, False, False, {'chat'}, False),
    (True, False, False, {'chat', 'embed'}, True),
    (True, True, False, set(), False),
])
def test_pending_models_do_not_hide_database_or_embedding_errors(db_ok, embed_ok, chat_ok, pending, expected):
    source = Path(__file__).resolve().parents[2] / 'scripts/server_infra.py'
    spec = importlib.util.spec_from_file_location('test_server_infra', source)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    health = {'database': {'ok': db_ok}, 'embedding_model': {'ok': embed_ok}, 'chat_model': {'ok': chat_ok}}
    assert module.can_serve_during_model_download(health, pending, 'chat', 'embed') is expected

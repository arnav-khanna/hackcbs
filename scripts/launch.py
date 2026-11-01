#!/usr/bin/env python3
"""Bootstrap and supervise the existing Pluto transport; no radio implementation here."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import venv
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / 'transport_stack'
RUNTIME = STACK / '.runtime'
LIBIIO_URL = 'https://codeload.github.com/analogdevicesinc/libiio/tar.gz/refs/tags/v0.25'
LIBIIO_SHA256 = '21972599a3c143ab1f98002ad2b3f28f4aff927fde5f677478311cd4e517730c'


class LaunchError(Exception):
    pass


def log(message):
    print(f'[EDGE-RESQ] {message}', flush=True)


def run(command, timeout=1800, **kwargs):
    """Show progress even when an installer/build tool itself goes quiet."""
    process = subprocess.Popen([str(part) for part in command], **kwargs)
    started = last_update = time.monotonic()
    try:
        while process.poll() is None:
            now = time.monotonic()
            if now - started > timeout:
                raise LaunchError(f'Timed out after {timeout}s: {command[0]}. Check the last output above and retry.')
            if now - last_update >= 10:
                log(f'Still working: {Path(str(command[0])).name} ({int(now-started)}s elapsed)…')
                last_update = now
            time.sleep(.2)
        if process.returncode:
            raise LaunchError(f'Command failed (exit {process.returncode}): {command[0]}. Fix the error above and rerun the same start command.')
        return process
    finally:
        if process.poll() is None:
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait()


@contextlib.contextmanager
def lock(path, *, blocking=False):
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as handle:
        started = last_update = time.monotonic()
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if not blocking or time.monotonic() - started > 600:
                    raise LaunchError(f'Another process holds {path.name}. Stop that instance/setup with Ctrl+C; no process was killed.') from exc
                if time.monotonic() - last_update >= 5:
                    log(f'Waiting for {path.name}, held by PID {handle.read().strip() or "another setup"}…')
                    handle.seek(0); last_update = time.monotonic()
                time.sleep(.2)
        handle.seek(0); handle.truncate(); handle.write(str(os.getpid())); handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def dependency_digest():
    content = b''.join((STACK / filename).read_bytes() for filename in ('requirements.txt', 'requirements-transport.txt'))
    return hashlib.sha256(content + sys.version.encode() + platform.machine().encode()).hexdigest()


def ensure_environment():
    target = RUNTIME / 'venv'
    python = target / 'bin/python'
    if Path(sys.prefix).resolve() == target.resolve():
        return
    with lock(RUNTIME / 'setup.lock', blocking=True):
        stamp = RUNTIME / 'dependencies.sha256'
        digest = dependency_digest()
        valid = False
        if python.exists():
            try:
                check = subprocess.run([str(python), '-c', 'import sys,pip; print(sys.version)'], capture_output=True, text=True)
                valid = check.returncode == 0 and check.stdout.strip() == sys.version
            except OSError:
                valid = False  # Copied interpreter/architecture or incomplete setup.
        if not valid:
            if target.exists():
                preserved = target.with_name(f'venv.previous-{time.time_ns()}')
                target.rename(preserved)
                log(f'Preserved incompatible/copied environment as {preserved.name}.')
            log('Creating an isolated Python environment…')
            venv.EnvBuilder(with_pip=True).create(target)
            stamp.unlink(missing_ok=True)
        imports_ok = False
        if valid and stamp.exists():
            imports_ok = subprocess.run([str(python), '-c', 'import fastapi,uvicorn,orjson,httpx,lz4,numpy,yaml,brotli,zstandard,cryptography'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        repair = valid and stamp.exists() and not imports_ok
        ready = stamp.exists() and stamp.read_text().strip() == digest and imports_ok
        if not ready:
            log('Installing project dependencies (first run or requirements changed)…')
            run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '--upgrade', 'pip'])
            run([python, '-m', 'pip', 'install', '--disable-pip-version-check', *(['--force-reinstall'] if repair else []), '-r', STACK / 'requirements-transport.txt'])
            run([python, '-m', 'pip', 'check'])
            stamp.write_text(digest + '\n')
        else:
            log('Using cached Python dependencies.')
    log('Entering the project Python environment…')
    os.execv(str(python), [str(python), '-u', str(Path(__file__).resolve()), *sys.argv[1:]])


def brew_path():
    return shutil.which('brew') or next((str(path) for path in (Path('/opt/homebrew/bin/brew'), Path('/usr/local/bin/brew')) if path.exists()), None)


def build_native():
    """Install a pinned, hash-verified legacy driver locally; never replace system libiio."""
    if platform.system() != 'Darwin':
        raise LaunchError('Automatic native driver setup supports macOS. Install a libiio 0.x-compatible runtime and set PLUTO_JSON_RADIO_LIBIIO to its library path on this OS.')
    brew = brew_path()
    if not brew:
        raise LaunchError('Native libiio is missing. Install Homebrew from https://brew.sh, then rerun this command; driver setup will continue automatically.')
    if subprocess.run(['xcode-select', '-p'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        raise LaunchError('Apple Command Line Tools are required to build the driver. Run xcode-select --install, finish the macOS installer, then rerun this command.')
    env = dict(os.environ, HOMEBREW_NO_AUTO_UPDATE='1', HOMEBREW_NO_INSTALL_UPGRADE='1', HOMEBREW_NO_INSTALL_CLEANUP='1')
    packages = ['cmake', 'pkgconf', 'libusb', 'libxml2']
    missing = [name for name in packages if subprocess.run([brew, 'list', '--versions', name], capture_output=True).returncode]
    if missing:
        log('Installing native build prerequisites through Homebrew: ' + ', '.join(missing))
        run([brew, 'install', *missing], env=env)
    prefix = Path(subprocess.check_output([brew, '--prefix'], text=True).strip())
    env['PATH'] = str(prefix / 'bin') + os.pathsep + env.get('PATH', '')
    lib_prefixes = [Path(subprocess.check_output([brew, '--prefix', name], text=True).strip()) for name in ('libusb', 'libxml2')]
    env['PKG_CONFIG_PATH'] = os.pathsep.join([*(str(p / 'lib/pkgconfig') for p in lib_prefixes), env.get('PKG_CONFIG_PATH', '')])
    archive = RUNTIME / 'libiio-v0.25.tar.gz'
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != LIBIIO_SHA256:
        import certifi
        log('Downloading the pinned Analog Devices libiio 0.25 source…')
        with urllib.request.urlopen(LIBIIO_URL, context=ssl.create_default_context(cafile=certifi.where()), timeout=60) as response:
            content = response.read(10 * 1024 * 1024)
        if hashlib.sha256(content).hexdigest() != LIBIIO_SHA256:
            raise LaunchError('Native driver source checksum mismatch. Nothing was built or installed.')
        archive.write_bytes(content)
    source_dir = RUNTIME / 'native-source'
    source_dir.mkdir(exist_ok=True)
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            destination = (source_dir / member.name).resolve()
            if source_dir.resolve() not in destination.parents or member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise LaunchError('Unsafe native source archive entry; extraction stopped.')
        bundle.extractall(source_dir)
    build = RUNTIME / f'native-build-{platform.machine()}'
    install = RUNTIME / 'native'
    log('Building USB/network libiio locally (first driver setup only)…')
    run([prefix / 'bin/cmake', '-S', source_dir / 'libiio-0.25', '-B', build,
         f'-DCMAKE_INSTALL_PREFIX={install}', '-DCMAKE_INSTALL_LIBDIR=lib',
         '-DCMAKE_BUILD_TYPE=Release', '-DOSX_FRAMEWORK=OFF',
         '-DWITH_USB_BACKEND=ON', '-DWITH_NETWORK_BACKEND=ON',
         '-DWITH_SERIAL_BACKEND=OFF', '-DWITH_TESTS=OFF', '-DWITH_EXAMPLES=OFF',
         '-DWITH_DOC=OFF', '-DPYTHON_BINDINGS=OFF', '-DINSTALL_UDEV_RULE=OFF',
         '-DCMAKE_POLICY_VERSION_MINIMUM=3.10',
         '-DCMAKE_PREFIX_PATH=' + ';'.join(str(p) for p in lib_prefixes)], env=env)
    run([prefix / 'bin/cmake', '--build', build, '--parallel', str(min(4, os.cpu_count() or 2))], env=env)
    run([prefix / 'bin/cmake', '--install', build], env=env)
    return install / 'lib/libiio.dylib'


def ensure_native():
    log('Checking the native Pluto driver (maximum 20 seconds per probe)…')
    explicit = os.environ.get('PLUTO_JSON_RADIO_LIBIIO')
    def probe():
        code = """import json,sys
from pluto_json_radio.native_libiio import _probe_candidate,probe_libiio_candidates
explicit,local=sys.argv[1:]
probes=[_probe_candidate(explicit)] if explicit else [_probe_candidate(local),*probe_libiio_candidates()]
print(json.dumps({'path':next((p.path for p in probes if p.ok),None),'detail':'; '.join(p.detail for p in probes)}))
"""
        try:
            result = subprocess.run([sys.executable, '-u', '-c', code, explicit or '', str(RUNTIME / 'native/lib/libiio.dylib')], env=dict(os.environ, PYTHONPATH=str(STACK)), capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired as exc:
            raise LaunchError('Native driver probing timed out. Set PLUTO_JSON_RADIO_LIBIIO to a known compatible library; no radio process was started.') from exc
        if result.returncode:
            raise LaunchError('Native driver probe failed: ' + result.stderr[-700:])
        return json.loads(result.stdout)
    with lock(RUNTIME / 'native-setup.lock', blocking=True):
        result = probe()
        if not result['path']:
            if explicit:
                raise LaunchError('PLUTO_JSON_RADIO_LIBIIO is unusable: ' + result['detail'] + '. Correct or unset that variable.')
            build_native()
            result = probe()
        if not result['path']:
            raise LaunchError('Built driver could not be loaded: ' + result['detail'])
    os.environ['PLUTO_JSON_RADIO_LIBIIO'] = result['path']
    log('Native libiio ready: ' + result['path'])


def discover_plutos():
    log('Discovering local Plutos (maximum 15 seconds)…')
    # Run vendor discovery in a bounded child, so a broken driver cannot hang setup.
    script = ('from pluto_json_radio.native_libiio import prepare_libiio; prepare_libiio(); '
              'import iio,json; print(json.dumps(iio.scan_contexts()))')
    env = dict(os.environ, PYTHONPATH=str(STACK))
    try:
        result = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True, timeout=15)
        if result.returncode:
            log('USB discovery failed; using the configured local device URI. Use --uri or --serial to select explicitly.')
            return {}
        return {uri: description for uri, description in json.loads(result.stdout).items()
                if uri.startswith('usb:') and 'pluto' in description.lower()}
    except (subprocess.TimeoutExpired, ValueError):
        log('USB discovery timed out or was unavailable; trying the configured local device.')
        return {}


def choose_device(local, discovered, *, uri=None, serial=None, saved=None, peer_serial=None):
    """Return only device overrides; never change node identity or radio parameters."""
    if uri:
        from pluto_json_radio.config import validate_pluto_uri
        validate_pluto_uri(uri)
        return {'uri': uri, 'usb_serial': None}
    if serial:
        return {'usb_serial': serial, 'uri': local['uri']}
    if saved and saved.get('usb_serial') is None and saved.get('uri', '').startswith('ip:'):
        return saved
    for preferred in ((saved or {}).get('usb_serial'), local.get('usb_serial')):
        if preferred and any(f'serial={preferred}' in description for description in discovered.values()):
            return {'usb_serial': preferred, 'uri': local['uri']}
    if peer_serial and len(discovered) == 2:
        other = {uri: description for uri, description in discovered.items() if f'serial={peer_serial}' not in description}
        if len(other) == 1:
            discovered = other
    if len(discovered) > 1:
        raise LaunchError('Multiple USB Plutos found. Select the local one with --serial SERIAL or --uri URI. Discovered: ' + json.dumps(discovered))
    if discovered:
        device_uri, description = next(iter(discovered.items()))
        import re
        match = re.search(r'\bserial=([^\s,;)]+)', description)
        return {'uri': device_uri, 'usb_serial': match.group(1) if match else None}
    return saved or {'uri': local['uri'], 'usb_serial': local.get('usb_serial')}


def check_port(bind, port):
    if not 1 <= port <= 65535:
        raise LaunchError('API port must be between 1 and 65535.')
    try:
        addresses = socket.getaddrinfo(bind, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
        family, kind, protocol, _, address = addresses[0]
        with socket.socket(family, kind, protocol) as listener:
            # Match Uvicorn's bind behavior: a recently closed TCP connection
            # must not prevent a restart while its socket is in TIME_WAIT.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(address)
    except OSError as exc:
        raise LaunchError(f'Cannot bind API at {bind}:{port}: {exc}. Stop the existing instance or pass --port PORT.') from exc


def stop_instance(mode):
    """Stop only a verified launcher owned by this checkout and selected role."""
    path = RUNTIME / f'{mode}.lock'
    if not path.exists():
        log(f'No {mode} instance is running from this checkout.'); return
    value = path.read_text().strip()
    if not value.isdigit() or int(value) <= 1:
        raise LaunchError('No valid launcher PID is recorded in ' + str(path))
    pid = int(value)
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True, timeout=5)
    if result.returncode:
        log(f'No {mode} instance is running from this checkout.'); return
    command = result.stdout
    if str(Path(__file__).resolve()) not in command or f'launch.py {mode}' not in command:
        raise LaunchError('Recorded PID belongs to another process; it was not stopped.')
    os.kill(pid, signal.SIGTERM)
    log(f'Stop requested for {mode} (PID {pid}); its radio/bridge will shut down. Docker data/services are retained.')


def load_config(args):
    from json_transport.config import Config, merge
    import yaml
    path = Path(args.config).expanduser().resolve()
    config = Config.load(path, args.mode)
    # Resolve references against the original config, not the generated runtime file.
    config.local['identity_file'] = str(config.path(config.local['identity_file']))
    if config.encryption['key_file']:
        config.encryption['key_file'] = str(config.path(config.encryption['key_file']))
    if args.simulate:
        config.raw['backend'] = config.backend = 'simulation'
    if args.port is not None:
        config.local['api_port'] = args.port
    if config.backend == 'radio':
        reliable_profile = getattr(args, 'transport_profile', 'reliable') == 'reliable'
        configured_ack = config.reliability['ack_timeout_ms']
        override_ack = getattr(args, 'ack_timeout_ms', None)
        config.reliability['ack_timeout_ms'] = override_ack if override_ack is not None else max(500, configured_ack) if reliable_profile else configured_ack
        if config.reliability['ack_timeout_ms'] != configured_ack:
            log(f'Radio ACK wait: {config.reliability["ack_timeout_ms"]} ms (configured {configured_ack} ms).')
        configured_copies = config.radio['burst_copies']
        config.radio['burst_copies'] = getattr(args, 'burst_copies', None) or configured_copies
        if reliable_profile:
            config.reliability['enabled'] = True
            config.radio['rx_buffer_size'] = max(32768, config.radio['rx_buffer_size'])
            config.radio['rx_kernel_buffers'] = max(4, config.radio['rx_kernel_buffers'])
            config.limits['packet_bytes'] = 128
            config.reliability['window_size'] = 1
            if config.compression['algorithm'] == 'none': config.compression['algorithm'] = 'zstd'
        log(f'Radio RX buffer: {config.radio["rx_buffer_size"]} samples · burst copies: {config.radio["burst_copies"]} · ACK wait: {config.reliability["ack_timeout_ms"]} ms.')
        log(f'Transport profile: {getattr(args, "transport_profile", "reliable")} · packet {config.limits["packet_bytes"]} bytes · window {config.reliability["window_size"]} · compression {config.compression["algorithm"]}.')
        ensure_native()
        profile = RUNTIME / f'device-{args.mode}.json'
        saved = json.loads(profile.read_text()) if profile.exists() and not (args.uri or args.serial) else None
        if saved is not None and (not isinstance(saved, dict) or set(saved) != {'uri', 'usb_serial'} or not isinstance(saved['uri'], str) or not (saved['usb_serial'] is None or isinstance(saved['usb_serial'], str))):
            raise LaunchError(f'Invalid device profile: {profile}. Correct it or select --uri/--serial explicitly.')
        peer_mode = 'server' if args.mode == 'host' else 'host'
        peer_profile = RUNTIME / f'device-{peer_mode}.json'
        peer_serial = config.modes[peer_mode].get('usb_serial')
        if peer_profile.exists():
            peer_serial = json.loads(peer_profile.read_text()).get('usb_serial') or peer_serial
        overrides = choose_device(config.local, {} if args.uri or args.serial else discover_plutos(), uri=args.uri, serial=args.serial, saved=saved, peer_serial=peer_serial)
        merge(config.local, overrides)
        # A serial selection is validated by the existing RadioLink at startup.
        # Persist a device selection only after the API has actually started.
        args.device_profile = (profile, overrides)
        log(f'Local Pluto: {config.local["usb_serial"] or config.local["uri"]}')
    from json_transport.security import Security
    Security(config)  # Validate existing secure-mode key files/env, without generating keys.
    runtime_config = RUNTIME / f'{args.mode}.yaml'
    runtime_config.write_text(yaml.safe_dump(config.raw, sort_keys=False))
    return config, runtime_config


def supervise(command, url, args):
    process = subprocess.Popen([str(part) for part in command], cwd=STACK)
    stopping = False
    bridge = None
    def stop(signum, _frame):
        nonlocal stopping
        if not stopping:
            stopping = True
            log('Stopping the local instance…')
            if bridge and bridge.poll() is None: bridge.terminate()
            if process.poll() is None: process.send_signal(signal.SIGTERM)
    previous = {signum: signal.signal(signum, stop) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        deadline = time.monotonic() + args.startup_timeout
        # Avoid system HTTP proxy settings for readiness checks on the local API.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while process.poll() is None and not stopping:
            if time.monotonic() >= deadline:
                raise LaunchError(f'API did not become healthy within {args.startup_timeout:g}s. Check the Pluto cable/URI and driver errors above; raise --startup-timeout only if needed.')
            try:
                with opener.open(url + 'health', timeout=1) as response:
                    health = json.load(response)
                if health.get('running') is True and health.get('mode') == args.mode:
                    if getattr(args, 'device_profile', None):
                        profile, values = args.device_profile
                        profile.write_text(json.dumps(values, indent=2) + '\n')
                    log(f'{args.mode.upper()} READY → {url}')
                    log('Both roles can send and receive. Ctrl+C stops this instance.')
                    if args.mode == 'server' and not args.radio_only:
                        log('Starting tower inbox → AI/RAG/MCP → radio response bridge…')
                        bridge = subprocess.Popen([sys.executable, '-u', str(ROOT / 'scripts/tower_bridge.py'), '--radio-url', url, '--tower-url', args.tower_url, '--state', str(RUNTIME / 'tower-bridge.sqlite3')], cwd=ROOT)
                    if not args.no_open:
                        try:
                            if not webbrowser.open(url):
                                log('Open the dashboard URL above in your browser.')
                        except webbrowser.Error:
                            log('Open the dashboard URL above in your browser.')
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.2)
        if stopping:
            return 0
        if process.poll() is not None:
            raise LaunchError(f'Transport exited before readiness (exit {process.returncode}). Check the error above. For a network Pluto use --uri ip:ADDRESS; for USB verify its cable and --serial.')
        while process.poll() is None:
            for model, pull in getattr(args, 'model_pulls', []):
                if pull.poll() is not None and not getattr(pull, '_reported', False):
                    log(f'Model {model}: ' + ('download complete.' if pull.returncode == 0 else 'download failed; configured model unchanged. Check the pull error above and retry server startup.'))
                    pull._reported = True
            if bridge and bridge.poll() is not None and not stopping:
                raise LaunchError(f'Tower bridge stopped (exit {bridge.returncode}); stopping this server radio instance.')
            if stopping:
                try:
                    process.wait(timeout=20)
                    return 0
                except subprocess.TimeoutExpired:
                    log('Local child exceeded the shutdown deadline; stopping that child only.')
                    process.kill()
                    process.wait()
                    return 0
            time.sleep(0.2)
        return 0 if stopping else process.returncode
    finally:
        if bridge and bridge.poll() is None:
            bridge.terminate()
            try: bridge.wait(timeout=10)
            except subprocess.TimeoutExpired: bridge.kill(); bridge.wait()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                log('Local child did not stop gracefully; stopping that child process only.')
                process.kill()
                process.wait()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['host', 'server'])
    parser.add_argument('--stop', action='store_true', help='Stop the running role from this checkout; retain Docker services/data')
    parser.add_argument('--config', default=str(STACK / 'configs/transport.yaml'))
    parser.add_argument('--simulate', action='store_true', help='Existing same-computer UDP simulation; no physical SDR')
    device = parser.add_mutually_exclusive_group()
    device.add_argument('--uri', help='Local Pluto URI, e.g. ip:192.168.2.1; remembered after successful startup')
    device.add_argument('--serial', help='Local USB Pluto serial; remembered after successful startup')
    parser.add_argument('--port', type=int, help='Override the local API port for this launch')
    parser.add_argument('--no-open', action='store_true', help='Do not launch a browser')
    parser.add_argument('--check', action='store_true', help='Set up dependencies and validate configuration without opening the radio or starting the API')
    parser.add_argument('--setup-only', action='store_true', help='Install dependencies/driver only, without device discovery or startup')
    parser.add_argument('--radio-only', action='store_true', help='Server diagnostic mode: skip tower infrastructure and bridge')
    parser.add_argument('--tower-url', default='http://127.0.0.1:8000', help='Server-side tower API (never used by the mobile browser)')
    parser.add_argument('--infra-timeout', type=float, default=1800, help='Maximum seconds per infrastructure/model setup stage')
    parser.add_argument('--wait-for-models', action='store_true', help='Wait for configured model downloads before opening the radio; default allows flagged built-in guidance while downloads run')
    parser.add_argument('--ack-timeout-ms', type=int, help='Radio ACK wait; launcher default is at least 500 ms')
    parser.add_argument('--burst-copies', type=int, help='Override configured radio burst repetition')
    parser.add_argument('--transport-profile', choices=['reliable', 'configured'], default='reliable', help='Conservative physical-radio settings, or unmodified configured packet/window/buffer settings')
    parser.add_argument('--startup-timeout', type=float, default=45, help='Seconds to wait for the local API (default: 45)')
    args = parser.parse_args()
    def stop_during_setup(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_during_setup)
    if args.stop:
        stop_instance(args.mode)
        return 0
    if not math.isfinite(args.startup_timeout) or args.startup_timeout <= 0:
        parser.error('--startup-timeout must be positive')
    if args.ack_timeout_ms is not None and args.ack_timeout_ms <= 0:
        parser.error('--ack-timeout-ms must be positive')
    if args.burst_copies is not None and not 1 <= args.burst_copies <= 64:
        parser.error('--burst-copies must be between 1 and 64')
    if not math.isfinite(args.infra_timeout) or args.infra_timeout <= 0:
        parser.error('--infra-timeout must be positive')
    if platform.system() not in ('Darwin', 'Linux'):
        raise LaunchError('The existing Pluto backend requires Unix (macOS/Linux). Native Windows is unsupported.')
    if not (3, 9) <= sys.version_info[:2] < (3, 14):
        raise LaunchError('Use Python 3.9–3.13; the pinned radio dependencies have not been validated on Python 3.14+. Set EDGE_RESQ_PYTHON to a compatible interpreter.')
    RUNTIME.mkdir(exist_ok=True)
    ensure_environment()
    sys.path.insert(0, str(STACK))
    log(f'Preparing {args.mode} role…')
    with lock(RUNTIME / f'{args.mode}.lock'):
        if args.setup_only:
            if not args.simulate:
                ensure_native()
            log('Setup complete. Start this role with: ./start.sh ' + args.mode)
            return 0
        if args.mode == 'server' and not args.radio_only and not args.check:
            from server_infra import start_infrastructure
            start_infrastructure(args, ROOT, RUNTIME, log, run, LaunchError)
        log('Loading transport configuration…')
        config, runtime_config = load_config(args)
        check_port(config.api['bind'], config.local['api_port'])
        if args.check:
            log('Configuration/dependencies/port checks passed. Radio connectivity is verified when the transport starts.')
            return 0
        bind = config.api['bind']
        browser_host = '127.0.0.1' if bind == '0.0.0.0' else '::1' if bind == '::' else bind
        if ':' in browser_host:
            browser_host = f'[{browser_host}]'
        url = f'http://{browser_host}:{config.local["api_port"]}/'
        log(f'Starting {args.mode} · node {config.local["node_id"]} ↔ peer {config.local["peer_id"]} · {config.backend}')
        if config.backend == 'simulation':
            log('Simulation uses loopback UDP on ONE computer. Use radio mode for two separate Macs.')
        return supervise([sys.executable, STACK / 'run.py', '--mode', args.mode, '--config', runtime_config], url, args)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (LaunchError, OSError, ValueError, KeyError, ImportError, subprocess.SubprocessError) as exc:
        print(f'\n[EDGE-RESQ] Startup failed: {exc}', file=sys.stderr)
        sys.exit(1)

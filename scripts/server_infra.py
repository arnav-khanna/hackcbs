"""Start and check the repository's existing Docker Compose tower services."""
import hashlib
import atexit
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
import urllib.error
import urllib.request


def can_serve_during_model_download(health, pending, chat, embed):
    """Only missing models may be deferred; database/schema errors still fail."""
    try:
        return bool(pending) and health['database']['ok'] and (health['embedding_model']['ok'] or embed in pending) and (health['chat_model']['ok'] or chat in pending)
    except (KeyError, TypeError):
        return False


def start_infrastructure(args, root, runtime, log, run, error):
    docker = shutil.which('docker')
    if not docker:
        for path in ('/Applications/Docker.app/Contents/Resources/bin/docker', '/opt/homebrew/bin/docker', '/usr/local/bin/docker'):
            if Path(path).exists(): docker = path; break
    if not docker:
        brew = shutil.which('brew') or ('/opt/homebrew/bin/brew' if Path('/opt/homebrew/bin/brew').exists() else None)
        if platform.system() != 'Darwin' or not brew:
            raise error('Docker Compose is required for the server. Install Docker Desktop and finish its first-run setup, then rerun ./start.sh server.')
        log('Installing Docker Desktop…')
        run([brew, 'install', '--cask', 'docker-desktop'], timeout=args.infra_timeout)
        docker = '/Applications/Docker.app/Contents/Resources/bin/docker'
    def inspect(command, timeout=15):
        return subprocess.run([docker, *command], cwd=root, text=True, capture_output=True, timeout=timeout)
    log('Checking Docker engine…')
    try: ready = inspect(['info', '--format', '{{.ServerVersion}}'], 5).returncode == 0
    except subprocess.TimeoutExpired: ready = False
    if not ready:
        if platform.system() == 'Darwin':
            log('Starting Docker Desktop; finish any first-run macOS/Docker dialogs if shown…')
            run(['open', '-a', 'Docker'], timeout=15)
        deadline = time.monotonic() + min(args.infra_timeout, 180)
        while time.monotonic() < deadline:
            try:
                if inspect(['info', '--format', '{{.ServerVersion}}'], 5).returncode == 0: break
            except subprocess.TimeoutExpired: pass
            log('Waiting for Docker engine…'); time.sleep(3)
        else: raise error('Docker engine did not start. Finish Docker Desktop setup and rerun; no data or volumes were removed.')
    compose = [docker, 'compose', '-f', root / 'docker-compose.yml']
    env_file = root / '.env'
    if not env_file.exists():
        env_file.write_text('OLLAMA_CHAT_MODEL=qwen2.5:3b\nOLLAMA_EMBED_MODEL=nomic-embed-text\n')
        log('Created local server model configuration in .env.')
    result = subprocess.run([str(p) for p in [*compose, 'config', '--format', 'json']], cwd=root, capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise error('Invalid Docker Compose configuration: ' + result.stderr[-1200:])
    services = json.loads(result.stdout)['services']
    api_env = services['api'].get('environment', {})
    chat = api_env.get('OLLAMA_CHAT_MODEL', 'qwen2.5:3b')
    embed = api_env.get('OLLAMA_EMBED_MODEL', 'nomic-embed-text')
    digest = hashlib.sha256(b''.join((root / path).read_bytes() for path in ('Dockerfile', 'edge-resq-app/requirements.txt'))).hexdigest()
    stamp = runtime / 'server-image.sha256'
    if not stamp.exists() or stamp.read_text().strip() != digest:
        log('Building tower API and MCP images…')
        run([*compose, 'build', 'api', 'mcp'], cwd=root, timeout=args.infra_timeout)
        stamp.write_text(digest + '\n')
    log('Starting YugabyteDB, Ollama, tower FastAPI and MCP…')
    run([*compose, 'up', '-d', 'yugabyte', 'ollama', 'api', 'mcp'], cwd=root, timeout=args.infra_timeout)
    source_digest = hashlib.sha256(b''.join(path.read_bytes() for path in sorted((root / 'edge-resq-app').rglob('*')) if path.suffix in ('.py', '.sql') and 'tests' not in path.parts)).hexdigest()
    source_stamp = runtime / 'server-source.sha256'
    if not source_stamp.exists() or source_stamp.read_text().strip() != source_digest:
        log('Reloading tower API/MCP after source changes…')
        run([*compose, 'restart', 'api', 'mcp'], cwd=root, timeout=120)
        source_stamp.write_text(source_digest + '\n')
    args.model_pulls = []
    pending_models = set()
    def stop_pulls():
        for _, process in args.model_pulls:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
    atexit.register(stop_pulls)
    for model in dict.fromkeys((chat, embed)):
        present = subprocess.run([str(p) for p in [*compose, 'exec', '-T', 'ollama', 'ollama', 'show', model]], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20).returncode == 0
        if present:
            log(f'Ollama model already available: {model}')
        else:
            log(f'Pulling configured model {model}; first download may take several minutes…')
            if not getattr(args, 'wait_for_models', False):
                process = subprocess.Popen([str(p) for p in [*compose, 'exec', '-T', 'ollama', 'ollama', 'pull', model]], cwd=root)
                args.model_pulls.append((model, process))
                pending_models.add(model)
                log(f'{model} is downloading in the background. Existing built-in guidance remains available until model services are ready.')
                continue
            try:
                run([*compose, 'exec', '-T', 'ollama', 'ollama', 'pull', model], cwd=root, timeout=args.infra_timeout)
            except error as exc:
                raise error(f'Could not prepare configured model {model}. Check that this exact Ollama model/tag exists and fix OLLAMA_CHAT_MODEL/OLLAMA_EMBED_MODEL in .env. No substitute model was selected. {exc}') from exc
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def wait_for(url, description, acceptable=None):
        deadline = time.monotonic() + min(args.infra_timeout, 300)
        last = 'not responding'
        while time.monotonic() < deadline:
            try:
                with opener.open(url, timeout=15) as response:
                    if acceptable or json.load(response).get('status') == 'ok':
                        log(description + ' ready.'); return
            except urllib.error.HTTPError as exc:
                if acceptable and exc.code in acceptable:
                    log(description + ' ready.'); return
                last = exc.read().decode('utf8', errors='replace')[:700]
                if not acceptable and exc.code == 503 and pending_models:
                    try:
                        health = json.loads(last)
                        if can_serve_during_model_download(health, pending_models, chat, embed):
                            log('Tower API ready with model download pending; assessments may use the existing, explicitly flagged fallback.')
                            return
                    except (ValueError, KeyError): pass
            except (OSError, ValueError) as exc: last = str(exc)
            log(f'Waiting for {description}: {last}'); time.sleep(3)
        raise error(f'{description} did not become healthy: {last}. Inspect docker compose logs api yugabyte ollama mcp.')
    wait_for(args.tower_url.rstrip('/') + '/rag/health', 'Tower database and AI API')
    ports = services['mcp'].get('ports', [])
    port = next((p.get('published') for p in ports if int(p.get('target', 0)) == 8001), '8001')
    wait_for(f'http://127.0.0.1:{port}/mcp', 'MCP resource server', {400, 405, 406, 415})
    log(f'Tower infrastructure started · configured chat {chat} · embeddings {embed}' + (' · model preparation continues.' if pending_models else ' · models ready.'))

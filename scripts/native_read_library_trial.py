"""Opt-in, bounded source-native trial; synthetic inference, never a model test.

Run with the project Python and --source pointing to a clean clone without .env.
Evidence is retained under --output (a new directory beneath the user's home).
The supervisor owns only its child process group; personal runners are untouched.
"""
from __future__ import annotations

import argparse
import faulthandler
import gc
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time


def guard(port: int) -> None:
    """Process-lifetime guards, also installed when spawn reimports this file."""
    def audit(event, args):
        if event == 'open' and isinstance(args[0], (str, bytes)):
            path = Path(os.fsdecode(args[0])).resolve()
            if path.name == '.env':
                raise PermissionError('Native trial forbids dotenv reads')
            personal = Path(os.environ['CHERQAIN_NATIVE_TRIAL_PERSONAL_HOME'])
            for root in (personal / 'Zotero',
                         personal / 'Library/Application Support/ChatEKLD',
                         personal / 'Library/Mobile Documents'):
                if path == root or root in path.parents:
                    raise PermissionError('Native trial forbids personal data')
        if event == 'socket.connect':
            address = args[1]
            if not isinstance(address, tuple) or address[:2] != ('127.0.0.1', port):
                raise OSError('Native trial forbids provider/network connections')
        if event in {'socket.sendto', 'socket.sendmsg', 'socket.gethostbyname',
                     'socket.gethostbyaddr', 'subprocess.Popen', 'os.system',
                     'os.posix_spawn', 'os.exec'}:
            raise OSError('Native trial forbids outbound/helpers')
        if event == 'socket.getaddrinfo' and args[0] not in {'127.0.0.1', 'localhost'}:
            raise OSError('Native trial forbids DNS')
    sys.addaudithook(audit)


if os.environ.get('CHERQAIN_NATIVE_TRIAL_CHILD') == '1':
    guard(int(os.environ['CHERQAIN_NATIVE_TRIAL_PORT']))


def child(source: Path, output: Path) -> None:
    sys.path.insert(0, str(source))
    base = output / 'data'
    evidence = open(output / 'threads.log', 'w', buffering=1)
    faulthandler.enable(evidence)
    faulthandler.dump_traceback_later(20, repeat=True, file=evidence)
    events = open(output / 'events.jsonl', 'a', buffering=1)

    def record(kind, **data):
        events.write(json.dumps(dict(time=time.monotonic(), kind=kind, **data)) + '\n')

    def gc_event(phase, info):
        record('gc', phase=phase, **info)
    gc.callbacks.append(gc_event)
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (90, 95))
    import launch
    launch._load_runtime()
    from core import constants
    from core.config import load_config
    cfg = load_config()
    for name in ('BASE_DIR', 'CONFIG_FILE', 'DB_PATH', 'OBSIDIAN_INDEX_DIR', 'OBSIDIAN_CACHE_DIR'):
        path = Path(getattr(constants, name)).resolve()
        if path != base and base not in path.parents:
            raise RuntimeError(f'Isolation refusal: {name}')
    for key in ('obsidian_vault_path', 'audit_zotero_sqlite', 'audit_zotero_storage'):
        if output not in Path(cfg[key]).resolve().parents:
            raise RuntimeError(f'Isolation refusal: {key}')
    assert cfg['vault_prewarm_enabled'] is False
    assert os.environ['CHATEKLD_SKIP_PROVIDER_STARTUP'] == '1'
    record('isolation_verified', source=str(source), base=str(base), provider='synthetic')
    from api.routes import paper, status
    from flask import request

    @launch.flask_app.route('/native-trial-heartbeat', methods=['POST'])
    def heartbeat():
        record('renderer_heartbeat', value=request.get_json(silent=True))
        return '', 204
    from api.routes import audit as audit_routes
    from api.routes import pending
    pending.workspace = lambda *a, **kw: None
    audit_routes._build_pdf_text_provider = lambda: None

    class Provider:
        def check_running(self):
            return True, None

        def get_models(self, **kwargs):
            return ['native-fixture'], None
    status.get_provider = lambda *a: Provider()

    def summary(*args, should_stop, **kwargs):
        record('summary_start', thread=threading.current_thread().name)
        try:
            yield 'Synthetic native lifecycle test. '
            # File is a test-only scenario switch, not a production setting.
            mode = (output / 'scenario').read_text().strip()
            deadline = time.monotonic() + (8 if mode == 'error' else 45)
            while time.monotonic() < deadline:
                if should_stop():
                    record('summary_cancelled')
                    return
                time.sleep(0.1)
            if mode == 'error':
                raise RuntimeError('Synthetic mid-stream failure')
            yield 'Synthetic stream completed.'
        finally:
            record('summary_end')
    paper.summarise_stream = summary
    port = int(os.environ['CHERQAIN_NATIVE_TRIAL_PORT'])
    launch.find_free_port = lambda: port
    create_window = launch.webview.create_window

    def window(*args, **kwargs):
        result = create_window('CHERQAiN — ISOLATED SYNTHETIC TRIAL', *args[1:], **kwargs)
        def renderer_probe():
            result.events.loaded.wait()
            while True:
                started = time.monotonic()
                record('renderer_probe_start')
                try:
                    result.run_js("fetch('/native-trial-heartbeat', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({time:Date.now(),visibility:document.visibilityState})})")
                    record('renderer_probe_dispatched', elapsed=time.monotonic()-started)
                except Exception as exc:
                    record('renderer_probe_error', error=str(exc))
                time.sleep(2)
        threading.Thread(target=renderer_probe, daemon=True, name='trial-renderer-probe').start()
        return result
    launch.webview.create_window = window
    launch.main()


def prepare(source: Path, output: Path, reviewed_diff: str | None = None) -> tuple[dict[str, str], int]:
    if (source / '.env').exists() or not (source / 'launch.py').is_file():
        raise ValueError('Use a clean source clone without .env')
    dirty = subprocess.check_output(['git', 'status', '--porcelain'], cwd=source).strip()
    untracked = subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard'], cwd=source)
    if dirty and (untracked or not reviewed_diff):
        raise ValueError('Source clone must be clean or match an exact reviewed diff hash')
    diff = subprocess.check_output(['git', 'diff', 'HEAD', '--binary'], cwd=source)
    if dirty and hashlib.sha256(diff).hexdigest() != reviewed_diff:
        raise ValueError('Source clone must be clean or match an exact reviewed diff hash')
    if Path.home() not in output.parents or output.exists():
        raise ValueError('Output must be a new directory beneath the user home')
    output.mkdir(parents=True, mode=0o700)
    (output / 'source.patch').write_bytes(diff)
    base = output / 'data'
    base.mkdir()
    vault = output / 'vault'
    pdfs = vault / 'Z_attachments' / 'biblio_articles'
    pdfs.mkdir(parents=True)
    (vault / 'Z_Zotero_Notes').mkdir()
    (vault / '_master.bib').write_text('\n'.join(
        f'@article{{fixture{number},title={{Synthetic lifecycle fixture {number}}}}}'
        for number in range(5000)))
    import fitz
    with fitz.open() as document:
        for number in range(4):
            page = document.new_page()
            page.insert_text((72, 100), f'Synthetic lifecycle fixture page {number + 1}. ' * 3)
        document.save(pdfs / 'fixture.pdf')
    import shutil
    for number in range(127):
        shutil.copyfile(pdfs / 'fixture.pdf', pdfs / f'copy{number}.pdf')
    (output / 'fixture-scale.json').write_text(json.dumps(dict(
        bib_entries=5000, notes=5000, pdfs=128, pdf_pages=512, pdf_bytes_equal=True)))
    for number in range(5000):
        (vault / 'Z_Zotero_Notes' / f'fixture{number}.md').write_text(
            f'---\ncitation-key: fixture{number}\n---\nDisposable note.\n' * 30)
    (output / 'glossary.md').write_text('Synthetic glossary')
    cfg = dict(obsidian_vault_path=str(vault), vault_prewarm_enabled=False,
               audit_zotero_sqlite=str(output / 'absent.sqlite'),
               audit_zotero_storage=str(output / 'absent-storage'), provider='ollama',
               model='native-fixture', first_run_completed=True)
    (base / 'config.json').write_text(json.dumps(cfg))
    (output / 'scenario').write_text('slow')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    env = {key: os.environ[key] for key in ('PATH', 'LANG', '__CF_USER_TEXT_ENCODING') if key in os.environ}
    env.update(HOME=str(output / 'home'), TMPDIR=str(output / 'tmp'),
               CHATEKLD_BASE_DIR=str(base), CHERQAIN_BASE_DIR=str(base),
               CHATEKLD_SKIP_PROVIDER_STARTUP='1', CHERQAIN_SKIP_PROVIDER_STARTUP='1',
               HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HOME=str(base / 'hf'),
               XDG_CACHE_HOME=str(base / 'cache'), PYTHONNOUSERSITE='1',
               CHERQAIN_NATIVE_TRIAL_CHILD='1', CHERQAIN_NATIVE_TRIAL_PORT=str(port),
               CHERQAIN_NATIVE_TRIAL_PERSONAL_HOME=str(Path.home()))
    Path(env['HOME']).mkdir()
    Path(env['TMPDIR']).mkdir()
    (output / 'preflight.json').write_text(json.dumps(dict(
        source=str(source), reviewed_diff_sha256=hashlib.sha256(diff).hexdigest(), revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip(),
        base=str(base), port=port, config=cfg, credentials_inherited=False,
        dotenv=False, network='only own app port', inference='synthetic',
        limits='supervisor: wall clock, process count, tree RSS; TERM then KILL'), indent=2))
    return env, port


def supervise(args) -> None:
    from urllib.request import urlopen, Request
    env, port = prepare(args.source.resolve(), args.output.resolve(), args.reviewed_diff_sha256)
    output = args.output.resolve()
    with open(output / 'console.log', 'w') as console:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--child',
                                    '--source', str(args.source.resolve()), '--output', str(output)],
                                   env=env, cwd=args.source, stdout=console, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        (output / 'pid').write_text(str(process.pid))
        stop = 'process exited'
        started = time.monotonic()
        try:
            with open(output / 'monitor.jsonl', 'w', buffering=1) as log:
                while process.poll() is None:
                    table = [tuple(map(int, line.split())) for line in subprocess.check_output(
                        ['/bin/ps', '-axo', 'pid=,ppid=,rss='], text=True).splitlines()]
                    owned = {process.pid}
                    while True:
                        expanded = owned | {pid for pid, parent, _ in table if parent in owned}
                        if expanded == owned:
                            break
                        owned = expanded
                    rss = sum(size*1024 for pid, _, size in table if pid in owned)
                    elapsed = time.monotonic() - started
                    row = dict(elapsed=elapsed, rss=rss, pids=sorted(owned))
                    if int(elapsed) % 5 == 0:
                        row['vm_stat'] = subprocess.check_output(['/usr/bin/vm_stat'], text=True)
                        row['swap'] = subprocess.check_output(['/usr/sbin/sysctl', 'vm.swapusage'], text=True)
                    tick = time.monotonic()
                    try:
                        with urlopen(Request(f'http://127.0.0.1:{port}/api/audit/status',
                                             headers={'X-Requested-With':'ChatEKLD'}), timeout=1) as response:
                            row['http_status'] = response.status
                    except Exception as exc:
                        row['http_error'] = type(exc).__name__
                    row['http_elapsed'] = time.monotonic()-tick
                    log.write(json.dumps(row)+'\n')
                    if elapsed > args.seconds or rss > args.memory_mib*1024**2 or len(owned) > 12:
                        stop = 'wall/memory/process limit'
                        break
                    if (output / 'STOP').exists():
                        stop = 'controlled stop requested'
                        break
                    time.sleep(1)
        finally:
            # Only this new session is signalled, including surviving workers.
            signals_sent = []
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                    signals_sent.append(sig.name)
                except ProcessLookupError:
                    break
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    continue
            (output / 'outcome.json').write_text(json.dumps(dict(reason=stop, returncode=process.poll(), signals=signals_sent)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=600)
    parser.add_argument('--memory-mib', type=int, default=2048)
    parser.add_argument('--reviewed-diff-sha256', help='Exact SHA-256 of git diff HEAD --binary, for an inspected isolated clone')
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 30 <= args.seconds <= 900 or not 256 <= args.memory_mib <= 3072:
        parser.error('Trial limits: 30–900 seconds, 256–3072 MiB')
    if args.child:
        child(args.source, args.output)
    else:
        supervise(args)


if __name__ == '__main__':
    main()

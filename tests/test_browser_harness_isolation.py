"""The browser backend must never contact a real provider, even for discovery."""
import os
from pathlib import Path
import subprocess
import sys


def test_browser_backend_denies_network_before_app_import(tmp_path):
    harness = Path(__file__).resolve().parents[1] / 'tests/browser/serve.py'
    script = '''
import os, runpy, socket, sys
module = runpy.run_path(sys.argv[1])
assert 'app' not in sys.modules
os.environ['OPENAI_API_KEY'] = 'synthetic-key-only'
module['isolate_network']()
assert 'OPENAI_API_KEY' not in os.environ
for method in ['connect', 'connect_ex']:
    with socket.socket() as client:
        try:
            getattr(client, method)(('127.0.0.1', 11434))
        except OSError as exc:
            assert 'outbound network disabled' in str(exc)
        else:
            raise AssertionError(method + ' bypassed isolation')
try:
    socket.getaddrinfo('provider.invalid', 443)
except OSError as exc:
    assert 'outbound network disabled' in str(exc)
else:
    raise AssertionError('DNS bypassed isolation')
assert socket.getaddrinfo('127.0.0.1', 5177)
# Accepting the test browser remains possible; no client connection needed.
with socket.socket() as server:
    server.bind(('127.0.0.1', 0))
    server.listen()
print('isolated')
'''
    result = subprocess.run([sys.executable, '-c', script, str(harness)],
                            env={'PATH': os.environ['PATH'], 'HOME': str(tmp_path)},
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'isolated'


def test_browser_backend_blocks_exec_bypasses_and_spawn_inherits_guard(tmp_path):
    harness = Path(__file__).resolve().parents[1] / 'tests/browser/serve.py'
    script = tmp_path / 'spawn_probe.py'
    script.write_text('''
import multiprocessing, os, runpy, socket, subprocess, sys
runpy.run_path(sys.argv[1])

def child():
    with socket.socket() as client:
        try:
            client.connect(('127.0.0.1', 11434))
        except OSError as exc:
            assert 'outbound network disabled' in str(exc)
        else:
            raise AssertionError('spawn child bypassed isolation')

if __name__ == '__main__':
    probes = [lambda: subprocess.run([sys.executable, '-c', 'pass']),
              lambda: os.system('true'),
              lambda: os.execv(sys.executable, [sys.executable, '-c', 'pass'])]
    if hasattr(os, 'posix_spawn'):
        probes.append(lambda: os.posix_spawn(sys.executable, [sys.executable, '-c', 'pass'], {}))
    for probe in probes:
        try:
            probe()
        except OSError as exc:
            assert 'external child processes disabled' in str(exc)
        else:
            raise AssertionError('external child escaped isolation')
    worker = multiprocessing.get_context('spawn').Process(target=child)
    worker.start()
    worker.join(10)
    if worker.is_alive():
        worker.kill()
        worker.join(5)
        raise AssertionError('spawn worker did not terminate')
    assert worker.exitcode == 0, worker.exitcode
    print('children isolated')
''')
    result = subprocess.run([sys.executable, str(script), str(harness)],
                            env={'PATH': os.environ['PATH'], 'HOME': str(tmp_path)},
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'children isolated'

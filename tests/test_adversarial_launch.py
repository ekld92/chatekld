"""Startup failures must not leave an owner after the process exits."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('entry', ['gui', 'cli'])
def test_failed_startup_releases_real_process_lease(tmp_path, entry):
    base = tmp_path / 'base'
    env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path),
           'CHATEKLD_BASE_DIR': str(base), 'HF_HUB_OFFLINE': '1',
           'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
    script = '''
import sys, types
from core import server_info
if sys.argv[1] == 'gui':
    import launch
    def fail():
        raise RuntimeError('synthetic runtime import failure')
    launch._load_runtime = fail
    launch.main()
else:
    from click.testing import CliRunner
    from cli.main import cli
    app = types.ModuleType('app')
    app.create_app = lambda: object()
    sys.modules['app'] = app
    wsgi = types.ModuleType('core.wsgi')
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic bind failure')
    wsgi.serve_loopback = fail
    sys.modules['core.wsgi'] = wsgi
    result = CliRunner().invoke(cli, ['server'])
    assert result.exit_code != 0
    assert server_info.read_server_info() is None
    server_info.acquire_server_lease().close()
    raise SystemExit(1)
'''
    result = subprocess.run([sys.executable, '-c', script, entry], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1, result.stderr
    probe = subprocess.run([sys.executable, '-c',
                            'from core.server_info import acquire_server_lease; '
                            'acquire_server_lease().close()'], env=env,
                           capture_output=True, text=True, timeout=10)
    assert probe.returncode == 0, probe.stderr
    assert not (base / 'server.json').exists()

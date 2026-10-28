"""The native harness must refuse unsafe inputs before any application import."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/native_read_library_trial.py'
spec = importlib.util.spec_from_file_location('native_trial', SCRIPT)
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


def test_personal_checkout_dotenv_refused_before_output(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / '.env').write_text('FAKE_API_KEY=never-load')
    output = tmp_path / 'output'
    with pytest.raises(ValueError, match='without .env'):
        trial.prepare(source, output)
    assert not output.exists()


def test_dirty_clone_refused_before_output(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    subprocess.run(['git','init','-q',str(source)], check=True)
    (source / 'launch.py').write_text('raise RuntimeError("must not import")')
    output = tmp_path / 'output'
    with pytest.raises(ValueError, match='must be clean'):
        trial.prepare(source, output)
    assert not output.exists()


def test_lifetime_guard_blocks_provider_helpers_and_dotenv(tmp_path):
    env = {k: v for k, v in os.environ.items() if k in {'PATH', 'LANG'}}
    env['CHERQAIN_NATIVE_TRIAL_PERSONAL_HOME'] = str(tmp_path)
    code = '''
import importlib.util, pathlib, socket, subprocess, sys
spec = importlib.util.spec_from_file_location('trial', sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.guard(59999)
for action in [lambda: socket.create_connection(('127.0.0.1',11434),timeout=.1),
               lambda: socket.create_connection(('1.1.1.1',443),timeout=.1),
               lambda: subprocess.run(['/usr/bin/true']),
               lambda: pathlib.Path(sys.argv[2], '.env').read_text(),
               lambda: pathlib.Path(sys.argv[2], 'Zotero', 'data').read_text()]:
    try: action()
    except (OSError, PermissionError): pass
    else: raise AssertionError('escaped isolation')
'''
    subprocess.run([sys.executable, '-c', code, str(SCRIPT), str(tmp_path)],
                   env=env, check=True, timeout=5)

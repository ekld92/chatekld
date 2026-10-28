"""The official maintenance build can omit every runtime cache loader."""
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("variable", ["CHATEKLD_SKIP_MODEL_CACHE", "CHERQAIN_SKIP_MODEL_CACHE"])
def test_skip_cache_does_not_execute_any_model_or_tokenizer_loader(tmp_path, variable):
    source = (Path(__file__).resolve().parents[1] / 'install_and_build.sh').read_text()
    # Execute the exact optional block from the build entry point. A fake
    # interpreter fails if any cache-loader command accidentally escapes it.
    start = source.index('if [[ "${CHERQAIN_SKIP_MODEL_CACHE:-')
    end = source.index('# --- 4. Models ---', start)
    script = '''set -eu
info() { :; }
warn() { :; }
VENV_DIR="$1"
SCRIPT_DIR="$1"
''' + source[start:end]
    (tmp_path / 'bin').mkdir()
    sentinel = tmp_path / 'called'
    python = tmp_path / 'bin/python3'
    python.write_text('#!/bin/sh\ntouch "' + str(sentinel) + '"\nexit 99\n')
    python.chmod(0o755)
    result = subprocess.run(['bash', '-c', script, 'fixture', str(tmp_path)],
                            env={'PATH': os.environ['PATH'],
                                 variable: '1'},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert not sentinel.exists()

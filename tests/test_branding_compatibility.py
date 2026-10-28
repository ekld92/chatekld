"""Rename compatibility: precedence, persistent locations and command installation."""
import importlib.util
from pathlib import Path

from core.branding import env_value


def test_environment_precedence(monkeypatch):
    monkeypatch.setenv("CHATEKLD_API_URL", "http://legacy.invalid")
    monkeypatch.delenv("CHERQAIN_API_URL", raising=False)
    assert env_value("CHATEKLD_API_URL") == "http://legacy.invalid"
    monkeypatch.setenv("CHERQAIN_API_URL", "http://new.invalid")
    assert env_value("CHATEKLD_API_URL") == "http://new.invalid"
    monkeypatch.setenv("CHERQAIN_API_URL", "")
    assert env_value("CHATEKLD_API_URL") == ""
    monkeypatch.setenv("PAPERMIND_PERF_METRICS", "1")
    monkeypatch.setenv("CHERQAIN_PERF_METRICS", "0")
    assert env_value("PAPERMIND_PERF_METRICS") == "0"


def test_base_override_and_stable_default(monkeypatch, tmp_path):
    from core import constants
    legacy = tmp_path / "legacy"
    modern = tmp_path / "modern"
    monkeypatch.setenv("CHATEKLD_BASE_DIR", str(legacy))
    monkeypatch.delenv("CHERQAIN_BASE_DIR", raising=False)
    assert constants._get_base_dir() == str(legacy)
    monkeypatch.setenv("CHERQAIN_BASE_DIR", str(modern))
    assert constants._get_base_dir() == str(modern)
    monkeypatch.delenv("CHERQAIN_BASE_DIR")
    monkeypatch.delenv("CHATEKLD_BASE_DIR")
    monkeypatch.setattr(constants.sys, "platform", "darwin")
    monkeypatch.setattr(constants.os.path, "expanduser", lambda path: path)
    made = []
    monkeypatch.setattr(constants.os, "makedirs", lambda path, **kw: made.append(path))
    assert constants._get_base_dir() == "~/Library/Application Support/ChatEKLD"
    assert made == ["~/Library/Application Support/ChatEKLD"]


def test_new_shim_preserves_existing_legacy_command(tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts/install_cli.py"
    spec = importlib.util.spec_from_file_location("install_cherqain_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    legacy = tmp_path / "papermind"
    legacy.write_text("legacy command\n")
    assert module.install(str(tmp_path), False, "cherqain") == 0
    assert legacy.read_text() == "legacy command\n"
    shim = tmp_path / "cherqain"
    assert module.MARKER in shim.read_text()
    assert "cherqain.py" in shim.read_text()
    assert module.install(str(tmp_path), False, "cherqain") == 0
    shim.write_text("unrelated executable\n")
    assert module.install(str(tmp_path), False, "cherqain") == 1
    assert shim.read_text() == "unrelated executable\n"


def test_deck_client_legacy_import_stays_standalone(tmp_path):
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    code = """
import importlib.abc
import sys
sys.path.insert(0, sys.argv[1])
class NoAppImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'app', 'api', 'core', 'rag', 'services', 'audit'}:
            raise AssertionError('standalone deck client imported ' + fullname)
sys.meta_path.insert(0, NoAppImports())
from deckgen.client import ChatEKLDClient, CHERQAiNClient
assert ChatEKLDClient is CHERQAiNClient
client = ChatEKLDClient('http://127.0.0.1:1')
assert client._session.headers['X-Requested-With'] == 'ChatEKLD'
"""
    result = subprocess.run([sys.executable, '-I', '-c', code, str(root)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_deck_discovery_honors_both_data_variables(monkeypatch, tmp_path):
    from deckgen.client import _default_base_dir, discover_port_from_log
    legacy = tmp_path / 'legacy'
    modern = tmp_path / 'modern'
    legacy.mkdir()
    modern.mkdir()
    (legacy / 'chatekld.log').write_text('Starting Flask on port 12345\n')
    (modern / 'chatekld.log').write_text('Starting Flask on port 23456\n')
    monkeypatch.setenv('CHATEKLD_BASE_DIR', str(legacy))
    monkeypatch.delenv('CHERQAIN_BASE_DIR', raising=False)
    assert _default_base_dir() == str(legacy)
    assert discover_port_from_log() == 12345
    monkeypatch.setenv('CHERQAIN_BASE_DIR', str(modern))
    assert _default_base_dir() == str(modern)
    assert discover_port_from_log() == 23456


def test_widget_new_override_preserves_legacy_default(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / 'packaging/widgets/chatekld.1d.py'
    spec = importlib.util.spec_from_file_location('widget_rename_compat', path)
    widget = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(widget)
    monkeypatch.setenv('CHATEKLD_BASE_DIR', str(tmp_path / 'legacy'))
    monkeypatch.delenv('CHERQAIN_BASE_DIR', raising=False)
    assert widget.base_dir() == str(tmp_path / 'legacy')
    monkeypatch.setenv('CHERQAIN_BASE_DIR', str(tmp_path / 'modern'))
    assert widget.base_dir() == str(tmp_path / 'modern')
    monkeypatch.delenv('CHATEKLD_BASE_DIR')
    monkeypatch.delenv('CHERQAIN_BASE_DIR')
    assert widget.base_dir().endswith('/Library/Application Support/ChatEKLD')


def test_seed_discovery_prefers_new_bundle_and_keeps_legacy_fallback(tmp_path):
    import subprocess
    path = Path(__file__).resolve().parents[1] / 'packaging/seed_models.command'
    source = path.read_text()
    function = source[source.index('find_app() {'):source.index('\nAPP=')]
    applications = tmp_path / 'Applications'
    companion = tmp_path / 'mounted-image'
    applications.mkdir()
    companion.mkdir()
    legacy = applications / 'ChatEKLD_2026-10-26.app'
    modern = applications / 'CHERQAiN_2026-10-27.app'
    legacy.mkdir()
    modern.mkdir()
    function = function.replace('"/Applications"', '"' + str(applications) + '"')

    def discover():
        return subprocess.run(['bash', '-c', function + '\nfind_app',
                               str(companion / 'seed_models.command')],
                              capture_output=True, text=True, check=True, timeout=5).stdout.strip()

    assert discover() == str(modern)
    modern.rmdir()
    assert discover() == str(legacy)

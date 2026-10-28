"""Isolation and installer refusal tests; never launch an application."""
from unittest.mock import Mock
from pathlib import Path
import tempfile

import pytest

import importlib.util

if importlib.util.find_spec('prototypes') is None:
    pytest.skip('Private prototype fixtures are absent from public source artifacts', allow_module_level=True)

from prototypes.zotero_desktop import install_companion, launch_lab, prepare_lab


@pytest.mark.parametrize('command', [
    '/Applications/Zotero.app/Contents/MacOS/zotero -profile /Users/real/profile',
    '/Applications/Zotero.app/Contents/MacOS/zotero -no-remote -profile /tmp/another/profile -purgecaches',
    '/usr/bin/something -no-remote -profile /tmp/fixture/profile -purgecaches',
])
def test_crash_verifier_refuses_wrong_process(monkeypatch, command):
    from prototypes.zotero_desktop import verify_interruption
    monkeypatch.setattr(verify_interruption.subprocess, 'check_output', lambda *a, **k: command)
    with pytest.raises(ValueError, match='expected disposable'):
        verify_interruption.checked_pid(Path('/tmp/fixture'), {'pid':42}, {'pid':42})


def test_crash_verifier_refuses_stale_checkpoint_pid():
    from prototypes.zotero_desktop import verify_interruption
    with pytest.raises(ValueError, match='PID'):
        verify_interruption.checked_pid(Path('/tmp/fixture'), {'pid':42}, {'pid':43})


def test_mailbox_refuses_competing_client_before_publishing(tmp_path):
    import fcntl
    from prototypes.zotero_desktop.verify_lab import mailbox
    with (tmp_path / '.mailbox.lock').open('w') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match='owns the mailbox'):
            mailbox(tmp_path, 'seed')
    assert not (tmp_path / 'command.json').exists()


def test_mailbox_does_not_follow_lock_symlink(tmp_path):
    from prototypes.zotero_desktop.verify_lab import mailbox
    destination = tmp_path / 'untouched'
    destination.write_text('unchanged')
    (tmp_path / '.mailbox.lock').symlink_to(destination)
    with pytest.raises(OSError):
        mailbox(tmp_path, 'seed')
    assert destination.read_text() == 'unchanged'


def test_control_mailbox_exposes_no_approval_action(tmp_path):
    from prototypes.zotero_desktop.verify_lab import fixture_control
    with pytest.raises(ValueError, match='Unsupported fixture control'):
        fixture_control(tmp_path, 'allow')
    assert not (tmp_path / 'control.json').exists()


def test_control_mailbox_refuses_competing_client_before_publishing(tmp_path):
    import fcntl
    from prototypes.zotero_desktop.verify_lab import fixture_control
    with (tmp_path / '.control.lock').open('w') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match='owns the control mailbox'):
            fixture_control(tmp_path, 'revoke-pending')
    assert not (tmp_path / 'control.json').exists()


def test_sandbox_hides_profile_registry_without_allowing_contents():
    policy = prepare_lab.isolation_profile('/Users/fixture')
    assert '(deny file-write* (subpath "/Users"))' in policy
    assert '(deny file-read* (with errno ENOENT) (subpath "/Users"))' in policy
    assert 'profiles.ini' not in policy
    assert '(deny network-outbound)' in policy
    assert all('file-read-metadata' in line or 'network-outbound' in line
               for line in policy.splitlines() if line.startswith('(allow ') and line != '(allow default)')


def test_launcher_refuses_non_disposable_path_before_starting(monkeypatch, tmp_path):
    spawn = Mock()
    monkeypatch.setattr(launch_lab.subprocess, 'Popen', spawn)
    with pytest.raises(ValueError, match='disposable root'):
        launch_lab.launch(tmp_path)
    spawn.assert_not_called()


def test_installer_refuses_a_different_process_before_evaluation(monkeypatch, tmp_path):
    companion = tmp_path / 'companion'
    companion.mkdir()
    (companion / 'manifest.json').write_text('{}')
    (companion / 'bootstrap.js').write_text('')
    (companion / 'reserved_create.js').write_text('')
    (companion / 'authorization.js').write_text('')
    (companion / 'authorization_review.js').write_text('')
    (companion / 'authorization_controller.js').write_text('')
    connection = Mock()
    connection.request.side_effect = [
        {'processDescriptor': {'actor': 'descriptor'}},
        {'process': {'processID': 99, 'consoleActor': 'console'}},
    ]
    monkeypatch.setattr(install_companion, 'Connection', lambda port: connection)
    with pytest.raises(ValueError, match='different process'):
        install_companion.install(tmp_path, 50000, 100)
    connection.evaluate.assert_not_called()
    connection.socket.close.assert_called_once()


def test_launcher_refuses_a_preference_file_link_outside_the_fixture(monkeypatch, tmp_path):
    spawn = Mock()
    monkeypatch.setattr(launch_lab.subprocess, 'Popen', spawn)
    external = tmp_path / 'untouched.js'
    external.write_text('unchanged')
    with tempfile.TemporaryDirectory(prefix='papermind-zotero-lab-', dir='/tmp') as name:
        root = Path(name)
        (root / 'DISPOSABLE').write_text('fixture')
        for folder in ('profile', 'data', 'companion'):
            (root / folder).mkdir()
        (root / 'profile/user.js').symlink_to(external)
        with pytest.raises(ValueError, match='files must remain inside'):
            launch_lab.launch(root, install_companion=True)
    spawn.assert_not_called()
    assert external.read_text() == 'unchanged'


def test_lab_bootstrap_gates_quit_revoke_and_binds_auth_choice():
    text = Path(__file__).resolve().parents[1].joinpath(
        'prototypes/zotero_desktop/bootstrap.js').read_text()
    assert 'Confirmed database-bound revoke required' in text
    assert 'Confirmed database-bound quit required' in text
    assert 'choice.server_id !== Zotero.Server.LocalAPI.getServerID()' in text


def test_mailbox_stamps_confirm_for_quit_and_revoke():
    src = Path(__file__).resolve().parents[1].joinpath(
        'prototypes/zotero_desktop/verify_lab.py').read_text()
    assert "'revoke', 'quit'" in src or '"revoke", "quit"' in src


def test_production_packages_never_import_disposable_lab():
    root = Path(__file__).resolve().parents[1]
    forbidden = ('prototypes.zotero_desktop', 'prototypes/zotero_desktop')
    for package in ('api', 'tagging', 'notesync', 'audit'):
        for path in (root / package).rglob('*.py'):
            text = path.read_text(encoding='utf-8')
            for needle in forbidden:
                assert needle not in text, f'{path} imports disposable lab'

from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from notesync import create_attempts
from tagging import journal


def test_journal_recovery_is_idempotent_and_never_grants_undo(tmp_path, monkeypatch):
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'paper.md'
    source.write_text('---\ntags: []\n---\n\n**Title**\n\n---\n\nOriginal\n')
    monkeypatch.setattr(create_attempts, 'store_path', lambda: tmp_path / 'attempts.sqlite3')
    from notesync.compare import split_note_parts
    from notesync.convert import md_to_html
    html = md_to_html(split_note_parts(source.read_text())[1].strip('\n'))
    c = dict(parent_key='PARENT23', transport=SimpleNamespace(base_url='http://fixture'),
             lib_path='users/0', server_id='fixture', html=html)
    identity, _ = create_attempts.reserve(vault, 'paper', source, c)
    data = dict(itemType='note', parentItem='PARENT23', note=html)
    create_attempts.acknowledge(identity, key='NOTE2345', version=4, creation_data=data)
    target = dict(base='http://fixture', library='users/0', server_id='fixture', parent='PARENT23')
    read = Mock(return_value=(data, 4))
    token = create_attempts.inspect(vault, 'paper', target=target, read_item=read)['evidence_token']
    cfg = {'tagging_archive_dir': str(tmp_path / 'archive')}
    args = dict(target=target, read_item=read, token=token, cfg=cfg)
    original = source.read_bytes()
    assert not create_attempts.repair_journal(vault, 'paper', **{**args, 'token': 'stale'})['ok']
    save = journal.save_merged
    def lost_response(*a, **kw):
        save(*a, **kw)
        raise OSError('reply lost after journal commit')
    with monkeypatch.context() as m:
        m.setattr(journal, 'save_merged', lost_response)
        with pytest.raises(OSError):
            create_attempts.repair_journal(vault, 'paper', **args)
    first = create_attempts.repair_journal(vault, 'paper', **args)
    assert first['ok']
    second = create_attempts.repair_journal(vault, 'paper', **args)
    assert second['status'] == 'existing' and second['op_id'] == first['op_id']
    ops = journal.load(vault, cfg)['ops']
    assert len(ops) == 1 and ops[0]['version_after'] == 0 and ops[0]['creation_data'] is None
    assert source.read_bytes() == original
    assert create_attempts.lookup(vault, 'paper')['automatic_retry_allowed'] is False
    # A journal state already marked terminal is not upgraded to applied.
    manifest = journal.load(vault, cfg)
    manifest['ops'][0]['state'] = 'reverted'
    journal.save(vault, cfg, manifest)
    assert create_attempts.repair_journal(vault, 'paper', **args)['state'] == 'reverted'
    read.return_value = ({**data, 'note': 'manual edit'}, 4)
    assert not create_attempts.repair_journal(vault, 'paper', **args)['ok']
    read.return_value = (data, 4)
    manifest = journal.load(vault, cfg)
    manifest['ops'] = []
    journal.save(vault, cfg, manifest)
    assert not create_attempts.repair_journal(vault, 'paper', **args)['ok']
    assert not journal.load(vault, cfg)['ops']

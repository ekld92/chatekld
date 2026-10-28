"""Retain a real published note's uncertain receipt through the batch route."""
from importlib import import_module
from types import SimpleNamespace

import pytest

from audit.config import Settings


@pytest.mark.parametrize('save_failure', [None, 'oserror', 'unreadable'])
@pytest.mark.parametrize('publication_uncertain', [True, False])
def test_uncertain_create_retains_receipt_and_restore_record(
        tmp_path, monkeypatch, save_failure, publication_uncertain):
    from api.routes import notesync
    from core.utils import CreatePublicationUncertainError
    from rag.vault import obsidian_manager
    from vault_history import journal

    create = import_module('vault_history.create_note')
    manifest = {'ops': []}
    monkeypatch.setattr(obsidian_manager, 'try_acquire_lock', lambda **kw: 'fixture')
    monkeypatch.setattr(obsidian_manager, 'release_lock', lambda epoch: None)
    monkeypatch.setattr(journal, 'load', lambda *a: manifest)
    def save(*args):
        if save_failure == 'oserror':
            raise OSError('synthetic permanent journal failure')
        if save_failure == 'unreadable':
            raise journal.JournalUnreadableError('synthetic unreadable journal')
    monkeypatch.setattr(journal, 'save', save)
    monkeypatch.setattr(journal, 'log_vault_write', lambda *a: None)
    monkeypatch.setattr(journal, '_MAX_OPS', 0)
    monkeypatch.setattr(notesync, 'zotero_note_text', lambda rec: 'source')
    monkeypatch.setattr(notesync, '_render_created_note', lambda *a, **kw: ('published', []))

    def publish(path, text):
        from pathlib import Path
        Path(path).write_text(text)
        if publication_uncertain:
            raise CreatePublicationUncertainError('synthetic directory fsync failure')

    monkeypatch.setattr(create, 'create_text_atomic', publish)
    rec = SimpleNamespace(obsidian_note=None, zotero_item=None)
    result, status = notesync._create_notes(
        SimpleNamespace(records={'cite': rec}), Settings(tmp_path), {}, ['cite'],
        render_ctx={'fixture': True})
    receipt = result['results']['cite']
    assert status == (500 if save_failure else 200)
    if publication_uncertain or save_failure:
        assert receipt['status'] == 'uncertain' and receipt['ok'] is False
        assert receipt['reason'] == 'publication_uncertain'
        assert result['created'] == 0
    else:
        assert receipt['ok'] is True and result['created'] == 1
    if save_failure:
        assert receipt['recovery_confirmed'] is False
        assert 'Do not assume Restore' in receipt['message']
    assert (tmp_path / receipt['rel']).read_text() == 'published'
    assert not result.get('created_bases')
    assert len(manifest['ops']) == 1
    assert manifest['ops'][0]['id'] == receipt['op_id']
    if publication_uncertain:
        assert manifest['ops'][0]['publication_uncertain'] is True

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from notesync import create_attempts
from notesync.compare import split_note_parts
from notesync.convert import md_to_html
from tagging import journal, zotero_write


@pytest.fixture
def attempt(tmp_path, monkeypatch):
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'paper.md'
    source.write_text('---\nzotero-key: PARENT23\nzotero-note-key: NOTE2345\n---\n\n**Title**\n\n---\n\nOriginal\n')
    monkeypatch.setattr(create_attempts, 'store_path', lambda: tmp_path / 'attempts.sqlite3')
    html = md_to_html(split_note_parts(source.read_text())[1].strip('\n'))
    commit = dict(parent_key='PARENT23', transport=SimpleNamespace(base_url='http://fixture'),
                  lib_path='users/0', server_id='fixture', html=html)
    identity, _ = create_attempts.reserve(vault, 'paper', source, commit)
    data = dict(itemType='note', parentItem='PARENT23', note=html)
    create_attempts.acknowledge(identity, key='NOTE2345', version=4, creation_data=data)
    target = dict(base='http://fixture', library='users/0', server_id='fixture', parent='PARENT23')
    reader = Mock(return_value=(data, 4))
    inspected = create_attempts.inspect(vault, 'paper', target=target, read_item=reader)
    cfg = {'tagging_archive_dir': str(tmp_path / 'archive')}
    args = dict(target=target, read_item=reader, token=inspected['evidence_token'],
                source_sha256=inspected['source_sha256'], expected_path=source, cfg=cfg)
    return vault, source, commit, args


def repair(vault, args):
    return create_attempts.repair_journal(vault, 'paper', **{k: args[k] for k in ('target', 'read_item', 'token', 'cfg')})


def test_resolution_is_durable_idempotent_and_does_not_release_create(attempt):
    vault, source, commit, args = attempt
    with pytest.raises(ValueError, match='journal record'):
        create_attempts.resolve(vault, 'paper', **args)
    repair(vault, args)
    with sqlite3.connect(create_attempts.store_path()) as db:
        before = db.execute('SELECT * FROM attempts').fetchall()
    resolved = create_attempts.resolve(vault, 'paper', **args)
    assert resolved['ok'] and not resolved['automatic_retry_allowed']
    assert create_attempts.resolve(vault, 'paper', **args) == resolved
    assert create_attempts.list_attempts(vault)[0]['resolution']['checked_at'] == resolved['checked_at']
    assert create_attempts.reserve(vault, 'paper', source, commit)[1]['note_key'] == 'NOTE2345'
    with sqlite3.connect(create_attempts.store_path()) as db:
        assert db.execute('SELECT * FROM attempts').fetchall() == before
    args['read_item'].side_effect = zotero_write.ZoteroNotFound('absent')
    with pytest.raises(ValueError):
        create_attempts.resolve(vault, 'paper', **args)


def test_resolution_refuses_source_changed_during_journal_read(attempt, monkeypatch):
    vault, source, _, args = attempt
    repair(vault, args)
    load = journal.load
    def changed(*a, **kw):
        result = load(*a, **kw)
        source.write_text(source.read_text() + 'manual edit\n')
        return result
    monkeypatch.setattr(journal, 'load', changed)
    with pytest.raises(ValueError, match='source changed'):
        create_attempts.resolve(vault, 'paper', **args)
    assert create_attempts.list_attempts(vault)[0]['resolution'] is None


def test_resolution_refuses_terminal_or_duplicate_journal_records(attempt):
    vault, _, _, args = attempt
    repair(vault, args)
    manifest = journal.load(vault, args['cfg'])
    manifest['ops'][0]['state'] = 'reverted'
    journal.save(vault, args['cfg'], manifest)
    with pytest.raises(ValueError, match='applied'):
        create_attempts.resolve(vault, 'paper', **args)
    manifest = journal.load(vault, args['cfg'])
    manifest['ops'][0]['state'] = 'applied'
    duplicate = json.loads(json.dumps(manifest['ops'][0]))
    duplicate['op_id'] += '-duplicate'
    manifest['ops'].append(duplicate)
    journal.save(vault, args['cfg'], manifest)
    with pytest.raises(ValueError, match='Exactly one'):
        create_attempts.resolve(vault, 'paper', **args)


def test_mark_satisfied_after_bookkeeping_keeps_provenance(attempt):
    """mark_satisfied must not thin a prior bookkeeping stamp's op_id/sha."""
    vault, _, _, args = attempt
    repair(vault, args)
    resolved = create_attempts.resolve(vault, 'paper', **args)
    assert resolved['status'] == 'bookkeeping_reconciled'
    assert resolved.get('op_id') and resolved.get('source_sha256')
    inspected = create_attempts.inspect(
        vault, 'paper', target=args['target'], read_item=args['read_item'])
    stamped = create_attempts.mark_satisfied(
        vault, 'paper', target=args['target'], read_item=args['read_item'],
        token=inspected['evidence_token'], inspected=inspected)
    # No-op keep: still the bookkeeping row, provenance intact.
    assert stamped['status'] == 'bookkeeping_reconciled'
    assert stamped['op_id'] == resolved['op_id']
    assert stamped['source_sha256'] == resolved['source_sha256']
    listed = create_attempts.list_attempts(vault)[0]['resolution']
    assert listed['op_id'] == resolved['op_id']
    assert listed['source_sha256'] == resolved['source_sha256']
    # Messaging still treats it as satisfied / note_exists.
    retained = create_attempts.lookup(vault, 'paper')
    assert retained['disposition'] == 'satisfied'
    assert retained['block_reason'] == 'note_exists'

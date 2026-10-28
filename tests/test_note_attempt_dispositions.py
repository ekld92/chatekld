"""Phase 3: create-attempt dispositions — never DELETE; trash-gated one retry."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from notesync import create_attempts
from tagging import zotero_write


@pytest.fixture
def setup(tmp_path, monkeypatch):
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'paper.md'
    source.write_text('# paper\n')
    monkeypatch.setattr(create_attempts, 'store_path', lambda: tmp_path / 'attempts.sqlite3')
    html = '<p>Original</p>'
    commit = dict(parent_key='PARENT23', transport=SimpleNamespace(base_url='http://fixture'),
                  lib_path='users/0', server_id='fixture', html=html)
    identity, _ = create_attempts.reserve(vault, 'paper', source, commit)
    data = dict(itemType='note', parentItem='PARENT23', note=html)
    create_attempts.acknowledge(identity, key='NOTE2345', version=4, creation_data=data)
    target = dict(base='http://fixture', library='users/0', server_id='fixture', parent='PARENT23')
    return vault, source, commit, identity, data, target


def test_satisfied_stays_blocked_with_clear_copy(setup):
    vault, source, commit, _, data, target = setup
    reader = Mock(return_value=(data, 4))
    inspected = create_attempts.inspect(vault, 'paper', target=target, read_item=reader)
    result = create_attempts.mark_satisfied(
        vault, 'paper', target=target, read_item=reader,
        token=inspected['evidence_token'], inspected=inspected)
    assert result['ok'] and result['disposition'] == 'satisfied'
    assert not result['automatic_retry_allowed']
    retained = create_attempts.lookup(vault, 'paper')
    assert retained['disposition'] == 'satisfied'
    assert retained['block_reason'] == 'note_exists'
    assert not retained['automatic_retry_allowed']
    msg = create_attempts.block_message(retained)
    assert 'NOTE2345' in msg
    assert 'not because the outcome is uncertain' in msg
    assert create_attempts.reserve(vault, 'paper', source, commit)[1]['note_key'] == 'NOTE2345'


def test_absent_404_cannot_unlock_retry(setup):
    vault, _, _, _, data, target = setup
    reader = Mock(side_effect=zotero_write.ZoteroNotFound('absent'))
    inspected = create_attempts.inspect(vault, 'paper', target=target, read_item=reader)
    assert inspected['remote'] == 'absent_unresolved'
    with pytest.raises(ValueError, match='trash'):
        create_attempts.allow_retry_after_trash(
            vault, 'paper', target=target, read_item=reader,
            token=inspected['evidence_token'], inspected=inspected)


def test_trashed_allows_exactly_one_superseding_reserve(setup):
    vault, source, commit, old_id, data, target = setup
    reader = Mock(return_value=({**data, 'deleted': True}, 4))
    inspected = create_attempts.inspect(vault, 'paper', target=target, read_item=reader)
    assert inspected['remote'] == 'trashed'
    allowed = create_attempts.allow_retry_after_trash(
        vault, 'paper', target=target, read_item=reader,
        token=inspected['evidence_token'], inspected=inspected)
    assert allowed['disposition'] == 'superseded'
    assert allowed['automatic_retry_allowed'] is True
    retained = create_attempts.lookup(vault, 'paper')
    assert retained['automatic_retry_allowed'] is True
    assert retained['block_reason'] == 'superseded_ready'

    new_id, previous = create_attempts.reserve(vault, 'paper', source, commit)
    assert previous is None
    assert new_id != old_id
    # Old row kept as history (superseded_by pointer); no DELETE.
    import sqlite3
    with sqlite3.connect(create_attempts.store_path()) as db:
        rows = db.execute(
            'SELECT id, superseded_by FROM attempts ORDER BY generation'
        ).fetchall()
    assert len(rows) == 2
    assert rows[0][0] == old_id and rows[0][1] == new_id
    assert rows[1][0] == new_id and rows[1][1] is None

    # Licence spent: second reserve blocks on the live uncertain generation.
    second_id, second_prev = create_attempts.reserve(vault, 'paper', source, commit)
    assert second_id == new_id
    assert second_prev == {}
    after = create_attempts.lookup(vault, 'paper')
    assert not after['automatic_retry_allowed']
    assert after['status'] == 'uncertain'


def test_uncertain_disposition_and_adopt_by_key(tmp_path, monkeypatch):
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'paper.md'
    source.write_text('# paper\n')
    monkeypatch.setattr(create_attempts, 'store_path', lambda: tmp_path / 'attempts.sqlite3')
    html = '<p>Original</p>'
    commit = dict(parent_key='PARENT23', transport=SimpleNamespace(base_url='http://fixture'),
                  lib_path='users/0', server_id='fixture', html=html)
    create_attempts.reserve(vault, 'paper', source, commit)
    target = dict(base='http://fixture', library='users/0', server_id='fixture', parent='PARENT23')
    reader = Mock()
    inspected = create_attempts.inspect(vault, 'paper', target=target, read_item=reader)
    assert inspected['remote'] == 'unknown_outcome'
    marked = create_attempts.mark_uncertain(
        vault, 'paper', target=target, read_item=reader,
        token=inspected['evidence_token'], inspected=inspected)
    assert marked['disposition'] == 'uncertain'
    assert 'Adopt key' in create_attempts.block_message(create_attempts.lookup(vault, 'paper'))

    # Re-inspect for a fresh token, then adopt a matching live note.
    inspected = create_attempts.inspect(vault, 'paper', target=target, read_item=reader)
    data = dict(itemType='note', parentItem='PARENT23', note=html)
    reader.return_value = (data, 7)
    adopted = create_attempts.adopt_note_key(
        vault, 'paper', note_key='FOUNDKEY', target=target, read_item=reader,
        token=inspected['evidence_token'], inspected=inspected)
    assert adopted['ok'] and adopted['adopted'] and adopted['note_key'] == 'FOUNDKEY'
    assert adopted['automatic_undo_allowed'] is False
    retained = create_attempts.lookup(vault, 'paper')
    assert retained['status'] == 'acknowledged'
    assert retained['adopted'] is True
    assert retained['disposition'] == 'satisfied'
    assert retained['note_key'] == 'FOUNDKEY'
    # creation_data cleared so revert never gets automatic trash authority.
    import json
    import sqlite3
    with sqlite3.connect(create_attempts.store_path()) as db:
        ack = json.loads(db.execute(
            'SELECT acknowledgement FROM attempts WHERE superseded_by IS NULL'
        ).fetchone()[0])
    assert ack.get('creation_data') is None
    assert ack.get('version') is None


def test_adopt_refuses_html_or_parent_mismatch(tmp_path, monkeypatch):
    vault = tmp_path / 'vault'
    vault.mkdir()
    source = vault / 'paper.md'
    source.write_text('# paper\n')
    monkeypatch.setattr(create_attempts, 'store_path', lambda: tmp_path / 'attempts.sqlite3')
    html = '<p>Original</p>'
    commit = dict(parent_key='PARENT23', transport=SimpleNamespace(base_url='http://fixture'),
                  lib_path='users/0', server_id='fixture', html=html)
    create_attempts.reserve(vault, 'paper', source, commit)
    target = dict(base='http://fixture', library='users/0', server_id='fixture', parent='PARENT23')
    reader = Mock()
    inspected = create_attempts.inspect(vault, 'paper', target=target, read_item=reader)
    reader.return_value = (dict(itemType='note', parentItem='OTHER234', note=html), 1)
    with pytest.raises(ValueError, match='parent'):
        create_attempts.adopt_note_key(
            vault, 'paper', note_key='WRONG', target=target, read_item=reader,
            token=inspected['evidence_token'], inspected=inspected)
    reader.return_value = (dict(itemType='note', parentItem='PARENT23', note='<p>Other</p>'), 1)
    with pytest.raises(ValueError, match='HTML'):
        create_attempts.adopt_note_key(
            vault, 'paper', note_key='WRONG', target=target, read_item=reader,
            token=inspected['evidence_token'], inspected=inspected)


def test_create_attempts_module_has_no_delete(tmp_path):
    import ast
    from pathlib import Path
    src = Path(create_attempts.__file__).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert 'DELETE FROM' not in node.value.upper()
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == 'execute' and node.args:
                arg0 = node.args[0]
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                    assert not arg0.value.strip().upper().startswith('DELETE')

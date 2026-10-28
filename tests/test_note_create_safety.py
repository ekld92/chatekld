"""Creation acknowledgement and conservative undo, without a live library."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from notesync import zotero_notes
from tagging import zotero_write
from tagging.local_api import Version


def target(monkeypatch):
    transport = SimpleNamespace(base_url='http://127.0.0.1:23119', kind='local', is_web=False)
    monkeypatch.setattr(zotero_notes, '_target', lambda *a: ((transport, None, 'users/0'), None))
    return SimpleNamespace(item_key='PARENT23', server_id='fixture', item_type='journalArticle',
                           title='Reviewed parent', doi='10.1234/one')


@pytest.mark.parametrize('action', ['create', 'write'])
def test_initial_note_read_is_bound_to_scanned_database(monkeypatch, action):
    zot = target(monkeypatch)
    def read(*args, **kwargs):
        assert kwargs['server_id'] == 'fixture'
        raise zotero_write.ZoteroError('Database changed')
    monkeypatch.setattr(zotero_write, 'get_item', read)
    result = (zotero_notes.prepare_create({}, zot=zot, parent_key=zot.item_key, html='<p>x</p>')
              if action == 'create' else zotero_notes.prepare_write({}, zot=zot, note_key='NOTE2345', after_html='<p>x</p>'))
    assert 'early' in result


def test_note_write_refuses_moved_note(monkeypatch):
    zot = target(monkeypatch)
    monkeypatch.setattr(zotero_write, 'get_item', lambda *a, **k: (
        dict(itemType='note', parentItem='OTHER234', note='<p>old</p>'), Version(2, 'fixture')))
    assert 'early' in zotero_notes.prepare_write({}, zot=zot, note_key='NOTE2345', after_html='<p>x</p>')


@pytest.mark.parametrize('change', [{'title': 'Other paper'}, {'DOI': '10.1234/other'}, {'itemType': 'book'}])
def test_note_creation_refuses_changed_parent(monkeypatch, change):
    zot = target(monkeypatch)
    data = dict(itemType=zot.item_type, title=zot.title, DOI=zot.doi)
    monkeypatch.setattr(zotero_write, 'get_item', lambda *a, **k: ({**data, **change}, Version(2, 'fixture')))
    assert 'early' in zotero_notes.prepare_create({}, zot=zot, parent_key=zot.item_key, html='<p>x</p>')


@pytest.mark.parametrize('changed', [False, True])
def test_note_creation_baseline_must_match_post_acknowledgement(monkeypatch, changed):
    data = dict(key='NOTE2345', itemType='note', parentItem='PARENT23', note='<p>x</p>',
                dateAdded='2026-09-10T00:00:00Z')
    key = zotero_write.CreatedAttachmentKey('NOTE2345', dict(data), 2)
    if changed:
        data['dateAdded'] = '2001-01-01T00:00:00Z'
    monkeypatch.setattr(zotero_write, 'create_child_note', lambda *a, **k: key)
    monkeypatch.setattr(zotero_write, 'get_item', lambda *a, **k: (data, Version(3 if changed else 2, 'fixture')))
    prepared = {'commit': dict(transport=SimpleNamespace(base_url='http://127.0.0.1:23119', kind='local'),
        lib_path='users/0', api_key=None, parent_key='PARENT23', server_id='fixture', html='<p>x</p>')}
    result, key, version = zotero_notes.post_create(prepared)
    assert result['ok']
    op = zotero_notes.journal_create(prepared=prepared, citation_key='fixture', note_key=key,
                                     version=version or 0, manifest={'ops': []})
    assert ('commit' in zotero_notes.revert_prepare({}, op)) is (not changed)
    assert version == (None if changed else 2)


def test_readback_only_note_record_cannot_authorize_undo(monkeypatch):
    data = dict(itemType='note', parentItem='PARENT23', note='<p>x</p>')
    monkeypatch.setattr(zotero_write, 'get_item', lambda *a, **k: (data, Version(2, 'fixture')))
    op = dict(kind=zotero_notes.OP_CREATE, transport='local', server_id='fixture',
        base_url='http://127.0.0.1:23119', lib_path='users/0', item_key='NOTE2345',
        parent_item='PARENT23', html='<p>x</p>', version_after=2)
    assert 'commit' not in zotero_notes.revert_prepare({}, op)


@pytest.mark.parametrize('existing_note', [False, True])
def test_note_preflight_refuses_new_child_or_changed_parent(monkeypatch, existing_note):
    zot = target(monkeypatch)
    data = dict(itemType=zot.item_type, title=zot.title, DOI=zot.doi)
    monkeypatch.setattr(zotero_write, 'get_item', Mock(side_effect=[(data, Version(2, 'fixture')),
        ({**data, 'title': 'Changed'}, Version(3, 'fixture'))]))
    prepared = zotero_notes.prepare_create({}, zot=zot, parent_key=zot.item_key, html='<p>x</p>')
    def children(*args, **kwargs):
        assert kwargs['server_id'] == 'fixture'
        return [{'itemType': 'note'}] if existing_note else []
    monkeypatch.setattr(zotero_write, 'get_item_children', children)
    create = Mock()
    monkeypatch.setattr(zotero_write, 'create_child_note', create)
    result, key, _ = zotero_notes.post_create(prepared)
    assert result['status'] == 'conflict' and key is None
    create.assert_not_called()


def test_http_note_create_keeps_object_acknowledgement(monkeypatch):
    import io
    import json
    data = dict(key='NOTE2345', itemType='note', parentItem='PARENT23', note='<p>x</p>')
    response = io.BytesIO(json.dumps({'successful': {'0': {'key': 'NOTE2345', 'version': 2, 'data': data}}}).encode())
    response.headers = {'Last-Modified-Version': '99'}
    monkeypatch.setattr(zotero_write, '_urlopen', lambda *a: response)
    key = zotero_write.create_child_note('https://api.zotero.org', 'users/1', 'PARENT23', '<p>x</p>')
    assert key == 'NOTE2345' and key.creation_version == 2 and key.creation_data == data


@pytest.mark.parametrize('failure', [zotero_write.ZoteroUnavailable,
                                   zotero_write.ZoteroVersionConflict,
                                   zotero_write.ZoteroError])
def test_successful_create_retains_key_when_readback_fails(monkeypatch, failure):
    monkeypatch.setattr(zotero_write, 'create_child_note', Mock(return_value='NOTE2345'))
    read = Mock(side_effect=failure('unverified'))
    monkeypatch.setattr(zotero_write, 'get_item', read)
    prepared = {'commit': dict(transport=SimpleNamespace(base_url='http://127.0.0.1:23119'),
                               lib_path='users/0', parent_key='PARENT23', html='<p>x</p>',
                               api_key=None, server_id='fixture')}
    result, key, version = zotero_notes.post_create(prepared)
    assert result['ok'] and result['verification_pending']
    assert key == 'NOTE2345' and version is None
    assert read.call_args.kwargs['server_id'] == 'fixture'


@pytest.mark.parametrize('change,baseline', [({'parentItem': 'OTHER234'}, 2),
    ({'tags': [{'tag': 'manual'}]}, 2), ({'relations': {'related': 'fixture'}}, 2),
    ({}, 0), ({}, 1)])
def test_note_undo_preserves_changed_or_unverified_note(monkeypatch, change, baseline):
    data = dict(itemType='note', parentItem='PARENT23', note='<p>x</p>')
    data.update(change)
    monkeypatch.setattr(zotero_write, 'get_item', Mock(return_value=(data, Version(2, 'fixture'))))
    patch = Mock()
    monkeypatch.setattr(zotero_write, 'set_item_deleted', patch)
    op = dict(kind='zotero_note_create', item_key='NOTE2345', parent_item='PARENT23',
              base_url='http://127.0.0.1:23119', lib_path='users/0', transport='local',
              server_id='fixture', html='<p>x</p>', version_after=baseline)
    assert zotero_notes.revert({}, op)['status'] == 'skipped'
    patch.assert_not_called()


@pytest.mark.parametrize('save_fails', [False, True])
def test_acknowledgement_is_retained_before_readback(monkeypatch, save_fails):
    c = dict(transport=SimpleNamespace(base_url='http://fixture'), lib_path='users/0',
             parent_key='PARENT23', html='<p>x</p>', api_key=None, server_id='fixture')
    monkeypatch.setattr(zotero_write, 'create_child_note', lambda *a, **kw: 'NOTE2345')
    events = []
    def retain(key):
        events.append(('save', key))
        if save_fails:
            raise OSError('disk full')
    def read(*args, **kwargs):
        assert events == [('save', 'NOTE2345')]
        events.append(('read', None))
        raise zotero_write.ZoteroUnavailable('lost readback')
    monkeypatch.setattr(zotero_write, 'get_item', read)
    result, key, version = zotero_notes.post_create({'commit': c}, on_created=retain)
    assert key == 'NOTE2345' and version is None and result['verification_pending']
    assert len(events) == (1 if save_fails else 2)
    assert bool(result.get('acknowledgement_failed')) == save_fails

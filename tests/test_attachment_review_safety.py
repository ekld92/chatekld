"""Adversarial attachment read/undo tests; no running app or real library."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tagging import writers, zotero_write
from tagging.local_api import Version
import io
import json


def test_desktop_create_undo_cannot_trash_after_late_child_insertion(monkeypatch):
    trash = Mock()
    monkeypatch.setattr(zotero_write, 'set_item_deleted', trash)
    prepared = {'commit': dict(base='http://127.0.0.1:23119', lib_path='users/0',
        key='ATTACH23', api_key=None, version=Version(4, 'fixture'))}
    op = {'state': 'applied'}
    result = writers._revert_zotero_attachment_create_commit(prepared, op)
    assert result['status'] == 'unsupported' and not result['ok']
    assert op['state'] == 'applied'
    trash.assert_not_called()


class Reply(io.BytesIO):
    def __init__(self, data, headers):
        super().__init__(json.dumps(data).encode())
        self.headers = headers


def test_recovery_does_not_adopt_current_version_as_creation_baseline(monkeypatch):
    data = dict(itemType='attachment', parentItem='PARENT23', path='/fixture.pdf',
                linkMode='linked_file', title='fixture.pdf', accessDate='2026-09-09')
    monkeypatch.setattr(zotero_write, 'get_item', Mock(return_value=(data, Version(9, 'fixture'))))
    post = Mock()
    monkeypatch.setattr(zotero_write, 'create_linked_file_attachment', post)
    prepared = {'commit': dict(item_key='ATTACH23', parent_key='PARENT23', path='/fixture.pdf',
                 transport=SimpleNamespace(base_url='http://127.0.0.1:23119', kind='local'),
                 api_key=None, lib_path='users/0', server_id='fixture', title='fixture.pdf')}
    result, key, version = writers.zotero_attachment_create_post(prepared)
    assert result['recovered'] and result['undo_review_required']
    assert key == 'ATTACH23' and version is None
    manifest = {'ops': []}
    op = writers.zotero_attachment_create_journal(prepared=prepared, citation_key='fixture',
            att_key=key, version=version or 0, manifest=manifest)
    assert 'commit' not in writers._revert_zotero_attachment_create_prepare({}, op)
    post.assert_not_called()


@pytest.mark.parametrize('change', [
    {'parentItem': 'OTHER234'}, {'linkMode': 'imported_file'},
    {'title': 'Manually edited'}, {'note': '<p>My notes</p>'},
    {'tags': [{'tag': 'important'}]},
])
def test_create_undo_preserves_later_attachment_edits(monkeypatch, change):
    op = dict(transport='local', server_id='fixture', base_url='http://127.0.0.1:23119',
              lib_path='users/0', item_key='ATTACH23', path='/fixture.pdf',
              parent_item='PARENT23', link_mode='linked_file', title='fixture.pdf',
              version_after=4, state='applied')
    data = dict(itemType='attachment', parentItem='PARENT23', path='/fixture.pdf',
                linkMode='linked_file', title='fixture.pdf')
    data.update(change)
    monkeypatch.setattr(zotero_write, 'get_item', Mock(return_value=(data, Version(5, 'fixture'))))
    patch = Mock()
    monkeypatch.setattr(zotero_write, 'set_item_deleted', patch)
    prepared = writers._revert_zotero_attachment_create_prepare({}, op)
    assert 'commit' not in prepared
    patch.assert_not_called()


def test_children_lookup_rejects_database_switch(monkeypatch):
    monkeypatch.setattr(zotero_write, '_urlopen', lambda *a: Reply([], {
        'Zotero-Server-ID': 'other', 'Last-Modified-Version': '3'}))
    with pytest.raises(zotero_write.ZoteroError, match='identity'):
        zotero_write.linked_file_paths('http://127.0.0.1:23119', 'users/0',
                                      'PARENT23', server_id='fixture')


def test_create_readback_remains_database_bound(monkeypatch):
    c = dict(transport=SimpleNamespace(base_url='http://127.0.0.1:23119'),
             lib_path='users/0', parent_key='PARENT23', path='/fixture.pdf',
             title='fixture.pdf', api_key=None, server_id='fixture')
    monkeypatch.setattr(zotero_write, 'create_linked_file_attachment', lambda *a, **kw: 'ATTACH23')
    def read(*args, **kwargs):
        assert kwargs.get('server_id') == 'fixture'
        raise zotero_write.ZoteroError('Zotero database identity changed')
    monkeypatch.setattr(zotero_write, 'get_item', read)
    result, key, version = writers.zotero_attachment_create_post({'commit': c})
    assert result['verification_pending'] and key == 'ATTACH23' and version is None


@pytest.mark.parametrize('children', [[{'itemType': 'annotation'}], [{'itemType': 'note'}]])
def test_undo_refuses_children_even_when_attachment_version_is_unchanged(monkeypatch, children):
    op = dict(transport='local', server_id='fixture', base_url='http://127.0.0.1:23119',
              lib_path='users/0', item_key='ATTACH23', path='/fixture.pdf',
              parent_item='PARENT23', title='fixture.pdf', version_after=4)
    data = dict(itemType='attachment', parentItem='PARENT23', path='/fixture.pdf',
                linkMode='linked_file', title='fixture.pdf')
    op['creation_data'] = dict(data)
    monkeypatch.setattr(zotero_write, 'get_item', Mock(return_value=(data, Version(4, 'fixture'))))
    monkeypatch.setattr(zotero_write, 'get_item_children', Mock(return_value=children))
    assert 'commit' not in writers._revert_zotero_attachment_create_prepare({}, op)


def test_undo_preserves_replaced_physical_pdf(tmp_path, monkeypatch):
    from accession import review
    path = tmp_path / 'fixture.pdf'
    path.write_bytes(b'original')
    op = dict(transport='local', server_id='fixture', base_url='http://127.0.0.1:23119',
              lib_path='users/0', item_key='ATTACH23', path=str(path),
              parent_item='PARENT23', title='fixture.pdf', version_after=4,
              source_identity=review.identity(path))
    path.write_bytes(b'replacement')
    data = dict(itemType='attachment', parentItem='PARENT23', path=str(path),
                linkMode='linked_file', title='fixture.pdf')
    monkeypatch.setattr(zotero_write, 'get_item', Mock(return_value=(data, Version(4, 'fixture'))))
    assert 'commit' not in writers._revert_zotero_attachment_create_prepare({}, op)


@pytest.mark.parametrize('change', [{}, {'accessDate': '2026-09-09'}, {'contentType': 'text/plain'}])
def test_post_acknowledgement_is_the_creation_baseline(monkeypatch, change):
    original = dict(key='ATTACH23', itemType='attachment', parentItem='PARENT23',
                    path='/fixture.pdf', title='fixture.pdf', linkMode='linked_file')
    acknowledged = zotero_write.CreatedAttachmentKey('ATTACH23', original, 4)
    later = {**original, **change}
    monkeypatch.setattr(zotero_write, 'create_linked_file_attachment', lambda *a, **k: acknowledged)
    monkeypatch.setattr(zotero_write, 'get_item', lambda *a, **k: (later, Version(9 if change else 4, 'fixture')))
    monkeypatch.setattr(zotero_write, 'get_item_children', lambda *a, **k: [])
    prepared = {'commit': dict(parent_key='PARENT23', path='/fixture.pdf', title='fixture.pdf',
        transport=SimpleNamespace(base_url='http://127.0.0.1:23119', kind='local'),
        api_key=None, lib_path='users/0', server_id='fixture')}
    result, key, version = writers.zotero_attachment_create_post(prepared)
    assert key == 'ATTACH23' and result['ok']
    op = writers.zotero_attachment_create_journal(prepared=prepared, citation_key='fixture',
        att_key=key, version=version or 0, manifest={'ops': []})
    undo = writers._revert_zotero_attachment_create_prepare({}, op)
    assert ('commit' in undo) is (not change)
    assert version == (None if change else 4)


def test_http_create_preserves_object_acknowledgement_not_library_version(monkeypatch):
    data = dict(key='ATTACH23', itemType='attachment', parentItem='PARENT23')
    monkeypatch.setattr(zotero_write, '_urlopen', lambda *a: Reply(
        {'successful': {'0': {'key': 'ATTACH23', 'version': 4, 'data': data}}},
        {'Last-Modified-Version': '99'}))
    key = zotero_write.create_linked_file_attachment('https://api.zotero.org', 'users/1',
                                                    'PARENT23', '/fixture.pdf', 'fixture.pdf')
    assert key == 'ATTACH23' and key.creation_version == 4 and key.creation_data == data


@pytest.mark.parametrize('change,server', [({'title': 'Other paper'}, 'fixture'),
    ({'DOI': '10.1234/other'}, 'fixture'), ({}, 'other-database')])
def test_parent_must_match_scan_identity(monkeypatch, change, server):
    transport = SimpleNamespace(base_url='http://127.0.0.1:23119', kind='local',
                                api_key=None, is_web=False, can_write=True)
    monkeypatch.setattr(writers, '_zotero_transport', lambda cfg: transport)
    monkeypatch.setattr(writers, '_zotero_lib_path', lambda *a: 'users/0')
    def read(*a, **kwargs):
        assert kwargs['server_id'] == 'fixture'
        return {**dict(itemType='journalArticle', title='Reviewed', DOI='10.1234/one'), **change}, Version(4, server)
    monkeypatch.setattr(zotero_write, 'get_item', read)
    zot = SimpleNamespace(item_key='PARENT23', item_type='journalArticle', title='Reviewed',
                          doi='10.1234/one', server_id='fixture')
    assert 'early' in writers.zotero_attachment_create_prepare({}, zot, '/fixture.pdf')


def test_parent_change_after_preparation_refuses_create(monkeypatch):
    transport = SimpleNamespace(base_url='http://127.0.0.1:23119', kind='local',
                                api_key=None, is_web=False, can_write=True)
    monkeypatch.setattr(writers, '_zotero_transport', lambda cfg: transport)
    monkeypatch.setattr(writers, '_zotero_lib_path', lambda *a: 'users/0')
    original = dict(itemType='journalArticle', title='Reviewed', DOI='10.1234/one')
    read = Mock(side_effect=[(original, Version(4, 'fixture')),
                            ({**original, 'title': 'Later edit'}, Version(5, 'fixture'))])
    monkeypatch.setattr(zotero_write, 'get_item', read)
    create = Mock()
    monkeypatch.setattr(zotero_write, 'create_linked_file_attachment', create)
    zot = SimpleNamespace(item_key='PARENT23', item_type='journalArticle', title='Reviewed',
                          doi='10.1234/one', server_id='fixture')
    prepared = writers.zotero_attachment_create_prepare({}, zot, '/fixture.pdf')
    assert 'commit' in prepared
    result, key, _ = writers.zotero_attachment_create_post(prepared)
    assert not result['ok'] and key is None
    create.assert_not_called()


def test_old_readback_only_record_requires_manual_undo_review(monkeypatch):
    data = dict(itemType='attachment', parentItem='PARENT23', path='/fixture.pdf',
                title='fixture.pdf', linkMode='linked_file')
    monkeypatch.setattr(zotero_write, 'get_item', lambda *a, **k: (data, Version(4, 'fixture')))
    op = dict(transport='local', server_id='fixture', base_url='http://127.0.0.1:23119',
              lib_path='users/0', item_key='ATTACH23', parent_item='PARENT23',
              path='/fixture.pdf', title='fixture.pdf', version_after=4)
    assert 'commit' not in writers._revert_zotero_attachment_create_prepare({}, op)


def test_confirmed_identifier_must_match_parent_even_when_scan_matches(monkeypatch, tmp_path):
    from accession import review
    from audit.engine import bridge
    pdf = tmp_path / 'fixture.pdf'
    pdf.write_bytes(b'%PDF fixture')
    mapping = tmp_path / 'mapping.json'
    bridge.add_match(mapping, pdf, 'fixture', tmp_path)
    monkeypatch.setattr(review, 'validate', lambda *a: {'chosen': {'doi': '10.1234/reviewed'}})
    c = dict(path=str(pdf), expected_identity=review.identity(pdf), mapping_file=str(mapping),
             review_rel=pdf.name, citation_key='fixture', vault_root=str(tmp_path),
             review_token={'fixture': True}, parent_data={'DOI': '10.1234/different'})
    with pytest.raises(ValueError, match='Confirmed PDF identifier'):
        writers._validate_attachment_source(c)

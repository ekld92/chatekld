"""Exact native serialization admission; synthetic note bodies only."""
from types import SimpleNamespace

import pytest

from notesync import zotero_notes
from notesync.convert import md_to_html


@pytest.mark.parametrize('change', ['text', 'href', 'attribute', 'parent', 'deleted'])
def test_changed_readback_never_counts_as_success(monkeypatch, change):
    html = md_to_html('12 participants. [Reference](https://example.invalid/fixture)')
    data = dict(itemType='note', parentItem='PARENT01', note=html)
    if change == 'text':
        data['note'] = html.replace('12 participants', '13 participants')
    elif change == 'href':
        data['note'] = html.replace('/fixture', '/different')
    elif change == 'attribute':
        # Same Markdown, different HTML: do not silently discard attributes.
        data['note'] = html.replace('<p>', '<p title="new annotation">')
    elif change == 'parent':
        data['parentItem'] = 'PARENT02'
    else:
        data['deleted'] = True
    monkeypatch.setattr(zotero_notes.zotero_write, 'set_item_note', lambda *a, **k: None)
    monkeypatch.setattr(zotero_notes.zotero_write, 'get_item', lambda *a, **k: (data, 2))
    result, version = zotero_notes.patch_write({'commit': dict(
        transport=SimpleNamespace(base_url='http://fixture.invalid'), lib_path='users/0',
        note_key='NOTE0001', parent_item='PARENT01', after_html=html, before_html='',
        version=1, api_key=None)})
    assert result['status'] == 'outcome_unknown' and not result['ok']
    assert version is None

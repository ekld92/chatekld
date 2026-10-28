import io
import json
from unittest.mock import patch

import pytest

from accession import web_snapshot
from tagging import zotero_write as zw


def item(number):
    key = f'{number:08d}'
    return dict(key=key, version=7, data=dict(key=key, version=7, itemType='journalArticle'))


def response(items, total, version='7'):
    stream = io.BytesIO(json.dumps(items).encode())
    stream.headers = {'Total-Results':str(total), 'Last-Modified-Version':version}
    return stream


def test_all_pages_trash_final_probes_and_no_post():
    replies = [response([item(n) for n in range(100)],101), response([item(100)],101),
               response([item(101)],1), response([item(0)],101), response([item(101)],1)]
    with patch.object(zw, '_urlopen', side_effect=replies) as opened:
        snapshot = web_snapshot.observe('https://api.zotero.org', 'users/42', api_key='fixture')
    assert len(snapshot['items']) == 102
    assert snapshot['items']['00000101']['deleted']
    assert all(c.args[0].get_method() == 'GET' for c in opened.call_args_list)
    assert all('/api/' not in c.args[0].full_url for c in opened.call_args_list)
    inspected = web_snapshot.inspect_attempt(snapshot, dict(base=snapshot['base'], library='users/42',parent_key='ABSENT00'))
    assert inspected['parent'] is None and not inspected['automatic_retry']


@pytest.mark.parametrize('replies', [
    [response([], 1)],
    [response([],0,version='')],
    [response([item(0)],1),response([],0,version='8')],
    [response([item(0)],1),response([item(0)],1)],
    [response([],0),response([],0),response([],0,version='8')],
])
def test_incomplete_or_changing_observations_refuse(replies):
    with patch.object(zw, '_urlopen', side_effect=replies):
        with pytest.raises(zw.ZoteroError):
            web_snapshot.observe('https://api.zotero.org', 'users/42', api_key='fixture')


def test_original_target_refuses_switch():
    with pytest.raises(ValueError):
        web_snapshot.inspect_attempt(dict(complete=True, base='https://api.zotero.org', library='users/42'),
                                     dict(base='https://api.zotero.org',library='users/43'))


def test_web_and_desktop_item_urls_use_distinct_contracts():
    assert zw._url('https://api.zotero.org', 'users/42', 'ITEM0001') == 'https://api.zotero.org/users/42/items/ITEM0001'
    assert zw._items_url('https://api.zotero.org/api', 'users/42') == 'https://api.zotero.org/users/42/items'
    assert zw._url('http://127.0.0.1:23119', 'users/0', 'ITEM0001') == 'http://127.0.0.1:23119/api/users/0/items/ITEM0001'


@pytest.mark.parametrize('attachment', [
    dict(itemType='attachment', linkMode='linked_file', path='attachments:one.pdf'),
    dict(itemType='attachment', linkMode='imported_file'),
    dict(itemType='journalArticle', DOI='10.1234/one', deleted=True),
])
def test_web_admission_preserves_ambiguous_and_trashed_barriers(tmp_path, attachment):
    import time
    pdf = tmp_path/'one.pdf'
    pdf.write_bytes(b'fixture')
    snapshot = dict(base='https://api.zotero.org', library='users/42', complete=True,
                    version=7, checked_at=time.time(), items={'ITEM0001':attachment})
    assert not web_snapshot.admission(snapshot, pdf, {'DOI':'10.1234/one'},
        expected_target=dict(base=snapshot['base'], library=snapshot['library']))['eligible']

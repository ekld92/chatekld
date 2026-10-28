import io
import json
from unittest.mock import Mock

import pytest
import importlib.util

if importlib.util.find_spec('prototypes') is None:
    pytest.skip('Private prototype fixtures are absent from public source artifacts', allow_module_level=True)

from prototypes.zotero_desktop.metadata_snapshot import fetch


class Reply(io.BytesIO):
    def __init__(self, body, *, server='fixture', version=7, total=101):
        super().__init__(json.dumps(body).encode())
        self.headers = {'Zotero-Server-ID':server, 'Last-Modified-Version':str(version), 'Total-Results':str(total)}


def item(number):
    key = f'{number:08}'
    return {'key':key, 'data':{'key':key, 'itemType':'journalArticle', 'title':str(number)}}


def opener(last):
    value = Mock()
    value.open.side_effect = [Reply([{'field':'title'}]), Reply([item(i) for i in range(100)]), last]
    return value


def test_complete_paginated_snapshot_is_database_bound():
    transport = opener(Reply([item(100)]))
    snapshot = fetch('http://127.0.0.1:50001', 'fixture', opener=transport)
    assert len(snapshot['items']) == snapshot['total'] == 101
    assert snapshot['library_version'] == 7
    assert all(call.args[0].get_header('Zotero-server-id') == 'fixture' for call in transport.open.call_args_list)


@pytest.mark.parametrize('body,kwargs,reason', [
    ([item(100)], {'server':'other'}, 'database changed'),
    ([item(100)], {'version':8}, 'pagination changed'),
    ([item(100)], {'total':102}, 'pagination changed'),
    ([], {}, 'Incomplete'),
    ([item(0)], {}, 'Duplicate'),
])
def test_incomplete_or_changing_pages_are_not_duplicate_evidence(body, kwargs, reason):
    with pytest.raises(ValueError, match=reason):
        fetch('http://127.0.0.1:50001', 'fixture', opener=opener(Reply(body, **kwargs)))

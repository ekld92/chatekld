import io
import json
from unittest.mock import Mock
from urllib.request import Request

import pytest
from tagging import local_api, zotero_write


class Reply(io.BytesIO):
    def __init__(self, data=None, headers=None):
        super().__init__(json.dumps(data or {}).encode())
        self.headers = headers or {}


@pytest.fixture(autouse=True)
def reset_grants():
    local_api._grants.clear()
    local_api._preferred.clear()
    yield
    local_api._grants.clear()
    local_api._preferred.clear()


def status(instance='fixture'):
    return Reply(headers={'Zotero-Server-ID':instance,'Zotero-Version':'10.0.1','Zotero-API-Version':'3'})


def test_reads_never_authorize_and_single_use_is_consumed():
    opener=Mock()
    opener.open.side_effect=[status(), Reply({'key':'a'*32,'remember':False}), Reply()]
    result=local_api.authorize('http://127.0.0.1:23119','fixture',opener=opener)
    assert 'key' not in result
    assert opener.open.call_args_list[1].args[0].full_url.endswith('/api/local/authorize')
    assert json.loads(opener.open.call_args_list[1].args[0].data) == {'appName': 'PaperMind'}
    req=Request('http://127.0.0.1:23119/api/users/0/items/TESTKEY1',data=b'{}',method='PATCH',headers={'Zotero-Server-ID':'fixture'})
    local_api.open_request(req,10,opener=opener)
    assert req.get_header('Zotero-api-key')=='a'*32
    with pytest.raises(zotero_write.ZoteroAuthError):
        local_api.open_request(req,10,opener=opener)
    assert local_api.prefers_local('http://127.0.0.1:23119')
    assert opener.open.call_count==3


def test_different_instance_cannot_receive_old_version_or_key():
    local_api._grants[('http://127.0.0.1:23119','old')]=('a'*32,True)
    opener=Mock()
    opener.open.return_value=status('new')
    state=local_api.capabilities('http://127.0.0.1:23119',opener=opener)
    assert not state['authorized']
    assert not local_api._grants
    with pytest.raises(zotero_write.ZoteroVersionConflict):
        local_api.open_request(Request('http://127.0.0.1:23119/api/users/0/items/TESTKEY1',method='PATCH'),10,opener=opener)


def test_version_carries_instance_from_read_into_patch(monkeypatch):
    calls=[]
    def fake(req, timeout):
        calls.append(req)
        if req.get_method()=='GET':
            return Reply({'data':{'version':3,'tags':[]}}, {'Last-Modified-Version':'3','Zotero-Server-ID':'fixture'})
        return Reply(headers={'Last-Modified-Version':'4'})
    monkeypatch.setattr(zotero_write,'_urlopen',fake)
    _, version=zotero_write.get_item('http://127.0.0.1:23119','users/0','TESTKEY1')
    zotero_write.set_item_tags('http://127.0.0.1:23119','users/0','TESTKEY1',[],version)
    assert calls[-1].get_header('Zotero-server-id')=='fixture'
    assert calls[-1].get_header('If-unmodified-since-version')=='3'


def test_tag_read_carries_instance(monkeypatch):
    monkeypatch.setattr(zotero_write,'_urlopen',lambda *a:Reply({'version':2,'data':{'tags':[]}}, {'Zotero-Server-ID':'fixture'}))
    version,_=zotero_write.get_item_tags_full('http://127.0.0.1:23119','users/0','TESTKEY1')
    assert version.server_id=='fixture'


def test_local_authorization_refuses_remote_origin():
    opener=Mock()
    with pytest.raises(zotero_write.ZoteroError):
        local_api.authorize('https://example.org','fixture',opener=opener)
    opener.open.assert_not_called()


def test_restore_reads_require_instance_and_send_it_before_lookup(monkeypatch):
    requests=[]
    def fake(req, timeout):
        requests.append(req)
        return Reply({'data':{'version':3}}, {'Zotero-Server-ID':'fixture'})
    monkeypatch.setattr(zotero_write, '_urlopen', fake)
    base='http://127.0.0.1:23119'
    with pytest.raises(zotero_write.ZoteroError, match='identity'):
        zotero_write.get_item(base,'users/0','TESTKEY1',**local_api.restore_kwargs({'transport':'local'}))
    assert not requests
    zotero_write.get_item(base,'users/0','TESTKEY1',**local_api.restore_kwargs({'transport':'local','server_id':'fixture'}))
    assert requests[-1].get_header('Zotero-server-id')=='fixture'


def test_remembered_grant_reused_but_revocation_forgets_it():
    from urllib.error import HTTPError
    base='http://127.0.0.1:23119'
    local_api._grants[(base,'fixture')]=('b'*32,True)
    opener=Mock()
    opener.open.side_effect=[Reply(), HTTPError(base,401,'revoked',{},None)]
    def request():
        return Request(base+'/api/users/0/items/TESTKEY1',method='PATCH',headers={'Zotero-Server-ID':'fixture'})
    local_api.open_request(request(),10,opener=opener)
    with pytest.raises(HTTPError):
        local_api.open_request(request(),10,opener=opener)
    with pytest.raises(zotero_write.ZoteroAuthError):
        local_api.open_request(request(),10,opener=opener)
    assert opener.open.call_count==2


def test_changed_database_blocks_authorization_without_prompt():
    opener=Mock()
    opener.open.return_value=status('new')
    with pytest.raises(zotero_write.ZoteroVersionConflict):
        local_api.authorize('http://127.0.0.1:23119','old',opener=opener)
    assert opener.open.call_count==1
    assert opener.open.call_args.args[0].get_method()=='GET'


def test_delayed_revocation_does_not_forget_a_new_grant():
    from urllib.error import HTTPError
    base = 'http://127.0.0.1:23119'
    local_api._grants[(base, 'fixture')] = ('a' * 32, True)
    replacement = ('b' * 32, True)
    def delayed_failure(*args, **kwargs):
        local_api._grants[(base, 'fixture')] = replacement
        raise HTTPError(base, 401, 'revoked', {}, None)
    opener = Mock()
    opener.open.side_effect = delayed_failure
    req = Request(base + '/api/users/0/items/TESTKEY1', method='PATCH',
                  headers={'Zotero-Server-ID': 'fixture'})
    with pytest.raises(HTTPError):
        local_api.open_request(req, 10, opener=opener)
    assert local_api._grants[(base, 'fixture')] == replacement


def test_delayed_status_does_not_erase_authorization_for_new_database():
    base = 'http://127.0.0.1:23119'
    replacement = ('b' * 32, True)
    def delayed_status(*args, **kwargs):
        # The old database response arrives after a new database was authorized.
        local_api._grants[(base, 'new')] = replacement
        return status('old')
    opener = Mock()
    opener.open.side_effect = delayed_status
    assert not local_api.capabilities(base, opener=opener)['authorized']
    assert local_api._grants[(base, 'new')] == replacement


def test_automatic_transport_prefers_authorized_desktop_only(monkeypatch):
    monkeypatch.setattr(zotero_write,'api_key_from_env',lambda:'web-fixture')
    assert zotero_write.resolve_transport({}).is_web
    local_api._preferred.add('http://127.0.0.1:23119')
    assert not zotero_write.resolve_transport({}).is_web
    assert zotero_write.resolve_transport({'tagging_zotero_transport':'web'}).is_web


def test_restore_refuses_a_server_that_does_not_honor_instance_header(monkeypatch):
    monkeypatch.setattr(zotero_write,'_urlopen',lambda *a: Reply({'data':{'version':3}}, {'Zotero-Server-ID':'other'}))
    with pytest.raises(zotero_write.ZoteroError, match='identity changed'):
        zotero_write.get_item('http://127.0.0.1:23119','users/0','TESTKEY1',server_id='fixture')


def test_reserved_attachment_key_is_create_only_on_wire(monkeypatch):
    requests=[]
    def fake(req, timeout):
        requests.append(req)
        return Reply({'successful':{'0':{'key':'ABCD2345'}}})
    monkeypatch.setattr(zotero_write,'_urlopen',fake)
    zotero_write.create_linked_file_attachment('http://127.0.0.1:23119','users/0','PARENT1','/fixture.pdf','PDF',item_key='ABCD2345',server_id='fixture')
    payload=json.loads(requests[0].data)[0]
    assert payload['key']=='ABCD2345'
    assert payload['version']==0
    assert payload['parentItem']=='PARENT1'


def test_desktop_reserved_key_bug_does_not_fall_back_to_unkeyed_post(monkeypatch):
    post = Mock(return_value=Reply({'failed': {'0': {
        'code': 400, 'key': 'ABCD2345',
        'message': "'primaryData' not loaded for item (null/1/ABCD2345)"}}}))
    monkeypatch.setattr(zotero_write, '_urlopen', post)
    with pytest.raises(zotero_write.ZoteroWriteUnsupported, match='no unkeyed retry'):
        zotero_write.create_linked_file_attachment('http://127.0.0.1:23119', 'users/0',
            'PARENT1', '/fixture.pdf', 'PDF', item_key='ABCD2345', server_id='fixture')
    assert post.call_count == 1
    assert json.loads(post.call_args.args[0].data)[0]['key'] == 'ABCD2345'

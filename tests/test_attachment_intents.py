from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from tagging import attachment_intents


def fixture():
    return dict(transport=SimpleNamespace(base_url='http://127.0.0.1:23119'),
                server_id='database-one', lib_path='users/0', parent_key='PARENT1',
                path='/fixture/one.pdf', review_token={'id':'file-one'},
                expected_identity={'dev':1,'ino':2,'birth':3,'sha256':'abc','mtime':4})


def test_durable_reservation_separates_files_and_database_instances(tmp_path, monkeypatch):
    path=tmp_path/'intents.sqlite3'
    monkeypatch.setattr(attachment_intents,'store_path',lambda:path)
    c=fixture()
    assert attachment_intents.reserve(tmp_path,c,create=False) is None
    assert not path.exists()
    key=attachment_intents.reserve(tmp_path,c)
    c['expected_identity']['mtime']=5
    assert attachment_intents.reserve(tmp_path,c,create=False)==key
    c['review_token']={'id':'file-two'}
    assert attachment_intents.reserve(tmp_path,c)!=key
    c['review_token']={'id':'file-one'}
    c['server_id']='database-two'
    assert attachment_intents.reserve(tmp_path,c)!=key


def test_concurrent_reservations_return_one_key(tmp_path, monkeypatch):
    monkeypatch.setattr(attachment_intents,'store_path',lambda:tmp_path/'intents.sqlite3')
    with ThreadPoolExecutor(max_workers=4) as pool:
        keys=list(pool.map(lambda _:attachment_intents.reserve(tmp_path,fixture()), range(12)))
    assert len(set(keys))==1

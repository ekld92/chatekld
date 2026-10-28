"""Outcome, retry and durability contracts for library runs."""
from types import SimpleNamespace
import test_library_workflow_lot5 as fixtures
from accession import library_run

UID = 'attachment:1:A'
ROW = {'unit': UID, 'citation_key': 'k', 'tags': ['dep']}

def test_unknown_tag_outcome_must_not_be_replayed():
    f = fixtures.FakeT()
    f.units[UID]['steps'] = {'tag': {'status': 'uncertain'}}
    library_run.apply(f.transport(), 's', {'tags': [ROW]})
    assert not any(name == 'apply_tags' for name, args in f.calls)

def test_resume_only_failed_destinations():
    f = fixtures.FakeT()
    f.units[UID]['steps'] = {'tag': {'status': 'failed', 'detail': {
        'tags': ['dep'], 'targets': {'finder': 'done', 'zotero': 'failed'}}}}
    library_run.apply(f.transport(), 's', {'tags': [ROW]})
    args = next(args for name, args in f.calls if name == 'apply_tags')
    assert args[3]['finder'] is False

def test_zotero_commit_failure_is_uncertain_but_preflight_refusal_is_retryable():
    for result, expected in [({'error': 'connection lost'}, 'uncertain'),
                             ({'error': 'not authorized', 'write_not_started': True}, 'failed')]:
        f = fixtures.FakeT(apply_tags=(200, {'results': {'zotero': result}}))
        row = {**ROW, 'targets': {'finder': False, 'note': False, 'zotero': True}}
        out = library_run.apply(f.transport(), 's', {'tags': [row]})
        assert out['tags'][0]['status'] == expected
        assert out['tags'][0]['targets']['zotero'] == expected

def test_explicit_retry_refuses_when_previous_receipt_is_no_longer_failed():
    f = fixtures.FakeT()
    f.units[UID]['steps'] = {'tag': {'status': 'done', 'detail': {
        'tags': ['dep'], 'targets': {'finder': 'done', 'zotero': 'done'}}}}
    row = {**ROW, 'retry_failed_only': True, 'targets': {'finder': False, 'note': False, 'zotero': True}}
    result = library_run.apply(f.transport(), 's', {'tags': [row]})
    assert result['tags'][0]['status'] == 'failed'
    assert not any(name == 'apply_tags' for name, _ in f.calls)

def test_missing_terminal_cannot_close_note():
    f = fixtures.FakeT(mirror=(200, {}))
    out = library_run.apply(f.transport(), 's', {'mirror': {'plan_digest': 'd', 'units': ['note:1:N']}})
    assert out['mirror']['status'] == 'uncertain'
    assert not f.steps

def test_stopped_after_writes_is_partial():
    f = fixtures.FakeT(mirror=(200, {'ok': False, 'stopped': 'indexing', 'created': 2}))
    out = library_run.apply(f.transport(), 's', {'mirror': {'plan_digest': 'd'}})
    assert out['mirror']['status'] == 'partial'

def test_record_failure_is_visible():
    f = fixtures.FakeT()
    t = f.transport()
    t.record_step = lambda *args: (400, {'error': 'Unit absorbed concurrently'})
    out = library_run.apply(t, 's', {'tags': [ROW]})
    assert out['tags'][0]['status'] != 'done'

def test_transport_missing_mirror_terminal_is_error(tmp_path):
    import flask
    from api.routes.library_workflow import _transport
    app = flask.Flask(__name__)
    @app.post('/api/notesync/mirror')
    def stub():
        return flask.Response('data: {"info":"working"}\n\ndata: [DONE]\n\n', mimetype='text/event-stream')
    t = _transport(SimpleNamespace(vault_root=tmp_path, biblio_articles_dir=tmp_path/'biblio'), app)
    code, body = t.mirror('d')
    assert code != 200


def test_unknown_rename_outcome_must_not_be_replayed():
    f = fixtures.FakeT()
    f.units[UID]['steps'] = {'rename': {'status': 'uncertain'}}
    library_run.apply(f.transport(), 's', {'renames': [
        {'unit': UID, 'rel': 'x.pdf', 'new_name': 'new.pdf'}]})
    assert not any(name == 'rename' for name, args in f.calls)


def test_failed_resume_preserves_completed_destinations_for_the_next_retry():
    f = fixtures.FakeT(apply_tags=(200, {'results': {'zotero': {'error': '412'}}}))
    f.units[UID]['steps'] = {'tag': {'status': 'failed', 'detail': {
        'tags': ['dep'], 'targets': {'finder': 'done', 'zotero': 'failed'}}}}
    library_run.apply(f.transport(), 's', {'tags': [ROW]})
    receipt = f.steps[-1][3]
    assert receipt['targets']['finder'] == 'done'


def test_http_refusal_preserves_completed_target_receipts():
    f = fixtures.FakeT(apply_tags=(409, {'error': 'Vocab changed'}))
    f.units[UID]['steps'] = {'tag': {'status': 'failed', 'detail': {
        'tags': ['dep'], 'targets': {'finder': 'done', 'zotero': 'failed'}}}}
    library_run.apply(f.transport(), 's', {'tags': [ROW]})
    detail = f.steps[-1][3]
    assert detail.get('targets', {}).get('finder') == 'done'

def test_capped_create_does_not_close_uncreated_note():
    f = fixtures.FakeT(mirror=(200, {'ok': True, 'created': 100, 'create_total': 101,
        'create_capped_at': 100, 'create_keys': ['other'], 'refresh_keys': [],
        'listed': {}, 'failed': {}, 'skipped': {'create': {}}}))
    library_run.apply(f.transport(), 's', {'mirror': {'plan_digest': 'd', 'units': ['note:1:N']}})
    assert not f.steps


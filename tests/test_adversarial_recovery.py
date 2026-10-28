"""Isolated counterexamples from the 2026-10-04 review: no external services."""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import flask
import pytest

from tagging import journal, writers, zotero_write
from tagging.local_api import Version


@pytest.mark.parametrize('state', ['applied', 'outcome_unknown'])
def test_tag_restore_refuses_type_only_edit(state):
    op = dict(state=state, item_key='ABCDEFGH', transport='local', server_id='db-A',
              before=['old'], before_raw=[{'tag': 'old', 'type': 0}],
              after=['old', 'new'], after_raw=[{'tag': 'old', 'type': 0}, {'tag': 'new'}])
    current = [{'tag': 'old', 'type': 1}, {'tag': 'new'}]
    with patch.object(zotero_write, 'get_item_tags_full', return_value=(Version(5, 'db-A'), current)), \
            patch.object(zotero_write, 'set_item_tags') as write:
        result = writers._revert_zotero_tag(None, {}, op)
    assert not result['ok']
    assert op['state'] == state
    write.assert_not_called()


def test_tag_restore_normalizes_order_and_default_type():
    op = dict(state='applied', item_key='ABCDEFGH', transport='local', server_id='db-A',
              before=['old'], before_raw=[{'tag': 'old', 'type': 1}],
              after=['old', 'new'], after_raw=[{'tag': 'old', 'type': 1}, {'tag': 'new', 'type': 0}])
    with patch.object(zotero_write, 'get_item_tags_full', return_value=(Version(5, 'db-A'), [{'tag': 'new'}, {'tag': 'old', 'type': 1}])), \
            patch.object(zotero_write, 'set_item_tags', return_value=6) as write, \
            patch('core.utils.log_vault_write'):
        result = writers._revert_zotero_tag(None, {}, op)
    assert result['status'] == 'reverted'
    assert write.call_args.args[3] == [{'tag': 'old', 'type': 1}]


def test_tag_restore_legacy_evidence_is_retained():
    op = dict(state='applied', item_key='ABCDEFGH', transport='local', server_id='db-A', before=['old'], after=['old', 'new'])
    with patch.object(zotero_write, 'get_item_tags_full', return_value=(Version(5, 'db-A'), [{'tag': 'old'}, {'tag': 'new'}])):
        result = writers._revert_zotero_prepare({}, op)
    assert result['early']['status'] == 'unverified'
    assert op['state'] == 'applied'


def test_missing_note_keeps_snapshot_even_over_retention_cap_and_retries(tmp_path):
    vault = tmp_path / 'vault'
    vault.mkdir()
    arch = tmp_path / 'archive'
    arch.mkdir()
    (arch / 'before.md').write_bytes(b'original')
    renamed = vault / 'renamed.md'
    renamed.write_bytes(b'after')
    op = dict(kind='note_tag', state='applied', note_rel='old.md', snapshot_rel='before.md',
              hash_after=hashlib.sha256(b'after').hexdigest())
    with patch.object(journal, 'archive_dir', return_value=arch), patch.object(journal, '_MAX_OPS', 0):
        assert journal._revert_note_tag(vault, {}, op)['status'] == 'missing_target'
        assert journal.prune(vault, {}, {'ops': [op]}) == 0
        assert (arch / 'before.md').read_bytes() == b'original'
        renamed.rename(vault / 'old.md')
        assert journal._revert_note_tag(vault, {}, op)['status'] == 'reverted'
        assert (vault / 'old.md').read_bytes() == b'original'


def test_mapping_fields_share_one_file_generation(tmp_path):
    from audit.engine import bridge
    path = tmp_path / 'mapping.json'
    old = dict(matches={'A': ['x.pdf']}, hashes={'x.pdf': 'abc'}, sources={'x.pdf': 'accession'},
               pending=['y.pdf'], exclude={'z.pdf': ['A']})
    new = dict(matches={}, pending=['x.pdf'])
    path.write_text(json.dumps(old))
    original = type(path).read_text
    calls = []
    def read(p, *args, **kwargs):
        result = original(p, *args, **kwargs)
        calls.append(p)
        path.write_text(json.dumps(new))
        return result
    with patch.object(type(path), 'read_text', read):
        result = bridge._load_mapping_all(path)
    assert len(calls) == 1
    assert result.matches == old['matches']
    assert result.pending == {'y.pdf'} and result.exclude == old['exclude']
    assert result.hashes['x.pdf']['sha256'] == 'abc' and result.sources == old['sources']


def test_repoint_pdf_extension_case(tmp_path):
    from api.routes.zotlink import _biblio_pdf_rels
    (tmp_path / 'sub').mkdir()
    for name in ('lower.pdf', 'UPPER.PDF', 'sub/Mixed.Pdf', 'ignored.txt'):
        (tmp_path / name).write_bytes(b'fixture')
    assert _biblio_pdf_rels(SimpleNamespace(biblio_articles_dir=tmp_path)) == ['UPPER.PDF', 'lower.pdf', 'sub/Mixed.Pdf']


@pytest.mark.parametrize('row', [None, SimpleNamespace(server_id=None), SimpleNamespace(server_id='desktop-A')])
def test_metadata_cannot_transfer_desktop_keys_to_web(row):
    with patch.dict('os.environ', {'ZOTERO_API_KEY': 'fixture-key'}), \
            patch.object(zotero_write, 'resolve_user_id') as resolve, patch.object(zotero_write, 'get_item') as read:
        result = writers.zotero_metadata_prepare({'tagging_zotero_transport': 'web'}, 'ABCDEFGH', {'title': 'new'}, {'title': 'old'}, row=row)
    assert 'early' in result and not result['early']['ok']
    resolve.assert_not_called()
    read.assert_not_called()


@pytest.mark.parametrize('mixed', [False, True])
def test_unknown_batch_is_reported_and_retained(tmp_path, mixed):
    from api.routes import tagging
    vault = tmp_path / 'vault'
    vault.mkdir()
    cfg = {'tagging_archive_dir': str(tmp_path / 'archive')}
    manifest = journal.load(vault, cfg)
    manifest['ops'].append(dict(op_id='unknown', kind='zotero_tag', batch_id='batch', state='outcome_unknown', item_key='A'))
    if mixed:
        manifest['ops'].append(dict(op_id='known', kind='zotero_tag', batch_id='batch', state='applied', item_key='B'))
    journal.save(vault, cfg, manifest)
    app = flask.Flask(__name__)
    app.register_blueprint(tagging.tagging_bp)
    def prepare(cfg, op):
        return ({'early': {'ok': False, 'status': 'outcome_unknown', 'message': 'unverified'}}
                if op['op_id'] == 'unknown' else {'commit': {}})
    def commit(prepared, op):
        op['state'] = 'reverted'
        return {'ok': True, 'status': 'reverted'}
    with patch.object(tagging, '_vault_root', return_value=vault), patch.object(tagging, 'load_config', return_value=cfg), \
            patch.object(tagging, '_prepare_zotero_revert', side_effect=prepare), \
            patch.object(tagging, '_revert_zotero_commit_locked', side_effect=commit):
        response = app.test_client().post('/api/tagging/restore', json={'batch_id': 'batch', 'confirm': True})
        frames = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines() if line.startswith('data: {')]
    assert response.status_code == 200
    result = next(f['restore'] for f in frames if 'restore' in f)
    assert not result['ok'] and result['unresolved'] == 1
    assert result['total'] == (2 if mixed else 1)
    assert result['reverted'] == int(mixed)
    assert journal.peek_op(vault, cfg, 'unknown')['state'] == 'outcome_unknown'


def test_dependency_notice_does_not_contaminate_json(capsys):
    from scripts import check_dependency_audits as audits
    with patch.object(audits.subprocess, 'run', return_value=SimpleNamespace(
            returncode=0, stdout='{"vulnerabilities":{}}', stderr='npm notice update available\n')) as run:
        code, output = audits._run(['npm', 'audit', '--json'])
    assert code == 0 and json.loads(output) == {'vulnerabilities': {}}
    assert 'npm notice' in capsys.readouterr().err
    assert run.call_args.kwargs['stderr'] == audits.subprocess.PIPE


@pytest.mark.parametrize('name', ['read_unzoterod', 'unread_unzoterod'])
def test_unavailable_zotero_cannot_produce_absence_rows(tmp_path, name):
    from importlib import import_module
    from audit import serialize
    from audit.config import Settings
    from audit.engine.bridge import BridgeResult
    from audit.engine.inventory import Inventory
    report = import_module('audit.engine.reports.' + name)
    inv = Inventory(records={}, bridge=BridgeResult(unmapped_pdfs=[tmp_path / 'a.pdf']),
                    pdfs_skipped=[], zotero_matched_by_title=0, zotero_error='snapshot unavailable')
    with patch.object(report.pdf_annotations, 'read_annotations') as read:
        result = report.find(inv, Settings(tmp_path))
    payload = getattr(serialize, 'report_' + name)(result, Settings(tmp_path))
    assert payload['rows'] == [] and 'absence cannot be established' in payload['error']
    read.assert_not_called()


def test_reset_refuses_active_context_and_keeps_lease():
    from api.audit_context import register_context_guard, retain_for_worker
    app = flask.Flask(__name__)
    register_context_guard(app)
    release = None
    @app.post('/api/tagging/test-lease')
    def mutation():
        nonlocal release
        release = retain_for_worker()
        return {}
    @app.post('/api/reset')
    def reset():
        pytest.fail('Reset reached while a mutation worker is active')
    try:
        assert app.test_client().post('/api/tagging/test-lease').status_code == 200
        response = app.test_client().post('/api/reset', json={'confirm': 'reset'})
        assert response.status_code == 409 and response.json['reason'] == 'audit_context_busy'
    finally:
        if release:
            release()


def test_reset_cannot_steal_another_vault_operation():
    from api.routes import config
    from rag.vault import obsidian_manager
    app = flask.Flask(__name__)
    app.register_blueprint(config.config_bp)
    epoch = obsidian_manager.try_acquire_lock(ttl=60)
    assert epoch
    try:
        with patch.object(obsidian_manager, 'request_stop'), patch.object(obsidian_manager, 'wait_for_indexing', return_value=True), \
                patch.object(obsidian_manager, 'cleanup') as cleanup, patch.object(obsidian_manager, 'force_release') as release:
            response = app.test_client().post('/api/reset', json={'confirm': 'reset'})
        assert response.status_code == 409
        cleanup.assert_not_called()
        release.assert_not_called()
        assert obsidian_manager.heartbeat(epoch)
    finally:
        obsidian_manager.release_lock(epoch)


def test_sse_budget_covers_retry_after_without_request_deadline():
    from api.sse import consumer_stall_s, _RETRY_BACKOFF_MAX_S
    from core.llm import retry
    from core.llm.types import LLMError, ErrorCategory
    clock = [0.0]
    attempts = []
    def call():
        attempts.append(clock[0])
        clock[0] += 150
        if len(attempts) < 3:
            raise LLMError(category=ErrorCategory.RATE_LIMIT, message='fixture', retry_after_s=90)
        return 'done'
    with patch.object(retry.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
        assert retry.retry_with_backoff(call, max_attempts=3) == 'done'
    assert _RETRY_BACKOFF_MAX_S >= retry.RETRY_AFTER_CEILING_S
    assert clock[0] == 630
    assert consumer_stall_s({'online_timeout_s': 150, 'online_max_retries': 3}) > clock[0]


def test_batch_restore_verifies_unknown_tags_then_records_revert(tmp_path):
    from api.routes import tagging
    vault = tmp_path / 'vault'
    vault.mkdir()
    cfg = {'tagging_archive_dir': str(tmp_path / 'archive')}
    manifest = journal.load(vault, cfg)
    manifest['ops'].append(dict(op_id='unknown', kind='zotero_tag', batch_id='batch',
                               state='outcome_unknown', item_key='A', server_id='fixture-db', transport='local',
                               before=[], before_raw=[], after=['new'], after_raw=[{'tag': 'new', 'type': 0}]))
    journal.save(vault, cfg, manifest)
    app = flask.Flask(__name__)
    app.register_blueprint(tagging.tagging_bp)
    with patch.object(tagging, '_vault_root', return_value=vault), patch.object(tagging, 'load_config', return_value=cfg), \
            patch.object(zotero_write, 'get_item_tags_full', return_value=(Version(5, 'fixture-db'), [{'tag': 'new'}])) as read, \
            patch.object(zotero_write, 'set_item_tags', return_value=6) as write:
        response = app.test_client().post('/api/tagging/restore', json={'batch_id': 'batch', 'confirm': True})
        frames = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines() if line.startswith('data: {')]
    result = next(f['restore'] for f in frames if 'restore' in f)
    assert result['ok'] and result['reverted'] == 1 and result['unresolved'] == 0
    assert read.call_args.kwargs['server_id'] == 'fixture-db'
    assert write.call_count == 1 and write.call_args.args[3] == []
    assert journal.peek_op(vault, cfg, 'unknown')['state'] == 'reverted'


def test_driver_preserves_partial_restore_and_journal_failure():
    from core import driver
    terminal = {'ok': False, 'total': 2, 'reverted': 2, 'unresolved': 0, 'journal_recorded': False,
                'cancelled': False, 'results': [{'op_id': 'a', 'status': 'reverted'}]}
    with patch.object(driver, 'resolve_base_url', return_value='http://127.0.0.1:1'), \
            patch.object(driver, '_http_sse', return_value={'events': [{'restore': terminal}]}) as request:
        result = driver.tagging_restore_batch('batch')
    assert result['restore'] == terminal
    assert request.call_args.args[:2] == ('tagging_restore', {'batch_id': 'batch', 'confirm': True})


def test_reset_reports_cache_removal_failure_and_completed_steps(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from unittest.mock import Mock
    from api.routes import config

    cache = tmp_path / 'cache'
    cache.mkdir()
    retained = cache / 'retained.txt'
    retained.write_text('fixture')
    app = flask.Flask(__name__)
    app.register_blueprint(config.config_bp)
    manager = Mock()
    manager.wait_for_indexing.return_value = True
    manager.try_acquire_lock.return_value = 123
    monkeypatch.setattr(config, 'obsidian_manager', manager)
    monkeypatch.setattr(config, 'OBSIDIAN_CACHE_DIR', str(cache))
    monkeypatch.setattr(config, 'OBSIDIAN_INDEX_DIR', str(tmp_path / 'absent-index'))
    connection = Mock()

    @contextmanager
    def database():
        yield connection

    monkeypatch.setattr(config, 'get_db_connection', database)
    monkeypatch.setattr('audit.manager.audit_manager.reset_to_idle', lambda: None)
    def fail_remove(path, **kwargs):
        assert path == str(cache)
        # Emulate shutil's old ignore_errors behavior to prove false success.
        if not kwargs.get('ignore_errors'):
            raise PermissionError('fixture removal refused')
    monkeypatch.setattr(config.shutil, 'rmtree', fail_remove)
    response = app.test_client().post('/api/reset', json={'confirm': 'reset'})
    assert response.status_code == 500
    assert response.json['ok'] is False
    assert response.json['deleted'] == ['uploads_db']
    assert 'extraction cache' in response.json['error']
    assert retained.read_text() == 'fixture'
    manager.release_lock.assert_called_once_with(123)

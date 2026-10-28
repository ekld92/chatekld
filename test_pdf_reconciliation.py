"""Behavioral reconciliation tests: real disposable SQLite/JSON/PDF bytes."""
import json
import sqlite3
from unittest.mock import patch

import pytest

from accession import reconciliation as rec, review
from audit.config import Settings
from zotcreate import attempts


@pytest.fixture
def world(tmp_path):
    vault = tmp_path / 'vault'
    pdfs = vault / 'biblio_articles'
    pdfs.mkdir(parents=True)
    pdf = pdfs / 'one.pdf'
    pdf.write_bytes(b'%PDF-fixture-one')
    (vault / '_master.bib').write_text('@article{KeyOne,title={One}}')
    settings = Settings(vault_root=vault, attachments_subdir='', zotero_sqlite=tmp_path/'zotero.sqlite',
                        zotero_storage=tmp_path/'storage', mapping_file_override=tmp_path/'mapping.json')
    settings.mapping_file.write_text(json.dumps({'pending':['biblio_articles/one.pdf']}))
    with sqlite3.connect(settings.zotero_sqlite) as db:
        db.executescript('''
        CREATE TABLE settings(setting,key,value);
        INSERT INTO settings VALUES ('localAPI','serverID','fixture-db');
        CREATE TABLE itemTypes(itemTypeID,typeName);
        INSERT INTO itemTypes VALUES (1,'journalArticle'),(2,'attachment');
        CREATE TABLE items(itemID,key,libraryID,itemTypeID);
        INSERT INTO items VALUES (1,'PARENT01',1,1),(2,'ATTACH01',1,2),(3,'PARENT02',1,1);
        CREATE TABLE deletedItems(itemID);
        CREATE TABLE itemData(itemID,fieldID,valueID);
        CREATE TABLE fields(fieldID,fieldName);
        INSERT INTO fields VALUES (1,'title'),(2,'citationKey'),(3,'publicationTitle');
        CREATE TABLE itemDataValues(valueID,value);
        INSERT INTO itemDataValues VALUES (1,'One'),(2,'KeyOne'),(3,'Journal of Fixtures');
        INSERT INTO itemData VALUES (1,1,1),(1,2,2),(1,3,3);
        CREATE TABLE creators(creatorID,firstName,lastName,fieldMode);
        INSERT INTO creators VALUES(1,'Ada','Test',0);
        CREATE TABLE creatorTypes(creatorTypeID,creatorType);
        INSERT INTO creatorTypes VALUES(1,'author');
        CREATE TABLE itemCreators(itemID,creatorID,creatorTypeID,orderIndex);
        INSERT INTO itemCreators VALUES(1,1,1,0);
        CREATE TABLE tags(tagID,name);
        CREATE TABLE itemTags(itemID,tagID);
        CREATE TABLE itemAttachments(itemID,parentItemID,path,linkMode);
        ''')
        db.execute('INSERT INTO itemAttachments VALUES (2,1,?,2)', (str(pdf),))
    with patch('core.constants.BASE_DIR', str(tmp_path/'app')):
        yield settings, pdf


def row(settings, observed=None):
    return rec.rows(settings, observed or rec.saved(settings))[0]


def mutate(settings, sql, params=()):
    with sqlite3.connect(settings.zotero_sqlite) as db:
        db.execute(sql, params)


def test_manual_attachment_delayed_bib_mapping_and_restart(world):
    s, pdf = world
    s.master_bib.write_text('')
    assert rec.refresh(s)['available']
    assert row(s)['state'] == 'Awaiting bibliography'
    assert not row(s)['can_create']
    s.master_bib.write_text('@article{KeyOne,title={One}}')
    rec.use_association(s, row(s))
    assert row(s)['state'] == 'Linked'
    assert json.loads(s.mapping_file.read_text())['matches']['KeyOne'] == ['biblio_articles/one.pdf']
    assert rec.refresh(s)['available']
    assert row(s)['state'] == 'Linked'
    assert not row(s)['can_create']


def test_accepted_difference_survives_restart_tag_edit_but_reparent_reopens(world):
    s, _ = world
    s.mapping_file.write_text(json.dumps({'matches':{'Other':['biblio_articles/one.pdf']}}))
    rec.refresh(s)
    assert row(s)['state'] == 'Discrepancies'
    rec.accept(row(s), 'keep')
    assert row(s)['state'] == 'Accepted differences'
    mutate(s, "INSERT INTO tags VALUES(1,'unrelated')")
    mutate(s, 'INSERT INTO itemTags VALUES(1,1)')
    rec.refresh(s)
    assert row(s)['state'] == 'Accepted differences'
    mutate(s, 'UPDATE itemAttachments SET parentItemID=3')
    rec.refresh(s)
    assert row(s)['state'] == 'Discrepancies'
    assert row(s)['reopened']


def test_partial_read_preserves_last_successful_snapshot(world):
    s, _ = world
    rec.refresh(s)
    before = rec.saved(s)
    mutate(s, 'DROP TABLE creators')
    result = rec.refresh(s)
    assert not result['available']
    assert rec.saved(s) == before
    assert result['changes'] == []
    assert rec.rows(s, before, available=False)[0]['state'] == 'Recovery'


@pytest.mark.parametrize('sql', ["INSERT INTO deletedItems VALUES(2)", 'DELETE FROM items WHERE itemID=2', "UPDATE itemAttachments SET path='/absent/moved.pdf'"])
def test_removed_trashed_or_moved_attachment_never_recreates(world, sql):
    s, _ = world
    rec.refresh(s)
    if sql.startswith('DELETE'):
        mutate(s, 'DELETE FROM itemAttachments WHERE itemID=2')
    mutate(s, sql)
    rec.refresh(s)
    assert row(s)['state'] == 'Discrepancies'
    rec.refresh(s)
    assert row(s)['state'] == 'Discrepancies'
    assert not row(s)['can_create']


def test_imported_identical_copy_is_not_exact_file(world):
    s, pdf = world
    stored = s.zotero_storage / 'ATTACH01' / 'one.pdf'
    stored.parent.mkdir(parents=True)
    stored.write_bytes(pdf.read_bytes())
    mutate(s, "UPDATE itemAttachments SET path='storage:one.pdf',linkMode=0")
    rec.refresh(s)
    assert not row(s)['associations']
    assert row(s)['imported_copies']
    assert not row(s)['can_create']


def test_replacement_does_not_reuse_registration(world):
    s, pdf = world
    rec.refresh(s)
    pdf.write_bytes(b'%PDF-different')
    rec.refresh(s)
    assert row(s)['state'] == 'Discrepancies'


def test_uncertain_attachment_barrier_survives_reopen(world):
    s, pdf = world
    identity, _, _ = attempts.reserve(s.vault_root, review.identity(pdf)['sha256'], pdf_rel='biblio_articles/one.pdf',
                                    item_type='journalArticle',title='One',library='users/0',base='local',server_id='fixture-db')
    attempts.acknowledge_parent(identity, key='PARENT01')
    attempts.mark_attachment_dispatch(identity)
    assert attempts.lookup(s.vault_root, review.identity(pdf)['sha256'])[2]['attachment_dispatch'] == 'sent_or_unknown'
    with pytest.raises(RuntimeError):
        attempts.mark_attachment_dispatch(identity)


def test_review_confirmation_wins_and_rejection_removes_cache_candidate(world):
    s, pdf = world
    candidate = dict(title='Corrected',doi='10.1234/correct')
    proposal = dict(outcome='candidates',tier='title_only',candidates=[candidate])
    opened = review.open_review(s.vault_root,'biblio_articles/one.pdf',review.identity(pdf),proposal)
    record = review.update(s.vault_root,'biblio_articles/one.pdf',opened['token'],review.identity(pdf),action='confirm',candidate_id=review.digest(candidate))
    effective, _ = review.effective_resolution(s.vault_root,'biblio_articles/one.pdf',review.identity(pdf),{'candidates':[{'title':'Wrong cache'}]})
    assert effective['candidates'] == [candidate]
    review.update(s.vault_root,'biblio_articles/one.pdf',record['token'],review.identity(pdf),action='reject',candidate_id=review.digest(candidate))
    effective, _ = review.effective_resolution(s.vault_root,'biblio_articles/one.pdf',review.identity(pdf),proposal)
    assert effective['candidates'] == []


def test_mapping_failure_retains_prepared_intent(world):
    s, _ = world
    rec.refresh(s)
    with patch('audit.engine.bridge.add_match', side_effect=OSError('crash before JSON')):
        with pytest.raises(OSError):
            rec.use_association(s,row(s))
    with rec.connection() as con:
        assert json.loads(con.execute('SELECT data FROM intents').fetchone()[0])['status'] == 'prepared'
    rec.use_association(s,row(s))
    assert row(s)['state'] == 'Linked'


def test_database_switch_reopens_acceptance_and_never_adopts(world):
    s, _ = world
    s.mapping_file.write_text(json.dumps({'no_match':['biblio_articles/one.pdf']}))
    rec.refresh(s)
    rec.accept(row(s),'keep')
    mutate(s, "UPDATE settings SET value='another-database'")
    rec.refresh(s)
    assert row(s)['state'] == 'Discrepancies'
    assert row(s)['reopened']
    rec.refresh(s)
    assert row(s)['state'] == 'Discrepancies'


def test_trash_restore_and_changed_citation_key_keep_item_identity(world):
    s, _ = world
    rec.refresh(s)
    rec.use_association(s,row(s))
    mutate(s,'INSERT INTO deletedItems VALUES(2)')
    rec.refresh(s)
    assert row(s)['state'] == 'Discrepancies'
    mutate(s,'DELETE FROM deletedItems')
    rec.refresh(s)
    assert row(s)['state'] == 'Linked'
    mutate(s,"UPDATE itemDataValues SET value='NewKey' WHERE valueID=2")
    s.master_bib.write_text('@article{NewKey,title={One}}')
    rec.refresh(s)
    assert row(s)['associations'][0]['parent'] == 'PARENT01'
    assert row(s)['state'] == 'Discrepancies'
    rec.use_association(s,row(s))
    assert row(s)['state'] == 'Linked'
    assert not row(s)['can_create']


def test_duplicate_citation_keys_and_multiple_parents_are_not_chosen(world):
    s, _ = world
    mutate(s,'INSERT INTO itemData VALUES(3,2,2)')
    rec.refresh(s)
    assert row(s)['bibliography_keys'] == []
    assert not row(s)['can_create']


def test_confirmation_cannot_be_bypassed_in_create_json(world):
    s, pdf = world
    candidate = dict(title='One',doi='10.1234/one')
    opened=review.open_review(s.vault_root,'biblio_articles/one.pdf',review.identity(pdf),dict(candidates=[candidate]))
    confirmed=review.update(s.vault_root,'biblio_articles/one.pdf',opened['token'],review.identity(pdf),action='confirm',candidate_id=review.digest(candidate))
    with pytest.raises(review.ReviewConflict,match='Draft DOI differs'):
        review.validate_creation(s.vault_root,'biblio_articles/one.pdf',review.identity(pdf),confirmed['token'],{'DOI':'10.1234/other'})


def test_parent_ack_is_durable_before_log_or_readback_failure(world):
    from types import SimpleNamespace
    from tagging import writers, zotero_write
    s, pdf = world
    identity, _, _ = attempts.reserve(s.vault_root, review.identity(pdf)['sha256'],pdf_rel='biblio_articles/one.pdf',item_type='journalArticle',title='One',library='users/0',base='local',server_id='fixture-db')
    prepared={'commit':dict(transport=SimpleNamespace(base_url='local',is_web=False),api_key=None,lib_path='users/0',item_type='journalArticle',fields={'title':'One'},creators=[],server_id='fixture-db',
              acknowledge=lambda key:attempts.acknowledge_parent(identity,key=str(key),creation_data=key.creation_data))}
    with patch.object(zotero_write,'create_parent_item',return_value=zotero_write.CreatedItemKey('PARENT01',{'title':'One'},1)), patch('core.utils.log_vault_write',side_effect=RuntimeError('crash after acknowledgement')):
        with pytest.raises(RuntimeError):
            writers.zotero_item_create_post(prepared)
    assert attempts.lookup(s.vault_root,review.identity(pdf)['sha256'])[2]['parent_key'] == 'PARENT01'


def test_attachment_ack_survives_readback_failure_without_undo_baseline(world):
    from types import SimpleNamespace
    from tagging import writers, zotero_write
    s, pdf = world
    identity, _, _ = attempts.reserve(s.vault_root,review.identity(pdf)['sha256'],pdf_rel='biblio_articles/one.pdf',item_type='journalArticle',title='One',library='users/0',base='local',server_id='fixture-db')
    attempts.acknowledge_parent(identity,key='PARENT01')
    prepared={'commit':dict(transport=SimpleNamespace(base_url='local'),api_key=None,lib_path='users/0',parent_key='PARENT01',path=str(pdf),title='one.pdf',server_id='fixture-db',
              before_dispatch=lambda:attempts.mark_attachment_dispatch(identity),
              acknowledge=lambda key:attempts.acknowledge_attachment(identity,key=str(key),creation_data=key.creation_data))}
    with patch.object(zotero_write,'create_linked_file_attachment',return_value=zotero_write.CreatedItemKey('ATTACH01',{'parentItem':'PARENT01'},1)), patch.object(zotero_write,'get_item',side_effect=zotero_write.ZoteroUnavailable('lost reply')), patch('core.utils.log_vault_write'):
        result,key,version=writers.zotero_attachment_create_post(prepared)
    assert key == 'ATTACH01' and version is None and result['verification_pending']
    assert attempts.lookup(s.vault_root,review.identity(pdf)['sha256'])[1] == attempts.STAGE_COMPLETE


def test_move_does_not_bypass_a_saved_exclusion(world):
    s,pdf=world
    s.mapping_file.write_text(json.dumps({'no_match':['biblio_articles/one.pdf']}))
    rec.refresh(s)
    moved=pdf.with_name('renamed.pdf')
    pdf.rename(moved)
    mutate(s,'UPDATE itemAttachments SET path=?',(str(moved),))
    rec.refresh(s)
    current=next(r for r in rec.rows(s,rec.saved(s)) if r['pdf']['name']=='renamed.pdf')
    assert current['state']=='Discrepancies'
    assert current['moved_from']['biblio_articles/one.pdf']['no_match']
    assert not current['can_create']


def test_multiple_exact_parents_remain_ambiguous(world):
    s,pdf=world
    mutate(s,"INSERT INTO items VALUES(4,'ATTACH02',1,2)")
    mutate(s,'INSERT INTO itemAttachments VALUES(4,3,?,2)',(str(pdf),))
    rec.refresh(s)
    assert row(s)['state']=='Discrepancies'
    assert len(row(s)['associations'])==2
    assert not row(s)['can_create']


@pytest.mark.parametrize('found', [True,False])
def test_recovery_endpoint_never_reposts_after_unknown_dispatch(world,found):
    from types import SimpleNamespace
    from flask import Flask
    from api.routes.zotcreate import zotcreate_bp
    from tagging import zotero_write
    from tagging.local_api import Version
    s,pdf=world
    physical=review.identity(pdf)
    baseline=dict(key='PARENT01',itemType='journalArticle',title='One',DOI='')
    identity,_,_=attempts.reserve(s.vault_root,physical['sha256'],pdf_rel='biblio_articles/one.pdf',item_type='journalArticle',title='One',library='users/0',base='http://localhost:23119/api',server_id='fixture-db')
    attempts.acknowledge_parent(identity,key='PARENT01',creation_data=baseline)
    attempts.mark_attachment_dispatch(identity)
    app=Flask(__name__)
    app.register_blueprint(zotcreate_bp)
    c=dict(transport=SimpleNamespace(base_url='http://localhost:23119/api',is_web=False),lib_path='users/0',api_key=None)
    children=[dict(key='ATTACH01',itemType='attachment',parentItem='PARENT01',linkMode='linked_file',path=str(pdf))] if found else []
    with patch('api.routes.zotcreate.audit_manager') as manager, patch('api.routes.zotcreate.load_config',return_value={}), patch('tagging.writers.zotero_item_create_prepare',return_value={'commit':c}), patch.object(zotero_write,'get_item',return_value=(baseline,Version(1,'fixture-db'))), patch.object(zotero_write,'get_item_children',return_value=children), patch.object(zotero_write,'create_linked_file_attachment') as post:
        manager.is_scanning.return_value=False
        manager.get_inventory.return_value=(object(),s)
        for _ in range(2):
            response=app.test_client().post('/api/zotcreate/recover',json=dict(pdf='biblio_articles/one.pdf',physical=physical,confirm=True))
            assert response.status_code == (200 if found else 409)
        post.assert_not_called()
    _,stage,retained=attempts.lookup(s.vault_root,physical['sha256'])
    assert stage == (attempts.STAGE_COMPLETE if found else attempts.STAGE_PARENT_DONE)
    assert retained['attachment_dispatch']=='sent_or_unknown'
    if found:
        assert retained['attachment']['adopted']
        assert retained['attachment']['creation_data'] is None


def test_stale_bibliography_row_cannot_map(world):
    s, _ = world
    rec.refresh(s)
    inspected = row(s)
    s.master_bib.write_text('@article{Different,title={Changed}}')
    before = s.mapping_file.read_bytes()
    with pytest.raises(ValueError, match='Bibliography'):
        rec.use_association(s, inspected)
    assert s.mapping_file.read_bytes() == before


def test_duplicate_bib_key_in_doi_fallback_is_ambiguous(world):
    s, _ = world
    mutate(s, 'DELETE FROM itemData WHERE fieldID=2')
    mutate(s, "INSERT INTO fields VALUES(4,'DOI')")
    mutate(s, "INSERT INTO itemDataValues VALUES(4,'10.1234/one')")
    mutate(s, 'INSERT INTO itemData VALUES(1,4,4)')
    s.master_bib.write_text('@article{Dup,title={One},doi={10.1234/one}}\n@article{Dup,title={Two},doi={10.1234/two}}')
    rec.refresh(s)
    assert row(s)['state'] == 'Discrepancies'
    assert not row(s)['bibliography_keys']


def test_confirmation_change_reopens_accepted_difference(world):
    s, pdf = world
    s.mapping_file.write_text(json.dumps({'no_match':['biblio_articles/one.pdf']}))
    candidate = dict(title='One', doi='10.1234/one')
    opened = review.open_review(s.vault_root, 'biblio_articles/one.pdf', review.identity(pdf),
                                dict(candidates=[candidate]))
    rec.refresh(s)
    rec.accept(row(s), 'keep')
    review.update(s.vault_root, 'biblio_articles/one.pdf', opened['token'], review.identity(pdf),
                  action='confirm', candidate_id=review.digest(candidate))
    assert row(s)['state'] == 'Discrepancies'


def test_fresh_dispatch_observation_retains_removed_attachment(world):
    s, _ = world
    rec.refresh(s)
    mutate(s, 'DELETE FROM itemAttachments WHERE itemID=2')
    mutate(s, 'DELETE FROM items WHERE itemID=2')
    observed = rec.retain_history(rec.saved(s), rec.observe(s))
    current = row(s, observed)
    assert current['associations']
    assert not current['can_create']


def _mapping_process(mapping, vault, pdf, key, started, done):
    from pathlib import Path
    from audit.engine import bridge
    started.set()
    bridge.add_match(Path(mapping), Path(pdf), key, Path(vault))
    done.set()


def test_mapping_guard_serializes_distinct_processes(world):
    import multiprocessing
    from audit.engine import bridge
    s, pdf = world
    context = multiprocessing.get_context('spawn')
    started, done = context.Event(), context.Event()
    process = context.Process(target=_mapping_process, args=(str(s.mapping_file), str(s.vault_root),
                               str(pdf), 'Child', started, done))
    with bridge.mapping_guard(s.mapping_file):
        process.start()
        assert started.wait(10)
        assert not done.wait(0.2)
        # A concurrent writer must reload this update after acquiring the lease.
        bridge.save_mapping(s.mapping_file, {'Other':['biblio_articles/other.pdf']}, set())
    process.join(10)
    assert process.exitcode == 0
    assert done.is_set()
    assert set(bridge._load_mapping_all(s.mapping_file).matches) == {'Other','Child'}


def legacy_plan(settings, pdf):
    from accession import migration
    identity, _, _ = attempts.reserve(settings.vault_root, review.identity(pdf)['sha256'],
        pdf_rel='biblio_articles/one.pdf', item_type='journalArticle', title='One', library='users/0', base='legacy')
    attempts.acknowledge_parent(identity, key='PARENT01')
    attempts.acknowledge_attachment(identity, key='ATTACH01')
    return migration.proposal(settings, rec.rows(settings, rec.observe(settings)))


def test_migration_idempotence_preserves_attempts_and_reviews(world):
    from accession import migration
    s, pdf = world
    plan = legacy_plan(s, pdf)
    before = attempts.store_path().read_bytes()
    approved = [plan['records'][0]['id']]
    assert migration.apply(s, plan, approved_records=approved, confirm=True)[0]['status'] == 'mapping_applied'
    assert migration.apply(s, plan, approved_records=approved, confirm=True)[0]['status'] == 'completion_verified'
    assert attempts.store_path().read_bytes() == before
    assert not review.store_path().exists()


@pytest.mark.parametrize('change', ['exclusion', 'file', 'bibliography', 'expired', 'unapproved'])
def test_migration_refuses_stale_or_unapproved(world, change):
    from accession import migration
    s, pdf = world
    plan = legacy_plan(s, pdf)
    approved = [plan['records'][0]['id']]
    if change == 'exclusion':
        s.mapping_file.write_text(json.dumps({'no_match':['biblio_articles/one.pdf']}))
    elif change == 'file':
        pdf.write_bytes(b'%PDF-changed')
    elif change == 'bibliography':
        s.master_bib.write_text('')
    elif change == 'expired':
        plan['generated_at'] -= 901
        plan['id'] = review.digest({k:v for k,v in plan.items() if k != 'id'})
    elif change == 'unapproved':
        approved = []
    before = s.mapping_file.read_bytes()
    with pytest.raises(ValueError):
        migration.apply(s, plan, approved_records=approved, confirm=True)
    assert s.mapping_file.read_bytes() == before


def test_migration_recovers_json_commit_before_sqlite_completion(world):
    from accession import migration
    from audit.engine import bridge
    s, pdf = world
    plan = legacy_plan(s, pdf)
    original = bridge.add_match
    def committed_then_crash(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('simulated process loss after JSON commit')
    with patch.object(bridge, 'add_match', side_effect=committed_then_crash):
        with pytest.raises(OSError):
            migration.apply(s, plan, approved_records=[plan['records'][0]['id']], confirm=True)
    assert rec.mapping_intents(s)[0]['status'] == 'prepared'
    assert migration.apply(s, plan, approved_records=[plan['records'][0]['id']], confirm=True)[0]['status'] == 'completion_verified'
    assert rec.mapping_intents(s)[0]['status'] == 'complete'


def test_decision_returns_current_workspace_category(world):
    from flask import Flask
    from api.routes import pending
    s, pdf = world
    mutate(s, 'DELETE FROM itemAttachments')
    mutate(s, 'DELETE FROM items')
    rec.refresh(s)
    candidate = dict(title='One',doi='10.1234/one')
    opened = review.open_review(s.vault_root,'biblio_articles/one.pdf',review.identity(pdf),dict(candidates=[candidate]))
    app = Flask(__name__)
    with app.test_request_context(json=dict(pdf='biblio_articles/one.pdf',token=opened['token'],
        candidate_id=review.digest(candidate), action='confirm',confirm=True)):
        with patch.object(pending, 'context', return_value=(None,s)):
            result = pending.decision().get_json()
    assert result['workspace_row']['state'] == 'Ready to register'


def repair_world(settings, pdf):
    from tagging.local_api import Version
    settings.mapping_file.write_text(json.dumps({'matches':{'KeyOne':['biblio_articles/one.pdf']}}))
    mutate(settings, 'UPDATE itemAttachments SET parentItemID=3')
    rec.refresh(settings)
    data = {'ATTACH01':dict(key='ATTACH01',itemType='attachment',parentItem='PARENT02',linkMode='linked_file',path=str(pdf)),
            'PARENT01':dict(key='PARENT01',itemType='journalArticle',citationKey='KeyOne',title='One'),
            'PARENT02':dict(key='PARENT02',itemType='journalArticle',title='Other')}
    return lambda base, lib, key, **kwargs:(data[key], Version(9,'fixture-db'))


def test_reparent_preview_requires_separate_confirmation_and_stale_refusal(world):
    from accession import reparent
    s, pdf = world
    get = repair_world(s,pdf)
    with patch('tagging.zotero_write.get_item', side_effect=get), patch('tagging.zotero_write._urlopen') as post:
        proposal = reparent.preview(s, {'tagging_zotero_transport':'local'}, 'biblio_articles/one.pdf')
        assert proposal['command']['desired_parent'] == 'PARENT01'
        post.assert_not_called()
        with pytest.raises(ValueError, match='confirmation'):
            reparent.apply(s, {'tagging_zotero_transport':'local'}, 'biblio_articles/one.pdf', proposal['token'])
        with pytest.raises(ValueError, match='evidence changed'):
            reparent.apply(s, {'tagging_zotero_transport':'local'}, 'biblio_articles/one.pdf', 'stale', confirm=True)
        post.assert_not_called()


def test_reparent_acknowledges_before_logging_and_only_patches_parent(world):
    from accession import reparent
    from types import SimpleNamespace
    s, pdf = world
    get = repair_world(s,pdf)
    from contextlib import contextmanager
    sent = []
    @contextmanager
    def respond(request, timeout):
        assert reparent.operations(s)[0]['status'] == 'sent_or_unknown'
        assert request.get_header('Zotero-server-id') == 'fixture-db'
        sent.append(json.loads(request.data))
        yield SimpleNamespace(status=204)
    with patch('tagging.zotero_write.get_item', side_effect=get), patch('tagging.zotero_write._urlopen',side_effect=respond):
        proposal = reparent.preview(s, {'tagging_zotero_transport':'local'}, 'biblio_articles/one.pdf')
        with patch('core.utils.log_vault_write',side_effect=OSError('log failed')):
            with pytest.raises(OSError):
                reparent.apply(s, {'tagging_zotero_transport':'local'}, 'biblio_articles/one.pdf', proposal['token'], confirm=True)
    assert sent == [{'parentItem':'PARENT01'}]
    assert reparent.operations(s)[0]['status'] == 'acknowledged'


def test_reparent_lost_response_blocks_repeated_patch(world):
    import urllib.error
    from accession import reparent
    from tagging import zotero_write
    s, pdf = world
    get = repair_world(s,pdf)
    with patch('tagging.zotero_write.get_item', side_effect=get), patch('tagging.zotero_write._urlopen', side_effect=urllib.error.URLError('lost')) as sent:
        proposal = reparent.preview(s, {'tagging_zotero_transport':'local'}, 'biblio_articles/one.pdf')
        with pytest.raises(zotero_write.ZoteroUnavailable):
            reparent.apply(s, {'tagging_zotero_transport':'local'}, 'biblio_articles/one.pdf', proposal['token'], confirm=True)
        with pytest.raises(ValueError, match='uncertain'):
            reparent.apply(s, {'tagging_zotero_transport':'local'}, 'biblio_articles/one.pdf', proposal['token'], confirm=True)
        assert sent.call_count == 1


@pytest.mark.parametrize('change', ['review', 'relationship'])
def test_migration_rechecks_evidence_inside_mapping_writer(world, change):
    from accession import migration
    from audit.engine import bridge
    s, pdf = world
    plan = legacy_plan(s, pdf)
    original = bridge.add_match
    before = s.mapping_file.read_bytes()
    def changed_before_commit(*args, **kwargs):
        if change == 'review':
            review.open_review(s.vault_root, 'biblio_articles/one.pdf', review.identity(pdf),
                               dict(candidates=[dict(title='Different', doi='10.1234/different')]))
        else:
            mutate(s, 'UPDATE itemAttachments SET parentItemID=3')
        return original(*args, **kwargs)
    with patch.object(bridge, 'add_match', side_effect=changed_before_commit):
        with pytest.raises(ValueError, match='changed'):
            migration.apply(s, plan, approved_records=[plan['records'][0]['id']], confirm=True)
    assert s.mapping_file.read_bytes() == before


def test_uncertain_reparent_inspection_and_guarded_completion(world):
    from accession import reparent
    from tagging import zotero_write as zw
    import urllib.error
    s, pdf = world
    get = repair_world(s, pdf)
    cfg = {'tagging_zotero_transport': 'local'}
    with patch.object(zw, 'get_item', side_effect=get), patch.object(zw, '_urlopen', side_effect=urllib.error.URLError('lost')):
        proposal = reparent.preview(s, cfg, 'biblio_articles/one.pdf')
        with pytest.raises(zw.ZoteroUnavailable):
            reparent.apply(s, cfg, 'biblio_articles/one.pdf', proposal['token'], confirm=True)
    original = reparent.operations(s)[0]
    with patch.object(zw, 'get_item', side_effect=get), patch.object(zw, '_urlopen') as sent:
        assert not reparent.inspect(s, cfg, original['id'])['eligible']
        mutate(s, 'UPDATE itemAttachments SET parentItemID=1')
        get('', '', 'ATTACH01')[0]['parentItem'] = 'PARENT01'
        inspected = reparent.inspect(s, cfg, original['id'])
        assert inspected['eligible']
        with pytest.raises(ValueError, match='confirmation'):
            reparent.complete(s, cfg, original['id'], inspected['token'])
        with pytest.raises(ValueError, match='changed'):
            reparent.complete(s, cfg, original['id'], 'stale', confirm=True)
        result = reparent.complete(s, cfg, original['id'], inspected['token'], confirm=True)
        assert result['operation']['status'] == 'completion_observed'
        assert result['operation']['original_outcome'] == 'sent_or_unknown'
        assert result['operation']['completion']['evidence']['operation'] == original
        assert result['undo'] == 'manual'
        assert not reparent.inspect(s, cfg, original['id'])['eligible']
        sent.assert_not_called()


def test_reparent_inspection_refuses_original_database_switch(world):
    from accession import reparent
    s, pdf = world
    get = repair_world(s, pdf)
    cfg = {'tagging_zotero_transport': 'local'}
    with patch('tagging.zotero_write.get_item', side_effect=get):
        proposal = reparent.preview(s, cfg, 'biblio_articles/one.pdf')
    entry = dict(**proposal['command'], id=proposal['token'], status='sent_or_unknown', at=1)
    with rec.connection(True) as con:
        con.execute('CREATE TABLE reparents(id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        con.execute('INSERT INTO reparents VALUES (?,?)', (entry['id'], json.dumps(entry)))
    mutate(s, "UPDATE settings SET value='different-db'")
    with patch('tagging.zotero_write.get_item') as read:
        with pytest.raises(ValueError, match='database'):
            reparent.inspect(s, cfg, entry['id'])
        read.assert_not_called()


def _migration_crash_process(settings, plan, app_dir):
    import os
    import core.constants
    from accession import migration
    from audit.engine import bridge
    core.constants.BASE_DIR = app_dir
    original = bridge.add_match
    def commit_then_exit(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(73)
    bridge.add_match = commit_then_exit
    migration.apply(settings, plan, approved_records=[r['id'] for r in plan['records']], confirm=True)


def test_migration_process_exit_partial_batch_and_resume(world):
    import multiprocessing
    from accession import migration
    from core.constants import BASE_DIR
    s, pdf = world
    legacy_plan(s, pdf)
    other = pdf.with_name('two.pdf')
    other.write_bytes(b'%PDF-other-fixture')
    rel = 'biblio_articles/two.pdf'
    mapping = json.loads(s.mapping_file.read_text())
    mapping['pending'].append(rel)
    s.mapping_file.write_text(json.dumps(mapping))
    mutate(s, "INSERT INTO items VALUES(4,'ATTACH02',1,2)")
    mutate(s, 'INSERT INTO itemAttachments VALUES(4,1,?,2)', (str(other),))
    identity, _, _ = attempts.reserve(s.vault_root, review.identity(other)['sha256'],
        pdf_rel=rel, item_type='journalArticle', title='One', library='users/0', base='legacy')
    attempts.acknowledge_parent(identity, key='PARENT01')
    attempts.acknowledge_attachment(identity, key='ATTACH02')
    plan = migration.proposal(s, rec.rows(s, rec.observe(s)))
    assert len(plan['records']) == 2 and all(r['eligible'] for r in plan['records'])
    process = multiprocessing.get_context('spawn').Process(target=_migration_crash_process,
        args=(s, plan, str(BASE_DIR)))
    process.start()
    process.join(15)
    assert process.exitcode == 73
    assert json.loads(s.mapping_file.read_text())['pending'] == [rel]
    result = migration.apply(s, plan, approved_records=[r['id'] for r in plan['records']], confirm=True)
    assert [r['status'] for r in result] == ['completion_verified', 'mapping_applied']
    assert all(r['status'] == 'completion_verified' for r in migration.apply(
        s, plan, approved_records=[r['id'] for r in plan['records']], confirm=True))


def test_shared_projection_search_freshness_and_decision_changes(world):
    from accession import projection
    from audit.search import search
    s, _ = world
    rec.refresh(s)
    before = rec.store_path().read_bytes()
    current = projection.read(s)
    assert current['counts'] == {'Needs review': 1}
    assert current['freshness']['fresh']
    assert search(projection.search_corpus([], current), 'one')['groups'][0]['count'] == 1
    assert rec.store_path().read_bytes() == before
    rec.accept(current['rows'][0], 'later')
    current = projection.read(s)
    assert current['rows'][0]['accepted']['action'] == 'later'
    with patch('accession.projection.time.time', return_value=rec.saved(s)['checked_at'] + 61):
        assert not projection.read(s)['freshness']['fresh']


def test_relative_attachment_requires_runtime_binding_and_containment(world):
    import time
    from accession import attachment_base
    s, pdf = world
    binding = dict(database='fixture-db', database_path=str(s.zotero_sqlite),
                   profile=str(s.vault_root.parent/'profile'), base_path=str(pdf.parent), observed_at=time.time())
    def resolve(raw='attachments:one.pdf', **changes):
        return attachment_base.resolve(raw, {**binding, **changes}, database='fixture-db', database_path=s.zotero_sqlite)
    assert resolve() == pdf.resolve()
    assert resolve(database='another-db') is None
    assert resolve(database_path=str(s.vault_root/'different.sqlite')) is None
    assert resolve(observed_at=time.time()-61) is None
    assert resolve('attachments:../_master.bib') is None
    outside = s.vault_root/'outside.pdf'
    outside.write_bytes(b'outside')
    (pdf.parent/'escape.pdf').symlink_to(outside)
    assert resolve('attachments:escape.pdf') is None
    mutate(s, "UPDATE itemAttachments SET path='attachments:one.pdf'")
    snapshot = rec.observe(s)
    assert not rec.rows(s,snapshot)[0]['associations']
    snapshot['attachment_base'] = binding  # trusted adapter fixture, not production configuration
    assert rec.rows(s,snapshot)[0]['associations'][0]['attachment'] == 'ATTACH01'


def test_migration_expiry_during_observation_cannot_write(world):
    from accession import migration
    s, pdf = world
    plan = legacy_plan(s, pdf)
    before = s.mapping_file.read_bytes()
    original = rec.use_association
    def expires(*args, **kwargs):
        with patch('accession.migration.time.time', return_value=plan['generated_at'] + 901):
            return original(*args, **kwargs)
    with patch.object(rec, 'use_association', side_effect=expires):
        with pytest.raises(ValueError, match='expired'):
            migration.apply(s, plan, approved_records=[plan['records'][0]['id']], confirm=True)
    assert s.mapping_file.read_bytes() == before


def test_migration_rejects_changed_parent_creators(world):
    from accession import migration
    s, pdf = world
    plan = legacy_plan(s, pdf)
    mutate(s, "UPDATE creators SET lastName='Different author'")
    before = s.mapping_file.read_bytes()
    with pytest.raises(ValueError, match='associations'):
        migration.apply(s, plan, approved_records=[plan['records'][0]['id']], confirm=True)
    assert s.mapping_file.read_bytes() == before



def test_parent_candidates_do_not_rescan_every_item_per_pdf(world):
    """2026-09-17 review, C2: the parent lookup iterated the whole observation
    for every PDF (O(pdfs × items)). The index is built once; the number of
    full passes over the items must not grow with the number of PDFs."""
    settings, pdf = world
    assert rec.refresh(settings)['available']

    class CountingItems(dict):
        passes = 0

        def values(self):
            CountingItems.passes += 1
            return super().values()

    def passes_for(n_pdfs):
        for i in range(n_pdfs):
            extra = pdf.with_name(f'extra_{i}.pdf')
            if not extra.exists():
                extra.write_bytes(b'%PDF-extra-' + str(i).encode())
        snapshot = dict(rec.saved(settings))
        snapshot['items'] = CountingItems(snapshot['items'])
        CountingItems.passes = 0
        rec.rows(settings, snapshot)
        return CountingItems.passes

    assert passes_for(2) == passes_for(20)


def _unattached(s, mapping):
    """The PDF has no Zotero attachment; only our own curation remains."""
    mutate(s, 'DELETE FROM itemAttachments WHERE itemID=2')
    s.mapping_file.write_text(json.dumps(mapping))
    return row(s, rec.observe(s))


def test_an_exclusion_alone_does_not_forbid_creating_a_record(world):
    """A detach exclusion says "this file is not THAT record", not "this file
    must not become A record" (2026-09-18, option A). It stays in the mapping
    and keeps the ladder from re-linking the wrong record, but it no longer
    blocks registration or files the PDF as Excluded."""
    s, _ = world
    r = _unattached(s, {'pending': ['biblio_articles/one.pdf'],
                        'exclude': {'biblio_articles/one.pdf': ['WrongKey2020']}})
    assert r['mapping']['exclude'] == ['WrongKey2020']
    assert r['state'] != 'Excluded'
    assert r['can_create']


def test_not_for_zotero_still_forbids_creating_a_record(world):
    s, _ = world
    r = _unattached(s, {'no_match': ['biblio_articles/one.pdf']})
    assert r['state'] == 'Excluded'
    assert not r['can_create']


def test_reconciliation_discovers_uppercase_pdf_without_mapping(world):
    settings, pdf = world
    upper = pdf.with_name('UNLINKED.PDF')
    upper.write_bytes(b'%PDF-uppercase-independent-file')
    rec.refresh(settings)
    rows = rec.rows(settings, rec.saved(settings))
    found = next(r for r in rows if r['pdf']['name'] == 'UNLINKED.PDF')
    assert found['physical'] is not None
    assert found['state'] == 'Needs identification'

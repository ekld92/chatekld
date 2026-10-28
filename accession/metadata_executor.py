"""Metadata executor for reviewed bibliography campaigns (phase 5, 2026-09-16).

Writes ONLY fields the reviewer explicitly approved, one item at a time,
through the shared Zotero transport (``tagging.writers.zotero_metadata_*``)
under the version guard, journalled before the PATCH in the tagging journal
(op kind ``zotero_metadata_write``) and reversible from Tagging → Restore.

The choreography is ``retire-tag``'s: reads lock-free → ONE op-lock hold to
journal → PATCHes lock-free → a short second hold to record outcomes. A
readback that differs from the approved value, or that shows an unselected
field moving (Zotero's own normalisation — the ``Extra: Citation Key`` →
``citationKey`` promotion observed on 10.0.1 is the known case), is never a
success: it is recorded on the op with before/after so the revert can put it
back, and reported as such.

``application_available()`` is ``False`` and deliberately not a flag: the
production route answers 501 until the executor is validated separately, as
the user decided. Fixture tests inject their own admission.
"""
from __future__ import annotations

import time
import uuid

from notesync import campaigns

APPLIED_STATUSES = frozenset({'applied', 'unselected_field_changed', 'readback_differs', 'outcome_unknown'})


class ApplicationClosed(RuntimeError):
    """``apply`` was reached while production application is closed."""


def application_available():
    """No production metadata write until the executor's separate validation.
    Not configurable; fixture tests patch this name. ``apply`` consults it
    itself (2026-09-17 review, E3), so the 501 is enforced at the layer that
    writes, not only by the route."""
    return False


def selected_writes(campaign, decisions):
    """Resolve *decisions* ``[{record_id, field, evidence}]`` against the
    campaign's recorded review: each must name an APPROVED field whose evidence
    still matches. Returns ``{record_id: {"fields": {...}, "current": {...},
    "zotero_key": key, "decisions": [...]}}`` or raises ``ValueError``."""
    if campaign.get('kind') != 'bibliography':
        raise ValueError('This is not a bibliography campaign.')
    if not isinstance(decisions, list) or not decisions or len(decisions) > 100:
        raise ValueError('Select 1–100 exact approved field decisions.')
    generation = campaign['outcomes'].get('generation', {})
    proposals = {p['id']: p for p in generation.get('proposals', [])}
    reviewed = {(d['record_id'], d['field']): d
                for d in campaign['outcomes'].get('review', {}).get('decisions', [])}
    records = {r['id']: r for r in campaign['proposals']['input']['corpus']}
    # Every earlier batch, not just the latest: campaigns.record stashes the
    # previous application outcome under `dispatch` (2026-09-17 review, F9).
    dispatched = set()
    application = campaign['outcomes'].get('application')
    while isinstance(application, dict):
        for record_id, result in (application.get('results') or {}).items():
            for field_name, prior in ((result or {}).get('fields') or {}).items():
                if isinstance(prior, dict) and prior.get('status') in APPLIED_STATUSES:
                    dispatched.add((record_id, field_name))
        application = application.get('dispatch')
    out: dict = {}
    seen = set()
    for decision in decisions:
        if not isinstance(decision, dict):
            raise ValueError('Each decision must be an object.')
        key = (decision.get('record_id'), decision.get('field'))
        if not all(isinstance(k, str) and k for k in key) or key in seen:
            raise ValueError('Select each exact record/field at most once.')
        seen.add(key)
        review = reviewed.get(key)
        if not review or review.get('approve') is not True or review.get('support_assessment') != 'supported':
            raise ValueError(f'{key[0]} / {key[1]} was not approved in the recorded review.')
        if decision.get('evidence') != review.get('evidence'):
            raise ValueError(f'{key[0]} / {key[1]}: the decision does not match the reviewed field evidence.')
        proposal = proposals.get(key[0])
        field = next((f for f in (proposal or {}).get('fields', []) if f['field'] == key[1]), None)
        if field is None or field.get('evidence') != review.get('evidence'):
            raise ValueError(f'{key[0]} / {key[1]}: the proposal no longer matches the review.')
        if key in dispatched:
            raise ValueError(f'{key[0]} / {key[1]} was already dispatched; inspect the journal, no replay.')
        record = records.get(key[0]) or {}
        item_key = record.get('zotero_key')
        if not isinstance(item_key, str) or not item_key.strip():
            raise ValueError(f'{key[0]}: the corpus record names no zotero_key; the executor cannot address it.')
        value = review['reviewed_value'] if 'reviewed_value' in review else field.get('proposed')
        entry = out.setdefault(key[0], dict(fields={}, current={}, zotero_key=item_key.strip(), decisions=[], server_id=record.get('server_id'), source_transport=record.get('source_transport')))
        entry['fields'][key[1]] = value
        entry['current'][key[1]] = field.get('current')
        entry['decisions'].append(decision)
    return out


def apply(identifier, scope, cfg, decisions, *, vault_root, confirm=False):
    """Write the selected approved fields; returns the recorded application outcome."""
    from rag.vault import obsidian_manager
    from tagging import journal as tjournal
    from tagging import writers
    if confirm is not True:
        raise ValueError('Confirm the exact approved field writes.')
    if not application_available():
        raise ApplicationClosed('Metadata application is closed pending its separate validation.')
    campaign = campaigns.read(identifier)
    if campaign['scope'] != scope:
        raise ValueError('Campaign belongs to a different vault.')
    selected = selected_writes(campaign, decisions)
    batch_id = uuid.uuid4().hex
    results: dict = {}
    prepared: dict = {}
    # Phase 1 — reads, lock-free; every early exit is a per-record result.
    for record_id, entry in selected.items():
        from types import SimpleNamespace
        row = SimpleNamespace(server_id=entry['server_id'], source_transport=entry['source_transport'])
        p = writers.zotero_metadata_prepare(cfg, entry['zotero_key'], entry['fields'], entry['current'], row=row)
        if 'early' in p:
            results[record_id] = dict(ok=p['early'].get('ok', False), status=p['early'].get('status'),
                                      message=p['early'].get('message'), detail={k: v for k, v in p['early'].items()
                                      if k not in ('ok', 'status', 'message')},
                                      fields={f: dict(status=p['early'].get('status')) for f in entry['fields']})
            continue
        prepared[record_id] = p
    ops: dict = {}
    if prepared:
        # Phase 2 — journal under one brief hold.
        epoch = obsidian_manager.try_acquire_lock(ttl=600)
        if not epoch:
            for record_id in prepared:
                results[record_id] = dict(ok=False, status='busy', message='Indexing is in progress; try again shortly.',
                                          fields={f: dict(status='busy') for f in selected[record_id]['fields']})
            prepared = {}
        else:
            try:
                manifest = tjournal.load_for_ops(vault_root, cfg)
                for record_id, p in prepared.items():
                    ops[record_id] = writers.zotero_metadata_journal(prepared=p, manifest=manifest, batch_id=batch_id,
                                                                     citation_key=record_id, campaign=identifier)
                tjournal.save(vault_root, cfg, manifest)
            except (tjournal.JournalUnreadableError, tjournal.ScopeError) as exc:
                for record_id in prepared:
                    results[record_id] = dict(ok=False, status='error', message=str(exc),
                                              fields={f: dict(status='error') for f in selected[record_id]['fields']})
                prepared = {}
            finally:
                obsidian_manager.release_lock(epoch)
    # Phase 3 — PATCHes, lock-free.
    outcomes: dict = {}
    for record_id, p in prepared.items():
        outcomes[record_id] = writers.zotero_metadata_patch(p)
    # Phase 4 — record on a RELOADED manifest.
    recorded = set()
    if outcomes:
        epoch2 = obsidian_manager.try_acquire_lock(ttl=600)
        if epoch2:
            try:
                manifest = tjournal.load_for_ops(vault_root, cfg, [v['op_id'] for v in ops.values()])
                for record_id, (res, _status, version) in outcomes.items():
                    op = tjournal.find_op(manifest, ops[record_id]['op_id'])
                    if op is None:
                        continue
                    if res.get('status') in ('conflict', 'unsupported'):
                        op['state'] = 'failed'
                    elif res.get('status') == 'outcome_unknown':
                        op['state'] = 'outcome_unknown'
                    else:
                        op['state'] = 'applied'
                        if version is not None:
                            op['version_after'] = int(version)
                        if res.get('unexpected_changes'):
                            op['unexpected_changes'] = res['unexpected_changes']
                        if res.get('status') == 'readback_differs':
                            op['readback_fields'] = res.get('fields')
                    recorded.add(record_id)
                tjournal.save(vault_root, cfg, manifest)
            except (tjournal.JournalUnreadableError, tjournal.ScopeError):
                pass
            finally:
                obsidian_manager.release_lock(epoch2)
    for record_id, (res, _status, version) in outcomes.items():
        out = dict(res, op_id=ops[record_id]['op_id'])
        if record_id not in recorded:
            out.update(ok=False, status='outcome_unknown',
                       message='Destination result could not be journalled; inspect before any retry.')
        if version is not None:
            out['version'] = int(version)
        out['fields'] = {f: dict(status=out['status']) for f in selected[record_id]['fields']}
        results[record_id] = out
    record = dict(status='applied' if all(r.get('ok') for r in results.values()) else 'partial',
                  ok=all(r.get('ok') for r in results.values()), batch_id=batch_id, results=results,
                  at=time.time(), applicable=False,
                  note='Only explicitly approved fields were sent; every write is journalled under this batch and reversible from Tagging → Restore.')
    campaigns.record(identifier, 'application', record)
    return record

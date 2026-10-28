"""Explicit adoption of current legacy associations, never historical rebinding.

No automatic selection, external writes, exclusion overrides, review changes,
barrier release or undo authority. Callers must obtain per-record approval of
bibliographic identity as well as authorization to change application mappings.
"""
from __future__ import annotations

import json
import time

from accession import reconciliation as rec, review

MAX_AGE_SECONDS = 900


def proposal(settings, rows):
    records = []
    for row in rows:
        retained = row['attempt']['retained']
        if not retained:
            continue
        live = [a for a in row['associations'] if not a['deleted']]
        exact = (len(live) == 1 and live[0]['parent'] == retained.get('parent_key')
                 and live[0]['attachment'] == retained.get('attachment_key'))
        eligible = (exact and row['state'] == 'Needs review' and len(row['bibliography_keys']) == 1
                    and not row['mapping']['keys'] and not row['mapping']['exclude']
                    and not row['mapping']['no_match'] and not row['accepted'])
        entry = dict(pdf=row['pdf']['rel'], eligible=bool(eligible),
                     action='adopt_current_mapping' if eligible else 'preserve_and_review',
                     key=row['bibliography_keys'][0] if eligible else None,
                     requires_identity_approval=True, inspected=row,
                     reason='Current association only; historical database and bibliographic identification are not established.'
                            if eligible else 'Uncertain outcome, saved decision, or ambiguous/conflicting evidence is preserved.')
        entry['id'] = review.digest(entry)
        records.append(entry)
    plan = dict(schema=1, scope=rec.scope(settings), generated_at=time.time(), records=records)
    plan['id'] = review.digest(plan)
    return plan


def apply(settings, plan, *, approved_records, confirm=False):
    """Apply only named semantic approvals, revalidating before each local write.

    Recover a crash after JSON by recognizing our retained intent and verifying
    the exact desired mapping plus unchanged file/bib/association/review/attempt.
    A partial batch stays inspectable and is resumable; it is not atomic.
    """
    if confirm is not True or not approved_records:
        raise ValueError('Explicit real-apply and per-record identity approval are required.')
    if (plan.get('schema') != 1 or plan.get('scope') != rec.scope(settings)
            or plan.get('id') != review.digest({k:v for k,v in plan.items() if k != 'id'})):
        raise ValueError('Migration proposal identity or scope changed.')
    def validate_age():
        if not 0 <= time.time() - plan['generated_at'] <= MAX_AGE_SECONDS:
            raise ValueError('Migration evidence expired; generate and approve a fresh preview.')
    validate_age()
    selected = set(approved_records)
    records = [r for r in plan['records'] if r['id'] in selected]
    if len(records) != len(selected) or any(not r['eligible'] for r in records):
        raise ValueError('Selection contains an unknown or ineligible migration record.')
    results = []
    for entry in records:
        validate_age()
        observed = rec.retain_history(rec.saved(settings), rec.observe(settings, allow_live_fallback=False))
        row = rec.rows(settings, observed, only=entry['pdf'])[0]
        old = entry['inspected']
        for field in ('physical','associations','bibliography_keys','bibliography_identity','review','attempt','accepted'):
            if row[field] != old[field]:
                raise ValueError(f'Migration evidence changed: {entry["pdf"]}: {field}')
        validate_age()
        intent_id = review.digest([old['subject'], old['evidence'], entry['key']])
        own_intent = next((i for i in rec.mapping_intents(settings) if i['id'] == intent_id), None)
        desired = dict(keys=[entry['key']], pending=False, no_match=False, exclude=[])
        if row['mapping'] == desired and own_intent:
            rec.finish_mapping_intents(settings, [row])
            if not any(i['id'] == intent_id and i['status'] == 'complete' for i in rec.mapping_intents(settings)):
                raise ValueError('Retained mapping intent lacks current completion evidence; inspect it manually.')
            status = 'completion_verified'
        elif row['evidence'] == old['evidence'] and row['mapping'] == old['mapping']:
            rec.use_association(settings, row, validate_evidence=validate_age)
            status = 'mapping_applied'
        else:
            raise ValueError(f'Migration mapping changed: {entry["pdf"]}')
        receipt = dict(plan=plan['id'], record=entry['id'], pdf=entry['pdf'], key=entry['key'],
                       status=status, checked_at=time.time(), historical_origin_verified=False)
        # Receipts share the durable SQLite store; never edit original attempts.
        with rec.connection(True) as con:
            con.execute('CREATE TABLE IF NOT EXISTS migrations(id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            con.execute('INSERT OR REPLACE INTO migrations VALUES (?,?)',
                        (review.digest([plan['id'], entry['id']]), json.dumps(receipt)))
        results.append(receipt)
    return results

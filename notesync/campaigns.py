"""Durable exact-selection campaigns, independent of note writers.

A dispatched/unknown entry is never replayed. Inspection is observational;
recovery remains a new human decision after checking both destinations.
"""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def connection(write=False):
    from core import constants
    path = Path(constants.BASE_DIR) / 'notesync' / 'campaigns.sqlite3'
    if not write and not path.exists():
        yield None
        return
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path) if write else path.as_uri() + '?mode=ro', uri=not write, timeout=30)
    try:
        if write:
            con.execute('PRAGMA synchronous=FULL')
            con.execute('CREATE TABLE IF NOT EXISTS campaigns (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
            con.execute('BEGIN IMMEDIATE')
        yield con
        if write:
            con.commit()
    finally:
        con.close()


def create(scope, proposals, *, kind="notes", method="deterministic three-way merge"):
    campaign = dict(id=uuid.uuid4().hex, schema=1, scope=scope, created_at=time.time(),
                    kind=kind, method=method, proposals=proposals, outcomes={})
    with connection(True) as con:
        con.execute('INSERT INTO campaigns VALUES (?,?)', (campaign['id'], json.dumps(campaign)))
    return campaign


def read(identifier):
    with connection() as con:
        row = con.execute('SELECT body FROM campaigns WHERE id=?', (identifier,)).fetchone() if con else None
    if not row:
        raise ValueError('Campaign not found.')
    return json.loads(row[0])


def record(identifier, key, result, *, reserve=False, merge_review=False):
    with connection(True) as con:
        row = con.execute('SELECT body FROM campaigns WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise ValueError('Campaign not found.')
        campaign = json.loads(row[0])
        if reserve and key in campaign['outcomes']:
            raise ValueError('This proposal was already dispatched. Inspect before any recovery; no automatic replay.')
        previous = campaign['outcomes'].get(key)
        if merge_review:
            selected = {(d['record_id'], d['field']): d for d in (previous or {}).get('decisions', [])}
            selected.update({(d['record_id'], d['field']): d for d in result['decisions']})
            result = dict(result, decisions=list(selected.values()))
        campaign['outcomes'][key] = dict(result, at=time.time())
        if previous:
            campaign['outcomes'][key]['dispatch'] = previous
        con.execute('UPDATE campaigns SET body=? WHERE id=?', (json.dumps(campaign), identifier))
    return campaign


SETTLED_STATUS = 'settled_after_interruption'
UNDONE_STATUS = 'undone'
UNDO_INCOMPLETE_STATUS = 'undo_incomplete'
DISPATCH_STATUSES = ('dispatched_or_unknown', 'recovery_dispatched_or_unknown')


def application_available():
    """No production campaign dispatch until native and human content acceptance.

    Deliberately not configurable by a flag. Fixture tests inject their own
    admission to exercise the orchestration, not to certify production.
    """
    return False


def outcome_evidence(outcome):
    import hashlib
    return hashlib.sha256(json.dumps(outcome, sort_keys=True).encode()).hexdigest()


def reserve_recovery(identifier, key, expected, decision):
    """CAS reservation preserves every interrupted recovery; no replay by timeout."""
    with connection(True) as con:
        row = con.execute('SELECT body FROM campaigns WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise ValueError('Campaign not found.')
        campaign = json.loads(row[0])
        previous = campaign['outcomes'].get(key)
        if not previous or outcome_evidence(previous) != expected:
            raise ValueError('Recovery outcome changed; inspect again.')
        campaign['outcomes'][key] = dict(status='recovery_dispatched_or_unknown',
            decision=decision, dispatch=previous, at=time.time())
        con.execute('UPDATE campaigns SET body=? WHERE id=?', (json.dumps(campaign), identifier))


@contextmanager
def recovery_hold(identifier):
    """Exclude initial application and recovery without an expiring replay permit."""
    import fcntl
    from core import constants
    directory = Path(constants.BASE_DIR) / 'notesync' / 'recovery_locks'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / outcome_evidence(identifier)).open('a') as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def inspect_proposal(proposal, outcome, current):
    """Compare a freshly read preview with the reserved result; never settle/replay.

    Equality here is limited to representable personal-content regions. Even a
    matching readback cannot silently rewrite a journal or advance a sync base.
    """
    from notesync.state import norm_text
    decision = outcome or {}
    while 'decision' not in decision and isinstance(decision.get('dispatch'), dict):
        decision = decision['dispatch']
    intended = decision.get('decision', {}).get('result')
    result = dict(applicable=False, replay_allowed=False, base_advanced=False,
                  current=current, intended=intended, outcome_evidence=outcome_evidence(outcome))
    if current.get('preview_status', 200) != 200:
        return dict(result, status='unavailable', next_action='Restore access and inspect again; absence is not established.')
    if current.get('content_issues'):
        return dict(result, status='content_blocked', next_action='Review unsupported or out-of-region content without removing it.')
    identity = proposal.get('identity')
    if not identity or identity != current.get('identity') or proposal.get('converter_id') != current.get('converter_id'):
        return dict(result, status='identity_review_required', next_action='Review note identity and converter; historical proposals cannot authorize recovery.')
    if not outcome:
        return dict(result, status='not_dispatched' if proposal.get('evidence') == current.get('evidence') else 'stale',
                    next_action='Review the current proposal before any separately admitted application.')
    if outcome.get('status') == UNDONE_STATUS:
        return dict(result, status='undone',
                    next_action='This campaign was undone; both notes carry their before-images. Review a new campaign for further changes.')
    if outcome.get('status') == UNDO_INCOMPLETE_STATUS:
        return dict(result, status='undo_incomplete',
                    next_action='An undo did not complete on every destination. Inspect the retained per-destination results; nothing is retried.')
    if outcome.get('status') == SETTLED_STATUS or outcome.get('ok') is True:
        return dict(result, status='completed',
                    next_action='This outcome is recorded; neither settlement nor recovery applies. Review a new campaign for further changes.')
    if proposal.get('base_evidence') != current.get('base_evidence'):
        return dict(result, status='base_changed', next_action='The sync base changed; review a new merge without replaying this campaign.')
    if not isinstance(intended, str):
        return dict(result, status='unknown_result', next_action='Inspect the original operation journal; the approved result is unavailable.')
    sides = {}
    for side, field in (('obsidian', 'ours'), ('zotero', 'theirs')):
        now = norm_text(current.get(field))
        sides[side] = ('matches_approved' if now == norm_text(intended) else
                       'unchanged_from_preview' if now == norm_text(proposal.get(field)) else 'changed_since_preview')
    if all(s == 'matches_approved' for s in sides.values()):
        status, action = 'concordant_observed', 'Both regions match. Journal settlement and base advancement remain separately gated.'
    elif 'changed_since_preview' in sides.values():
        status, action = 'concurrent_change', 'Preserve the new edits and prepare a newly reviewed merge; do not replay.'
    elif 'matches_approved' in sides.values():
        status, action = 'incomplete', 'One destination matches. Preserve both sides; recovery application remains closed.'
    else:
        status, action = 'not_observed', 'Neither destination matches. Inspect retained journals; do not assume dispatch never happened.'
    return dict(result, status=status, destinations=sides, next_action=action)


def approved_result(outcome):
    """The originally approved result retained with a dispatch, or ``None``."""
    decision = outcome or {}
    while 'decision' not in decision and isinstance(decision.get('dispatch'), dict):
        decision = decision['dispatch']
    intended = decision.get('decision', {}).get('result')
    return intended if isinstance(intended, str) else None


def settlement_check(proposal, outcome, current, *, base, local_ops, native_ops, dispatch_at=None):
    """Bookkeeping-only settlement of a dispatch interrupted after its base save.

    Pure. ``base`` is the current base record as JSON (or ``None``), ``local_ops``
    the refactor-journal ops for this note and ``native_ops`` the tagging-journal
    Zotero note writes for this key. Every blocker is named; a blocked check
    never authorizes a rewrite, a retry, an undo baseline or history erasure.
    Text equality alone is never sufficient: the retained approval, the base
    record, the journals and their ordering must all describe the same write.
    """
    from notesync.convert import md_to_html
    from notesync.state import norm_text
    reasons = []
    intended = approved_result(outcome)
    status = (outcome or {}).get('status')
    if status not in DISPATCH_STATUSES:
        reasons.append('outcome_not_dispatched')
    if intended is None:
        reasons.append('approved_result_missing')
    identity = proposal.get('identity') or {}
    if not identity or identity != current.get('identity') or proposal.get('converter_id') != current.get('converter_id'):
        reasons.append('identity_or_converter_changed')
    if current.get('preview_status', 200) != 200:
        reasons.append('destination_unavailable')
    if current.get('content_issues'):
        reasons.append('content_blocked')
    if intended is not None:
        html = md_to_html(intended.strip('\n'))
        for side, field in (('obsidian', 'ours'), ('zotero', 'theirs')):
            if norm_text(current.get(field)) != norm_text(intended):
                reasons.append(side + '_differs_from_approved')
        if not isinstance(base, dict):
            reasons.append('base_missing_or_unreadable')
        else:
            if base.get('converter_id') != current.get('converter_id'):
                reasons.append('base_converter_differs')
            if (base.get('item_key') != (identity.get('scope') or {}).get('item')
                    or base.get('note_key') != identity.get('note_key')):
                reasons.append('base_identity_differs')
            if norm_text(base.get('base_region')) != norm_text(intended) or base.get('base_html') != html:
                reasons.append('base_content_differs_from_approved')
            if base.get('zotero_version') != current.get('zotero_version'):
                reasons.append('native_version_moved_since_base')
            if dispatch_at is not None and _iso_epoch(base.get('synced_at')) is not None \
                    and _iso_epoch(base.get('synced_at')) < int(dispatch_at):
                reasons.append('base_predates_dispatch')
        # Journal evidence: a destination that already matched at preview time
        # needs no write; otherwise exactly one applied write must describe it.
        if any(op.get('state') == 'outcome_unknown' for op in native_ops):
            reasons.append('native_write_outcome_unknown')
        local_needed = norm_text(proposal.get('ours')) != norm_text(intended)
        local_after = [op for op in local_ops if op.get('state') == 'applied'
                       and op.get('hash_before') == proposal.get('content_sha256')
                       and op.get('hash_after') == current.get('content_sha256')
                       and (dispatch_at is None or _iso_epoch(op.get('ts')) is None or _iso_epoch(op.get('ts')) >= int(dispatch_at))]
        if local_needed and len(local_after) != 1:
            reasons.append('local_journal_evidence_missing' if not local_after else 'local_journal_evidence_ambiguous')
        if not local_needed and proposal.get('content_sha256') != current.get('content_sha256'):
            reasons.append('local_note_changed_without_approved_write')
        native_needed = norm_text(proposal.get('theirs')) != norm_text(intended)
        scope = identity.get('scope') or {}
        native_after = [op for op in native_ops if op.get('state') == 'applied'
                        and op.get('after_html') == html
                        and op.get('item_key') == identity.get('note_key')
                        and op.get('parent_item') == scope.get('item')
                        and op.get('server_id') == scope.get('database')
                        and op.get('version_after') is not None
                        and isinstance(base, dict) and op.get('version_after') == base.get('zotero_version')
                        and (dispatch_at is None or _iso_epoch(op.get('ts')) is None or _iso_epoch(op.get('ts')) >= int(dispatch_at))]
        if native_needed and len(native_after) != 1:
            reasons.append('native_journal_evidence_missing' if not native_after else 'native_journal_evidence_ambiguous')
        if not native_needed and native_after:
            reasons.append('native_write_not_expected')
    if reasons:
        return dict(applicable=False, status='blocked', reasons=reasons,
                    next_action='Settlement is not proven; keep the retained outcome for reconciliation review. Do not apply the campaign again.')
    return dict(applicable=True, status='settleable', reasons=[],
                evidence=dict(base_evidence=current.get('base_evidence'), evidence=current.get('evidence'),
                              local_op_ids=[op.get('id') for op in local_after],
                              native_op_ids=[op.get('op_id') for op in native_after]),
                next_action='Both destinations, the base and the journals describe the approved result; settlement records this outcome without writing.')


def _iso_epoch(value):
    import datetime
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return int(parsed.timestamp())


def settle(identifier, key, expected, evidence):
    """CAS bookkeeping record; retains the dispatch, writes no destination."""
    with connection(True) as con:
        row = con.execute('SELECT body FROM campaigns WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise ValueError('Campaign not found.')
        campaign = json.loads(row[0])
        previous = campaign['outcomes'].get(key)
        if not previous or previous.get('status') not in DISPATCH_STATUSES or outcome_evidence(previous) != expected:
            raise ValueError('Outcome changed since inspection; inspect again before settlement.')
        campaign['outcomes'][key] = dict(ok=True, status=SETTLED_STATUS, settlement=evidence,
                                          dispatch=previous, at=time.time())
        con.execute('UPDATE campaigns SET body=? WHERE id=?', (json.dumps(campaign), identifier))
    return campaign


def undoable_outcome(outcome):
    """Only an initial application that succeeded on both destinations can be
    undone: not a recovery, not a settlement, not an undone or partial record."""
    return (isinstance(outcome, dict) and outcome.get('ok') is True
            and outcome.get('status') not in (SETTLED_STATUS, UNDONE_STATUS, UNDO_INCOMPLETE_STATUS)
            and not outcome.get('recovery') and outcome.get('http_status') == 200
            and isinstance(outcome.get('obsidian'), dict) and isinstance(outcome.get('zotero'), dict)
            and isinstance(outcome.get('dispatch'), dict) and 'previous_base' in outcome['dispatch'])


def undo_check(proposal, outcome, current, *, local_op, native_version, native_html):
    """Pure admissibility verdict for a full campaign undo.

    Both destinations must still carry the EXACT after-image of the campaign
    write: the local note hashes to the journalled ``hash_after`` (or, when no
    local write was needed, to the previewed content) and the native note's
    stored HTML and item version are the ones the write left behind. Any later
    edit on either side, an identity or converter change, an unavailable
    destination or an unreadable pre-campaign base blocks the undo. A blocked
    verdict authorizes nothing.
    """
    from notesync.convert import md_to_html
    reasons = []
    if not undoable_outcome(outcome):
        reasons.append('outcome_not_undoable')
    intended = approved_result(outcome)
    if intended is None:
        reasons.append('approved_result_missing')
    identity = proposal.get('identity') or {}
    if not identity or identity != current.get('identity') or proposal.get('converter_id') != current.get('converter_id'):
        reasons.append('identity_or_converter_changed')
    if current.get('preview_status', 200) != 200:
        reasons.append('destination_unavailable')
    if undoable_outcome(outcome):
        dispatch = outcome['dispatch']
        if dispatch.get('previous_base_unreadable'):
            reasons.append('previous_base_unreadable')
        local = outcome['obsidian']
        if local.get('op_id'):
            if (not isinstance(local_op, dict) or local_op.get('state') != 'applied'
                    or local_op.get('kind') != 'notesync_update'
                    or local_op.get('hash_before') != proposal.get('content_sha256')):
                reasons.append('local_journal_evidence_missing')
            elif local_op.get('hash_after') != current.get('content_sha256'):
                reasons.append('obsidian_changed_after_write')
        elif current.get('content_sha256') != proposal.get('content_sha256'):
            reasons.append('obsidian_changed_after_write')
        native = outcome['zotero']
        expected_html = md_to_html(intended.strip('\n')) if intended is not None else None
        if native.get('op_id'):
            if native.get('status') != 'applied' or native.get('version') is None:
                reasons.append('native_journal_evidence_missing')
            elif native_html != expected_html or native_version != native.get('version'):
                reasons.append('zotero_changed_after_write')
        elif native.get('status') == 'skipped':
            if native_html != expected_html or native_version != current.get('zotero_version'):
                reasons.append('zotero_changed_after_write')
        else:
            reasons.append('native_journal_evidence_missing')
    if reasons:
        return dict(applicable=False, status='blocked', reasons=reasons,
                    next_action='Undo is not admissible; both notes must still carry the exact written image. Review a new campaign instead.')
    return dict(applicable=True, status='undoable', reasons=[],
                next_action='Both notes still carry the exact written image; undo restores both before-images and the pre-campaign base.')


def record_undo(identifier, key, expected, result):
    """CAS record of an undo outcome; the applied outcome is retained beneath it."""
    with connection(True) as con:
        row = con.execute('SELECT body FROM campaigns WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise ValueError('Campaign not found.')
        campaign = json.loads(row[0])
        previous = campaign['outcomes'].get(key)
        if not undoable_outcome(previous) or outcome_evidence(previous) != expected:
            raise ValueError('Outcome changed since inspection; inspect again before undo.')
        campaign['outcomes'][key] = dict(result, dispatch=previous, at=time.time())
        con.execute('UPDATE campaigns SET body=? WHERE id=?', (json.dumps(campaign), identifier))
    return campaign

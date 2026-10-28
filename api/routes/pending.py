"""Explicit local PDF review, page preview, and bibliography reconciliation."""
from __future__ import annotations

import io
import logging
import sqlite3
import threading
import time
from functools import wraps

from flask import Blueprint, jsonify, request, send_file

from accession import cache
from tagging.zotero_write import ZoteroError
from accession import review
from audit.core.bib import normalise_doi, parse_bib
from audit.manager import audit_manager
from audit.engine import bridge
from api.security import sanitise_error_msg

pending_bp = Blueprint('pending_review', __name__)
logger = logging.getLogger(__name__)
_RENDER_GATE = threading.BoundedSemaphore(2)


def guarded(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (review.ReviewConflict, ZoteroError) as exc:
            return jsonify(error=str(exc)), 409
        except (ValueError, TypeError, KeyError) as exc:
            return jsonify(error=sanitise_error_msg(str(exc))), 400
        except (OSError, sqlite3.Error):
            logger.exception('PDF review storage or file unavailable')
            return jsonify(error='Review storage or PDF unavailable. No success was recorded.'), 503
        except Exception as exc:
            from accession.pdf_management import VaultBusy
            if isinstance(exc, VaultBusy):
                return jsonify(error=str(exc)), 503
            raise
    return wrapped


def context():
    inv, settings = audit_manager.get_inventory()
    if inv is None or settings is None:
        from audit.config import load_settings
        from accession import reconciliation
        from types import SimpleNamespace
        settings = load_settings()
        if not reconciliation.saved(settings):
            raise review.ReviewConflict('Run a Library Audit scan first.')
        inv = SimpleNamespace(reconciliation_only=True, records={entry.citation_key:SimpleNamespace(
            bib_entry=entry,citation_key=entry.citation_key) for entry in parse_bib(settings.master_bib)})
    return inv, settings


def target(settings, raw):
    from api.routes.audit import _normalise_rel_path
    rel = _normalise_rel_path(raw)
    if not rel:
        raise ValueError('A vault-relative PDF path is required.')
    path = (settings.vault_root / rel).resolve(strict=True)
    scope = settings.biblio_articles_dir.resolve(strict=True)
    if not path.is_relative_to(scope) or path.suffix.lower() != '.pdf' or not path.is_file():
        raise ValueError('Not a PDF in the bibliography folder.')
    return path, path.relative_to(settings.vault_root.resolve()).as_posix()


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError('Expected a JSON object.')
    return data


def enrich_proposal(proposal, inv):
    if not proposal:
        return None
    proposal = dict(proposal)
    proposal.setdefault('schema_version', 1)
    if proposal['schema_version'] == 1:
        proposal['legacy_evidence'] = True
    candidates = [dict(c) for c in proposal.get('candidates', []) if isinstance(c, dict)]
    doi = normalise_doi(proposal.get('doi') or '')
    if not candidates and doi:
        for rec in inv.records.values():
            bib = rec.bib_entry
            if bib and bib.doi == doi:
                candidates.append(dict(title=bib.title, authors=bib.authors, year=bib.year,
                                       doi=doi, source='bibliography', citation_key=rec.citation_key))
        if not candidates:
            candidates.append(dict(doi=doi, title=None, source='cached identifier'))
    for c in candidates:
        c['doi'] = normalise_doi(c.get('doi') or '')
    proposal['candidates'] = candidates
    return proposal


def payload(record):
    record = dict(record)
    record['candidate_ids'] = [review.digest(c) for c in (record.get('proposal') or {}).get('candidates', [])]
    return record


def enrich_report(result, settings):
    """One read, no initialization or hashing, and explicit storage errors."""
    try:
        records = review.snapshot(settings.vault_root)
        mapping = bridge._load_mapping_all(settings.mapping_file)
        known = {row['pdf']['rel'] for row in result['rows']}
        for rel, record in records.items():
            path = settings.vault_root / rel
            state = mapping_state(settings, rel, mapping)
            if rel not in known and state['keys'] and path.is_file():
                result['rows'].append(dict(pdf=dict(rel=rel, name=path.name, abs=str(path)), exists=True,
                                           state='mapped', resolution=record.get('proposal')))
        for row in result['rows']:
            current = mapping_state(settings, row['pdf']['rel'], mapping)
            if current['keys']:
                row['state'] = 'mapped'
                row['mapped_key'] = current['keys'][0] if len(current['keys']) == 1 else None
            record = records.get(row['pdf']['rel'])
            row['review'] = payload(review.public(record, settings.vault_root / record['rel'])) if record else None
    except (OSError, sqlite3.Error, ValueError):
        logger.exception('Cannot read PDF review store')
        result['review_error'] = 'Review storage is unavailable. Confirmation and export are disabled until it is readable.'
    result['count'] = len(result['rows'])
    result['review_scope'] = review.digest(str(settings.vault_root.resolve()))
    return result


@pending_bp.route('/api/accession/review/open', methods=['POST'])
@guarded
def open_pdf():
    data = body()
    inv, settings = context()
    path, rel = target(settings, data.get('pdf'))
    current = review.identity(path)
    stored = cache.lookup(cache.load_cache(), current['sha256'])
    record = review.open_review(settings.vault_root, rel, current, enrich_proposal(stored, inv))
    import fitz
    with fitz.open(path) as pdf:
        if pdf.needs_pass:
            raise ValueError('This PDF requires a password.')
        record['pages'] = len(pdf)
    return jsonify(payload(record))


@pending_bp.route('/api/accession/review/decision', methods=['POST'])
@guarded
def decision():
    data = body()
    if data.get('confirm') is not True:
        raise ValueError('Explicit confirmation is required.')
    _, settings = context()
    path, rel = target(settings, data.get('pdf'))
    result = payload(review.update(settings.vault_root, rel, data.get('token'),
        review.identity(path), action=data.get('action'), candidate_id=data.get('candidate_id')))
    from accession import reconciliation as rec
    observed = rec.saved(settings)
    if observed:
        result['workspace_row'] = rec.rows(settings, observed, only=rel,
            available=(rec.availability(settings) or {}).get('available', True))[0]
    return jsonify(result)


@pending_bp.route('/api/accession/review/page')
@guarded
def page():
    _, settings = context()
    path, rel = target(settings, request.args.get('pdf'))
    record = review.snapshot(settings.vault_root).get(rel)
    if not record or record['sha256'] != request.args.get('sha256'):
        raise review.ReviewConflict('Open this PDF before loading a preview.')
    if not review.same_identity(review.fingerprint(path), record['fingerprint']):
        raise review.ReviewConflict('PDF changed. Reopen the review.')
    number = int(request.args.get('page', '1'))
    scale = float(request.args.get('scale', '1'))
    if not 0.5 <= scale <= 3:
        raise ValueError('Zoom must be between 50% and 300%.')
    if not _RENDER_GATE.acquire(False):
        return jsonify(error='Another preview is rendering. Try again.'), 429
    try:
        import fitz
        with fitz.open(path) as pdf:
            if pdf.needs_pass or not 1 <= number <= len(pdf):
                raise ValueError('Page unavailable.')
            pg = pdf[number - 1]
            # Bound raster memory even for malformed or enormous page boxes.
            max_side = max(pg.rect.width, pg.rect.height)
            if max_side <= 0:
                raise ValueError('Invalid page size.')
            zoom = min(scale * 1.5, 2400 / max_side)
            png = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes('png')
        if not review.same_identity(review.fingerprint(path), record['fingerprint']):
            raise review.ReviewConflict('PDF changed during preview.')
        response = send_file(io.BytesIO(png), mimetype='image/png', max_age=0)
        response.headers['Cache-Control'] = 'no-store'
        return response
    finally:
        _RENDER_GATE.release()


def selected(data):
    rows = data.get('items')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 500 or not all(isinstance(x, dict) for x in rows):
        raise ValueError('Select between 1 and 500 PDFs.')
    return rows


@pending_bp.route('/api/accession/review/export', methods=['POST'])
@guarded
def export():
    _, settings = context()
    included, excluded, dois = [], [], []
    for item in selected(body()):
        try:
            _, rel = target(settings, item.get('pdf'))
            record = review.validate(settings.vault_root, rel, item.get('token'))
            doi = normalise_doi(record['chosen']['doi'])
            included.append(dict(pdf=rel, title=record['chosen'].get('title'), doi=doi))
            if doi not in dois:
                dois.append(doi)
        except (OSError, ValueError) as exc:
            excluded.append(dict(pdf=item.get('pdf'), reason=str(exc)))
    return jsonify(included=included, excluded=excluded, dois=dois)


def bibliography(settings):
    before = review.fingerprint(settings.master_bib)
    entries = {e.citation_key: e for e in parse_bib(settings.master_bib)}
    if review.fingerprint(settings.master_bib) != before:
        raise review.ReviewConflict('Bibliography changed while reading. Reconcile again.')
    return entries, review.digest(before)


def mapping_state(settings, rel, mapping=None):
    m = mapping if mapping is not None else bridge._load_mapping_all(settings.mapping_file)
    return dict(keys=sorted(k for k, paths in m.matches.items() if rel in paths),
                pending=rel in m.pending, no_match=rel in m.no_match,
                exclude=sorted(m.exclude.get(rel, [])))


@pending_bp.route('/api/accession/review/reconcile', methods=['POST'])
@guarded
def reconcile():
    _, settings = context()
    entries, version = bibliography(settings)
    rows = []
    for item in selected(body()):
        try:
            _, rel = target(settings, item.get('pdf'))
            record = review.validate(settings.vault_root, rel, item.get('token'))
            matches = [dict(citation_key=k, title=e.title, authors=e.authors, year=e.year)
                       for k, e in entries.items() if e.doi == record['chosen']['doi']]
            rows.append(dict(pdf=rel, token=review.token(record), matches=matches,
                             mapping=mapping_state(settings, rel)))
        except (OSError, ValueError) as exc:
            rows.append(dict(pdf=item.get('pdf'), error=str(exc)))
    return jsonify(rows=rows, bibliography_revision=version, checked_at=time.time())


@pending_bp.route('/api/accession/review/link', methods=['POST'])
@guarded
def link():
    data = body()
    if data.get('confirm') is not True:
        raise ValueError('Explicit confirmation is required.')
    _, settings = context()
    path, rel = target(settings, data.get('pdf'))
    record = review.validate(settings.vault_root, rel, data.get('token'))
    entries, version = bibliography(settings)
    key = data.get('citation_key')
    if version != data.get('bibliography_revision') or key not in entries or entries[key].doi != record['chosen']['doi']:
        raise review.ReviewConflict('Bibliography changed. Reconcile again.')
    expected = data.get('mapping')
    if not isinstance(expected, dict) or (not expected.get('pending') and expected.get('keys') != [key]):
        raise review.ReviewConflict('This PDF is no longer pending. Reconcile its current mapping.')
    def validate_bibliography():
        fresh_entries, fresh_version = bibliography(settings)
        if (fresh_version != version or key not in fresh_entries
                or fresh_entries[key].doi != record['chosen']['doi']):
            raise review.ReviewConflict('Bibliography changed before mapping. Reconcile again.')

    try:
        with review.decision_guard(settings.vault_root, rel, data.get('token')):
            bridge.add_match(settings.mapping_file, path, key, settings.vault_root,
                             require_hash=True, expected_state=expected, expected_sha256=record['sha256'],
                             expected_fingerprint=record['fingerprint'],
                             validate_evidence=validate_bibliography)
    except ValueError as exc:
        raise review.ReviewConflict(str(exc)) from exc
    return jsonify(ok=True, pdf=rel, citation_key=key, state='mapped')


@pending_bp.route('/api/accession/review/search', methods=['POST'])
@guarded
def search():
    from api.routes import accession as routes
    from api.sse import run_sse_worker
    from accession import evidence
    data = body()
    if data.get('confirm') is not True:
        raise ValueError('Confirm the online search.')
    got, err = routes._gates(network=True)
    if err:
        return err
    inv, settings, knobs = got
    path, rel = target(settings, data.get('pdf'))
    current = review.identity(path)
    record = review.snapshot(settings.vault_root).get(rel)
    if record is None:
        raise review.ReviewConflict('Open the PDF before searching.')
    review._check(record, data.get('token'))
    if record['sha256'] != current['sha256']:
        raise review.ReviewConflict('PDF changed. Reopen it.')
    search_fields = data.get('search')
    if search_fields is not None:
        if not isinstance(search_fields, dict) or set(search_fields) - {'title','author','year','doi'}:
            raise ValueError('Search fields must be title, author, year, and DOI.')
        for k, v in search_fields.items():
            if not isinstance(v, str) or len(v) > (500 if k == 'title' else 200):
                raise ValueError('Search value is too long or invalid.')
        search_fields = {k:v.strip() for k,v in search_fields.items()}
        if search_fields.get('doi'):
            doi = normalise_doi(search_fields['doi'])
            if not doi:
                raise ValueError('Invalid DOI.')
            search_fields['doi'] = doi
        if search_fields.get('year') and (not search_fields['year'].isdigit() or len(search_fields['year']) != 4):
            raise ValueError('Year must have four digits.')
        if not any(search_fields.get(k) for k in ('doi','title','author')):
            raise ValueError('Enter a title, author, or DOI.')
    if not routes._ACCESSION_GATE.acquire(False):
        return jsonify(error='An identification is already running.'), 429
    try:
        ctx = routes._build_context(inv, settings, knobs)
    except BaseException:
        routes._ACCESSION_GATE.release()
        raise

    def worker(put, cancel):
        try:
            extracted = evidence.extract(path, ctx.text_provider)
            proposed = evidence.resolve(extracted, search=search_fields, context=ctx,
                budget=routes.ACCESSION_TOTAL_BUDGET_S,
                progress=lambda source: put({'info': f'Querying {source}…'}),
                cancel=lambda: cancel.is_set() or audit_manager.is_scanning(),
                enrich=lambda r: enrich_proposal(r, inv))
            if cancel.is_set():
                return
            result = review.update(settings.vault_root, rel, data['token'], review.identity(path),
                                   action='proposal', proposal=proposed,
                                   search=search_fields or {'automatic_review': True})
            put({'review': payload(result)})
        except Exception as exc:
            logger.exception('Review search failed')
            put({'error': sanitise_error_msg(str(exc))})
    return run_sse_worker(worker, consumer_timeout_s=int(routes.ACCESSION_TOTAL_BUDGET_S) + routes.SSE_STALL_MARGIN_S,
                          release=routes._ACCESSION_GATE.release)


@pending_bp.route('/api/accession/review/rename-preview', methods=['POST'])
@guarded
def rename_preview():
    """Reuse Maintenance's naming recipe; this endpoint never applies a rename."""
    from types import SimpleNamespace
    try:
        from maintenance.rename import proposal_for, referencing_notes
    except ImportError:
        return jsonify(error='Vault Maintenance is unavailable in this build.'), 501
    from tagging.writers import find_linked_attachments
    _, settings = context()
    path, rel = target(settings, body().get('pdf'))
    before = review.identity(path)
    state = mapping_state(settings, rel)
    entries, bibliography_revision = bibliography(settings)
    key = state['keys'][0] if len(state['keys']) == 1 else None
    mapping = bridge._load_mapping_all(settings.mapping_file)
    if key and mapping.hashes.get(rel, {}).get('sha256') != before['sha256']:
        raise review.ReviewConflict('PDF changed since mapping. Review and reconcile before proposing a name.')
    record = SimpleNamespace(bib_entry=entries.get(key)) if key else None
    proposal = proposal_for(path.name, record, {p.name.lower() for p in path.parent.iterdir()})
    references = referencing_notes(settings.vault_root, rel)
    attachments, error = find_linked_attachments(settings, str(path))
    if review.identity(path) != before or mapping_state(settings, rel) != state:
        raise review.ReviewConflict('PDF or mapping changed during preview. Try again.')
    return jsonify(
        pdf=rel, preview_only=True, proposal=proposal, bibliography_revision=bibliography_revision,
        physical_filename={'current':path.name, 'proposed':proposal['proposal']},
        linked_paths=[{'item_key':a.item_key, 'parent_key':a.parent_key,
                       'current':a.raw_path, 'proposed':str(path.with_name(proposal['proposal'])) if proposal['proposal'] else None}
                      for a in attachments],
        attachment_titles={'action':'preserve', 'note':'The physical filename and Zotero display title are separate.'},
        reference_notes={'filenames':'preserve', 'references':references},
        verification_error=error,
        limitations=['Preview only. Naming plugins and settings must be checked before execution.',
                     'Filesystem, note-link and Zotero updates require separate recovery; they are not atomic.',
                     'Reference-note filename changes require their own preview and approval.'])


def workspace(settings, force=False):
    """Reports never refresh or apply mappings. Explicit refresh observes only."""
    from accession import reconciliation as rec, projection
    if force:
        rec.refresh(settings)
    joined = projection.read(settings)
    snapshot = rec.saved(settings)
    return {**joined, 'review_scope': rec.scope(settings),
            'changes': (snapshot or {}).get('changes', []) if joined['freshness']['available'] else []}


@pending_bp.route('/api/accession/review/workspace', methods=['GET'])
@guarded
def get_workspace():
    _, settings = context()
    return jsonify(workspace(settings))


@pending_bp.route('/api/accession/review/refresh-zotero', methods=['POST'])
@guarded
def refresh_zotero():
    _, settings = context()
    return jsonify(workspace(settings, force=True))


@pending_bp.route('/api/accession/review/discrepancy', methods=['POST'])
@guarded
def discrepancy():
    from accession import reconciliation as rec
    data = body()
    if data.get('confirm') is not True:
        raise ValueError('Explicit confirmation is required.')
    _, settings = context()
    _, rel = target(settings, data.get('pdf'))
    state = workspace(settings, force=True)
    if not state['freshness']['available']:
        raise review.ReviewConflict('Zotero is unavailable. Existing decisions are preserved.')
    row = next(r for r in state['rows'] if r['pdf']['rel'] == rel)
    if row['evidence'] != data.get('evidence'):
        raise review.ReviewConflict('Relationship evidence changed. Review the current associations.')
    if data.get('action') == 'use':
        rec.use_association(settings, row)
    elif data.get('action') == 'repair':
        from accession import reparent
        from core.config import load_config
        try:
            return jsonify(preview=reparent.preview(settings, load_config(), rel))
        except Exception as exc:  # noqa: BLE001 - preserve refusal as a visible preview result
            return jsonify(error=sanitise_error_msg(exc)), 409
    else:
        rec.accept(row, data.get('action'))
    return jsonify(workspace(settings))


@pending_bp.route('/api/accession/review/metadata', methods=['GET'])
@guarded
def metadata_suggestions():
    from accession import reconciliation as rec
    _, settings = context()
    snapshot = rec.saved(settings)
    if not snapshot:
        raise review.ReviewConflict('Refresh Zotero changes for library metadata suggestions.')
    journals, creators = set(), {}
    for item in snapshot['items'].values():
        if item['deleted']:
            continue
        if item['fields'].get('publicationTitle'):
            journals.add(item['fields']['publicationTitle'])
        for creator in item['creators']:
            creators[review.digest(creator)] = creator
    return jsonify(journals=sorted(journals), creators=list(creators.values()), checked_at=snapshot['checked_at'])


@pending_bp.route('/api/accession/review/repair', methods=['POST'])
@guarded
def repair():
    from accession import reparent
    from core.config import load_config
    from api.routes.zotlink import rename_gate, _rule_matches
    data = body()
    if data.get('confirm') is not True:
        raise ValueError('Explicit confirmation of the displayed repair is required.')
    _, settings = context()
    path, rel = target(settings, data.get('pdf'))
    error, rules, problems = rename_gate(False)
    if error is not None:
        return error
    if problems or (rules is not None and not _rule_matches(rules, path.stem)):
        raise ValueError('Resolve Zotero/plugin renaming risks before reparent repair.')
    try:
        return jsonify(reparent.apply(settings, load_config(), rel, data.get('token'), confirm=True))
    except Exception as exc:  # noqa: BLE001 - retained dispatch evidence must remain visible on failure
        return jsonify(error=sanitise_error_msg(exc)), 409


@pending_bp.route('/api/accession/review/repair-inspect', methods=['POST'])
@guarded
def repair_inspect():
    from accession import reparent
    from core.config import load_config
    _, settings = context()
    return jsonify(reparent.inspect(settings, load_config(), body().get('operation')))


@pending_bp.route('/api/accession/review/repair-complete', methods=['POST'])
@guarded
def repair_complete():
    from accession import reparent
    from core.config import load_config
    _, settings = context()
    data = body()
    return jsonify(reparent.complete(settings, load_config(), data.get('operation'),
                                    data.get('token'), confirm=data.get('confirm')))


# Entry-level management shares the existing authenticated PDF blueprint.
def _managed_entry():
    inv, settings = context()
    key = request.args.get('key') if request.method == 'GET' else body().get('key')
    if not isinstance(key, str) or key not in inv.records:
        raise ValueError('Choose an entry from the current inventory.')
    zot = getattr(inv.records[key], 'zotero_item', None)
    return settings, key, getattr(zot, 'item_key', None)


@pending_bp.route('/api/accession/pdfs/entry', methods=['GET'])
@guarded
def managed_entry():
    from accession import pdf_management as pm
    settings, key, parent = _managed_entry()
    result, _ = pm.entry(settings, key, parent)
    return jsonify(result)


@pending_bp.route('/api/accession/pdfs/candidates', methods=['GET'])
@guarded
def managed_candidates():
    _, settings = context()
    query = (request.args.get('q') or '').casefold()
    # A bounded result list, scoped to the same directory as PDF preview.
    paths = sorted(p.relative_to(settings.vault_root).as_posix()
                   for p in settings.biblio_articles_dir.rglob('*.pdf') if query in p.name.casefold())
    return jsonify(paths=paths[:100], total=len(paths))


@pending_bp.route('/api/accession/pdfs/preview', methods=['POST'])
@guarded
def managed_preview():
    from accession import pdf_management as pm
    from core.config import load_config
    settings, key, parent = _managed_entry()
    data = body()
    action = data.get('action')
    if action == 'link':
        result = pm.link_preview(settings, key, data.get('pdf'), parent)
    elif action == 'trash':
        result = pm.trash_preview(settings, load_config(), key, data.get('attachment'), data.get('survivor'), parent)
    elif action == 'cleanup':
        result = pm.preview(settings, key, data.get('survivor'), data.get('actions'), parent,
                            acknowledge_annotations=data.get('acknowledge_annotations') is True)
    else:
        raise ValueError('Unknown PDF action.')
    return jsonify(result)


@pending_bp.route('/api/accession/pdfs/apply', methods=['POST'])
@guarded
def managed_apply():
    from accession import pdf_management as pm
    from api.routes.zotlink import rename_gate
    from core.config import load_config
    if audit_manager.is_scanning():
        raise review.ReviewConflict('Wait for the audit scan to finish before changing PDFs.')
    settings, key, parent = _managed_entry()
    data = body()
    args = dict(confirm=data.get('confirm') is True, parent_key=parent)
    if data.get('action') == 'link':
        err, _, _ = rename_gate(data.get('acknowledge_plugins') is True)
        if err is not None:
            return err
        result = pm.link(settings, load_config(), key, data.get('pdf'), data.get('token'), **args)
    elif data.get('action') == 'trash':
        result = pm.trash(settings, load_config(), key, data.get('attachment'), data.get('survivor'), data.get('token'),
                          accept_annotation_loss=data.get('accept_annotation_loss') is True, **args)
    elif data.get('action') == 'cleanup':
        result = pm.apply(settings, key, data.get('survivor'), data.get('actions'), data.get('token'),
                          acknowledge_annotations=data.get('acknowledge_annotations') is True,
                          decision_ref=data.get('decision_ref'), **args)
    else:
        raise ValueError('Unknown PDF action.')
    return jsonify(operation=result)


@pending_bp.route('/api/accession/pdfs/holding', methods=['GET'])
@guarded
def managed_holding():
    """Recoverable storage contents and unsettled operations (read-only)."""
    from accession import pdf_management as pm
    _, settings = context()
    return jsonify(pm.holding(settings))


@pending_bp.route('/api/accession/pdfs/restore', methods=['POST'])
@guarded
def managed_restore():
    from accession import pdf_management as pm
    if audit_manager.is_scanning():
        raise review.ReviewConflict('Wait for the audit scan to finish before restoring PDFs.')
    _, settings = context()
    data = body()
    return jsonify(operation=pm.restore(settings, data.get('operation'), confirm=data.get('confirm') is True))


@pending_bp.route('/api/accession/pdfs/inspect', methods=['POST'])
@guarded
def managed_inspect():
    from accession import pdf_management as pm
    from core.config import load_config
    _, settings = context()
    return jsonify(pm.inspect(settings, load_config(), body().get('operation')))


@pending_bp.route('/api/accession/pdfs/complete', methods=['POST'])
@guarded
def managed_complete():
    from accession import pdf_management as pm
    from core.config import load_config
    _, settings = context()
    data = body()
    return jsonify(operation=pm.complete(settings, load_config(), data.get('operation'), data.get('token'), confirm=data.get('confirm') is True))

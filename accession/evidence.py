"""Bounded evidence extraction and review-only corroboration.

The bridge/duplicate extractors retain their historical contracts. Review
proposals retain all observed identifiers; a resolved identifier is not approval.
"""
from __future__ import annotations

import time
from pathlib import Path

from accession.candidates import resolve_queries, score_title, MIN_TITLE_SCORE
from accession.identify import Query, identify
from accession.sources import SourceError
from audit.core.bib import normalise_doi
from audit.core.pdf_identifiers import _DOI_RE, read_pdf_meta


def extract(path: Path, text_provider=None) -> dict:
    import fitz
    with fitz.open(path) as pdf:
        if pdf.needs_pass or not len(pdf):
            raise ValueError('PDF has no readable first page.')
        text = pdf[0].get_text()[:20000]
        author = (pdf.metadata or {}).get('author', '').strip()
    origin = 'first_page_text'
    if not text.strip() and text_provider:
        text = (text_provider(path) or '')[:20000]
        origin = 'cached_text_prefix' if text.strip() else 'no_text'
    observations = []
    for source, value in [('embedded_metadata', read_pdf_meta(path)['info_blob']), (origin, text[:4000])]:
        for match in _DOI_RE.finditer(value):
            doi = normalise_doi(match.group().rstrip('.,;)]}'))
            item = dict(kind='doi', value=doi, origin=source)
            if doi and item not in observations:
                observations.append(item)
    queries = identify(None, text, path.stem)
    titles = [q.value for q in queries if q.kind == 'title']
    return dict(identifiers=observations, title=titles[0] if titles else None,
                authors=[author] if author else [], author_origin='embedded_metadata' if author else 'unavailable',
                text_origin=origin, queries=queries)


def resolve(evidence, *, search, context, budget, progress, cancel, enrich):
    queries = evidence['queries']
    if search:
        fn_author = (search.get('author') or '').strip()
        year = search.get('year') or None
        queries = []
        if search.get('doi'):
            queries.append(Query('doi', search['doi'], 'user'))
        if search.get('title'):
            queries.append(Query('title', search['title'], 'user',
                                 fn_authors=(fn_author,) if fn_author else (), fn_year=year))
        elif fn_author:
            queries.append(Query('authoryear', fn_author, 'user', fn_authors=(fn_author,), fn_year=year))
    else:
        identifiers = []
        for obs in evidence['identifiers']:
            if obs['value'] not in [q.value for q in identifiers]:
                identifiers.append(Query('doi', obs['value'], 'meta' if obs['origin'] == 'embedded_metadata' else 'text'))
        queries = identifiers + [q for q in queries if q.kind != 'doi']
    candidates, notes, consulted = [], [], []
    attempts, observations = [], []
    ambiguous = False
    class TrackedSource:
        def __init__(self, source):
            self.source = source
        def __getattr__(self, name):
            return getattr(self.source, name)
        def run(self, kind, value, **kwargs):
            try:
                found = self.source.run(kind, value, **kwargs)
            except SourceError as exc:
                attempts.append(dict(source=self.source.name, query_kind=kind, query=value, status='failed', reason=str(exc)))
                raise
            attempts.append(dict(source=self.source.name, query_kind=kind, query=value, status='complete', count=len(found)))
            observations.extend((self.source.name, candidate) for candidate in found)
            return found
    sources = [TrackedSource(s) for s in context.sources]
    start = time.monotonic()
    incomplete = not queries or len(queries) > 6
    for q in queries[:6]:
        remaining = budget - (time.monotonic() - start)
        if cancel() or remaining < 5:
            incomplete = True
            notes.append('Lookup cancelled or time budget exhausted; some evidence was not resolved.')
            break
        res = resolve_queries([q], bib_doi_index=context.bib_index, sources=sources,
                              total_budget_s=remaining, rows=context.max_candidates,
                              per_call_timeout_s=context.timeout_s,
                              progress_cb=progress, should_cancel=cancel)
        ambiguous = ambiguous or res.outcome == "ambiguous"
        if not any(q.kind in source.supports for source in sources) and res.outcome == 'none':
            incomplete = True
            notes.append('No configured source supports this query; lookup was not completed.')
        raw = enrich(res.to_jsonable())
        notes.extend(res.notes)
        consulted.extend(s for s in res.consulted if s not in consulted)
        for c in raw.get('candidates', []):
            c = dict(c)
            provenance = dict(source=c.get('source'), query_kind=q.kind, query=q.value, evidence=q.evidence)
            old = next((x for x in candidates if (c.get('doi') and x.get('doi') == c['doi']) or
                        (not c.get('doi') and x.get('title') == c.get('title') and x.get('year') == c.get('year'))), None)
            if old:
                old['provenance'].append(provenance)
                for field in ('authors', 'year', 'container', 'title'):
                    if not old.get(field) and c.get(field):
                        old[field] = c[field]
                        old.setdefault('field_sources', {})[field] = c.get('source')
            else:
                c['provenance'] = [provenance]
                c['field_sources'] = {k:c.get('source') for k in ('title','authors','year','container','doi') if c.get(k)}
                for source, observed in observations:
                    same = (c.get('doi') and observed.doi == c['doi']) or (not c.get('doi') and observed.title == c.get('title'))
                    if same:
                        for field in ('title','authors','year','container','doi'):
                            observed_value = getattr(observed, field)
                            value = c.get(field)
                            if value and (list(value) if isinstance(value, tuple) else value) == (list(observed_value) if isinstance(observed_value, tuple) else observed_value):
                                c['field_sources'].setdefault(field, source)
                                if c['field_sources'][field] == c.get('source') and source != c.get('source'):
                                    primary = [x for src,x in observations if src == c.get('source') and x.doi == c.get('doi')]
                                    if not any(getattr(x, field) for x in primary):
                                        c['field_sources'][field] = source
                candidates.append(c)
    title = evidence.get('title')
    for c in candidates:
        c['title_agreement'] = score_title(title, c.get('title') or '') if title and c.get('title') else None
        # Only claim an author comparison when both sources actually contain names.
        authors = evidence.get('authors') or []
        from audit.engine.bridge import _normalize_author_lastname
        left = {_normalize_author_lastname(a) for a in authors}
        right = {_normalize_author_lastname(a) for a in c.get('authors', [])}
        c['author_agreement'] = bool((left - {''}) & (right - {''})) if authors and right else None
    # The shared resolver fills missing fields but keeps the first nonempty
    # value. Preserve disagreements from the observed responses for review.
    for c in candidates:
        conflicts = {}
        for field in ('title', 'authors', 'year', 'container'):
            values = []
            for source, observed in observations:
                if not c.get('doi') or observed.doi != c['doi']:
                    continue
                value = getattr(observed, field)
                if value:
                    value = list(value) if isinstance(value, tuple) else value
                    values.append({'source': source, 'value': value})
            if len({str(x['value']) for x in values}) > 1:
                conflicts[field] = values
        c['field_conflicts'] = conflicts
    candidates.sort(key=lambda c: c.get('title_agreement') if c.get('title_agreement') is not None else -1, reverse=True)
    identifiers = {o['value'] for o in evidence['identifiers']}
    conflict = any(c['field_conflicts'] for c in candidates) or len(identifiers) > 1 or any(c.get('title_agreement') is not None and
                                          c['title_agreement'] < MIN_TITLE_SCORE for c in candidates
                                          if any(p['query_kind'] == 'doi' for p in c['provenance']))
    incomplete = incomplete or any('skipped' in n or 'budget' in n for n in notes)
    failed = bool(attempts) and all(a['status'] == 'failed' for a in attempts)
    incomplete = incomplete or (not failed and any(a['status'] == 'failed' for a in attempts))
    outcome = 'conflict' if conflict else 'incomplete' if incomplete else 'lookup_failed' if failed else 'ambiguous' if ambiguous else 'candidates' if candidates else 'none'
    return dict(outcome=outcome, tier='review_evidence', candidates=candidates,
                notes=list(dict.fromkeys(notes)), consulted=consulted,
                evidence='user_corrected_search' if search else evidence['text_origin'],
                evidence_snapshot={k:v for k,v in evidence.items() if k != 'queries'},
                search=search, source_attempts=attempts, schema_version=2)

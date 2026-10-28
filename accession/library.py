"""Read adapters for Library. Native identities, physical files and bib records
remain distinct. This module neither refreshes nor applies any decision."""
from collections import Counter
from pathlib import Path

from accession import duplicates, projection, reconciliation
from audit.core.bib import normalise_doi


def documents(settings, inventory, *, pdf_workspace=None):
    pdfs = pdf_workspace if pdf_workspace is not None else projection.read(settings)
    snapshot = reconciliation.saved(settings) or {}
    native = snapshot.get('items', {})
    records = {}
    for item in native.values():
        if item['deleted'] or item['item_type'] in ('note', 'attachment'):
            continue
        identity = f"zotero:{snapshot.get('database')}:{item['library']}:{item['key']}"
        records[identity] = dict(id=identity, kind='reference', title=item['fields'].get('title', ''),
            citation_key=item['fields'].get('citationKey'), database=snapshot.get('database'),
            library=item['library'], item_key=item['key'], files=[], attachments=[],
            metadata=item['fields'], provenance='Saved Zotero observation', observed_at=snapshot.get('checked_at'))
    native_ids = {(r['library'], r['item_key']): r for r in records.values()}
    standalone = []
    attachment_count = 0
    for item in native.values():
        if item['deleted'] or item['item_type'] != 'attachment':
            continue
        parent = native_ids.get((item['library'], item.get('parent')))
        attachment_count += 1
        attachment = dict(key=item['key'], path=item.get('path'),
            link_mode=item.get('link_mode'), storage={'linked_file': 'linked', 'imported_file': 'Zotero storage',
                'imported_url': 'Zotero storage', 'linked_url': 'remote URL',
                'embedded_image': 'embedded image'}.get(item.get('link_mode'), 'unknown'))
        if parent is not None:
            parent['attachments'].append(attachment)
        else:
            standalone.append(dict(id=f"attachment:{snapshot.get('database')}:{item['library']}:{item['key']}",
                kind='attachment', title=item.get('fields', {}).get('title') or item['key'],
                database=snapshot.get('database'), library=item['library'], item_key=item['key'],
                parent_key=item.get('parent'), attachments=[attachment],
                provenance='Saved Zotero attachment observation; no reference parent joined',
                observed_at=snapshot.get('checked_at'), applicable=False))
    by_scan_key = {}
    for key, record in inventory.records.items():
        zot = getattr(record, 'zotero_item', None)
        # Do not guess library identity from a citation-key collision.
        native_record = (native_ids.get((getattr(zot, 'library_id', None), getattr(zot, 'item_key', None)))
                         if getattr(zot, 'server_id', None) == snapshot.get('database') else None)
        if native_record is not None:
            native_record.setdefault('citation_keys', []).append(key)
            native_record['citation_key'] = native_record.get('citation_key') or key
            by_scan_key[key] = native_record
            continue
        records['bibliography:' + key] = dict(id='bibliography:' + key, kind='reference',
            citation_key=key, title=getattr(getattr(record, 'bib_entry', None), 'title', '') or '',
            files=[], attachments=[], provenance='Saved scan record; native identity not joined')
        by_scan_key[key] = records['bibliography:' + key]
    pdfs['freshness']['scan_at'] = getattr(inventory, 'built_at', None)
    pdfs['freshness']['bibliography'] = (pdfs['rows'][0].get('bibliography_identity') if pdfs['rows'] else None)
    pdfs['freshness']['notes_read_at'] = None
    files = []
    for row in pdfs['rows']:
        refs = []
        for p in row.get('parents', []):
            parent = native_ids.get((p['library'], p['key']))
            if parent:
                refs.append(parent['id'])
                parent['files'].append(row['pdf']['rel'])
        for key in row['mapping']['keys']:
            record = by_scan_key.get(key)
            if record and record['id'] not in refs:
                refs.append(record['id'])
                record['files'].append(row['pdf']['rel'])
        files.append(dict(id='file:' + row['pdf']['rel'], kind='file', title=row['pdf']['name'],
                          references=refs, row=row))
    for document in records.values():
        document['files'] = sorted(set(document['files']))
        document['storage_preparation'] = conversion_preparation(document, settings)
    _document_evidence(records, files, inventory)
    retained = _scan_only_documents(settings, inventory, files)
    return dict(documents=[*records.values(), *files, *standalone, *retained],
                coverage=dict(scan_only_entries=len(retained),
                    basis='Saved scan entries without a current file projection remain separately visible; no absence is inferred.'),
                populations={'references': len(records), 'files': len(files),
                             'attachments': attachment_count},
                pdf_workspace=pdfs, freshness=pdfs['freshness'])


def _scan_only_documents(settings, inventory, files):
    """Retain exact legacy file/note membership without constructing write rows."""
    known = {d['id'] for d in files}
    retained = {}
    def add(path, report, key=None, kind='scan_file'):
        path = Path(path)
        try:
            relative = path.relative_to(settings.vault_root).as_posix()
        except ValueError:
            relative = str(path)
        identifier = ('note:' if kind == 'note' else 'file:') + relative
        if identifier in known:
            return
        doc = retained.setdefault(identifier, dict(id=identifier, kind=kind, title=path.name,
            path=relative, scan_keys=[], owning_reports=[], applicable=False,
            provenance='Saved scan only; current file evidence unavailable', observed_at=getattr(inventory, 'built_at', None)))
        if report not in doc['owning_reports']:
            doc['owning_reports'].append(report)
        if key and key not in doc['scan_keys']:
            doc['scan_keys'].append(key)
    for key, record in inventory.records.items():
        for path in getattr(record, 'pdf_paths', []):
            add(path, 'inventory', key)
    bridge = getattr(inventory, 'bridge', None)
    for attribute, report in (('unmapped_pdfs', 'read_unzoterod'), ('ambiguous_pdfs', 'ambiguous_pdfs'),
            ('confirmed_no_match_pdfs', 'not_in_zotero'), ('pending_pdfs', 'not_in_zotero')):
        for path in getattr(bridge, attribute, []):
            add(path, report)
    for path in getattr(inventory, 'pdfs_skipped', []):
        add(path, 'skipped_pdfs')
    for path in getattr(inventory, 'notes_without_record', []):
        add(path, 'orphan_notes', kind='note')
    return list(retained.values())


def search_corpus(view):
    """Search the exact Documents population, including native-only identities.

    Historical report entries remain separate, with their original actions and
    exclusions. A stable document ID selects the exact new-view entry.

    The haystack is built from what the Documents view SHOWS — title, citation
    keys, file name and path, kind and state, DOI, the stable id — never a JSON dump of the
    whole document (2026-09-17 review, C2): the dump cost ~2 s per request on
    the live library and let a query match internal keys nobody can see.
    """
    from audit.search import _Entry, _hay, _row
    result = []
    for document in view['documents']:
        pdf = (document.get('row') or {}).get('pdf') or {}
        row = _row(document.get('title') or document.get('citation_key') or document['id'],
            document['kind'], citation_key=document.get('citation_key'),
            pdf_rel=pdf.get('rel'))
        row['document_id'] = document['id']
        metadata = document.get('metadata') or {}
        # The stable document id (and its Zotero item key) stay searchable:
        # the id is how an exact entry is selected.
        hay = _hay(document['id'], document.get('item_key'), document.get('title'), document.get('citation_key'), document.get('citation_keys'),
                   document.get('scan_keys'), document.get('kind'), document.get('path'),
                   pdf.get('rel'), pdf.get('name'), (document.get('row') or {}).get('state'),
                   metadata.get('DOI'), document.get('files'))
        result.append(_Entry('library_documents', hay, row))
    return result


def checks(settings, inventory):
    view = documents(settings, inventory)
    try:
        bibliography, bib_identity = reconciliation.bibliography_snapshot(settings.master_bib)
    except (OSError, ValueError) as exc:
        return dict(results=[], counts={}, freshness={**view['freshness'], 'available': False, 'error': str(exc)},
                    method='deterministic', writes=False, available=False)
    entries = {}
    for entry in bibliography:
        entries.setdefault(entry.citation_key, []).append(entry)
    results = []
    for doc in view['documents']:
        if doc['kind'] in ('scan_file', 'note'):
            results.append(dict(id=doc['id'], kind=doc['kind'], status='review', contradictions=[],
                uncertainties=['Saved scan entry lacks current document evidence; absence is not established.'],
                sources=dict(saved_scan=doc), observation=view['freshness'], applicable=False))
            continue
        if doc['kind'] == 'attachment':
            results.append(dict(id=doc['id'], kind='attachment', status='review', contradictions=[],
                uncertainties=['No reference parent joined in the saved observation; inspect before any operation.'],
                sources=dict(attachment=doc), observation=view['freshness'], applicable=False))
            continue
        if doc['kind'] != 'file':
            results.append(_reference_check(doc, entries, bib_identity, view['freshness']))
            continue
        row = doc['row']
        contradictions, uncertainties = [], []
        bib_sources = []
        for key in row['mapping']['keys']:
            matches = entries.get(key, [])
            if len(matches) > 1:
                contradictions.append('Bibliography citation key is not unique: ' + key)
            if not matches:
                uncertainties.append('Associated key is not in the current bibliography export: ' + key)
            for entry in matches:
                bib_sources.append(dict(citation_key=key, doi=entry.doi, title=entry.title))
        if not row.get('physical'):
            uncertainties.append('Physical file is unavailable; disappearance is not inferred.')
        if row.get('moved_from'):
            uncertainties.append('A file move needs relationship and dependent-link review.')
        if row.get('attempt', {}).get('stage') not in (None, 'fresh'):
            uncertainties.append('Inspect the retained creation attempt before another operation.')
        if len(row['mapping']['keys']) > 1:
            contradictions.append('More than one CHERQAiN association.')
        if row.get('parent_state') == 'ambiguous':
            contradictions.append('Multiple candidate Zotero parents.')
        if row['state'] == 'Discrepancies':
            contradictions.append(row.get('reason') or 'Relationship differs from saved decision.')
        chosen = ((row.get('review') or {}).get('chosen') or {}).get('doi')
        observed = {normalise_doi(p['metadata'].get('DOI', '')) for p in row.get('parents', [])}
        observed.discard('')
        if chosen and observed and normalise_doi(chosen) not in observed:
            contradictions.append('Confirmed PDF DOI contradicts the observed parent DOI.')
        bib_dois = {normalise_doi(e['doi'] or '') for e in bib_sources} - {''}
        if observed and bib_dois and observed != bib_dois:
            contradictions.append('Zotero and associated bibliography DOI evidence differ.')
        if chosen and bib_dois and normalise_doi(chosen) not in bib_dois:
            contradictions.append('Confirmed PDF DOI contradicts the associated bibliography DOI.')
        if not view['freshness']['available']:
            uncertainties.append('Zotero observation unavailable; last evidence does not establish absence.')
        if row.get('attachment_state') == 'none_observed':
            uncertainties.append('No attachment observed for this physical file.')
        if row.get('bibliography_state') != 'available':
            uncertainties.append('Bibliography export is awaiting verification.')
        if not chosen:
            uncertainties.append('PDF identity not independently confirmed; title/filename are only clues.')
        results.append(dict(id=doc['id'], pdf=row['pdf']['rel'], method='deterministic evidence comparison',
            status='contradiction' if contradictions else 'review' if uncertainties else 'relationship_checked',
            contradictions=contradictions, uncertainties=uncertainties, evidence=row['evidence'],
            sources=dict(physical=row['physical'], mapping=row['mapping'], parents=row.get('parents', []),
                         attachments=row['associations'], bibliography=bib_identity, bibliography_records=bib_sources,
                         versions=doc.get('versions', []), dependent_links=doc.get('dependent_links', [])),
            observation=view['freshness'], applicable=False))
    return dict(results=results, counts=dict(Counter(r['status'] for r in results)),
                freshness=view['freshness'], method='deterministic', writes=False)


def conversion_preparation(document, settings):
    """Never converts, copies or cleans up storage. Already linked is satisfied."""
    attachments = document.get('attachments', [])
    return dict(document=document['id'], destination=str(settings.biblio_articles_dir),
        steps=[dict(attachment=a['key'], path=a.get('path'),
                    status=('already_linked' if a.get('link_mode') == 'linked_file' else
                            'native_validation_required' if a.get('link_mode') in ('imported_file', 'imported_url') else 'not_a_storage_conversion'))
               for a in attachments], applicable=False,
        blockers=['Identity-preserving conversion, annotation/resource inventory and native/human acceptance are required.'],
        cleanup_authorized=False)


def _document_evidence(records, files, inventory):
    """Bounded comparisons over saved observations; no vault walk or new hash."""
    by_name, groups = {}, {}
    for doc in files:
        by_name.setdefault(doc['title'].casefold(), []).append(doc['id'])
        stem, variant, counter = duplicates.variant_stem(doc['title'])
        doc['variant'] = dict(kind=variant, counter=counter, base_stem=stem)
        groups.setdefault(stem, []).append(doc)
        for ref in doc['references']:
            groups.setdefault('reference:' + ref, []).append(doc)
        doc['versions'], doc['dependent_links'] = [], []
    for group in groups.values():
        if len(group) < 2:
            continue
        for doc in group:
            known = {v['file'] for v in doc['versions']}
            for other in group:
                if other['id'] == doc['id'] or other['id'] in known:
                    continue
                left = (doc['row'].get('physical') or {}).get('sha256')
                right = (other['row'].get('physical') or {}).get('sha256')
                relation = 'unknown' if not left or not right else 'same_bytes' if left == right else 'different_bytes'
                doc['versions'].append(dict(file=other['id'], relation=relation,
                    basis='saved physical fingerprints; filename or shared reference is only a candidate clue',
                    preserve=True, annotation_equivalence=False))
                known.add(other['id'])
    by_id = {d['id']: d for d in files}
    for key, record in inventory.records.items():
        note = getattr(record, 'obsidian_note', None)
        if note is None:
            continue
        links = []
        for name in sorted(getattr(note, 'pdf_links', [])):
            candidates = by_name.get(name.casefold(), [])
            link = dict(note=str(note.path), target=name, candidates=candidates,
                status='unresolved' if not candidates else 'ambiguous' if len(candidates) > 1 else 'candidate',
                basis='saved scan basename link; current path and target require explicit reread')
            links.append(link)
            for identifier in candidates:
                by_id[identifier]['dependent_links'].append(link)
        for ref in records.values():
            if ref.get('citation_key') == key or key in ref.get('citation_keys', []):
                ref.setdefault('dependent_links', []).extend(links)


def _reference_check(doc, entries, identity, freshness):
    key = doc.get('citation_key')
    matches = entries.get(key, []) if key else []
    contradictions, uncertainties = [], []
    if len(matches) > 1:
        contradictions.append('Bibliography citation key is not unique: ' + key)
    if not matches:
        uncertainties.append('No current export match; this does not establish an absent Zotero reference.')
    native_doi = normalise_doi(doc.get('metadata', {}).get('DOI', ''))
    exported = {normalise_doi(e.doi or '') for e in matches} - {''}
    if native_doi and exported and exported != {native_doi}:
        contradictions.append('Observed Zotero DOI and current bibliography DOI differ.')
    if not freshness.get('available'):
        uncertainties.append('Zotero observation unavailable; saved identity retained.')
    if not doc.get('attachments'):
        uncertainties.append('No attachment in the saved observation; reference remains visible.')
    if any(link['status'] in ('unresolved', 'ambiguous') for link in doc.get('dependent_links', [])):
        uncertainties.append('Dependent PDF links require path resolution review.')
    uncertainties.append('Publication existence and citation support are not certified by identifier agreement.')
    return dict(id=doc['id'], citation_key=key, kind='reference', method='deterministic evidence comparison',
        status='contradiction' if contradictions else 'review', contradictions=contradictions,
        uncertainties=uncertainties, sources=dict(reference=doc, bibliography=identity,
        bibliography_records=[dict(doi=e.doi, title=e.title) for e in matches]), observation=freshness, applicable=False)

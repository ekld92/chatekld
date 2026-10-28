"""Explicit bounded DOI acquisition through the existing fixed-host client.

A catalogue observation supports registry existence only. It does not establish
PDF identity, publication quality, or whether a passage supports an assertion.
"""
from dataclasses import asdict
import hashlib
import json
import time
from urllib.parse import quote

from accession.sources import CrossrefSource, SourceError
from audit.core.bib import normalise_doi


def identifiers(corpus):
    result = []
    for record in corpus:
        raw = record.get('doi') or record.get('current', {}).get('DOI') or record.get('current', {}).get('doi')
        doi = normalise_doi(raw) if isinstance(raw, str) else None
        if not doi:
            raise ValueError('DOI acquisition requires one explicit valid DOI per record.')
        result.append(doi)
    if len(result) > 5:
        raise ValueError('DOI acquisition is limited to five selected records per request.')
    return result


def acquire(corpus):
    dois = identifiers(corpus)
    source = CrossrefSource()
    augmented, observations = [], []
    for record, doi in zip(corpus, dois):
        observation = dict(record_id=record['id'], doi=doi, source='crossref',
            locator=source.BASE + '/works/' + quote(doi, safe=''), retrieved_at=time.time(),
            publication_existence='unknown', pdf_correspondence='unverified', citation_support='unverified')
        received = []
        try:
            candidates = source.run('doi', doi, rows=1, timeout_s=5.0)
            matching = [c for c in candidates if normalise_doi(c.doi or '') == doi]
            if len(matching) != 1:
                observation.update(status='no_exact_record_observed',
                    reason='No unique exact DOI response; publication absence is not inferred.')
            else:
                body = json.dumps(asdict(matching[0]), sort_keys=True, ensure_ascii=False)
                digest = hashlib.sha256(body.encode()).hexdigest()
                identifier = 'acquired:crossref:' + digest
                if any(s['id'] == identifier for s in record.get('sources', [])):
                    raise ValueError('Supplied source collides with acquired provenance.')
                observation.update(status='observed', publication_existence='registry_record_observed',
                                   content_sha256=digest)
                received.append(dict(id=identifier, locator=observation['locator'], text=body,
                    retrieved_at=observation['retrieved_at'], content_sha256=digest,
                    provenance='Server-acquired structured Crossref response; not a PDF quotation.',
                    kind='catalogue_metadata', acquisition='crossref_exact_doi'))
        except SourceError as exc:
            observation.update(status='unavailable', reason=str(exc))
        augmented.append(dict(record, sources=[*record.get('sources', []), *received]))
        observations.append(observation)
    return augmented, observations

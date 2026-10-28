from types import SimpleNamespace

from accession import evidence
from accession.identify import Query
from accession.sources import Candidate, SourceUnavailable


class Source:
    name = 'fixture'
    supports = {'doi','title','authoryear'}
    def __init__(self, fail=False):
        self.fail = fail
    def run(self, kind, value, **kwargs):
        if self.fail:
            raise SourceUnavailable('fixture unavailable')
        if kind == 'doi':
            return [Candidate(source=self.name, title='Completely unrelated astronomy', doi=value)]
        return [Candidate(source=self.name, title='Depression treatments in adults', doi='10.1234/right', authors=('Smith, Jane',))]


def resolve(identifiers=None, sources=None, search=None):
    info = dict(identifiers=identifiers or [], title='Depression treatments in adults', authors=['Smith, Jane'],
                text_origin='first_page_text', author_origin='embedded_metadata',
                queries=[Query('title','Depression treatments in adults','text')])
    context = SimpleNamespace(sources=sources if sources is not None else [Source()], bib_index={}, max_candidates=5,timeout_s=5)
    return evidence.resolve(info,search=search,context=context,budget=60,progress=lambda _:None,cancel=lambda:False,enrich=lambda r:r)


def test_identifier_success_does_not_hide_title_disagreement():
    result = resolve([dict(kind='doi',value='10.1234/wrong',origin='embedded_metadata')])
    assert result['outcome'] == 'conflict'
    assert result['candidates'][0]['doi'] == '10.1234/right'
    assert result['candidates'][0]['author_agreement'] is True
    assert any(c['title_agreement'] < .65 for c in result['candidates'])


def test_lookup_failure_is_distinct_from_no_candidate():
    assert resolve(sources=[Source(True)])['outcome'] == 'lookup_failed'
    assert resolve(sources=[])['outcome'] == 'incomplete'


def test_corrected_query_and_observed_identifiers_remain_separate():
    result = resolve([dict(kind='doi',value='10.1234/original',origin='first_page_text')], search={'doi':'10.1234/corrected'})
    assert result['search']['doi'] == '10.1234/corrected'
    assert result['evidence_snapshot']['identifiers'][0]['value'] == '10.1234/original'
    assert result['candidates'][0]['doi'] == '10.1234/corrected'


def test_extract_keeps_printed_and_embedded_doi_origins(tmp_path):
    import fitz
    path = tmp_path / 'paper.pdf'
    with fitz.open() as doc:
        doc.new_page().insert_text((40, 40), 'Depression treatments in adults\ndoi:10.1234/printed')
        doc.set_metadata({'subject':'doi:10.1234/embedded', 'author':'Smith, Jane'})
        doc.save(path)
    result = evidence.extract(path)
    assert {'kind':'doi', 'value':'10.1234/printed', 'origin':'first_page_text'} in result['identifiers']
    assert {'kind':'doi', 'value':'10.1234/embedded', 'origin':'embedded_metadata'} in result['identifiers']
    assert result['authors'] == ['Smith, Jane']


def test_textless_pdf_labels_cached_fallback_and_missing_evidence(tmp_path):
    import fitz
    path = tmp_path / 'scan.pdf'
    with fitz.open() as doc:
        doc.new_page()
        doc.save(path)
    empty = evidence.extract(path, lambda _: '')
    assert empty['text_origin'] == 'no_text'
    assert empty['authors'] == []
    result = evidence.extract(path, lambda _: 'A cached transcription\ndoi:10.1234/cached')
    assert result['text_origin'] == 'cached_text_prefix'
    assert result['identifiers'] == [{'kind':'doi', 'value':'10.1234/cached', 'origin':'cached_text_prefix'}]


def test_partial_failure_without_candidates_is_incomplete():
    class Empty(Source):
        name = 'empty'
        def run(self, *args, **kwargs):
            return []
    result = resolve(sources=[Source(True), Empty()])
    assert result['outcome'] == 'incomplete'
    assert {a['status'] for a in result['source_attempts']} == {'failed','complete'}


def test_completed_empty_search_is_none():
    class Empty(Source):
        def run(self, *args, **kwargs):
            return []
    assert resolve(sources=[Empty()])['outcome'] == 'none'


def test_same_doi_source_disagreement_survives_candidate_merge():
    class First(Source):
        name = 'first'
        def run(self, *args, **kwargs):
            return [Candidate(source=self.name, title='Depression treatments in adults', doi='10.1234/one', year=2020)]
    class Second(First):
        name = 'second'
        def run(self, *args, **kwargs):
            return [Candidate(source=self.name, title='Depression treatments in adults', doi='10.1234/one', year=2021)]
    result = resolve(sources=[First(), Second()])
    assert result['outcome'] == 'conflict'
    assert result['candidates'][0]['field_conflicts']['year'] == [
        {'source':'first','value':2020}, {'source':'second','value':2021}]

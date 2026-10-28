"""The cross-report search finds what the tables show (2026-09-19 review).

The inventory and tagging rows display bib fields through
``widget.textclean.clean_display``; the vendored corpus indexes the raw LaTeX
source. Without ``api.routes.audit._clean_corpus_display`` a user who typed
the name on screen ("Régis") found nothing, because the haystack only held
``r{\\'e}gis``.
"""

from audit.search import _Entry, fold, search
from api.routes.audit import _clean_corpus_display


def _entry(label, detail):
    return _Entry("inventory", fold(f"key {label} {detail}"),
                  {"label": label, "detail": detail, "citation_key": "key", "pdf_rel": None})


def test_the_displayed_spelling_is_searchable():
    corpus = [_entry(r"Comment \'evolue la sant\'e", r"R{\'e}gis 2020")]
    assert search(corpus, "Régis")["total"] == 0          # the defect
    _clean_corpus_display(corpus)
    assert search(corpus, "Régis")["total"] == 1
    assert search(corpus, "évolue santé")["total"] == 1


def test_the_hit_shows_the_cleaned_text_and_the_raw_stays_matchable():
    corpus = [_entry(r"Find a {{Program}}", "Smith 2020")]
    _clean_corpus_display(corpus)
    assert corpus[0].row["label"] == "Find a Program"
    assert search(corpus, "{{program")["total"] == 1       # nothing was lost


def test_plain_text_is_left_alone():
    corpus = [_entry("Plain title", "Smith 2020")]
    before = corpus[0].haystack
    _clean_corpus_display(corpus)
    assert corpus[0].haystack == before
    assert corpus[0].row["label"] == "Plain title"

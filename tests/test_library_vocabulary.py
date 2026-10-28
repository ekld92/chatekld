"""The Library UI's vocabulary mirror must carry every public label the server
defines (plan §5): a label added to ``accession/vocabulary.py`` and not to
``static/js/audit.js::_LIBRARY_STATE_LABELS`` would show the internal id."""
from pathlib import Path

from accession.vocabulary import STATE_LABELS

ROOT = Path(__file__).resolve().parents[1]


def test_javascript_mirror_carries_every_server_label():
    source = (ROOT / "static/js/audit.js").read_text(encoding="utf-8")
    start = source.index("const _LIBRARY_STATE_LABELS = {")
    block = source[start:source.index("};", start)]
    for state, label in STATE_LABELS.items():
        assert repr(state)[1:-1] in block, state
        assert label in block, label


def test_no_library_view_renders_raw_json():
    """A source ratchet on the 2026-09-16 decision: no Library view uses a
    JSON dump. The remaining JSON.stringify calls in audit.js serve request
    bodies and the accession/export paths, never a rendered element."""
    source = (ROOT / "static/js/audit.js").read_text(encoding="utf-8")
    library = source[source.index("function _mountLibrary("):source.index("function _campaignElement(")]
    review = source[source.index("function _mountBibliographyReview("):]
    review = review[:review.index("\n}\n") + 3]
    for chunk, name in ((library, "_mountLibrary"), (review, "_mountBibliographyReview")):
        # The pretty-print signature is the render; the Documents text filter's
        # single-line haystack (`fold(JSON.stringify(d))`) is not shown to anyone.
        assert ", null, 2)" not in chunk, name
        assert "'pre'" not in chunk, name
    # 2026-09-17 review, D3: single-line dumps rendered too — `_libraryList`
    # stringified non-string items and the review panel printed
    # `Current: ${JSON.stringify(field.current)}`. The primitives never
    # stringify, and no rendered text in the two mounts embeds a dump; request
    # bodies and the confirm text for a reviewed JSON value remain.
    primitives = source[source.index("function _libraryFacts("):source.index("/** Shared Library entry points")]
    assert "JSON.stringify(" not in primitives
    assert "JSON.stringify(field." not in review
    assert "JSON.stringify(d)" not in library

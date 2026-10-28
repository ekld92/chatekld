"""The disposable evaluation corpus (phase 5): its planted errors are real,
its traps are named, and the deterministic authentication already sees them."""
import json

import importlib.util
import pytest

if importlib.util.find_spec('prototypes') is None:
    pytest.skip('Private prototype fixtures are absent from public source artifacts', allow_module_level=True)

from prototypes.bibliography_corpus.build import build, CITED, TITLES


def test_corpus_plants_the_errors_it_declares(tmp_path):
    build(tmp_path / "corpus")
    root = tmp_path / "corpus"
    corpus = {r["id"]: r for r in json.loads((root / "corpus.json").read_text())}
    key = json.loads((root / "answer_key.json").read_text())
    assert set(corpus) == set(TITLES) == set(key)
    assert corpus["wrong_year"]["current"]["date"] == "2023" and key["wrong_year"]["planted"][0]["printed"] == "2024"
    assert corpus["wrong_journal"]["current"]["publicationTitle"] == "Journal of Fixtures"
    assert corpus["cited_doi"]["current"]["DOI"] == CITED
    assert [c["lastName"] for c in corpus["swapped"]["current"]["creators"]] == ["Martin", "Noether"]
    assert {s["kind"] for s in corpus["contradict"]["sources"]} == {"pdf_passage", "catalogue_metadata"}
    assert corpus["correct"]["claim"] and key["correct"]["claim"][1] == "supported"
    # Inline sources make the corpus usable without pointing the app at this folder.
    assert all("pdf" not in r for r in corpus.values())
    assert all(len(json.dumps(r)) < 60000 for r in corpus.values())


def test_deterministic_authentication_sees_the_cited_doi_trap(tmp_path):
    summary = build(tmp_path / "corpus")
    assert all(v == "authenticated" for v in summary["authentication_statuses"].values())
    assert summary["doi_statuses"]["cited_doi"] == "reference_list_only"
    assert all(v == "front_matter" for k, v in summary["doi_statuses"].items() if k != "cited_doi")
    reports = json.loads((tmp_path / "corpus" / "authentication.json").read_text())
    assert reports["swapped"]["authors"]["found"] == ["Martin", "Noether"]
    assert (tmp_path / "corpus" / "DISPOSABLE").exists() and (tmp_path / "corpus" / "README.txt").exists()

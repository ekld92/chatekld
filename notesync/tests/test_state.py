"""``notesync.state`` — the 2×2 sync table, the bootstrap rule and the base record."""

from __future__ import annotations

import json

import pytest

from notesync import base_store
from notesync.state import BaseRecord, bootstrap_state, classify, equivalent, norm_text, sha256_text

CONV = "conv/1"


def _base(region="alpha beta", html="<p>alpha beta</p>", conv=CONV):
    return BaseRecord(citation_key="k", item_key="ABCD1234", note_key="WXYZ5678",
                      zotero_version=10, base_html=html, base_region=region,
                      synced_at="2026-09-04T00:00:00Z", converter_id=conv)


class TestTable:
    def test_in_sync(self):
        d = classify(base=_base(), zotero_md_now="alpha beta", zotero_md_base="alpha beta",
                     region_now="alpha beta", converter_id=CONV)
        assert d.state == "in_sync" and d.baselined

    def test_pull(self):
        d = classify(base=_base(), zotero_md_now="alpha beta gamma", zotero_md_base="alpha beta",
                     region_now="alpha beta", converter_id=CONV)
        assert (d.state, d.zotero_changed, d.region_changed) == ("pull", True, False)
        assert d.missing_words == 1

    def test_push(self):
        d = classify(base=_base(), zotero_md_now="alpha beta", zotero_md_base="alpha beta",
                     region_now="alpha beta mine", converter_id=CONV)
        assert (d.state, d.zotero_changed, d.region_changed) == ("push", False, True)
        assert d.extra_words == 1

    def test_conflict(self):
        d = classify(base=_base(), zotero_md_now="alpha beta theirs", zotero_md_base="alpha beta",
                     region_now="alpha beta mine", converter_id=CONV)
        assert d.state == "conflict"

    def test_whitespace_only_differences_are_not_changes(self):
        d = classify(base=_base(region="alpha beta\n"), zotero_md_now="alpha beta  \n",
                     zotero_md_base="alpha beta", region_now="alpha beta\r\n", converter_id=CONV)
        assert d.state == "in_sync"

    def test_other_converter_means_unbaselined(self):
        d = classify(base=_base(conv="old"), zotero_md_now="alpha beta", zotero_md_base="alpha beta",
                     region_now="alpha beta", converter_id=CONV)
        assert d.state == "unbaselined" and not d.baselined

    def test_no_base_means_unbaselined_with_bootstrap_counts(self):
        d = classify(base=None, zotero_md_now="alpha beta gamma", zotero_md_base=None,
                     region_now="alpha beta", converter_id=CONV)
        assert d.state == "unbaselined" and d.missing_words == 1 and d.zotero_changed


class TestBootstrap:
    @pytest.mark.parametrize("z,r,state", [
        ("alpha beta", "alpha beta", "in_sync"),
        ("alpha beta", "- alpha beta", "push"),          # the user's bullet
        ("alpha beta gamma", "alpha beta", "pull"),
        ("alpha beta gamma", "alpha beta mine", "conflict"),
    ])
    def test_rule(self, z, r, state):
        assert bootstrap_state(z, r).state == state


class TestEquivalentAfterConverter:
    """Plan Phase 5: the two sides are compared AFTER the current converter.
    Measured 2026-09-18, 633 of 670 live notes differed only in Markdown
    spelling (``n=29`` against the converter's ``n\\=29``)."""

    def test_spelling_only_difference_is_equal_through_the_rendering(self):
        assert equivalent("n\\=29 studies", "n=29 studies", region_rendered="n\\=29 studies")
        assert not equivalent("n\\=29 studies", "n=29 studies")   # literal test alone

    def test_a_round_trip_that_drops_words_never_certifies_equality(self):
        # A renderer that lost "secret" must not make the region look equal.
        assert not equivalent("alpha", "alpha secret", region_rendered="alpha")

    def test_classify_and_bootstrap_use_it(self):
        d = classify(base=_base(region="n=29"), zotero_md_now="n\\=29", zotero_md_base="n\\=29",
                     region_now="n=29", converter_id=CONV, region_rendered="n\\=29")
        assert d.state == "in_sync"
        assert bootstrap_state("n\\=29", "n=29", "n\\=29").state == "in_sync"


class TestBaseRecord:
    def test_json_roundtrip_and_digests(self):
        b = _base()
        d = b.to_json()
        assert d["schema"] == 1
        assert d["zotero_html_sha256"] == sha256_text(b.base_html)
        assert BaseRecord.from_json(d) == BaseRecord(**{**b.__dict__,
                                                        "zotero_html_sha256": sha256_text(b.base_html),
                                                        "region_sha256": sha256_text(b.base_region)})

    def test_tampered_text_loads_as_none(self):
        d = _base().to_json()
        d["base_region"] = "someone else's bytes"
        assert BaseRecord.from_json(d) is None

    @pytest.mark.parametrize("bad", [None, [], {"schema": 2}, {"schema": 1, "citation_key": "k"}])
    def test_malformed_is_none(self, bad):
        assert BaseRecord.from_json(bad) is None

    def test_norm_text(self):
        assert norm_text("a \r\nb  \n\n") == "a\nb"
        assert norm_text(None) == ""


class TestStore:
    def test_save_load_delete(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        b = _base()
        p = base_store.save(vault, b)
        assert p.exists() and str(base_store.store_dir(vault)) in str(p)
        assert base_store.load(vault, "k") == BaseRecord.from_json(b.to_json())
        assert base_store.load(vault, "other") is None
        assert base_store.load_all(vault) == {"k": BaseRecord.from_json(b.to_json())}
        assert base_store.delete(vault, "k") is True
        assert base_store.delete(vault, "k") is False
        assert base_store.load(vault, "k") is None

    def test_corrupt_file_loads_as_none(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        p = base_store.save(vault, _base())
        p.write_text("{not json", encoding="utf-8")
        assert base_store.load(vault, "k") is None
        assert base_store.load_all(vault) == {}

    def test_foreign_record_under_this_key_is_refused(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        p = base_store.save(vault, _base())
        d = json.loads(p.read_text(encoding="utf-8"))
        d["citation_key"] = "someone-else"
        p.write_text(json.dumps(d), encoding="utf-8")
        assert base_store.load(vault, "k") is None

    def test_key_never_shapes_the_path(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        b = BaseRecord(**{**_base().__dict__, "citation_key": "../../evil/../k"})
        p = base_store.save(vault, b)
        assert p.parent == base_store.store_dir(vault)


@pytest.mark.parametrize('left,right', [
    ('alpha beta', 'beta alpha'),
    ('dose 10 mg', 'dose 20 mg'),
    ('[source](https://a.example)', '[source](https://b.example)'),
    ('alpha alpha beta', 'alpha beta'),
])
def test_unchanged_divergent_base_never_certifies_equality(left, right):
    d = classify(base=_base(region=right), zotero_md_now=left, zotero_md_base=left,
                 region_now=right, converter_id=CONV)
    assert d.state == 'conflict'
    assert not d.zotero_changed and not d.region_changed
    assert bootstrap_state(left, right).state != 'in_sync'

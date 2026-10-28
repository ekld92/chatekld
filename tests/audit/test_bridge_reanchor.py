"""The scan-time curated-mapping re-anchor must not overwrite a FRESHER anchor.

DEFECT PINNED (performance plan, Phase 5): `refresh_mapping_hashes` loads the
map, hashes every curated file (O(curated) full-file reads), then re-loads
under the RMW lock and wrote back on PRESENCE alone. A `POST
/api/audit/mapping` landing in that window re-anchors a PDF to its CURRENT
bytes; the refresh then put its own older digest back, silently reverting the
curation the user had just made.

Asserted on the FILE — the level both the pre- and post-change code reach.
"""

from __future__ import annotations

import json
from pathlib import Path

from audit.engine import bridge


def _read(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _fixture(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / "Z_attachments").mkdir(parents=True)
    pdf = vault / "Z_attachments" / "smith_2020.pdf"
    pdf.write_bytes(b"%PDF-1.4 original")
    mapping = tmp_path / "mapping.json"
    bridge.add_match(mapping, pdf, "smith2020", vault)
    return vault, pdf, mapping


def test_a_concurrent_reanchor_is_not_reverted(tmp_path: Path, monkeypatch) -> None:
    vault, pdf, mapping = _fixture(tmp_path)
    rel = "Z_attachments/smith_2020.pdf"

    real_hash_entry = bridge._hash_entry
    fired = {"n": 0}

    def _hash_entry_then_concurrent_write(path: Path):
        """Hash as usual, then simulate the POST that lands mid-refresh."""
        entry = real_hash_entry(path)
        if fired["n"] == 0:
            fired["n"] = 1
            # The user annotates the PDF and re-curates it: a NEWER anchor.
            pdf.write_bytes(b"%PDF-1.4 annotated by the user")
            bridge.add_match(mapping, pdf, "smith2020", vault)
        return entry

    monkeypatch.setattr(bridge, "_hash_entry", _hash_entry_then_concurrent_write)
    fresh = _read(mapping)  # before the refresh runs
    bridge.refresh_mapping_hashes(mapping, vault)

    after = _read(mapping)["hashes"][rel]
    expected = real_hash_entry(pdf)          # the concurrent write's digest
    # Assert the DEFECT first, so a failure names it rather than reading as a
    # precondition problem (pre-change, `after` is the reverted original).
    assert after["sha256"] == expected["sha256"], (
        "the refresh reverted a fresher anchor to its own older digest")
    assert expected["sha256"] != fresh["hashes"][rel]["sha256"], (
        "precondition: the concurrent write must change the anchor")


def test_an_untouched_entry_still_refreshes(tmp_path: Path) -> None:
    """The feature itself: annotate → the next scan re-anchors."""
    vault, pdf, mapping = _fixture(tmp_path)
    rel = "Z_attachments/smith_2020.pdf"
    before = _read(mapping)["hashes"][rel]["sha256"]

    pdf.write_bytes(b"%PDF-1.4 annotated, not re-curated")
    updated = bridge.refresh_mapping_hashes(mapping, vault)

    assert updated == 1
    assert _read(mapping)["hashes"][rel]["sha256"] != before


def test_an_entry_curated_away_mid_refresh_is_not_resurrected(tmp_path: Path,
                                                              monkeypatch) -> None:
    vault, pdf, mapping = _fixture(tmp_path)
    rel = "Z_attachments/smith_2020.pdf"
    real_hash_entry = bridge._hash_entry

    def _hash_then_clear(path: Path):
        entry = real_hash_entry(path)
        bridge.clear_curation(mapping, pdf, vault)
        return entry

    monkeypatch.setattr(bridge, "_hash_entry", _hash_then_clear)
    bridge.refresh_mapping_hashes(mapping, vault)
    assert rel not in _read(mapping).get("hashes", {})

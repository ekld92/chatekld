"""macOS reassigns a volume's device number (st_dev) at boot (2026-09-17).

After the maintainer's Mac restarted, the device number of the data volume
changed from 16777229 to 16777230. Every stored file identity still carried
the old number, so:

* the PDF reconciliation workspace called 6,399 unchanged PDFs "replaced"
  (Discrepancies);
* every saved PDF review read as "changed" and lost export eligibility;
* restoring a file from recoverable storage would have been refused;
* the attachment-intent barrier hashed the number into its reservation, so a
  retry after a restart reserved a NEW key — silently disarming the guard that
  stops Zotero from creating the same attachment twice.

A file's identity across time is its content hash, inode and birth time; the
device number is kept in records (the same-volume check needs the CURRENT
value) but never compared across time.
"""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from accession import pdf_management as pm, reconciliation as rec, review
from tagging import attachment_intents
from test_pdf_management import copies, proposal, _readable_annotation_counts  # noqa: F401
from test_pdf_reconciliation import row, world  # noqa: F401


def _reboot(identity):
    """The same file as seen after macOS renumbered its volume."""
    return {**identity, "dev": identity["dev"] + 1}


def test_same_identity_ignores_only_the_device_number():
    a = dict(dev=1, ino=2, size=3, mtime=4, birth=5, sha256="x")
    assert review.same_identity(a, _reboot(a))
    for key, value in (("ino", 9), ("size", 9), ("mtime", 9), ("birth", 9), ("sha256", "y")):
        assert not review.same_identity(a, {**a, key: value}), key
    assert not review.same_identity(a, None)


def test_reconciliation_does_not_call_a_renumbered_volume_a_replacement(world):  # noqa: F811
    s, _pdf = world
    rec.refresh(s)
    before = row(s)["state"]
    saved = rec.saved(s)
    saved["physical"] = {rel: _reboot(p) for rel, p in saved["physical"].items()}
    with rec.connection(True) as con:
        con.execute("UPDATE snapshots SET data=? WHERE scope=?", (json.dumps(saved), rec.scope(s)))
    assert before != "Discrepancies"
    assert row(s)["state"] == before


def test_a_saved_review_stays_verified_after_a_reboot(world):  # noqa: F811
    s, pdf = world
    rel = pdf.relative_to(s.vault_root).as_posix()
    record = review.open_review(s.vault_root, rel, review.identity(pdf), None)
    with review.connection(True) as con:
        stored = json.loads(con.execute("SELECT data FROM files").fetchone()[0])
        stored["fingerprint"] = _reboot(stored["fingerprint"])
        con.execute("UPDATE files SET data=?", (json.dumps(stored),))
    current = review.snapshot(s.vault_root)[rel]
    assert review.public(current, pdf)["identity_state"] == "verified"
    review.effective_resolution(s.vault_root, rel, review.identity(pdf), None)
    reopened = review.open_review(s.vault_root, rel, review.identity(pdf), None)
    assert reopened["revision"] == record["revision"]


def test_a_held_file_can_be_restored_after_a_reboot(copies):  # noqa: F811
    s, keep, remove = copies
    token = proposal(copies)["token"]
    op = pm.apply(s, "KeyOne", keep.relative_to(s.vault_root).as_posix(),
                  {remove.relative_to(s.vault_root).as_posix(): "delete"}, token, confirm=True)
    assert op["status"] == "complete"
    for step in op["steps"]:
        step["physical"] = _reboot(step["physical"])
    pm._save(op)
    assert pm.restore(s, op["id"], confirm=True)["status"] == "restored"
    assert remove.exists()


def test_the_bridge_fingerprint_guard_ignores_the_device_number(tmp_path):
    from audit.engine import bridge
    pdf = tmp_path / "vault" / "a_2020.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF-x")
    fp = {k: v for k, v in review.identity(pdf).items() if k != "sha256"}
    mapping = tmp_path / "mapping.json"
    bridge.add_matches(mapping, [(pdf, "a2020")], pdf.parent, expected_fingerprint=_reboot(fp))
    assert json.loads(mapping.read_text())["matches"]["a2020"] == ["a_2020.pdf"]
    with pytest.raises(ValueError):
        bridge.add_matches(mapping, [(pdf, "b2020")], pdf.parent,
                           expected_fingerprint={**fp, "ino": fp["ino"] + 1})


def _intent():
    return dict(transport=SimpleNamespace(base_url="http://127.0.0.1:23119"),
                server_id="database-one", lib_path="users/0", parent_key="PARENT1",
                path="/fixture/one.pdf", review_token={"id": "file-one"},
                expected_identity={"dev": 1, "ino": 2, "birth": 3, "sha256": "abc", "mtime": 4})


def test_an_attachment_intent_survives_a_reboot(tmp_path, monkeypatch):
    monkeypatch.setattr(attachment_intents, "store_path", lambda: tmp_path / "intents.sqlite3")
    c = _intent()
    key = attachment_intents.reserve(tmp_path, c)
    c["expected_identity"] = _reboot(c["expected_identity"])
    assert attachment_intents.reserve(tmp_path, c, create=False) == key
    assert attachment_intents.reserve(tmp_path, c) == key
    other = _intent()
    other["expected_identity"] = {**other["expected_identity"], "ino": 99}
    assert attachment_intents.reserve(tmp_path, other) != key


def test_a_legacy_intent_hashed_with_the_old_device_number_is_still_found(tmp_path, monkeypatch):
    """Reservations written before this fix hashed `dev` into their identity;
    a post-reboot retry must find them instead of minting a second key."""
    import hashlib
    from pathlib import Path
    path = tmp_path / "intents.sqlite3"
    monkeypatch.setattr(attachment_intents, "store_path", lambda: path)
    c = _intent()
    target = dict(vault=str(Path(tmp_path).resolve()), base=c["transport"].base_url,
                  instance=c["server_id"], library=c["lib_path"], parent=c["parent_key"],
                  path=c["path"], identity=c["expected_identity"])
    stable = {k: v for k, v in target.items() if k not in ("path", "identity")}
    stable["file"] = "file-one"
    stable["identity"] = {k: target["identity"][k] for k in ("dev", "ino", "birth", "sha256")}
    legacy = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE intents (identity TEXT PRIMARY KEY, item_key TEXT NOT NULL UNIQUE, target TEXT NOT NULL)")
        con.execute("INSERT INTO intents VALUES (?,?,?)", (legacy, "LEGACY01", json.dumps(target, sort_keys=True)))
    c["expected_identity"] = _reboot(c["expected_identity"])
    assert attachment_intents.reserve(tmp_path, c, create=False) == "LEGACY01"
    assert attachment_intents.reserve(tmp_path, c) == "LEGACY01"
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 1

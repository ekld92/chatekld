"""Bibliography metadata executor (phase 5, 2026-09-16): approved fields only,
version-guarded through the shared transport, journalled, revertible, and the
Zotero-side normalisation named rather than hidden.

The transport is a fake ``zotero_write`` shim patched at the WRITERS' name
(``tagging.writers.zotero_write``) — the consumer's binding, never the module
that defines it — with a ``promote`` switch that reproduces the 10.0.1
observation: a PATCH that leaves ``citationKey`` empty while ``extra`` carries
``Citation Key: x`` comes back with ``citationKey == "x"``.
"""
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from accession import bibliography_campaign, metadata_executor
from notesync import campaigns
from tagging import journal as tjournal, writers


class _FakeItems:
    def __init__(self, items, *, promote=False):
        self.items = {k: {"data": dict(v), "version": v.get("version", 1)} for k, v in items.items()}
        self.calls = []
        self.promote = promote

    def get_item(self, base, lib, key, timeout=None, api_key=None, server_id=None):
        from tagging.local_api import Version
        from tagging.zotero_write import ZoteroError
        it = self.items.get(key)
        if it is None:
            raise ZoteroError("HTTP 404")
        return dict(it["data"]), Version(it["version"], "fixture-db")

    def set_item_fields(self, base, lib, key, fields, version, timeout=None, api_key=None):
        from tagging.zotero_write import ZoteroVersionConflict
        it = self.items[key]
        if int(it["version"]) != int(version):
            raise ZoteroVersionConflict("412")
        it["data"].update(fields)
        if self.promote and not it["data"].get("citationKey"):
            pinned = writers.extra_citation_key(it["data"].get("extra"))
            if pinned:
                it["data"]["citationKey"] = pinned
        it["version"] += 1
        self.calls.append(("patch", key, dict(fields)))
        return it["version"]

    def api_key_from_env(self):
        return None


@pytest.fixture
def scope(tmp_path, monkeypatch):
    monkeypatch.setattr("core.constants.BASE_DIR", str(tmp_path))
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = {"tagging_zotero_transport": "local", "tagging_archive_dir": str(tmp_path / "archive")}
    return vault, cfg


def _shim(fake):
    from tagging import zotero_write as real
    shim = SimpleNamespace(get_item=fake.get_item, set_item_fields=fake.set_item_fields,
                           api_key_from_env=fake.api_key_from_env, API_KEY_ENV=real.API_KEY_ENV,
                           METADATA_RESERVED_FIELDS=real.METADATA_RESERVED_FIELDS,
                           ZoteroUnavailable=real.ZoteroUnavailable, ZoteroError=real.ZoteroError,
                           ZoteroVersionConflict=real.ZoteroVersionConflict,
                           ZoteroWriteUnsupported=real.ZoteroWriteUnsupported)
    transport = SimpleNamespace(is_web=False, api_key=None, base_url="http://127.0.0.1:1", kind="local")
    stack = contextlib.ExitStack()
    stack.enter_context(patch("tagging.writers.zotero_write", shim))
    stack.enter_context(patch("tagging.writers._zotero_transport", return_value=transport))
    stack.enter_context(patch("tagging.writers._zotero_lib_path", return_value="users/0"))
    stack.enter_context(patch("tagging.local_api.origin", return_value=True))
    return stack


def _campaign(scope_path, *, current=None, proposed="Corrected title", extra_fields=None):
    """A bibliography campaign with one approved title field, without a model."""
    current = current or {"title": "Old title", "date": "2019"}
    data = dict(provider="ollama", model="fixture", max_tokens=128, corpus=[dict(
        id="rec", zotero_key="ITEM0001", server_id="fixture-db", current=current,
        sources=[dict(id="s1", locator="fixture:p1", text=f"The title reads {proposed}.")])])
    reply = [{"id": "rec", "fields": [{"field": "title", "proposed": proposed, "source_id": "s1", "excerpt": proposed},
                                       *(extra_fields or [])]}]
    with patch("core.llm.chat.stream_chat_messages", return_value=iter([json.dumps(reply)])):
        campaign = bibliography_campaign.prepare(data, scope_path, {})
    fields = campaign["outcomes"]["generation"]["proposals"][0]["fields"]
    decisions = [dict(record_id="rec", field=f["field"], evidence=f["evidence"], approve=True,
                      support_assessment="supported", reason="Stated verbatim in the source.") for f in fields]
    bibliography_campaign.save_decisions(campaign["id"], scope_path, decisions)
    return campaign["id"], [dict(record_id="rec", field=f["field"], evidence=f["evidence"]) for f in fields]


def test_writes_only_the_approved_field_journals_and_reads_back(scope):
    vault, cfg = scope
    identifier, decisions = _campaign(str(vault))
    fake = _FakeItems({"ITEM0001": {"itemType": "journalArticle", "title": "Old title", "date": "2019", "extra": "", "version": 7}})
    with _shim(fake), patch.object(metadata_executor, "application_available", return_value=True):
        record = metadata_executor.apply(identifier, str(vault), cfg, decisions, vault_root=vault, confirm=True)
    assert record["ok"] and record["status"] == "applied"
    assert fake.calls == [("patch", "ITEM0001", {"title": "Corrected title"})]
    assert fake.items["ITEM0001"]["data"]["date"] == "2019"
    result = record["results"]["rec"]
    assert result["status"] == "applied" and result["version"] == 8
    op = tjournal.find_op(tjournal.load(vault, cfg), result["op_id"])
    assert (op["kind"], op["state"], op["version_after"]) == ("zotero_metadata_write", "applied", 8)
    assert op["before_fields"] == {"title": "Old title"} and op["after_fields"] == {"title": "Corrected title"}
    assert campaigns.read(identifier)["outcomes"]["application"]["results"]["rec"]["status"] == "applied"
    # No replay of an already dispatched field.
    with _shim(fake), patch.object(metadata_executor, "application_available", return_value=True), \
            pytest.raises(ValueError, match="already dispatched"):
        metadata_executor.apply(identifier, str(vault), cfg, decisions, vault_root=vault, confirm=True)


def test_apply_itself_refuses_while_production_application_is_closed(scope):
    """2026-09-17 review, E3: the fixture tests patched application_available,
    which apply() never read — the 501 lived only in the route."""
    vault, cfg = scope
    identifier, decisions = _campaign(str(vault))
    fake = _FakeItems({"ITEM0001": {"itemType": "journalArticle", "title": "Old title", "date": "2019", "extra": "", "version": 7}})
    with _shim(fake), pytest.raises(RuntimeError, match="closed"):
        metadata_executor.apply(identifier, str(vault), cfg, decisions, vault_root=vault, confirm=True)
    assert fake.calls == []
    assert "application" not in campaigns.read(identifier)["outcomes"]


def test_refuses_a_field_that_changed_since_review_and_an_unapproved_field(scope):
    vault, cfg = scope
    identifier, decisions = _campaign(str(vault))
    fake = _FakeItems({"ITEM0001": {"itemType": "journalArticle", "title": "Edited meanwhile", "date": "2019", "version": 7}})
    with _shim(fake), patch.object(metadata_executor, "application_available", return_value=True):
        record = metadata_executor.apply(identifier, str(vault), cfg, decisions, vault_root=vault, confirm=True)
    assert record["results"]["rec"]["status"] == "stale"
    assert record["results"]["rec"]["detail"]["fields"]["title"] == dict(reviewed="Old title", current="Edited meanwhile")
    assert fake.calls == []
    with pytest.raises(ValueError, match="not approved"):
        metadata_executor.selected_writes(campaigns.read(identifier), [dict(record_id="rec", field="date", evidence="x")])
    with pytest.raises(ValueError, match="does not match"):
        metadata_executor.selected_writes(campaigns.read(identifier), [dict(decisions[0], evidence="stale")])


def test_predicted_citation_key_promotion_refuses_before_any_patch(scope):
    """Zotero 10.0.1: a title-only PATCH promoted `Extra: Citation Key:` into the
    native citationKey. The executor refuses BEFORE the PATCH unless
    citationKey is selected too, and names the field it would move."""
    vault, cfg = scope
    identifier, decisions = _campaign(str(vault))
    fake = _FakeItems({"ITEM0001": {"itemType": "journalArticle", "title": "Old title", "date": "2019",
                                    "extra": "Citation Key: smith2019\nPMID: 1", "citationKey": "", "version": 7}}, promote=True)
    with _shim(fake), patch.object(metadata_executor, "application_available", return_value=True):
        record = metadata_executor.apply(identifier, str(vault), cfg, decisions, vault_root=vault, confirm=True)
    result = record["results"]["rec"]
    assert result["status"] == "unselected_field_would_change"
    assert result["detail"]["predicted"][0]["field"] == "citationKey"
    assert result["detail"]["predicted"][0]["predicted"] == "smith2019"
    assert fake.calls == []
    assert fake.items["ITEM0001"]["data"]["citationKey"] == ""


def test_unpredicted_normalisation_is_recorded_on_the_op_and_reverted_whole(scope):
    """When Zotero moves an unselected field the writer could not predict, the
    outcome is not a success: it is journalled with before/after, and the revert
    restores the selected field AND the moved one."""
    vault, cfg = scope
    identifier, decisions = _campaign(str(vault))
    fake = _FakeItems({"ITEM0001": {"itemType": "journalArticle", "title": "Old title", "date": "2019",
                                    "extra": "", "citationKey": "", "language": "en", "version": 7}})
    original = fake.set_item_fields

    def normalising(base, lib, key, fields, version, **kw):
        result = original(base, lib, key, fields, version, **kw)
        if "language" not in fields:
            fake.items[key]["data"]["language"] = "eng"  # an unrequested normalisation, once
        return result
    fake.set_item_fields = normalising
    with _shim(fake), patch.object(metadata_executor, "application_available", return_value=True):
        record = metadata_executor.apply(identifier, str(vault), cfg, decisions, vault_root=vault, confirm=True)
    result = record["results"]["rec"]
    assert not record["ok"] and result["status"] == "unselected_field_changed"
    assert result["unexpected_changes"] == {"language": dict(before="en", after="eng")}
    op = tjournal.find_op(tjournal.load(vault, cfg), result["op_id"])
    assert op["state"] == "applied" and op["unexpected_changes"] == result["unexpected_changes"]
    with _shim(fake):
        undone = writers._revert_zotero_metadata(vault, cfg, op)
    assert undone["status"] == "reverted"
    assert fake.calls[-1] == ("patch", "ITEM0001", {"title": "Old title", "language": "en"})
    assert fake.items["ITEM0001"]["data"]["title"] == "Old title" and fake.items["ITEM0001"]["data"]["language"] == "en"


def test_revert_refuses_after_a_later_edit_and_skips_when_already_original(scope):
    vault, cfg = scope
    identifier, decisions = _campaign(str(vault))
    fake = _FakeItems({"ITEM0001": {"itemType": "journalArticle", "title": "Old title", "date": "2019", "version": 7}})
    with _shim(fake), patch.object(metadata_executor, "application_available", return_value=True):
        record = metadata_executor.apply(identifier, str(vault), cfg, decisions, vault_root=vault, confirm=True)
    op = tjournal.find_op(tjournal.load(vault, cfg), record["results"]["rec"]["op_id"])
    fake.items["ITEM0001"]["data"]["title"] = "Edited again by hand"
    with _shim(fake):
        refused = writers._revert_zotero_metadata(vault, cfg, op)
    assert refused["status"] == "skipped" and not refused["ok"] and op["state"] == "applied"
    fake.items["ITEM0001"]["data"]["title"] = "Old title"
    with _shim(fake):
        done = writers._revert_zotero_metadata(vault, cfg, op)
    assert done["status"] == "skipped" and done["ok"] and op["state"] == "reverted"


def test_version_conflict_is_journalled_failed_not_applied(scope):
    vault, cfg = scope
    identifier, decisions = _campaign(str(vault))
    fake = _FakeItems({"ITEM0001": {"itemType": "journalArticle", "title": "Old title", "date": "2019", "version": 7}})
    original = fake.get_item

    def racing_get(*args, **kwargs):
        data, version = original(*args, **kwargs)
        fake.items["ITEM0001"]["version"] = 9  # someone saves between read and PATCH
        return data, version
    fake.get_item = racing_get
    with _shim(fake), patch.object(metadata_executor, "application_available", return_value=True):
        record = metadata_executor.apply(identifier, str(vault), cfg, decisions, vault_root=vault, confirm=True)
    result = record["results"]["rec"]
    assert result["status"] == "conflict" and not record["ok"]
    assert tjournal.find_op(tjournal.load(vault, cfg), result["op_id"])["state"] == "failed"


def test_reserved_fields_and_extra_key_removal_are_refused():
    assert writers.extra_citation_key("PMID: 1\nCitation Key: abc\n") == "abc"
    assert writers.extra_citation_key("") is None
    data = {"extra": "Citation Key: abc", "citationKey": ""}
    assert writers.predicted_unselected_changes(data, {"title": "x"})[0]["reason"] == "extra_citation_key_promotion"
    assert writers.predicted_unselected_changes(data, {"title": "x", "citationKey": "abc"}) == []
    assert writers.predicted_unselected_changes({"extra": "Citation Key: abc", "citationKey": "abc"},
                                                {"extra": "PMID: 2"})[0]["reason"] == "extra_edit_drops_citation_key"
    early = writers.zotero_metadata_prepare({}, "K", {"itemType": "book"}, {})
    assert early["early"]["status"] == "invalid"


def test_route_gate_and_authentication_route(scope, tmp_path):
    """The apply route is 501 in production; the authenticate route is a pure
    read confined to the bibliography folder."""
    import fitz
    import flask
    from api.routes.audit import audit_bp
    vault, _cfg = scope
    biblio = vault / "biblio_articles"
    biblio.mkdir()
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Corrected title\nAda Test\ndoi:10.1000/x.1", fontsize=10)
    doc.save(str(biblio / "test_2020.pdf"))
    doc.close()
    app = flask.Flask(__name__)
    app.register_blueprint(audit_bp)
    client = app.test_client()
    headers = {"X-Requested-With": "ChatEKLD"}
    # The real ``load_settings`` runs; only the config it reads is supplied.
    cfg = {"obsidian_vault_path": str(vault), "audit_attachments_subdir": "."}
    with patch("core.config.load_config", return_value=cfg):
        gate = client.post("/api/audit/library/bibliography/x/apply", json={"confirm": True, "decisions": []}, headers=headers)
        assert gate.status_code == 501 and gate.get_json()["reason"] == "executor_validation_required"
        ok = client.post("/api/audit/library/bibliography/authenticate", json={"records": [
            {"id": "r", "pdf": "test_2020.pdf", "current": {"DOI": "10.1000/x.1", "title": "Corrected title", "creators": [{"lastName": "Test"}]}},
            {"id": "escape", "pdf": "../../etc/passwd", "current": {"title": "x"}}]}, headers=headers)
    body = ok.get_json()
    assert ok.status_code == 200 and body["reports"]["r"]["status"] == "authenticated"
    assert body["reports"]["escape"]["status"] == "unavailable" and "refused" in body["reports"]["escape"]["reason"]
    assert body["applicable"] is False


def test_plan_authenticates_a_named_pdf_before_the_model(scope):
    """A record naming its PDF is authenticated first; the located passages
    join its sources and the model is told the verdict as untrusted data."""
    import fitz
    vault, _cfg = scope
    biblio = vault / "biblio_articles"
    biblio.mkdir()
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Some other paper\nBy Someone Else", fontsize=10)
    doc.new_page().insert_text((72, 72), "Body text.", fontsize=10)
    doc.new_page().insert_text((72, 72), "References\n1. https://doi.org/10.1000/cited.9", fontsize=10)
    doc.save(str(biblio / "cited_2020.pdf"))
    doc.close()
    data = dict(provider="ollama", model="fixture", max_tokens=128, corpus=[dict(
        id="rec", pdf="cited_2020.pdf", current={"DOI": "10.1000/cited.9", "title": "Not this paper"},
        sources=[dict(id="s1", locator="fixture", text="x")])])
    with patch("core.llm.chat.stream_chat_messages", return_value=iter(["[]"])) as model:
        campaign = bibliography_campaign.prepare(data, str(vault), {}, pdf_paths={"rec": biblio / "cited_2020.pdf"})
    report = campaign["outcomes"]["pdf_authentication"]["reports"]["rec"]
    assert report["status"] == "reference_list_only"
    sent = json.loads(model.call_args.kwargs["messages"][0]["content"])[0]
    assert sent["pdf_authentication"]["status"] == "reference_list_only"
    assert any(s.get("kind") == "pdf_passage" for s in sent["sources"])


def test_no_replay_reads_every_earlier_application_batch(scope):
    """2026-09-17 review, F9: only the latest outcomes['application'] was read,
    so a field applied in an earlier batch became replayable after any later
    batch recorded over it."""
    vault, cfg = scope
    identifier, decisions = _campaign(str(vault))
    campaigns.record(identifier, "application", {"results": {"rec": {"fields": {"title": {"status": "applied"}}}}})
    campaigns.record(identifier, "application", {"results": {"other": {"fields": {"date": {"status": "applied"}}}}})
    with pytest.raises(ValueError, match="already dispatched"):
        metadata_executor.selected_writes(campaigns.read(identifier), decisions)


def test_reviewed_value_must_be_text_or_keep_the_current_shape(scope, tmp_path):
    """2026-09-17 review, F6: any JSON was stored as the reviewed value and
    later written to Zotero."""
    vault, cfg = scope
    identifier, decisions = _campaign(str(vault))
    campaign = campaigns.read(identifier)
    field = campaign["outcomes"]["generation"]["proposals"][0]["fields"][0]
    base = dict(record_id="rec", field="title", evidence=field["evidence"], approve=True,
                support_assessment="supported", reason="Stated verbatim in the source.")
    with pytest.raises(ValueError, match="shape"):
        bibliography_campaign.save_decisions(identifier, str(vault), [dict(base, reviewed_value={"title": "x"})])
    saved = bibliography_campaign.save_decisions(identifier, str(vault), [dict(base, reviewed_value="Edited title")])
    stored = saved["outcomes"]["review"]["decisions"][0]
    assert stored["reviewed_value"] == "Edited title" and len(stored["reviewed_value_sha256"]) == 64

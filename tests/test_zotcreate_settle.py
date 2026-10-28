"""Unblocking a PDF whose Web create never reached Zotero (2026-09-17).

The attempt store refuses to release a barrier on a missing search result,
and that stays true for every automatic path. This verb is the maintainer's
explicit override, and the tests pin what makes it safe: it acts only on an
UNACKNOWLEDGED Web attempt, only after a COMPLETE observation of the original
library (live items and trash) holds no item with the reserved title and no
attachment naming the file; a dry run writes nothing; the attempt is moved,
never deleted; and reinstating puts the barrier back.
"""

from unittest.mock import patch

import flask
import pytest

from accession import review
from audit.config import Settings
from zotcreate import attempts

H = {"X-Requested-With": "ChatEKLD"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "biblio_articles").mkdir(parents=True)
    pdf = vault / "biblio_articles" / "godefroy_2015.pdf"
    pdf.write_bytes(b"%PDF-godefroy")
    settings = Settings(vault_root=vault, attachments_subdir="", zotero_sqlite=tmp_path / "z.sqlite",
                        zotero_storage=tmp_path / "storage", mapping_file_override=tmp_path / "m.json")
    monkeypatch.setenv("ZOTERO_API_KEY", "k" * 24)
    from api.routes.zotcreate import zotcreate_bp
    app = flask.Flask(__name__)
    app.register_blueprint(zotcreate_bp)
    # The attempts listing prefers a cached scan's settings; pin "no scan" so
    # an inventory cached by an earlier test in the suite cannot leak in.
    with patch("core.constants.BASE_DIR", str(tmp_path / "app")), \
            patch("audit.config.load_settings", return_value=settings), \
            patch("api.routes.zotcreate.audit_manager.get_inventory", return_value=(None, None)):
        sha = review.identity(pdf)["sha256"]
        identity, stage, _ = attempts.reserve(
            vault, sha, pdf_rel="Z/godefroy_2015.pdf", item_type="book",
            title="Vocabulaire psychologique et psychiatrique",
            library="users/7666987", base="https://api.zotero.org")
        assert stage == attempts.STAGE_FRESH
        yield app.test_client(), vault, sha, identity


def _observed(items):
    return dict(base="https://api.zotero.org", library="users/7666987", version=42, complete=True,
                checked_at=1.0, counts={"items": len(items), "items/trash": 0}, items=items)


def _post(client, url, body):
    return client.post(url, json=body, headers=H)


def test_dry_run_shows_evidence_and_writes_nothing(env):
    client, vault, sha, _ = env
    with patch("accession.web_snapshot.observe", return_value=_observed({})) as obs:
        r = _post(client, "/api/zotcreate/settle-never-created",
                  {"pdf": "biblio_articles/godefroy_2015.pdf", "dry_run": True})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["evidence"]["observed_version"] == 42
    obs.assert_called_once_with("https://api.zotero.org", "users/7666987", api_key="k" * 24)
    assert attempts.lookup(vault, sha)[1] == attempts.STAGE_PARENT_UNKNOWN


def test_confirm_retires_with_evidence_and_reinstate_restores_the_barrier(env):
    client, vault, sha, identity = env
    with patch("accession.web_snapshot.observe", return_value=_observed({})):
        r = _post(client, "/api/zotcreate/settle-never-created",
                  {"pdf": "biblio_articles/godefroy_2015.pdf", "confirm": True})
    assert r.status_code == 200, r.get_json()
    seq = r.get_json()["retired"]
    assert attempts.lookup(vault, sha)[1] == attempts.STAGE_FRESH
    [retired] = attempts.list_retired(vault)
    assert retired["seq"] == seq and retired["evidence"]["title_matches"] == []
    listing = client.get("/api/zotcreate/attempts", headers=H).get_json()
    assert listing["count"] == 0 and listing["retired"][0]["seq"] == seq
    assert _post(client, "/api/zotcreate/reinstate-attempt", {"seq": seq}).status_code == 400
    r = _post(client, "/api/zotcreate/reinstate-attempt", {"seq": seq, "confirm": True})
    assert r.status_code == 200
    assert attempts.lookup(vault, sha)[1] == attempts.STAGE_PARENT_UNKNOWN
    assert attempts.list_retired(vault) == []


@pytest.mark.parametrize("item", [
    {"itemType": "book", "title": "VOCABULAIRE psychologique et psychiatrique", "deleted": False},
    {"itemType": "book", "title": "Vocabulaire psychologique et psychiatrique", "deleted": True},
    {"itemType": "attachment", "path": "/x/biblio_articles/godefroy_2015.pdf", "deleted": False},
])
def test_any_trace_in_the_library_keeps_it_blocked(env, item):
    client, vault, sha, _ = env
    with patch("accession.web_snapshot.observe", return_value=_observed({"ABCD1234": item})):
        r = _post(client, "/api/zotcreate/settle-never-created",
                  {"pdf": "biblio_articles/godefroy_2015.pdf", "confirm": True})
    assert r.status_code == 409
    assert r.get_json()["reason"] == "possible_existing_item"
    assert attempts.lookup(vault, sha)[1] == attempts.STAGE_PARENT_UNKNOWN


def test_an_acknowledged_parent_is_never_retired(env):
    client, vault, sha, identity = env
    attempts.acknowledge_parent(identity, key="PARENT01")
    with patch("accession.web_snapshot.observe") as obs:
        r = _post(client, "/api/zotcreate/settle-never-created",
                  {"pdf": "biblio_articles/godefroy_2015.pdf", "confirm": True})
    assert r.status_code == 409
    obs.assert_not_called()
    with pytest.raises(RuntimeError):
        attempts.retire_never_created(identity, evidence={})


def test_an_unconfirmed_request_and_a_failed_observation_change_nothing(env, monkeypatch):
    client, vault, sha, _ = env
    assert _post(client, "/api/zotcreate/settle-never-created",
                 {"pdf": "biblio_articles/godefroy_2015.pdf"}).status_code == 400
    from tagging import zotero_write
    with patch("accession.web_snapshot.observe",
               side_effect=zotero_write.ZoteroError("Web library changed during observation.")):
        r = _post(client, "/api/zotcreate/settle-never-created",
                  {"pdf": "biblio_articles/godefroy_2015.pdf", "confirm": True})
    assert r.status_code == 409
    monkeypatch.delenv("ZOTERO_API_KEY")
    assert _post(client, "/api/zotcreate/settle-never-created",
                 {"pdf": "biblio_articles/godefroy_2015.pdf", "dry_run": True}).status_code == 503
    assert attempts.lookup(vault, sha)[1] == attempts.STAGE_PARENT_UNKNOWN


def test_reinstate_without_a_store_is_a_clean_refusal(tmp_path):
    from api.routes.zotcreate import zotcreate_bp
    app = flask.Flask(__name__)
    app.register_blueprint(zotcreate_bp)
    with patch("core.constants.BASE_DIR", str(tmp_path / "nothing")):
        r = app.test_client().post("/api/zotcreate/reinstate-attempt",
                                   json={"seq": 1, "confirm": True}, headers=H)
    assert r.status_code == 409
    assert not (tmp_path / "nothing").exists()

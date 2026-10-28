"""``POST /api/audit/pdf-facts`` — read-only per-PDF evidence (annual cleanup,
2026-09-17).

A duplicate decision needs, per file: bytes (sha256), page count, the
provenance class the reports reason about, and the DOI the document prints
about itself. ``zotcreate``'s preview computes these for ORPHANS only; this
route answers the same questions for any biblio PDF, through the same helpers,
so the numbers in a decision file are the numbers the app shows.
"""
from unittest.mock import patch

import flask
import pytest

HEADERS = {"X-Requested-With": "ChatEKLD"}
BIBLIO = "Z_attachments/biblio_articles"


@pytest.fixture
def env(tmp_path):
    from api.routes.audit import audit_bp
    vault = tmp_path / "vault"
    biblio = vault / BIBLIO
    biblio.mkdir(parents=True)
    (biblio / "smith_2020.pdf").write_bytes(b"%PDF-1.4 smith")
    (vault / "outside.pdf").write_bytes(b"%PDF-1.4 outside")
    app = flask.Flask(__name__)
    app.register_blueprint(audit_bp)
    cfg = {"obsidian_vault_path": str(vault)}
    with patch("core.config.load_config", return_value=cfg), \
            patch("api.routes.zotcreate._provenance",
                  return_value={"klass": "article", "pages": 7}), \
            patch("api.routes.zotcreate._page1_text",
                  return_value="Title\nhttps://doi.org/10.1000/xyz123 more text"):
        yield app.test_client(), vault


def test_facts_for_a_biblio_pdf(env):
    client, _vault = env
    r = client.post("/api/audit/pdf-facts", json={"pdfs": [f"{BIBLIO}/smith_2020.pdf"]},
                    headers=HEADERS)
    assert r.status_code == 200, r.get_data(as_text=True)
    (fact,) = r.get_json()["facts"]
    assert fact["rel"] == f"{BIBLIO}/smith_2020.pdf"
    assert fact["biblio_rel"] == "smith_2020.pdf"
    assert fact["pages"] == 7 and fact["klass"] == "article"
    assert fact["printed_identifier"] == "doi:10.1000/xyz123"
    assert len(fact["sha256"]) == 64 and fact["size"] == len(b"%PDF-1.4 smith")
    assert "Title" in fact["page1_excerpt"]


def test_a_path_outside_the_biblio_folder_is_refused_per_row(env):
    client, _vault = env
    r = client.post("/api/audit/pdf-facts",
                    json={"pdfs": ["outside.pdf", f"{BIBLIO}/../../outside.pdf",
                                   f"{BIBLIO}/missing.pdf"]}, headers=HEADERS)
    assert r.status_code == 200
    facts = r.get_json()["facts"]
    assert [f.get("error") is not None for f in facts] == [True, True, True]
    assert all("sha256" not in f for f in facts)


def test_bounds_and_shape(env):
    client, _vault = env
    assert client.post("/api/audit/pdf-facts", json={"pdfs": []},
                       headers=HEADERS).status_code == 400
    assert client.post("/api/audit/pdf-facts", json={"pdfs": "x.pdf"},
                       headers=HEADERS).status_code == 400
    many = [f"{BIBLIO}/f{i}.pdf" for i in range(51)]
    assert client.post("/api/audit/pdf-facts", json={"pdfs": many},
                       headers=HEADERS).status_code == 400


def test_refused_while_a_scan_runs(env):
    client, _vault = env
    with patch("api.routes.audit.audit_manager.is_scanning", return_value=True):
        r = client.post("/api/audit/pdf-facts", json={"pdfs": [f"{BIBLIO}/smith_2020.pdf"]},
                        headers=HEADERS)
    assert r.status_code == 409

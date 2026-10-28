"""The bibliography routes build their audit ``Settings`` through the REAL
``audit.config.load_settings`` signature (2026-09-17 review, A1).

Every earlier test patched ``api.routes.audit.load_settings`` with a
``return_value``, which accepts any argument — so ``load_settings(load_config())``
(a ``TypeError`` against the zero-argument function) shipped green while each of
these routes answered 500. These tests patch only the config the real function
reads (``core.config.load_config``, the name ``load_settings`` imports at call
time), never ``load_settings`` itself.
"""
from unittest.mock import patch

import flask
import pytest

HEADERS = {"X-Requested-With": "ChatEKLD"}


@pytest.fixture
def client(tmp_path):
    from api.routes.audit import audit_bp
    vault = tmp_path / "vault"
    (vault / "Z_attachments" / "biblio_articles").mkdir(parents=True)
    app = flask.Flask(__name__)
    app.register_blueprint(audit_bp)
    cfg = {"obsidian_vault_path": str(vault)}
    with patch("core.config.load_config", return_value=cfg):
        yield app.test_client()


def test_authenticate_reaches_real_settings(client):
    r = client.post("/api/audit/library/bibliography/authenticate",
                    json={"records": [{"id": "r", "pdf": "missing.pdf", "current": {"title": "x"}}]}, headers=HEADERS)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["reports"]["r"]["status"] == "unavailable"


def test_plan_reaches_real_settings(client):
    body = dict(provider="ollama", model="fixture", max_tokens=128,
                corpus=[dict(id="r", pdf="../escape.pdf", current={"title": "x"},
                             sources=[dict(id="s", locator="l", text="t")])])
    r = client.post("/api/audit/library/bibliography/plan", json=body, headers=HEADERS)
    # The escaping PDF is refused by the resolver, which needs real Settings.
    assert r.status_code == 400, r.get_data(as_text=True)
    assert "r:" in r.get_json()["error"]


def test_decisions_reaches_real_settings(client):
    r = client.post("/api/audit/library/bibliography/nonexistent/decisions",
                    json={"confirm": True, "decisions": []}, headers=HEADERS)
    assert r.status_code != 500, r.get_data(as_text=True)


def test_apply_reaches_real_settings_when_the_gate_is_open(client):
    with patch("accession.metadata_executor.application_available", return_value=True):
        r = client.post("/api/audit/library/bibliography/nonexistent/apply",
                        json={"confirm": True, "decisions": []}, headers=HEADERS)
    assert r.status_code != 500, r.get_data(as_text=True)

"""Unit tests for the /api/health probe (TTL-cached, lock-safe accessors)."""
from unittest import mock

import pytest
from flask import Flask

import api.routes.status as status_mod
from api.routes.status import status_bp


@pytest.fixture
def client():
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(status_bp)

    # Item 4.1: origin guard is now a before_request hook; register it on
    # the test app and patch at the canonical source location.
    from api.security import register_origin_guard
    register_origin_guard(app)

    # Each test starts with a cold health cache — the TTL would otherwise
    # serve one test's payload to the next.
    status_mod._health_cache["payload"] = None
    status_mod._health_cache["at"] = 0.0

    with mock.patch("api.security.origin_is_local", return_value=True):
        yield app.test_client()


def _healthy_mocks():
    """Patches for a fully-healthy probe; individual tests override pieces."""
    mock_prov = mock.Mock()
    mock_prov.check_running.return_value = (True, None)
    # A4: _compute_health now also calls get_models() on a RUNNING local provider
    # to flag "up but no models". A healthy install has models, so return a
    # non-empty list here; the no-models test below overrides it with ([], None).
    mock_prov.get_models.return_value = (["llama3.2"], None)
    return [
        mock.patch("rag.vault.obsidian_manager.get_vault_path", return_value="/tmp/vault"),
        mock.patch("os.path.isdir", return_value=True),
        # -1 = no lancedb table => the simple/docstore branch runs.
        mock.patch("rag.lancedb_store.lancedb_table_count", return_value=-1),
        mock.patch("rag.vault.obsidian_manager.docstore_doc_count", return_value=42),
        mock.patch("core.config.load_config", return_value={"provider": "ollama"}),
        mock.patch("api.routes.status.get_provider", return_value=mock_prov),
    ]


def _run(client, patches):
    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        res = client.get("/api/health")
    return res


def test_api_health_success(client):
    res = _run(client, _healthy_mocks())
    assert res.status_code == 200
    data = res.json
    assert data["status"] == "ok"
    assert data["details"]["vector_store"]["status"] == "ok"
    assert data["details"]["local_model"]["status"] == "ok"


def test_api_health_running_but_no_models_is_degraded(client):
    # A4: a runner that is UP but has zero models installed can't index or chat.
    # Surface an actionable degraded hint (distinct from "not running") so the
    # first-run banner tells the user to pull a model.
    prov = mock.Mock()
    prov.check_running.return_value = (True, None)
    prov.get_models.return_value = ([], None)
    patches = _healthy_mocks()
    patches[5] = mock.patch("api.routes.status.get_provider", return_value=prov)
    res = _run(client, patches)
    data = res.json
    assert data["details"]["local_model"]["status"] == "degraded"
    assert "no models installed" in data["details"]["local_model"]["error"]
    assert "nomic-embed-text" in data["details"]["local_model"]["error"]


def test_api_health_get_models_error_is_not_flagged_as_no_models(client):
    # A transient get_models failure (empty list arrives WITH an error, or the call
    # raises) must NOT be reported as "no models" — that would nag a healthy install
    # on a blip. The runner is running, so local_model stays ok.
    prov = mock.Mock()
    prov.check_running.return_value = (True, None)
    prov.get_models.return_value = (None, "list failed")
    patches = _healthy_mocks()
    patches[5] = mock.patch("api.routes.status.get_provider", return_value=prov)
    res = _run(client, patches)
    assert res.json["details"]["local_model"]["status"] == "ok"


def test_api_health_degraded_when_no_vault(client):
    patches = _healthy_mocks()
    patches[0] = mock.patch("rag.vault.obsidian_manager.get_vault_path", return_value="")
    res = _run(client, patches)
    data = res.json
    assert data["status"] == "degraded"
    assert data["details"]["vector_store"]["status"] == "degraded"
    assert "No vault folder" in data["details"]["vector_store"]["error"]


def test_api_health_lancedb_zero_rows_is_degraded(client):
    # Pre-fix the lancedb row count was computed and IGNORED, so an empty
    # table reported "ok" while the simple backend reported degraded.
    patches = _healthy_mocks()
    patches[2] = mock.patch("rag.lancedb_store.lancedb_table_count", return_value=0)
    res = _run(client, patches)
    data = res.json
    assert data["details"]["vector_store"]["status"] == "degraded"
    assert "no vectors" in data["details"]["vector_store"]["error"]


def test_api_health_bad_vault_path_is_overall_error(client):
    # An "error" sub-check must surface as overall "error", not be flattened
    # into "degraded" (the pre-fix behaviour).
    patches = _healthy_mocks()
    patches[1] = mock.patch("os.path.isdir", return_value=False)
    res = _run(client, patches)
    data = res.json
    assert data["details"]["vector_store"]["status"] == "error"
    assert data["status"] == "error"


def test_api_health_unloaded_index_with_persisted_docstore_is_ok(client):
    # docstore_doc_count() -> None means "not loaded yet / briefly busy" —
    # with a persisted docstore on disk that is a normal lazy-load state, not
    # a finding.
    patches = _healthy_mocks()
    patches[3] = mock.patch("rag.vault.obsidian_manager.docstore_doc_count", return_value=None)
    patches.append(mock.patch("os.path.isfile", return_value=True))
    res = _run(client, patches)
    assert res.json["details"]["vector_store"]["status"] == "ok"


def test_api_health_ttl_cache_collapses_probes(client):
    calls = {"n": 0}
    real_compute = status_mod._compute_health

    def _counting_compute():
        calls["n"] += 1
        return {"status": "ok", "details": {
            "vector_store": {"status": "ok", "error": None},
            "local_model": {"status": "ok", "error": None},
        }}

    with mock.patch.object(status_mod, "_compute_health", _counting_compute):
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/health").status_code == 200
    # The 15s UI poll must not become a probe storm: within the TTL every
    # request after the first is served from the cache.
    assert calls["n"] == 1
    assert real_compute is not None  # silence linters about the unused ref

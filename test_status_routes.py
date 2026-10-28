"""Bounds and caching on the polled provider-status routes.

PERF (2026-09-02, performance plan Phase 1): ``/api/status`` probed the local
backend on every request (app.js polls it every 15 s) with the GENERATION
timeout, which defaults to 0 = unbounded, so a wedged Ollama parked one of the
32 Waitress workers per poll indefinitely. ``/api/models`` had the same
unbounded probe, and ``/api/health`` had a TTL but computed outside its lock,
so N concurrent misses all probed. ``/api/pull`` used the module-level Ollama
client, resolving a host independently of the one every other call uses.
"""
import threading
import time
from unittest import mock

import pytest
from flask import Flask

import api.routes.status as status_mod


@pytest.fixture
def client():
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(status_mod.status_bp)
    from api.routes.config import config_bp
    app.register_blueprint(config_bp)
    from api.security import register_origin_guard
    register_origin_guard(app)
    # getattr, not direct attribute access: this file must fail on its
    # ASSERTIONS when run against the pre-cache routes, not on a missing name —
    # a red that only proves "this attribute is new" pins nothing.
    for name in ("_STATUS_CACHE", "_MODELS_CACHE", "_HEALTH_CACHE"):
        cache = getattr(status_mod, name, None)
        if cache is not None:
            cache.clear()
    with mock.patch("api.security.origin_is_local", return_value=True):
        yield app.test_client()


class _Probe:
    """A local provider whose probe blocks until released."""

    def __init__(self, gate=None):
        self.calls = 0
        self.gate = gate

    def check_running(self, **kw):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        return True, ""

    def get_models(self, **kw):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        return ["m1"], ""


class TestProbeCaching:
    def test_status_probes_once_for_repeat_polls(self, client):
        p = _Probe()
        with mock.patch("api.routes.status.get_provider", return_value=p), \
                mock.patch("api.routes.status.is_online", return_value=False):
            for _ in range(5):
                assert client.get("/api/status").status_code == 200
        assert p.calls == 1, "the 15 s badge poll re-probed the backend"

    def test_models_probes_once_for_repeat_polls(self, client):
        p = _Probe()
        with mock.patch("api.routes.status.get_provider", return_value=p), \
                mock.patch("api.routes.status.is_online", return_value=False):
            for _ in range(5):
                assert client.get("/api/models").status_code == 200
        assert p.calls == 1

    def test_concurrent_misses_share_one_probe(self, client):
        """The single-flight half: two windows must not mean two probes."""
        gate = threading.Event()
        p = _Probe(gate=gate)
        results = []

        def _hit():
            results.append(client.get("/api/status").status_code)

        # Patched ONCE around the threads, never per thread: four nested
        # mock.patch enters/exits racing on one attribute restore the mock a
        # sibling saw as "the original", leaving is_online a Mock for every
        # later test in the module (found 2026-09-22 by the ?provider= tests).
        with mock.patch("api.routes.status.get_provider", return_value=p), \
                mock.patch("api.routes.status.is_online", return_value=False):
            threads = [threading.Thread(target=_hit) for _ in range(4)]
            for t in threads:
                t.start()
            time.sleep(0.15)          # every thread is now past the cache check
            gate.set()
            for t in threads:
                t.join(10)
        assert results == [200] * 4
        assert p.calls == 1, f"{p.calls} concurrent probes, expected 1"

    def test_an_error_is_never_memoised(self, client):
        """A failing probe must not be served from cache for the whole TTL, and
        must not leave concurrent waiters parked."""
        calls = []

        def _boom():
            calls.append(1)
            raise RuntimeError("probe exploded")

        if not hasattr(status_mod, "_TtlSingleFlight"):
            pytest.fail("no single-flight cache: every concurrent miss probes")
        cache = status_mod._TtlSingleFlight(60.0)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cache.get("k", _boom)
        assert len(calls) == 2, "a failing probe was cached"
        # A later success is cached normally — the failure left no wedged
        # in-flight marker behind.
        assert cache.get("k", lambda: "ok") == ("ok", False)
        assert cache.get("k", lambda: "other") == ("ok", True)

    def test_warnings_are_not_cached_with_the_probe(self, client):
        """A newly raised provider warning must not wait out the probe TTL."""
        p = _Probe()
        seen = []
        with mock.patch("api.routes.status.get_provider", return_value=p), \
                mock.patch("api.routes.status.is_online", return_value=False):
            with mock.patch("api.routes.status.get_provider_warnings",
                            return_value=[]):
                seen.append(client.get("/api/status").get_json()["warnings"])
            with mock.patch("api.routes.status.get_provider_warnings",
                            return_value=["ollama is not installed"]):
                seen.append(client.get("/api/status").get_json()["warnings"])
        assert seen == [[], ["ollama is not installed"]]
        assert p.calls == 1, "the warning refresh re-probed"


class TestProbeBounds:
    def test_ollama_liveness_probe_is_bounded_independently_of_the_config(self):
        """`local_request_timeout_s` defaults to 0 = unbounded; the probe must
        not inherit that."""
        from core.providers.ollama import PROBE_TIMEOUT_S, OllamaProvider

        seen = []

        class _C:
            def list(self):
                return mock.Mock(models=[])

        p = OllamaProvider()
        with mock.patch.object(OllamaProvider, "_client",
                               lambda self, timeout=None: (seen.append(timeout)
                                                           or _C())):
            p.check_running()
        assert seen == [PROBE_TIMEOUT_S]
        assert 0 < PROBE_TIMEOUT_S <= 5

    def test_get_models_keeps_the_config_timeout_by_default(self):
        """`resolve_model` calls it on the GENERATION path, where a 3 s ceiling
        would be a new failure mode rather than a bound."""
        from core.providers.ollama import _USE_CONFIG_TIMEOUT, OllamaProvider

        seen = []

        class _C:
            def list(self):
                return mock.Mock(models=[])

        p = OllamaProvider()
        with mock.patch.object(OllamaProvider, "_client",
                               lambda self, timeout=None: (seen.append(timeout)
                                                           or _C())):
            p.get_models()
        assert seen == [_USE_CONFIG_TIMEOUT]


class TestPullUsesTheResolvedHost:
    def test_pull_binds_the_apps_own_host(self, client):
        """A pull that lands on a different server than generation reads as
        'it downloaded and then was not installed'."""
        made = []

        class _C:
            def pull(self, model, stream=False):
                return iter(())

        def _factory(host, timeout):
            made.append((host, timeout))
            return _C()

        with mock.patch("core.providers.ollama._ollama_client", _factory), \
                mock.patch("core.providers.base.resolve_ollama_host",
                           return_value="http://elsewhere:9999"), \
                mock.patch("api.routes.config.load_config",
                           return_value={"provider": "ollama"}):
            r = client.post("/api/pull", json={"model": "llama3"},
                            headers={"X-Requested-With": "ChatEKLD"})
            r.get_data()
        assert made == [("http://elsewhere:9999", None)]


class TestModelsProviderArg:
    """``?provider=`` (follow-ups lot 5, 2026-09-22): list another provider's
    models for one run, with that provider's configured model, without
    touching the configuration. Red on 69efd20a."""

    CFG = {"provider": "ollama", "llm": "llama3.1:8b", "openai_model": "gpt-4o-mini"}

    def test_a_named_provider_is_listed_with_its_configured_model(self, client):
        local = _Probe()
        online = mock.Mock()
        online.list_models.return_value = (["gpt-5", "gpt-4o-mini"], None)
        with mock.patch("core.config.load_config", return_value=dict(self.CFG)), \
                mock.patch("api.routes.status.get_provider", return_value=local), \
                mock.patch("api.routes.status.get_llm_provider", return_value=online):
            d = client.get("/api/models?provider=openai").get_json()
            assert d["provider"] == "openai" and d["models"] == ["gpt-5", "gpt-4o-mini"]
            assert d["configured"] == "gpt-4o-mini"
            assert local.calls == 0, "the configured local provider was probed for an online listing"
            d2 = client.get("/api/models?provider=lm_studio").get_json()
            # Audit remediation 2026-09-23 (N10): `llm` is the ACTIVE local
            # provider's (Ollama's) model; LM Studio is not active → "".
            assert d2["provider"] == "lm_studio" and d2["configured"] == ""
            # No argument: the configured provider, as before.
            d3 = client.get("/api/models").get_json()
            assert d3["provider"] == "ollama" and d3["configured"] == "llama3.1:8b"

    def test_an_unknown_provider_is_a_400_never_the_configured_one(self, client):
        p = _Probe()
        with mock.patch("core.config.load_config", return_value=dict(self.CFG)), \
                mock.patch("api.routes.status.get_provider", return_value=p):
            r = client.get("/api/models?provider=nope")
            assert r.status_code == 400 and "Unknown provider" in r.get_json()["error"]
            assert p.calls == 0

    def test_the_cache_is_keyed_per_provider(self, client):
        p = _Probe()
        with mock.patch("core.config.load_config", return_value=dict(self.CFG)), \
                mock.patch("api.routes.status.get_provider", return_value=p), \
                mock.patch("api.routes.status.is_online", return_value=False):
            for _ in range(3):
                assert client.get("/api/models?provider=ollama").status_code == 200
                assert client.get("/api/models?provider=lm_studio").status_code == 200
        assert p.calls == 2

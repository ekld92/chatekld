"""Library-workflow lot 3: the session's model, spend and resumption; tagging a
PDF by its identity after a rename.

Hermetic: disposable Zotero SQLite + vault (the lot 2 `Library` fixture, via
module import), the real tagging blueprint, and fake LLM providers injected
at `core.llm.factory.get_llm_provider` — BELOW `stream_chat_messages` and the
fallback policy, so the policy under test is the production one.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import flask
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_library_imports as _imports_tests  # noqa: E402  (module import: no re-collection)

from accession import imports  # noqa: E402
from api.routes.library_workflow import library_workflow_bp  # noqa: E402
from api.routes.tagging import tagging_bp  # noqa: E402
from core.llm.base import StreamingResponse  # noqa: E402
from core.llm.types import ErrorCategory, LLMError  # noqa: E402
from tagging import session_llm  # noqa: E402

_H = {"X-Requested-With": "ChatEKLD"}
_VOCAB = {"dep": "Depression", "mood": "Mood"}
_TEXT = "a long paper about depression and mood disorders in adult patients " * 3


def _app():
    app = flask.Flask(__name__)
    app.register_blueprint(tagging_bp)
    app.register_blueprint(library_workflow_bp)
    return app


class FakeProvider:
    """Records every request; streams `reply` or raises `error` before token 1."""

    def __init__(self, name, reply='[{"tag": "dep", "confidence": 0.9}]', error=None):
        self.name, self.reply, self.error, self.calls = name, reply, error, []

    def stream(self, request):
        self.calls.append(request)
        if self.error:
            raise self.error
        return StreamingResponse(response_gen=iter([self.reply]))


class Harness:
    def __init__(self, tmp_path, cfg_extra=None):
        self.lib = _imports_tests.Library(tmp_path)
        self.pdf = self.lib.biblio / "smith_2020.pdf"
        self.pdf.write_bytes(b"%PDF smith")
        self.cfg = {"provider": "ollama", "llm": "llama3", "fallback_provider": "openai",
                    "fallback_on": ["network", "timeout", "server_error", "rate_limit"],
                    "tagging_candidate_tags": 0, "tagging_fewshot_k": 0,
                    "tagging_propose_max_tokens": 256, **(cfg_extra or {})}
        self.cfg_before = copy.deepcopy(self.cfg)
        self.providers: dict[str, FakeProvider] = {}
        self.client = _app().test_client()
        self.text = _TEXT
        self.vocab = dict(_VOCAB)

    def record(self, *, item_key="ABCD1234", pdf_paths=None, library_id=1):
        from audit.engine.inventory import Record
        rec = Record(citation_key="smith2020")
        rec.pdf_paths = pdf_paths if pdf_paths is not None else [self.pdf]
        rec.bib_entry = SimpleNamespace(title="A depression study", year="2020", authors=["Smith"],
                                        keywords=set(), entry_type="article")
        rec.zotero_item = SimpleNamespace(item_key=item_key, library_id=library_id, item_type="journalArticle",
                                          title="A depression study", abstract=None, tags=[])
        rec.obsidian_note = None
        return rec

    def provider(self, name, **kw):
        self.providers[name] = FakeProvider(name, **kw)
        return self.providers[name]

    def patches(self, rec=None):
        from audit.manager import audit_manager
        inv = mock.MagicMock()
        inv.records = {"smith2020": rec or self.record()}
        settings = self.lib.settings

        def get_provider(name, cfg=None):
            return self.providers.setdefault(name, FakeProvider(name))

        return [
            mock.patch.object(audit_manager, "get_inventory", return_value=(inv, settings)),
            mock.patch("api.routes.tagging.load_config", side_effect=lambda: self.cfg),
            mock.patch("api.routes.library_workflow.load_config", side_effect=lambda: self.cfg),
            mock.patch("api.routes.library_workflow.load_settings", return_value=settings),
            mock.patch("api.routes.tagging._read_vocab", side_effect=lambda *_: (self.vocab, "_tags.md")),
            mock.patch("api.routes.tagging.text_source.read_pdf_text",
                       side_effect=lambda *a, **k: (self.text, False)),
            mock.patch("core.llm.factory.get_llm_provider", side_effect=get_provider),
        ]

    def __enter__(self):
        self._p = self.patches()
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._p):
            p.stop()

    def session(self, **llm):
        r = self.client.post("/api/library/sessions", json={"llm": llm} if llm else {}, headers=_H)
        assert r.status_code == 200, r.get_json()
        return r.get_json()["session"]["id"]

    def propose(self, **body):
        return self.client.post("/api/tagging/propose", json={"citation_key": "smith2020", **body},
                                headers=_H)


# ---------------------------------------------------------------------------
# The model choice itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, match", [
    ({"provider": "nope", "model": "x"}, "Unknown provider"),
    ({"provider": "ollama", "model": ""}, "model id"),
    ({"provider": "openai", "model": "gpt-4o-mini"}, "spending limit"),
    ({"provider": "openai", "model": "not-a-priced-model", "budget_usd": 1}, "no price"),
    ({"provider": "openai", "model": "gpt-4o-mini", "budget_usd": True}, "budget_usd"),
    ({"provider": "openai", "model": "gpt-4o-mini", "budget_usd": 500}, "budget_usd"),
])
def test_invalid_choices_are_refused(raw, match):
    with pytest.raises(session_llm.SessionLLMError, match=match):
        session_llm.validate(raw, {})


def test_an_unpriced_online_model_needs_no_limit():
    # Audit remediation 2026-09-23 (L3): a ceiling cannot bound an unpriced
    # model, so "No spending limit" is its only acceptance; the retired
    # allow_unpriced flag is refused, never silently mapped.
    with pytest.raises(session_llm.SessionLLMError, match="No spending limit"):
        session_llm.validate({"provider": "openai", "model": "not-a-priced-model",
                              "budget_usd": 1}, {})
    with pytest.raises(session_llm.SessionLLMError, match="retired"):
        session_llm.validate({"provider": "openai", "model": "not-a-priced-model", "budget_usd": 1,
                              "allow_unpriced": True}, {})
    c = session_llm.validate({"provider": "openai", "model": "not-a-priced-model",
                              "no_limit": True}, {})
    assert c.online and c.no_limit


def test_call_config_disables_every_fallback_and_leaves_the_original_alone():
    cfg = {"fallback_provider": "openai", "fallback_on": ["network"], "nested": {"a": 1}}
    out = session_llm.call_config(cfg)
    assert out["fallback_provider"] is None and out["fallback_on"] == []
    out["nested"]["a"] = 2
    assert cfg == {"fallback_provider": "openai", "fallback_on": ["network"], "nested": {"a": 1}}


# ---------------------------------------------------------------------------
# Propose inside a session
# ---------------------------------------------------------------------------

def test_the_session_model_is_used_and_the_global_config_is_untouched(tmp_path):
    with Harness(tmp_path) as h:
        sid = h.session(provider="lm_studio", model="qwen-local")
        r = h.propose(session=sid)
        assert r.status_code == 200, r.get_json()
        d = r.get_json()
        assert (d["provider"], d["model"]) == ("lm_studio", "qwen-local")
        assert [c.model for c in h.providers["lm_studio"].calls] == ["qwen-local"]
        assert "ollama" not in h.providers or not h.providers["ollama"].calls
        assert h.cfg == h.cfg_before
        assert d["sent"] == {"provider": "lm_studio", "model": "qwen-local", "online": False,
                             "chars_sent": d["sent"]["chars_sent"], "source": "pdf"}
        assert d["sent"]["chars_sent"] > len(_TEXT)
        assert d["confidence_is_calibrated"] is False


def test_a_failing_local_session_model_never_falls_back_online(tmp_path):
    """The global config names an online fallback; the session must not use it."""
    with Harness(tmp_path) as h:
        h.provider("ollama", error=LLMError(category=ErrorCategory.NETWORK, message="refused",
                                            provider="ollama", model="llama3"))
        sid = h.session(provider="ollama", model="llama3")
        r = h.propose(session=sid)
        assert r.status_code == 502
        d = r.get_json()
        assert (d["provider"], d["model"]) == ("ollama", "llama3")
        assert "openai" not in h.providers or not h.providers["openai"].calls


def test_without_a_session_the_global_fallback_still_applies(tmp_path):
    """Control: the historical route keeps its configured fallback."""
    with Harness(tmp_path, {"openai_model": "gpt-4o-mini"}) as h:
        h.provider("ollama", error=LLMError(category=ErrorCategory.NETWORK, message="refused",
                                            provider="ollama", model="llama3"))
        r = h.propose()
        assert r.status_code == 200, r.get_json()
        assert len(h.providers["openai"].calls) == 1


def test_resumption_reuses_a_proposal_and_invalidates_on_a_changed_dependency(tmp_path):
    with Harness(tmp_path) as h:
        sid = h.session(provider="ollama", model="llama3")
        first = h.propose(session=sid).get_json()
        again = h.propose(session=sid).get_json()
        assert again["from_cache"] is True and again["sent"] is None
        assert again["proposed"] == first["proposed"]
        assert len(h.providers["ollama"].calls) == 1
        h.vocab["scz"] = "Schizophrenia"  # the vocabulary changed
        assert h.propose(session=sid).get_json()["from_cache"] is False
        h.text = _TEXT + " re-extracted"  # the source text changed
        assert h.propose(session=sid).get_json()["from_cache"] is False
        assert h.propose(session=sid, force=True).get_json()["from_cache"] is False
        assert len(h.providers["ollama"].calls) == 4


def test_an_online_session_is_refused_before_it_could_overrun(tmp_path):
    with Harness(tmp_path) as h:
        # gpt-4o-mini output is priced per million tokens: a 256-token cap on a
        # zero budget cannot fit.
        sid = h.session(provider="openai", model="gpt-4o-mini", budget_usd=0.0)
        r = h.propose(session=sid)
        assert r.status_code == 409 and r.get_json()["reason"] == "budget"
        assert "openai" not in h.providers or not h.providers["openai"].calls


def test_real_spend_is_recorded_and_stops_the_session_at_its_limit(tmp_path):
    with Harness(tmp_path) as h:
        sid = h.session(provider="openai", model="gpt-4o-mini", budget_usd=0.01)
        billed = SimpleNamespace(cost_usd=0.006)
        with mock.patch("core.llm.usage.usage_tracker.records_since", return_value=[billed]):
            d = h.propose(session=sid).get_json()
            assert d["spend"]["usd"] == pytest.approx(0.006) and d["sent"]["online"] is True
            h.text = _TEXT + " another paper"
            d = h.propose(session=sid).get_json()
            assert d["spend"]["usd"] == pytest.approx(0.012)
            h.text = _TEXT + " a third paper"
            r = h.propose(session=sid)
        assert r.status_code == 409 and r.get_json()["reason"] == "budget"
        assert len(h.providers["openai"].calls) == 2


def test_a_failed_call_is_charged_its_worst_case_not_zero(tmp_path):
    """The usage tracker records a failed call at $0 although the provider
    billed what it read; the session must charge the pre-call worst case."""
    with Harness(tmp_path) as h:
        h.provider("openai", error=LLMError(category=ErrorCategory.SERVER_ERROR, message="500",
                                            provider="openai", model="gpt-4o-mini"))
        sid = h.session(provider="openai", model="gpt-4o-mini", budget_usd=1.0)
        failed = SimpleNamespace(cost_usd=0.0, success=False, error_category="server_error")
        with mock.patch("core.llm.usage.usage_tracker.records_since", return_value=[failed]):
            r = h.propose(session=sid)
        assert r.status_code == 502
        assert r.get_json()["spend"]["usd"] > 0
        assert r.get_json()["sent"]["online"] is True


def test_session_routes_refuse_an_invalid_choice_without_creating_a_session(tmp_path):
    with Harness(tmp_path) as h:
        before = imports.history(h.lib.settings)
        r = h.client.post("/api/library/sessions", json={"llm": {"provider": "openai",
                                                                  "model": "gpt-4o-mini"}},
                          headers=_H)
        assert r.status_code == 400
        assert imports.history(h.lib.settings) == before
        sid = h.session()
        r = h.client.post(f"/api/library/sessions/{sid}/llm",
                          json={"provider": "ollama", "model": "llama3"}, headers=_H)
        assert r.get_json()["session"]["llm"]["online"] is False
        assert h.cfg == h.cfg_before


def test_changing_the_model_keeps_the_sessions_spend(tmp_path):
    with Harness(tmp_path) as h:
        sid = h.session(provider="openai", model="gpt-4o-mini", budget_usd=1.0)
        imports.charge_session(h.lib.settings, sid, 0.25)
        h.client.post(f"/api/library/sessions/{sid}/llm",
                      json={"provider": "ollama", "model": "llama3"}, headers=_H)
        s = h.client.get(f"/api/library/sessions/{sid}", headers=_H).get_json()["session"]
        assert s["spend"]["usd"] == pytest.approx(0.25)


def test_a_pass_does_not_erase_the_sessions_model_choice(tmp_path):
    with Harness(tmp_path) as h:
        sid = h.session(provider="ollama", model="llama3")
        imports.record_pass(h.lib.settings, sid, imports.observe(h.lib.settings))
        assert imports.session(h.lib.settings, sid)["llm"]["model"] == "llama3"


# ---------------------------------------------------------------------------
# Tagging a PDF found by identity after a rename
# ---------------------------------------------------------------------------

def _tracked_attachment(h, name="smith_2020.pdf"):
    e = h.lib.entry("ABCD1234", "smith2020")
    att, path = h.lib.pdf(e, "PDFA0001", name)
    sid = imports.begin_session(h.lib.settings)
    imports.record_pass(h.lib.settings, sid, imports.observe(h.lib.settings))  # baseline
    return att, path


def _apply_with(h, rec, unit):
    """apply-all through the REAL journal (archive in a tmp dir outside the vault)."""
    from api.routes import _finder_tagwrite
    from audit.manager import audit_manager
    inv = mock.MagicMock()
    inv.records = {"smith2020": rec}
    cfg = {"tagging_archive_dir": str(h.lib.vault.parent / "archive")}
    writes = []
    with mock.patch.object(audit_manager, "get_inventory", return_value=(inv, h.lib.settings)), \
         mock.patch("tagging.writers._read_vocab", return_value=(_VOCAB, "_tags.md")), \
         mock.patch("tagging.writers.load_config", return_value=cfg), \
         mock.patch.object(_finder_tagwrite, "is_macos", return_value=True), \
         mock.patch.object(_finder_tagwrite, "read_finder_tags_full", return_value=[]), \
         mock.patch.object(_finder_tagwrite, "write_finder_tags",
                           side_effect=lambda p, tags: writes.append((p, list(tags)))), \
         mock.patch("rag.vault.obsidian_manager.try_acquire_lock", return_value=1), \
         mock.patch("rag.vault.obsidian_manager.release_lock"):
        r = h.client.post("/api/tagging/apply-all", headers=_H, json={
            "citation_key": "smith2020", "tags": ["dep"], "confirm": True, "unit": unit,
            "targets": {"note": False, "zotero": False, "finder": True}})
    return r, writes


def test_a_renamed_pdf_is_tagged_at_its_new_path(tmp_path):
    h = Harness(tmp_path)
    att, path = _tracked_attachment(h, "Smith - 2020 - Full text.pdf")
    rec = h.record(pdf_paths=[path])          # the cached scan, taken before the rename
    new = h.lib.rename(att, path, "smith_2020.pdf")
    imports.record_pass(h.lib.settings, imports.begin_session(h.lib.settings),
                        imports.observe(h.lib.settings))
    r, writes = _apply_with(h, rec, "attachment:1:PDFA0001")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["results"]["finder"].get("ok") is True, r.get_json()
    assert [Path(p).name for p, _ in writes] == [new.name]


def test_a_unit_of_another_record_is_refused(tmp_path):
    h = Harness(tmp_path)
    _tracked_attachment(h)
    rec = h.record(item_key="ZZZZ9999")
    r, writes = _apply_with(h, rec, "attachment:1:PDFA0001")
    assert r.get_json()["results"]["finder"]["skipped"] == "unit" and writes == []


def test_a_replaced_file_is_refused_until_observed_again(tmp_path):
    import os
    h = Harness(tmp_path)
    _, path = _tracked_attachment(h)
    tmp = path.with_name(".tmp.pdf")
    tmp.write_bytes(b"%PDF other bytes")
    os.replace(tmp, path)                       # same path, different file
    r, writes = _apply_with(h, h.record(), "attachment:1:PDFA0001")
    assert r.get_json()["results"]["finder"]["skipped"] == "unit_stale" and writes == []


# ---------------------------------------------------------------------------
# Adversarial review of lot 3 (2026-09-21). Each test was red on 22e8a74a.
# ---------------------------------------------------------------------------

from core.llm.types import LLMUsage  # noqa: E402
from core.llm.usage import PRICING_TABLE, usage_tracker  # noqa: E402


class RecordingProvider(FakeProvider):
    """Records usage in the stream's finally, the way the online adapters do."""

    def __init__(self, name, *, success=True, raise_after=None, cancelled=False, **kw):
        super().__init__(name, **kw)
        self.success, self.raise_after, self.cancelled = success, raise_after, cancelled

    def stream(self, request):
        self.calls.append(request)

        def gen():
            try:
                yield self.reply
                if self.raise_after:
                    raise self.raise_after
            finally:
                usage_tracker.record(
                    provider=self.name, model=request.model, workflow=request.workflow,
                    usage=LLMUsage(input_tokens=0 if self.cancelled else 2000, output_tokens=200),
                    latency_ms=1, stream=True, success=self.success,
                    error_category="cancelled" if self.cancelled else ("" if self.success else "server_error"))
        return StreamingResponse(response_gen=gen())


def test_an_unpriced_sessions_spend_is_unknown_never_zero(tmp_path):
    with Harness(tmp_path) as h:
        h.providers["openai"] = RecordingProvider("openai")
        sid = h.session(provider="openai", model="not-a-priced-model", no_limit=True)
        assert h.propose(session=sid).get_json()["spend"]["unknown"] is True


def test_a_midstream_failure_is_charged_what_was_billed(tmp_path):
    with Harness(tmp_path) as h:
        h.providers["openai"] = RecordingProvider(
            "openai", success=False, raise_after=LLMError(
                category=ErrorCategory.SERVER_ERROR, message="boom", provider="openai",
                model="gpt-4o-mini"))
        sid = h.session(provider="openai", model="gpt-4o-mini", budget_usd=1.0)
        r = h.propose(session=sid)
        assert r.status_code == 502 and r.get_json()["spend"]["usd"] > 0


def test_a_cancelled_stream_is_charged_its_prompt():
    from datetime import datetime, timezone
    from api.routes.tagging import _measured_cost
    choice = session_llm.validate({"provider": "openai", "model": "gpt-4o-mini", "budget_usd": 1.0}, {})
    start = datetime.now(timezone.utc).isoformat()
    usage_tracker.record(provider="openai", model="gpt-4o-mini", workflow="tagging_propose",
                         usage=LLMUsage(input_tokens=0, output_tokens=5), latency_ms=1,
                         stream=True, success=True, error_category="cancelled")
    assert _measured_cost(choice, start, 0.01) >= 2000 / 1e6 * PRICING_TABLE["gpt-4o-mini"].input


def test_an_empty_parse_is_not_cached(tmp_path):
    with Harness(tmp_path) as h:
        h.provider("ollama", reply="")
        sid = h.session(provider="ollama", model="llama3")
        assert h.propose(session=sid).get_json()["status"] == "empty"
        h.providers["ollama"].reply = '[{"tag": "dep", "confidence": 0.9}]'
        assert h.propose(session=sid).get_json()["from_cache"] is False


def test_a_replay_recomputes_the_status_from_todays_threshold(tmp_path):
    with Harness(tmp_path, {"tagging_min_conf": 0.95}) as h:
        sid = h.session(provider="ollama", model="llama3")
        assert h.propose(session=sid).get_json()["status"] == "low_confidence"
        h.cfg["tagging_min_conf"] = 0.5
        again = h.propose(session=sid).get_json()
        assert again["from_cache"] is True and again["status"] == "ok"


def test_a_changed_fewshot_setting_is_a_miss(tmp_path):
    with Harness(tmp_path) as h:
        sid = h.session(provider="ollama", model="llama3")
        h.propose(session=sid)
        h.cfg["tagging_fewshot_k"] = 5
        with mock.patch("api.routes.tagging.tag_candidates.propose_context",
                        side_effect=lambda **kw: (kw["vocab"], [])):
            assert h.propose(session=sid).get_json()["from_cache"] is False


def test_a_session_uses_one_attempt_per_call():
    assert session_llm.call_config({"online_max_retries": 3})["online_max_retries"] == 1


def test_an_ollama_cloud_model_is_online():
    with pytest.raises(session_llm.SessionLLMError, match="spending limit"):
        session_llm.validate({"provider": "ollama", "model": "gpt-oss:120b-cloud"}, {})
    c = session_llm.validate({"provider": "ollama", "model": "gpt-oss:120b-cloud",
                              "no_limit": True}, {})
    assert c.online


def test_a_session_created_after_a_scan_is_scoped_like_the_scan(tmp_path):
    """The session routes read the scan's settings, not the saved config."""
    with Harness(tmp_path) as h:
        other = SimpleNamespace(vault_root=tmp_path / "elsewhere", zotero_sqlite=tmp_path / "x.sqlite")
        with mock.patch("api.routes.library_workflow.load_settings", return_value=other):
            sid = h.session(provider="ollama", model="llama3")
        assert h.propose(session=sid).status_code == 200


def _remove_with(h, rec, unit, body_extra=None):
    from api.routes import _finder_tagwrite
    from audit.manager import audit_manager
    inv = mock.MagicMock()
    inv.records = {"smith2020": rec}
    body = {"citation_key": "smith2020", "tags": ["dep"], "confirm": True, "unit": unit,
            "targets": {"note": False, "zotero": False, "finder": True}, **(body_extra or {})}
    writes = []
    with mock.patch.object(audit_manager, "get_inventory", return_value=(inv, h.lib.settings)), \
         mock.patch("tagging.writers.load_config",
                    return_value={"tagging_archive_dir": str(h.lib.vault.parent / "archive")}), \
         mock.patch.object(_finder_tagwrite, "is_macos", return_value=True), \
         mock.patch.object(_finder_tagwrite, "read_finder_tags_full", return_value=["dep"]), \
         mock.patch.object(_finder_tagwrite, "write_finder_tags",
                           side_effect=lambda p, tags: writes.append(p)), \
         mock.patch("rag.vault.obsidian_manager.try_acquire_lock", return_value=1), \
         mock.patch("rag.vault.obsidian_manager.release_lock"):
        r = h.client.post("/api/tagging/remove-tags", headers=_H, json=body)
    return r, writes


def test_remove_tags_refuses_a_unit_rather_than_ignoring_it(tmp_path):
    h = Harness(tmp_path)
    _tracked_attachment(h)
    r, writes = _remove_with(h, h.record(item_key="ZZZZ9999"), "attachment:1:PDFA0001")
    assert r.status_code == 400 and writes == []
    r, writes = _remove_with(h, None, "attachment:1:PDFA0001",
                             {"citation_key": None, "pdf_rel": "smith_2020.pdf"})
    assert r.status_code == 400 and writes == []


def test_a_file_replaced_after_the_check_is_not_tagged(tmp_path):
    import os
    import tagging.writers as writers
    h = Harness(tmp_path)
    _, path = _tracked_attachment(h)
    real = writers._finder_write_locked

    def replace_then_write(**kw):
        tmp = path.with_name(".swap.pdf")
        tmp.write_bytes(b"%PDF foreign")
        os.replace(tmp, path)
        return real(**kw)

    with mock.patch("api.routes.tagging._finder_write_locked", side_effect=replace_then_write):
        r, writes = _apply_with(h, h.record(), "attachment:1:PDFA0001")
    assert writes == [], r.get_json()

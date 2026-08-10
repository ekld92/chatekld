"""Replay harness: drive the REAL routes with the driver's OWN requests.

The body-key half of the contract ratchet (2026-07-22 audit T1/C4b). The v1
ratchet checked that routes exist; it could not see that ``generate_deck``
sent a body missing ``template_tex`` — so the deck/docgen generate tools
shipped 400-ing on every call and no test noticed. Here every driver HTTP call
is intercepted at ``urllib.request.urlopen`` and replayed against the Flask
test client (validation runs for real; the heavy SSE workers are stubbed to an
immediate ``[DONE]`` that still releases their op locks), with the rule:

* 405 / route-level 404 (non-JSON body) are ALWAYS failures;
* a 400 is a failure when its message says a field the driver should have sent
  is missing/required — unless the case's ``allowed_400`` regex explains it as
  environmental (no vault configured in the sandbox, etc.).

This file would have failed on the pre-2026-07-22 generate bodies
("A Beamer template is required.").
"""
import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from flask import Response  # noqa: E402

_MISSING_FIELD_RE = re.compile(r"required|missing", re.IGNORECASE)


class _FakeHTTPResponse:
    """Duck-types what core.driver expects from urlopen's return value."""

    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __iter__(self):
        # _http_sse iterates the response line by line.
        return iter(self._body.splitlines(keepends=True))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestDriverReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import create_app
        cls.app = create_app()
        cls.app.config["TESTING"] = True

    def _stub_sse(self):
        """Patch every route module's run_sse_worker to an instant [DONE].

        The stub still calls ``release=`` so op locks don't leak across cases.
        Validation in each route runs BEFORE run_sse_worker, which is exactly
        the part this harness exists to exercise.
        """
        def _stub(worker, *, consumer_timeout_s=None, preflight_msgs=None,
                  release=None, **kw):
            if release is not None:
                try:
                    release()
                except Exception:
                    pass
            return Response("data: [DONE]\n\n", mimetype="text/event-stream")

        patches = []
        for mod in ("api.routes.vault", "api.routes.deck", "api.routes.docgen",
                    "api.routes.refactor", "api.routes.paper",
                    "api.routes.plainchat"):
            patches.append(patch(f"{mod}.run_sse_worker", side_effect=_stub))
        return patches

    def _run(self, fn, allowed_400: str = "", expect_min_requests: int = 1):
        """Invoke *fn* with urlopen replayed through the test client."""
        client = self.app.test_client()
        seen: list = []

        def _fake_urlopen(req, timeout=None):
            url = req.full_url
            path = url.split("127.0.0.1:1", 1)[-1] if "127.0.0.1:1" in url else url
            # Strip scheme/host generically.
            m = re.search(r"(/api/.*)$", url)
            path = m.group(1) if m else path
            method = req.get_method()
            headers = {k: v for k, v in req.header_items()}
            resp = client.open(path, method=method, data=req.data,
                               headers=headers)
            seen.append((path, method, resp))
            status = resp.status_code
            body = resp.get_data()
            if status >= 400:
                raise urllib.error.HTTPError(url, status, resp.status, resp.headers,
                                             io.BytesIO(body))
            return _FakeHTTPResponse(status, body)

        patches = self._stub_sse()
        with patch("core.driver.urllib.request.urlopen", side_effect=_fake_urlopen), \
                patch("core.driver.resolve_base_url",
                      return_value="http://127.0.0.1:1"):
            for p in patches:
                p.start()
            try:
                result = fn()
            finally:
                for p in patches:
                    p.stop()

        self.assertGreaterEqual(len(seen), expect_min_requests,
                                f"driver made no HTTP request: {result}")
        for path, method, resp in seen:
            self.assertNotEqual(resp.status_code, 405,
                                f"{method} {path}: method not allowed")
            body = resp.get_data(as_text=True)
            if resp.status_code == 404:
                # A domain 404 (JSON error body) is legitimate ("run a scan
                # first"); a routing 404 (Flask HTML) means a phantom endpoint.
                self.assertTrue(body.strip().startswith("{"),
                                f"{method} {path}: routing 404 (phantom endpoint)")
            if resp.status_code == 400:
                try:
                    err = (json.loads(body) or {}).get("error", "")
                except ValueError:
                    err = body
                if allowed_400 and re.search(allowed_400, err, re.IGNORECASE):
                    continue
                self.assertFalse(
                    _MISSING_FIELD_RE.search(err or ""),
                    f"{method} {path}: 400 for a missing field the driver "
                    f"should send — {err!r}")
        return result, seen

    # ------------------------------------------------------------------

    def test_get_status_composes_from_real_routes(self):
        from core.driver import get_status
        result, seen = self._run(get_status, expect_min_requests=3)
        self.assertTrue(result.get("server_running"))
        self.assertIn("config", result)
        self.assertIn("vault_index", result)
        # The rich payload shape, not a bare string.
        self.assertIsInstance(result["vault_index"], dict)

    def test_set_config_body_accepted(self):
        from core.driver import set_config
        result, _ = self._run(lambda: set_config({"chat_temperature": 0.4}))
        self.assertEqual(result.get("status"), "ok")

    def test_vault_ask_body_accepted(self):
        from core.driver import vault_ask
        # The stubbed SSE returns no tokens; the point is the request validates.
        self._run(lambda: vault_ask("what is depression?"))

    def test_vault_index_body_accepted(self):
        from core.driver import vault_index
        # Sandbox has no vault configured — an environmental 400 naming the
        # vault is fine; a missing-field 400 is not.
        self._run(vault_index, allowed_400=r"vault|folder|path")

    def test_tagging_propose_and_apply_bodies_accepted(self):
        from core.driver import tagging_apply, tagging_propose
        # No audit scan in the sandbox → domain 404/409 allowed by the rules.
        self._run(lambda: tagging_propose("smith2020"),
                  allowed_400=r"scan|vault|inventory")
        self._run(lambda: tagging_apply("smith2020", ["dep"], targets=["note"]),
                  allowed_400=r"scan|vault|inventory")

    def test_refactor_plan_body_accepted(self):
        from core.driver import refactor_plan
        self._run(lambda: refactor_plan(""), allowed_400=r"vault|folder|path")

    def test_generate_deck_sends_template_content(self):
        """THE regression this harness exists for: the old driver body had no
        template_tex/out_dir and 400'd on every call, forever."""
        from core.driver import generate_deck
        with tempfile.TemporaryDirectory() as td:
            tpl = os.path.join(td, "template.tex")
            with open(tpl, "w", encoding="utf-8") as fh:
                fh.write("\\documentclass{beamer}\n\\begin{document}\n"
                         "\\end{document}\n")
            out_dir = os.path.join(td, "out")
            os.makedirs(out_dir)
            result, seen = self._run(
                lambda: generate_deck("Test topic", template_path=tpl,
                                      out_dir=out_dir),
                expect_min_requests=2)
        paths = [p for p, _m, _r in seen]
        self.assertIn("/api/deck/load-template", paths)
        self.assertIn("/api/deck/generate", paths)
        # Both requests validated (no missing-field 400 per the harness rules).
        self.assertNotIn("error", result)

    def test_generate_doc_latex_sends_template_content(self):
        from core.driver import generate_doc
        with tempfile.TemporaryDirectory() as td:
            tpl = os.path.join(td, "article.tex")
            with open(tpl, "w", encoding="utf-8") as fh:
                fh.write("\\documentclass{article}\n\\begin{document}\n"
                         "\\end{document}\n")
            out_dir = os.path.join(td, "out")
            os.makedirs(out_dir)
            result, seen = self._run(
                lambda: generate_doc("Test topic", mode="latex",
                                     template_path=tpl, out_dir=out_dir),
                expect_min_requests=2)
        paths = [p for p, _m, _r in seen]
        self.assertIn("/api/docgen/load-template", paths)
        self.assertIn("/api/docgen/generate", paths)

    def test_generate_doc_markdown_body_accepted(self):
        from core.driver import generate_doc
        self._run(lambda: generate_doc("Test topic", mode="markdown"),
                  allowed_400=r"vault|folder|path")

    def test_summarize_paper_upload_request_shape(self):
        from core.driver import summarize_paper
        # A non-PDF body: the upload route must reject it as a DOMAIN error
        # (extension/content), never a routing/method failure — proving the
        # multipart request (the old f-string blindspot) reaches the real route.
        with tempfile.TemporaryDirectory() as td:
            pdf = os.path.join(td, "paper.pdf")
            with open(pdf, "wb") as fh:
                fh.write(b"%PDF-1.4 not really a pdf")
            self._run(lambda: summarize_paper(pdf),
                      allowed_400=r"pdf|extract|upload|file|invalid")


if __name__ == "__main__":
    unittest.main()

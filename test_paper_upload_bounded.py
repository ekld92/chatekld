"""POST /api/upload extracts off the Waitress request thread (2026-09-02).

`process_pdf_upload` joined the extraction subprocess INLINE for up to
`PDF_EXTRACT_TIMEOUT_S` (600 s), holding one of thirty-two pool slots with no
ceiling of its own and no way to answer "busy". The streamed save still runs on
the request thread — it reads the request stream, so it must — but the
extraction now goes through `api.bounded.run_bounded_action`.

The deadline sits OUTSIDE the subprocess's own 600 s bound, per the timeout
chain's outer-must-exceed-inner rule: a tighter one would 504 a legal
extraction and discard work the user is waiting for.
"""
from __future__ import annotations

import io
import threading
from unittest import mock

import pytest


@pytest.fixture
def client():
    from app import app
    from core.database import init_db
    app.config["TESTING"] = True
    init_db()
    return app.test_client()


HDR = {"X-Requested-With": "ChatEKLD"}


def _upload(client, name="paper.pdf"):
    return client.post(
        "/api/upload",
        data={"file": (io.BytesIO(b"%PDF-1.4 stub"), name)},
        content_type="multipart/form-data",
        headers=HDR,
    )


def test_extraction_runs_off_the_request_thread(client):
    import services.pdf_service as svc

    request_thread = threading.current_thread()
    seen = {}

    def _spy(path, **kw):
        seen["thread"] = threading.current_thread()
        return "extracted text"

    with mock.patch.object(svc, "_extract_with_timeout", _spy):
        r = _upload(client)

    assert r.status_code == 200, r.get_json()
    assert seen["thread"] is not request_thread
    assert seen["thread"].name.startswith("bounded-action-")


def test_a_wedged_extraction_frees_the_worker_with_504(client):
    import api.routes.paper as route
    import services.pdf_service as svc

    started = threading.Event()
    release = threading.Event()

    def _wedged(path, **kw):
        started.set()
        release.wait(timeout=30)
        return "late"

    try:
        with (
            mock.patch.object(svc, "_extract_with_timeout", _wedged),
            mock.patch.object(route, "_UPLOAD_DEADLINE_S", 0.2),
        ):
            r = _upload(client)
        assert r.status_code == 504, r.get_json()
        assert "timed out" in r.get_json()["error"].lower()
        assert started.is_set()
    finally:
        release.set()


def test_a_second_concurrent_upload_is_told_busy(client):
    import services.pdf_service as svc

    started = threading.Event()
    release = threading.Event()

    def _slow(path, **kw):
        started.set()
        release.wait(timeout=30)
        return "extracted text"

    out = {}
    try:
        with mock.patch.object(svc, "_extract_with_timeout", _slow):
            t = threading.Thread(
                target=lambda: out.__setitem__("first", _upload(client)),
                daemon=True)
            t.start()
            assert started.wait(timeout=10)
            second = _upload(client)
            assert second.status_code == 429, second.get_json()
            release.set()
            t.join(timeout=20)
    finally:
        release.set()
    assert out["first"].status_code == 200, out["first"].get_json()


def test_a_paper_upload_and_a_chat_attachment_do_not_block_each_other(client):
    """Distinct action names: sharing one would make the two queue on each other."""
    import services.pdf_service as svc

    started = threading.Event()
    release = threading.Event()

    def _slow(path, **kw):
        started.set()
        release.wait(timeout=30)
        return "extracted text"

    out = {}
    try:
        with mock.patch.object(svc, "_extract_with_timeout", _slow):
            t = threading.Thread(
                target=lambda: out.__setitem__("paper", _upload(client)),
                daemon=True)
            t.start()
            assert started.wait(timeout=10)
            # Reach _EXTRACTORS through the CONSUMER's globals, never through
            # a fresh `import services.attachment_service` — see
            # test_chat_attachment.py::_live_extractors for why.
            import api.routes.chat_attachment as att_route
            with mock.patch.dict(
                    att_route.store_attachment.__globals__["_EXTRACTORS"],
                    {"pdf": lambda p: ("chat attachment pdf text, long enough", {})}):
                other = client.post(
                    "/api/chat-attachment",
                    data={"file": (io.BytesIO(b"%PDF-1.4 stub"), "a.pdf")},
                    content_type="multipart/form-data", headers=HDR)
            assert other.status_code == 200, other.get_json()
            release.set()
            t.join(timeout=20)
    finally:
        release.set()
    assert out["paper"].status_code == 200, out["paper"].get_json()


def test_the_deadline_sits_outside_the_subprocess_bound():
    import api.routes.paper as route
    from core.constants import PDF_EXTRACT_TIMEOUT_S

    assert route._UPLOAD_DEADLINE_S > PDF_EXTRACT_TIMEOUT_S

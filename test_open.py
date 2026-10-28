"""Tests for POST /api/open — the app-wide link-opening chokepoint (WP-L1).

The JS link interceptor preventDefaults every rendered anchor and posts here;
these tests pin the two modes (http/https → browser, vault path → Finder
reveal) and the refusal surface (scheme allowlist, traversal, outside-vault).
"""
import sys
from unittest import mock

import pytest
from flask import Flask

from api.routes.open import open_bp


@pytest.fixture
def client():
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(open_bp)
    from api.security import register_origin_guard

    register_origin_guard(app)
    with mock.patch("api.security.origin_is_local", return_value=True):
        yield app.test_client()


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "note.md").write_text("hi", encoding="utf-8")
    with mock.patch(
        "api.routes.open.load_config",
        return_value={"obsidian_vault_path": str(tmp_path)},
    ):
        yield tmp_path


def _post(client, body):
    return client.post("/api/open", json=body)


class TestOpenUrl:
    def test_https_url_opens_browser(self, client):
        with mock.patch("webbrowser.open") as opened:
            resp = _post(client, {"url": "https://example.com/x"})
        assert resp.status_code == 200
        assert resp.json["action"] == "url"
        opened.assert_called_once_with("https://example.com/x")

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "javascript:alert(1)",
            "obsidian://open?vault=x",
            "ftp://host/x",
            "https:",  # no netloc
            "//example.com/x",  # scheme-relative — not an allowlisted absolute URL
        ],
    )
    def test_non_http_scheme_refused(self, client, url):
        with mock.patch("webbrowser.open") as opened:
            resp = _post(client, {"url": url})
        assert resp.status_code == 400
        opened.assert_not_called()

    def test_overlong_url_refused(self, client):
        resp = _post(client, {"url": "https://e.com/" + "a" * 3000})
        assert resp.status_code == 400


@pytest.mark.skipif(sys.platform != "darwin", reason="Finder reveal is macOS-only")
class TestOpenPath:
    def test_vault_relative_path_reveals(self, client, vault):
        with mock.patch("api.routes.open.run_open") as popen:
            resp = _post(client, {"path": "notes/note.md"})
        assert resp.status_code == 200
        assert resp.json == {"ok": True, "action": "reveal", "rel": "notes/note.md"}
        popen.assert_called_once_with(
            ["open", "-R", str(vault / "notes" / "note.md")]
        )

    def test_percent_decoded_path_from_client(self, client, vault):
        # The JS interceptor decodes %20 before posting; a literal space works.
        (vault / "notes" / "my note.md").write_text("x", encoding="utf-8")
        with mock.patch("api.routes.open.run_open"):
            resp = _post(client, {"path": "notes/my note.md"})
        assert resp.status_code == 200

    def test_traversal_refused(self, client, vault):
        with mock.patch("api.routes.open.run_open") as popen:
            resp = _post(client, {"path": "../outside.md"})
        assert resp.status_code == 404
        popen.assert_not_called()

    def test_absolute_path_outside_vault_refused(self, client, vault):
        with mock.patch("api.routes.open.run_open") as popen:
            resp = _post(client, {"path": "/etc/hosts"})
        assert resp.status_code == 404
        popen.assert_not_called()

    def test_missing_file_refused(self, client, vault):
        resp = _post(client, {"path": "notes/nope.md"})
        assert resp.status_code == 404

    def test_no_vault_configured(self, client):
        with mock.patch(
            "api.routes.open.load_config", return_value={"obsidian_vault_path": ""}
        ):
            resp = _post(client, {"path": "notes/note.md"})
        assert resp.status_code == 400


def test_empty_body_refused(client):
    resp = _post(client, {})
    assert resp.status_code == 400


def test_requires_local_origin():
    app = Flask(__name__)
    app.secret_key = "t"
    app.register_blueprint(open_bp)
    from api.security import register_origin_guard

    register_origin_guard(app)
    with mock.patch("api.security.origin_is_local", return_value=False):
        resp = app.test_client().post("/api/open", json={"url": "https://x.com"})
    assert resp.status_code == 403


class TestOpenChildIsReaped:
    """DURABILITY (2026-09-02): every `open` child was a zombie until exit.

    Four route sites called subprocess.Popen and never waited, so each
    reveal/open left a defunct process entry for the life of the app — one per
    click, all session long. Asserted at ``subprocess.run`` / ``Popen`` in the
    module, the level both the pre- and post-change code reach.
    """

    def test_the_helper_waits_instead_of_leaving_a_child(self):
        from api.routes import open as open_mod
        with mock.patch.object(open_mod.subprocess, "run") as run, \
                mock.patch.object(open_mod.subprocess, "Popen") as popen:
            helper = getattr(open_mod, "run_open", None)
            if helper is None:
                open_mod.subprocess.Popen(["open", "-R", "/tmp/x"])
            else:
                helper(["open", "-R", "/tmp/x"])
        assert popen.call_count == 0, "an unwaited child was spawned"
        assert run.call_count == 1
        assert "timeout" in run.call_args.kwargs

    def test_a_wedged_open_is_killed_not_leaked(self):
        import subprocess as sp
        from api.routes import open as open_mod
        helper = getattr(open_mod, "run_open", None)
        if helper is None:
            pytest.skip("no reaping helper on this build")
        with mock.patch.object(open_mod.subprocess, "run",
                               side_effect=sp.TimeoutExpired("open", 10)):
            helper(["open", "/tmp/x"])   # must not raise: run() killed it

    def test_an_os_error_still_reaches_the_caller(self):
        """The routes' 500 path is unchanged."""
        from api.routes import open as open_mod
        helper = getattr(open_mod, "run_open", None)
        if helper is None:
            pytest.skip("no reaping helper on this build")
        with mock.patch.object(open_mod.subprocess, "run",
                               side_effect=OSError("no such binary")):
            with pytest.raises(OSError):
                helper(["open", "/tmp/x"])


def test_production_open_calls_go_through_run_open():
    """Ratchet: no leftover ``Popen(["open"`` zombies in production modules."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent
    # Strip comments so the run_open docstring's "Before:" example is not a hit.
    comment = re.compile(r"^\s*#.*$", re.M)
    offenders = []
    for path in [
        *sorted((root / "api" / "routes").glob("*.py")),
        root / "core" / "providers" / "server.py",
    ]:
        if path.name == "open.py":
            continue  # defines run_open; may document the old Popen form
        text = comment.sub("", path.read_text(encoding="utf-8"))
        if 'Popen(["open"' in text or "Popen(['open'" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"use run_open instead of Popen open: {offenders}"

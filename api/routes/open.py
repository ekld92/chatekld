"""``POST /api/open`` — the app-wide "open this link for me" chokepoint.

Rendered answers (vault chat, plain chat, refactor previews) and the
retrieval-context panel carry links the PyWebView renderer must never
navigate to itself: a relative vault path would 404 the whole window (the
"dead page" failure), and an external URL would replace the app. The JS
link interceptor (``ui.js::installLinkInterceptor``) preventDefaults every
anchor and posts here instead:

- ``{"path": "<vault-relative or absolute-under-vault>"}`` → reveal the file
  in Finder (``open -R``; macOS only, 501 elsewhere). Path resolution goes
  through :func:`core.paths.resolve_under_root` against the configured vault
  root, so traversal/NUL/escapes and anything outside the vault are refused.
- ``{"url": "<http(s)://...>"}`` → open in the system default browser.
  Scheme-allowlisted to http/https — never ``file:``/custom schemes (the
  Electron ``shell.openExternal`` RCE class).

Sibling of ``/api/audit/reveal`` (which stays audit-scoped: Zotero storage
roots, ``zotero://`` jumps); this route is deliberately vault-only.
"""
import os
import subprocess
import sys
import urllib.parse

from flask import Blueprint, jsonify, request

from api.security import sanitise_error_msg
from core.config import load_config
from core.paths import resolve_under_root

open_bp = Blueprint("open", __name__)

_URL_MAX = 2048
_PATH_MAX = 1024


def _vault_root() -> str:
    cfg = load_config()
    return (cfg.get("obsidian_vault_path") or "").strip()


@open_bp.route("/api/open", methods=["POST"])
def api_open():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400

    url = data.get("url")
    path = data.get("path")

    if isinstance(url, str) and url.strip():
        url = url.strip()
        if len(url) > _URL_MAX:
            return jsonify({"error": "URL too long"}), 400
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            return jsonify({"error": "Malformed URL"}), 400
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return jsonify({"error": "Only http/https URLs can be opened"}), 400
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception as exc:
            return jsonify({"error": sanitise_error_msg(exc)}), 500
        return jsonify({"ok": True, "action": "url"})

    if isinstance(path, str) and path.strip():
        if sys.platform != "darwin":
            return jsonify({"error": "Reveal is supported on macOS only"}), 501
        if len(path) > _PATH_MAX:
            return jsonify({"error": "Path too long"}), 400
        vault_root = _vault_root()
        if not vault_root or not os.path.isdir(vault_root):
            return jsonify({"error": "No Obsidian vault is configured"}), 400
        rel = resolve_under_root(path.strip(), vault_root, must_exist=True)
        if rel is None:
            return jsonify({"error": "File not found in the vault"}), 404
        target = os.path.join(vault_root, rel)
        try:
            subprocess.Popen(["open", "-R", target])  # noqa: S603,S607
        except OSError as exc:
            return jsonify({"error": sanitise_error_msg(exc)}), 500
        return jsonify({"ok": True, "action": "reveal", "rel": rel})

    return jsonify({"error": "url or path required"}), 400

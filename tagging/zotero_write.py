"""Zotero tag writer — local API or zotero.org Web API v3, never SQLite.

Two transports, one wire format. Zotero 7+ exposes a **local** HTTP API
(``http://127.0.0.1:23119/api/``) that mirrors Web API v3 and is *mediated by
Zotero itself*, so writing through it cannot corrupt ``zotero.sqlite``. The
**web** transport talks to ``https://api.zotero.org`` with a user API key and
reaches the same library through Zotero's sync.

Zotero 10 supports local writes after explicit runtime authorization. Local
versions carry their database's server ID into writes and restores; Web API
versions remain separate. Under ``auto``, an explicitly authorized Desktop
session takes priority; otherwise existing Web API-key selection is retained.
Local authorization keys stay in CHERQAiN process memory and never enter
configuration, results or restore journals.

Two behavioural differences the callers must respect:

* **Write direction inverts.** A web write lands on zotero.org and reaches the
  desktop only on its next sync — "applied" is not "visible in Zotero now".
* **Rate limits exist.** The web API sends ``Backoff`` / ``Retry-After``; the
  loopback endpoint never did. Honoured below (bounded), and a 429/503 becomes
  :class:`ZoteroRateLimited`.

The API key is read from the environment at call time, never persisted to
``config.json`` and never written into the restore journal — the provider-key
discipline in the root ``CLAUDE.md``. Only its sha256 fingerprint is used as a
cache key, so a rotated key can never reuse a stale-auth cache entry.

stdlib ``urllib`` only — no new dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import NoReturn

from tagging.local_api import version_headers as _version_headers

DEFAULT_BASE_URL = "http://127.0.0.1:23119"
WEB_BASE_URL = "https://api.zotero.org"
API_KEY_ENV = "ZOTERO_API_KEY"
_TIMEOUT_S = 15.0

#: Hard ceiling on how long we will honour a server ``Backoff`` hint inline.
#: The wait happens only on the READ path (which runs outside the obsidian
#: op-lock); a longer hint is truncated rather than stalling the request.
_MAX_BACKOFF_WAIT_S = 5.0


class ZoteroUnavailable(Exception):
    """The Zotero API could not be reached (Zotero closed / offline / DNS)."""


class ZoteroNotSent(ZoteroUnavailable):
    """The connection itself failed (refused, host not resolvable) BEFORE any
    request bytes left the socket, so the write cannot have landed. A timeout
    or a reset after connecting stays a plain :class:`ZoteroUnavailable`:
    the request may have been received (2026-09-17 review, B2)."""


def _unavailable(exc: BaseException) -> ZoteroUnavailable:
    reason = getattr(exc, "reason", exc)
    import socket
    if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
        return ZoteroNotSent(str(exc))
    return ZoteroUnavailable(str(exc))


class ZoteroVersionConflict(Exception):
    """The item changed in Zotero since we read it (HTTP 412) — retry."""


class ZoteroError(Exception):
    """Any other API failure (bad status, malformed response)."""

    def __init__(self, message: str = "", *, http_code: int | None = None):
        super().__init__(message)
        self.http_code = http_code


class ZoteroAuthError(ZoteroError):
    """Missing/invalid/insufficient API key (HTTP 401/403) — not retryable."""


class ZoteroNotFound(ZoteroError):
    """An authoritative item read returned 404, not a connection failure."""


class ZoteroRateLimited(ZoteroError):
    """HTTP 429/503 with a server wait hint. Subclasses :class:`ZoteroError`
    so existing ``except ZoteroError`` handlers keep working (→ HTTP 502)."""

    def __init__(self, message: str, retry_after_s: float | None = None,
                 *, http_code: int | None = None):
        super().__init__(message, http_code=http_code)
        self.retry_after_s = retry_after_s


class ZoteroWriteUnsupported(ZoteroError):
    """The endpoint refused the write method (HTTP 501).

    The signature failure of the local API on Zotero ≤ 9: reads work, writes
    do not. Distinguished from a generic error so the caller can tell the user
    to configure a web key instead of reporting an opaque 502.
    """


# --------------------------------------------------------------------------
# Transport selection
# --------------------------------------------------------------------------

class ZoteroTransport:
    """Resolved endpoint + credentials for one Zotero conversation.

    ``kind`` is ``"local"`` or ``"web"``. ``api_key`` is None for local (the
    loopback endpoint takes no key) and required for web.
    """

    __slots__ = ("kind", "base_url", "api_key")

    def __init__(self, kind: str, base_url: str, api_key: str | None):
        self.kind = kind
        self.base_url = base_url
        self.api_key = api_key

    @property
    def is_web(self) -> bool:
        return self.kind == "web"

    @property
    def can_write(self) -> bool:
        """True when this transport is *expected* to accept a tag write.

        This is transport eligibility, not a permission probe. Local authorization
        is checked at the request boundary; reads and capability checks never
        prompt. Web requires a configured key.
        """
        return bool(self.api_key) if self.is_web else True

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        # NEVER include the key itself.
        return f"<ZoteroTransport {self.kind} {self.base_url} key={'yes' if self.api_key else 'no'}>"


def api_key_from_env() -> str | None:
    """Read the Zotero API key from the environment, at call time.

    Mirrors the provider-key rule: never persisted to ``config.json``, never
    returned by ``/api/config``, never journalled.
    """
    return (os.environ.get(API_KEY_ENV) or "").strip() or None


def _validated_web_base(raw: str) -> str:
    """Return an **https** web base, or raise (E2, 2026-07-20 audit).

    The web base carries the API key, so it is re-validated at CALL TIME — not
    just by the ``/api/config`` shape check — mirroring the ``deck_compile_engine``
    "re-validate because it reaches a sensitive sink" pattern. ``resolve_transport``
    does ``str(cfg.get(...))`` with no scheme check, so a hand-edited
    ``config.json`` (``http://attacker`` for cleartext key exfiltration, or
    ``file://…``) would otherwise sail straight into urllib with the credential.
    Only ``https://`` is accepted for the web transport; anything else fails
    closed with an actionable message.
    """
    base = str(raw or "").strip()
    if not base:
        return WEB_BASE_URL
    scheme = urllib.parse.urlparse(base).scheme.lower()
    if scheme != "https":
        raise ZoteroError(
            "The Zotero web API base must be an https:// URL (it carries the "
            f"API key). Refusing to use {base!r}. Fix tagging_zotero_web_api_base "
            "in Settings.")
    return base


def resolve_transport(cfg: dict | None = None) -> ZoteroTransport:
    """Pick the transport from config + environment.

    ``tagging_zotero_transport``: ``auto`` (default) | ``local`` | ``web``.
    Under ``auto``, explicit Desktop authorization takes priority for this
    session. Otherwise a configured Web API key selects web, falling back to
    local when absent. Explicit local/web configuration remains authoritative.
    The web base is https-validated at call time (see ``_validated_web_base``);
    a malformed one raises rather than sending the key over an unsafe transport.
    """
    cfg = cfg or {}
    mode = str(cfg.get("tagging_zotero_transport") or "auto").strip().lower()
    if mode not in ("auto", "local", "web"):
        mode = "auto"
    local_base = str(cfg.get("tagging_zotero_api_base") or DEFAULT_BASE_URL)
    key = api_key_from_env()
    if mode == "local":
        return ZoteroTransport("local", local_base, None)
    if mode == "web":
        return ZoteroTransport("web", _validated_web_base(cfg.get("tagging_zotero_web_api_base")), key)
    from tagging.local_api import prefers_local
    if prefers_local(local_base):
        return ZoteroTransport('local', local_base, None)
    # Explicit Desktop authorization overrides only automatic selection.
    if key:
        return ZoteroTransport("web", _validated_web_base(cfg.get("tagging_zotero_web_api_base")), key)
    return ZoteroTransport("local", local_base, None)


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------

_backoff_lock = threading.Lock()
_backoff_until = 0.0  # monotonic deadline advertised by the server


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse EVERY redirect (E1, 2026-07-20 audit).

    stdlib's default redirect handler copies request headers — including
    ``Zotero-API-Key`` — onto the redirected request, cross-origin included. A
    malicious or MITM'd base that answers a GET with a 30x would therefore
    forward the API key to any host on the very next probe (which fires on
    Tagging-tab load, no confirm). A redirect from a Zotero API endpoint is
    never legitimate, so we fail closed instead of following it. (PATCH is
    already redirect-safe — urllib won't auto-redirect a non-GET/HEAD — but
    routing every request through one opener keeps the guarantee uniform.)
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"Refusing redirect to {newurl} (would forward the API key)",
            headers, fp)


# One opener for every request in this module — no redirects, ever.
_opener = urllib.request.build_opener(_NoRedirect)


def _urlopen(req, timeout):
    """Open *req* through the no-redirect opener (E1)."""
    from tagging.local_api import open_request
    return open_request(req, timeout, opener=_opener)


def _key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def _headers(api_key: str | None, extra: dict | None = None) -> dict:
    h = {"Zotero-API-Version": "3"}
    if api_key:
        h["Zotero-API-Key"] = api_key
    if extra:
        h.update(extra)
    return h


def _arm_backoff(secs: float | None) -> None:
    """Extend the shared backoff deadline by *secs* (ignores absent/≤0).

    Only READ paths consult it (:func:`_await_backoff`); the write path runs
    under the obsidian op-lock where sleeping would stall indexing, so arming
    from anywhere is safe — it can never make a writer sleep.
    """
    global _backoff_until
    try:
        value = float(secs) if secs else 0.0
    except (TypeError, ValueError):
        return
    if value > 0:
        with _backoff_lock:
            _backoff_until = max(_backoff_until, time.monotonic() + value)


def _note_backoff(headers) -> None:
    """Record a server ``Backoff`` hint (seconds) from a SUCCESSFUL response."""
    _arm_backoff(headers.get("Backoff") if headers else None)


def _await_backoff() -> None:
    """Honour a pending ``Backoff`` hint, bounded by ``_MAX_BACKOFF_WAIT_S``.

    Called only from read paths — the write path runs under the obsidian
    op-lock, where sleeping would stall indexing and every other tag write.
    """
    with _backoff_lock:
        remaining = _backoff_until - time.monotonic()
    if remaining > 0:
        time.sleep(min(remaining, _MAX_BACKOFF_WAIT_S))


def _retry_after_s(exc: urllib.error.HTTPError) -> float | None:
    raw = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _raise_for_http(exc: urllib.error.HTTPError, what: str) -> NoReturn:
    """Map an HTTPError onto the module's exception vocabulary."""
    if exc.code == 404:
        raise ZoteroNotFound(
            'Zotero item not found (404).', http_code=404,
        ) from exc
    if exc.code in (401, 403):
        raise ZoteroAuthError(
            f"Zotero {what} refused (HTTP {exc.code}) — check the API key's permissions.",
            http_code=exc.code,
        ) from exc
    if exc.code == 501:
        raise ZoteroWriteUnsupported(
            "This Zotero endpoint does not implement writes (HTTP 501). "
            "Zotero's local API is read-only before Zotero 10 — configure a "
            f"{API_KEY_ENV} to write through the web API instead.",
            http_code=501,
        ) from exc
    if exc.code in (429, 503):
        retry_after = _retry_after_s(exc)
        # Arm the shared deadline from the ERROR path too (2026-07-21 audit;
        # fixed 2026-07-30). Only successful responses' `Backoff` header used
        # to arm it, so the one signal that actually means "you are being
        # throttled" was captured onto the exception and then dropped — the
        # next read re-hit the same 429 immediately. Reads honour this;
        # writes never sleep on it (see _arm_backoff).
        _arm_backoff(retry_after)
        raise ZoteroRateLimited(
            f"Zotero {what} rate-limited (HTTP {exc.code}).", retry_after,
            http_code=exc.code,
        ) from exc
    raise ZoteroError(
        f"Zotero {what} failed: HTTP {exc.code}", http_code=exc.code,
    ) from exc


def library_path(item, *, user_id: int | None = None) -> str:
    """Return the API library segment for a ``ZoteroItem``.

    ``groups/<groupID>`` for a group-library item that carries a group id (the
    same on both transports). For the personal library the LOCAL API uses the
    magic ``users/0``; the WEB API needs the real numeric userID, which the
    caller resolves via :func:`resolve_user_id` and passes as *user_id*.
    """
    if getattr(item, "library_type", None) == "group" and getattr(item, "group_id", None):
        return f"groups/{int(item.group_id)}"
    return f"users/{int(user_id)}" if user_id else "users/0"


def _url(base_url: str, lib_path: str, item_key: str) -> str:
    return _items_url(base_url, lib_path) + '/' + item_key


def _items_url(base_url: str, lib_path: str) -> str:
    from tagging.local_api import origin
    if origin(base_url):
        return f"{base_url.rstrip('/')}/api/{lib_path}/items"
    return _web_url(_validated_web_base(base_url), f'{lib_path}/items')


def _web_url(base_url: str, path: str) -> str:
    """Join a path onto a web base, tolerating a base that already ends /api."""
    base = base_url.rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    return f"{base}/{path.lstrip('/')}"


#: ``{cache_key: (user_id, monotonic_expiry)}``.
_user_id_cache: dict[str, tuple[int, float]] = {}
_user_id_lock = threading.Lock()

#: Lifetime of a cached userID→key binding. The cache also stands in for
#: "this key still authenticates" (``probe`` returns True straight off a hit),
#: so an unbounded cache meant a key REVOKED at zotero.org kept reporting the
#: Zotero target as available until the app restarted — the user only found
#: out when a write failed (2026-07-21 audit; fixed 2026-07-30). 15 min keeps
#: the common case network-free while bounding how long a dead key can lie.
#: A ROTATED key needs no expiry: the fingerprint is part of the cache key.
_USER_ID_TTL_S = 900.0


def _cached_user_id(cache_key: str) -> int | None:
    """The cached userID for *cache_key* if present and unexpired, else None."""
    with _user_id_lock:
        entry = _user_id_cache.get(cache_key)
        if entry is None:
            return None
        uid, expiry = entry
        if time.monotonic() >= expiry:
            _user_id_cache.pop(cache_key, None)   # expired: force revalidation
            return None
        return uid


def resolve_user_id(base_url: str, api_key: str, timeout: float = _TIMEOUT_S) -> int:
    """Return the numeric userID for *api_key* (``GET /keys/current``), cached.

    Cached under the key's sha256 fingerprint (never the key itself), so a
    rotated key resolves afresh instead of reusing a stale identity, and for at
    most :data:`_USER_ID_TTL_S` so a revoked-but-unrotated key cannot keep
    reporting itself usable forever.
    """
    if not api_key:
        raise ZoteroAuthError(f"No {API_KEY_ENV} configured for the Zotero web API.")
    cache_key = f"{base_url}|{_key_fingerprint(api_key)}"
    hit = _cached_user_id(cache_key)
    if hit is not None:
        return hit

    req = urllib.request.Request(
        _web_url(base_url, "keys/current"), method="GET", headers=_headers(api_key))
    try:
        with _urlopen(req, timeout) as resp:
            _note_backoff(resp.headers)
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        _raise_for_http(exc, "key lookup")
    except (urllib.error.URLError, OSError) as exc:
        raise ZoteroUnavailable(str(exc)) from exc
    except ValueError as exc:
        raise ZoteroError(f"Malformed Zotero key response: {exc}") from exc

    uid = payload.get("userID") if isinstance(payload, dict) else None
    if not isinstance(uid, int):
        raise ZoteroError("Zotero key response has no integer userID.")
    if not ((payload.get("access") or {}).get("user") or {}).get("write"):
        raise ZoteroAuthError(
            "The Zotero API key has no write permission on the personal library.")
    with _user_id_lock:
        _user_id_cache[cache_key] = (uid, time.monotonic() + _USER_ID_TTL_S)
    return uid


def probe(base_url: str = DEFAULT_BASE_URL, timeout: float = _TIMEOUT_S,
          api_key: str | None = None) -> bool:
    """True iff the endpoint answers.

    Local: any HTTP status means Zotero is up. Web: the key must actually
    authenticate, so a 401/403 is *not* "available" — an unauthenticated web
    endpoint is reachable but useless.
    """
    if api_key:
        try:
            resolve_user_id(base_url, api_key, timeout=timeout)
            return True
        except (ZoteroUnavailable, ZoteroError):
            return False
    try:
        req = urllib.request.Request(f"{base_url.rstrip('/')}/api/", method="GET")
        _urlopen(req, timeout).close()
        return True
    except urllib.error.HTTPError:
        return True  # server answered (e.g. 404 at /api/) — it's up
    except (urllib.error.URLError, OSError, ValueError):
        return False


def get_item_tags_full(base_url: str, lib_path: str, item_key: str,
                       timeout: float = _TIMEOUT_S,
                       api_key: str | None = None, server_id: str | None = None) -> tuple[int, list[dict]]:
    """Return ``(version, raw_tags)`` — the item's tag dicts VERBATIM.

    ``raw_tags`` preserves every per-tag field Zotero stores (notably
    ``type: 1`` marking an *automatic* tag, and colour assignments ride on the
    name): the writer must PATCH the full array back, so dropping fields here
    would silently rewrite metadata on tags the user never touched — the
    pre-2026-07-18 name-only round-trip converted every automatic tag to
    manual on each write. Raises :class:`ZoteroUnavailable` /
    :class:`ZoteroError` on transport / shape problems.
    """
    from tagging.local_api import read_headers
    extra = read_headers(base_url, server_id)
    _await_backoff()
    req = urllib.request.Request(
        _url(base_url, lib_path, item_key), method="GET", headers=_headers(api_key, extra))
    try:
        with _urlopen(req, timeout) as resp:
            _note_backoff(resp.headers)
            payload = json.loads(resp.read().decode("utf-8"))
            server_id = resp.headers.get('Zotero-Server-ID')
            if extra and server_id != extra['Zotero-Server-ID']:
                raise ZoteroError('Zotero database identity changed during restore lookup.')
    except urllib.error.HTTPError as exc:
        _raise_for_http(exc, "GET")
    except (urllib.error.URLError, OSError) as exc:
        raise ZoteroUnavailable(str(exc)) from exc
    except ValueError as exc:
        raise ZoteroError(f"Malformed Zotero response: {exc}") from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ZoteroError("Unexpected Zotero item shape (no 'data').")
    version = payload.get("version") or data.get("version")
    if not isinstance(version, int):
        raise ZoteroError("Zotero item has no integer version.")
    raw_tags = [t for t in data.get("tags", [])
                if isinstance(t, dict) and str(t.get("tag", "")).strip()]
    from tagging.local_api import Version
    return Version(version, server_id), raw_tags


def tag_names(raw_tags: list[dict]) -> list[str]:
    """Extract the plain name list from raw tag dicts (display / compare)."""
    return [str(t.get("tag", "")).strip() for t in raw_tags]


def get_item_tags(base_url: str, lib_path: str, item_key: str,
                  timeout: float = _TIMEOUT_S,
                  api_key: str | None = None, server_id: str | None = None) -> tuple[int, list[str]]:
    """Name-only convenience over :func:`get_item_tags_full`.

    Kept for callers that only *compare* tag sets (the conservative revert
    guard) — anything that will WRITE the array back must use the full variant
    or it reintroduces the metadata-stripping bug documented there.
    """
    version, raw = get_item_tags_full(base_url, lib_path, item_key,
                                      timeout=timeout, api_key=api_key, **({"server_id": server_id} if server_id is not None else {}))
    return version, tag_names(raw)


def set_item_tags(base_url: str, lib_path: str, item_key: str, tags: list,
                  version: int, timeout: float = _TIMEOUT_S,
                  api_key: str | None = None) -> int:
    """PATCH an item's full tag set, guarded by *version*; return the new version.

    *tags* entries may be plain names (wrapped as ``{"tag": name}`` — a NEW tag
    has no metadata yet) or raw dicts from :func:`get_item_tags_full` (passed
    through verbatim so existing tags keep ``type``/colour metadata). Sends
    ``If-Unmodified-Since-Version``; raises :class:`ZoteroVersionConflict` on
    412 (the item moved under us), :class:`ZoteroWriteUnsupported` on 501 (the
    read-only local API before Zotero 10), :class:`ZoteroUnavailable` on
    transport failure, :class:`ZoteroError` else.

    *version* MUST come from a fresh :func:`get_item_tags_full` — never from a
    previous write's response. Zotero's create/write responses report the
    library version, which can already differ from the item's own (observed
    live: a POST returned 77060 while the item was at 77061, so a PATCH reusing
    it 412'd).
    """
    payload = [t if isinstance(t, dict) else {"tag": str(t)} for t in tags]
    body = json.dumps({"tags": payload}).encode("utf-8")
    req = urllib.request.Request(
        _url(base_url, lib_path, item_key), data=body, method="PATCH",
        headers=_headers(api_key, {
            "Content-Type": "application/json",
            **_version_headers(version),
        }),
    )
    try:
        with _urlopen(req, timeout) as resp:
            _note_backoff(resp.headers)
            new_version = resp.headers.get("Last-Modified-Version")
            return int(new_version) if new_version and new_version.isdigit() else version
    except urllib.error.HTTPError as exc:
        if exc.code == 412:
            raise ZoteroVersionConflict("Item changed in Zotero (412).") from exc
        _raise_for_http(exc, "PATCH")
    except (urllib.error.URLError, OSError) as exc:
        raise _unavailable(exc) from exc


# --- child NOTE bodies (note sync Phase 4, 2026-09-04) ----------------------
#
# A Zotero note is an item whose ``note`` field is HTML. Both verbs are
# version-guarded exactly like the tag writers, and the same rule applies:
# *version* comes from a fresh :func:`get_item`, never from a write response.
# Verified live 2026-09-04 (spike S1): PATCH answers 204, a stale version 412,
# and an ``<img data-attachment-key>`` node inside the HTML survives untouched.

def set_item_note(base_url: str, lib_path: str, note_key: str, html: str,
                  version: int, timeout: float = _TIMEOUT_S,
                  api_key: str | None = None) -> int:
    """PATCH a note item's ``note`` (HTML), guarded by *version*; return the
    new version. Raises :class:`ZoteroVersionConflict` on 412,
    :class:`ZoteroWriteUnsupported` on 501, :class:`ZoteroUnavailable` on
    transport failure, :class:`ZoteroError` else."""
    body = json.dumps({"note": str(html or "")}).encode("utf-8")
    req = urllib.request.Request(
        _url(base_url, lib_path, note_key), data=body, method="PATCH",
        headers=_headers(api_key, {
            "Content-Type": "application/json",
            **_version_headers(version),
        }),
    )
    try:
        with _urlopen(req, timeout) as resp:
            _note_backoff(resp.headers)
            new_version = resp.headers.get("Last-Modified-Version")
            return int(new_version) if new_version and new_version.isdigit() else version
    except urllib.error.HTTPError as exc:
        if exc.code == 412:
            raise ZoteroVersionConflict("Note changed in Zotero (412).") from exc
        _raise_for_http(exc, "PATCH")
    except (urllib.error.URLError, OSError) as exc:
        raise _unavailable(exc) from exc


def create_child_note(base_url: str, lib_path: str, parent_key: str, html: str,
                      timeout: float = _TIMEOUT_S,
                      api_key: str | None = None, server_id: str | None = None) -> str:
    """POST one child note under *parent_key*; return the NEW note's key.

    The only verb in the app that CREATES a Zotero item. Its undo is a trash
    (``set_item_deleted``), never a DELETE — the same argument as the
    attachment de-link.
    """
    payload = [{"itemType": "note", "parentItem": parent_key, "note": str(html or "")}]
    url = _items_url(base_url, lib_path)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers=_headers(api_key, {"Content-Type": "application/json",
                                  **({'Zotero-Server-ID': server_id} if server_id else {})}))
    try:
        with _urlopen(req, timeout) as resp:
            _note_backoff(resp.headers)
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        _raise_for_http(exc, "POST")
    except (urllib.error.URLError, OSError) as exc:
        raise ZoteroUnavailable(str(exc)) from exc
    except ValueError as exc:
        raise ZoteroError(f"Malformed Zotero response: {exc}") from exc
    ok = (body.get("successful") or {}) if isinstance(body, dict) else {}
    entry = ok.get("0") if isinstance(ok, dict) else None
    key = entry.get("key") if isinstance(entry, dict) else None
    if not key:
        failed = (body.get("failed") or {}) if isinstance(body, dict) else {}
        raise ZoteroError(f"Zotero did not create the note: {failed or body}")
    return _created_item_key(entry)


class CreatedItemKey(str):
    """A string-compatible key carrying only POST acknowledgement evidence."""

    creation_data: dict | None
    creation_version: int | None

    def __new__(cls, key, data=None, version=None):
        obj = super().__new__(cls, key)
        obj.creation_data = data
        obj.creation_version = version
        return obj


# Backward-compatible name for attachment callers and stored test fixtures.
CreatedAttachmentKey = CreatedItemKey


def _created_item_key(entry):
    key = entry['key']
    data, version = entry.get('data'), entry.get('version')
    if (not isinstance(data, dict) or data.get('key') != key
            or type(version) is not int or version <= 0):
        data, version = None, None
    return CreatedItemKey(str(key), data, version)


#: Item types a proposal may mint. A CLOSED vocabulary, for the same reason
#: `_canonicalise_tags` gates tags: the type decides which fields Zotero will
#: keep, and a client (or an LLM proposal) that could name any type could mint
#: an item whose fields silently vanish on save. `attachment` and `note` are
#: absent deliberately — they have their own verbs, and both need a parent.
PARENT_ITEM_TYPES = (
    "journalArticle", "bookSection", "book", "report", "thesis",
    "conferencePaper", "webpage", "blogPost", "magazineArticle",
    "newspaperArticle", "preprint", "document", "manuscript",
)

#: Keys a caller may never put in `fields` — every one of them is STRUCTURE
#: rather than bibliographic content, and the field map is the one part of
#: this payload a client controls.
#:
#: `parentItem` would make a CHILD (this verb mints top-level items);
#: `key`/`version` would make it a keyed create, which Zotero Desktop refuses
#: with `primaryData not loaded` (see `create_linked_file_attachment`, whose
#: reservation is retained rather than retried unkeyed); `itemType`,
#: `creators` and `tags` arrive as their own arguments, and letting the map
#: reach them would mean a string landing where Zotero expects an array;
#: `deleted` would create an item already in the trash; the two `date*`
#: stamps are the server's to write. Refusing them explicitly matters
#: because the alternative — relying on these assignments running after the
#: loop — is a safety property that a later reordering silently removes.
_RESERVED_FIELD_KEYS = frozenset({
    "itemType", "parentItem", "key", "version", "collections", "relations",
    "creators", "tags", "deleted", "dateAdded", "dateModified",
})


def create_parent_item(base_url: str, lib_path: str, item_type: str,
                       fields: dict | None = None,
                       creators: list | None = None,
                       tags: list | None = None,
                       timeout: float = _TIMEOUT_S,
                       api_key: str | None = None,
                       server_id: str | None = None) -> CreatedItemKey:
    """POST one TOP-LEVEL item; return its key plus creation evidence.

    The second item-creating verb after `create_child_note`, and the first
    that mints a PARENT. Deliberately unkeyed: a reserved-key create is
    refused by Zotero Desktop (see `create_linked_file_attachment`), so the
    lost-response guard is the caller's attempt store, never a key we chose.

    Its undo is a trash (`set_item_deleted`), never a DELETE — the same
    argument as the note create and the attachment de-link.
    """
    if item_type not in PARENT_ITEM_TYPES:
        raise ZoteroError(f"Refusing to create an item of type {item_type!r}.")
    content: dict = {}
    for name, value in (fields or {}).items():
        if name in _RESERVED_FIELD_KEYS:
            raise ZoteroError(f"Field {name!r} may not be set on a create.")
        if value in (None, ""):
            continue
        content[str(name)] = str(value)
    # Structure last, so it wins whatever the field map contained. The guard
    # above already refuses these names; this is the belt to its braces.
    payload: dict = {**content, "itemType": item_type,
                     "tags": list(tags or []), "relations": {},
                     "collections": []}
    cleaned: list[dict] = []
    for creator in (creators or []):
        if not isinstance(creator, dict):
            raise ZoteroError("Each creator must be an object.")
        ctype = str(creator.get("creatorType") or "author")
        if "name" in creator:
            built = {"creatorType": ctype, "name": str(creator["name"])}
        else:
            built = {"creatorType": ctype,
                     "firstName": str(creator.get("firstName") or ""),
                     "lastName": str(creator.get("lastName") or "")}
            if not built["lastName"]:
                continue
        cleaned.append(built)
    if cleaned:
        payload["creators"] = cleaned
    url = _items_url(base_url, lib_path)
    req = urllib.request.Request(
        url, data=json.dumps([payload]).encode("utf-8"), method="POST",
        headers=_headers(api_key, {"Content-Type": "application/json",
                                  **({'Zotero-Server-ID': server_id} if server_id else {})}))
    try:
        with _urlopen(req, timeout) as resp:
            _note_backoff(resp.headers)
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        _raise_for_http(exc, "POST")
    except (urllib.error.URLError, OSError) as exc:
        raise ZoteroUnavailable(str(exc)) from exc
    except ValueError as exc:
        raise ZoteroError(f"Malformed Zotero response: {exc}") from exc
    ok = (body.get("successful") or {}) if isinstance(body, dict) else {}
    entry = ok.get("0") if isinstance(ok, dict) else None
    key = entry.get("key") if isinstance(entry, dict) else None
    if not key:
        failed = (body.get("failed") or {}) if isinstance(body, dict) else {}
        raise ZoteroError(f"Zotero did not create the item: {failed or body}")
    return _created_item_key(entry)


def create_linked_file_attachment(base_url: str, lib_path: str, parent_key: str,
                                  path: str, title: str,
                                  content_type: str = "application/pdf",
                                  timeout: float = _TIMEOUT_S,
                                  api_key: str | None = None, item_key: str | None = None,
                                  server_id: str | None = None) -> str:
    """POST one ``linked_file`` attachment under *parent_key*; return its key.

    Spike S1 (2026-09-06, live against api.zotero.org): a linked-file
    attachment is a plain item write — ``path`` and no upload step; the
    server holds no bytes for it (``GET …/file`` answers 404), so it consumes
    no Zotero storage, and it syncs down to the desktop within seconds. The
    bytes stay in the vault. Its undo is a trash (``set_item_deleted``), never
    a DELETE — the same argument as the de-link and the note create.
    """
    payload: list[dict] = [{
        "itemType": "attachment", "linkMode": "linked_file",
        "parentItem": parent_key, "title": str(title or ""),
        "contentType": content_type, "charset": "", "path": str(path),
        "tags": [], "relations": {},
    }]
    if item_key is not None:
        if len(item_key) != 8 or any(c not in '23456789ABCDEFGHIJKLMNPQRSTUVWXYZ' for c in item_key):
            raise ZoteroError('Invalid reserved attachment key.')
        payload[0].update(key=item_key, version=0)
    url = _items_url(base_url, lib_path)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers=_headers(api_key, {"Content-Type": "application/json",
                                  **({'Zotero-Server-ID': server_id} if server_id else {})}))
    try:
        with _urlopen(req, timeout) as resp:
            _note_backoff(resp.headers)
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        _raise_for_http(exc, "POST")
    except (urllib.error.URLError, OSError) as exc:
        raise ZoteroUnavailable(str(exc)) from exc
    except ValueError as exc:
        raise ZoteroError(f"Malformed Zotero response: {exc}") from exc
    ok = (body.get("successful") or {}) if isinstance(body, dict) else {}
    entry = ok.get("0") if isinstance(ok, dict) else None
    key = entry.get("key") if isinstance(entry, dict) else None
    if not key:
        failed = (body.get("failed") or {}) if isinstance(body, dict) else {}
        failure = failed.get('0', {}) if isinstance(failed, dict) else {}
        from tagging.local_api import origin
        if (item_key is not None and origin(base_url) and isinstance(failure, dict)
                and failure.get('code') == 400
                and "'primaryData' not loaded" in str(failure.get('message', ''))):
            raise ZoteroWriteUnsupported(
                'Zotero Desktop rejected creation with a reserved attachment key '
                '(verified in Zotero 10.0.1). The reservation is retained; no unkeyed '
                'retry was sent. Durable attachment creation requires a compatible '
                'Desktop implementation or an explicitly selected Web transport.')
        raise ZoteroError(f"Zotero did not create the attachment: {failed or body}")
    # The successful entry describes this object at creation. The response
    # header is a library version and is deliberately not an undo baseline.
    return _created_item_key(entry)


# --- attachment de-link ----------------------------------------------------
#
# Removing an attachment from a Zotero item is a TRASH, never a DELETE, and the
# distinction is the whole safety argument (see api/routes/zotlink.py):
#
#   * `PATCH {"deleted": 1}` is reversible with `{"deleted": 0}`, keeps the
#     item KEY (so its relations and any annotations survive), and — for an
#     `imported_*` attachment, whose bytes live in ~/Zotero/storage/<key>/ —
#     leaves those bytes on disk until the user empties Zotero's trash.
#   * `DELETE /items/<key>` destroys all three. Re-creating the attachment
#     afterwards mints a NEW key, so the round trip is not an undo.
#
# So no DELETE verb is offered here. Doing that deliberately, and saying why,
# is cheaper than discovering it from a user's lost highlights.

def get_item(base_url: str, lib_path: str, item_key: str,
             timeout: float = _TIMEOUT_S,
             api_key: str | None = None, server_id: str | None = None) -> tuple[dict, int]:
    """GET one item; return ``(data, version)``.

    ``data`` is the item's ``data`` object verbatim — for an attachment that
    carries ``linkMode``, ``parentItem``, ``filename``/``path`` and, when the
    item is already trashed, ``deleted``. The version is read from the
    ``Last-Modified-Version`` header, falling back to the body's own field, and
    is what the caller must pass to :func:`set_item_deleted`.
    """
    from tagging.local_api import read_headers
    extra = read_headers(base_url, server_id)
    _await_backoff()
    req = urllib.request.Request(
        _url(base_url, lib_path, item_key), method="GET", headers=_headers(api_key, extra))
    try:
        with _urlopen(req, timeout) as resp:
            _note_backoff(resp.headers)
            payload = json.loads(resp.read().decode("utf-8", "replace"))
            header_version = resp.headers.get("Last-Modified-Version")
            server_id = resp.headers.get('Zotero-Server-ID')
            if extra and server_id != extra['Zotero-Server-ID']:
                raise ZoteroError('Zotero database identity changed during restore lookup.')
    except urllib.error.HTTPError as exc:
        _raise_for_http(exc, "GET")
        raise                                   # pragma: no cover — _raise_for_http raises
    except (urllib.error.URLError, OSError) as exc:
        raise ZoteroUnavailable(str(exc)) from exc
    except ValueError as exc:
        raise ZoteroError("Malformed Zotero item response.") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ZoteroError("Unexpected item payload from Zotero.")
    if header_version and str(header_version).isdigit():
        version = int(header_version)
    else:
        try:
            version = int(data.get("version") or payload.get("version") or 0)
        except (TypeError, ValueError) as exc:
            raise ZoteroError("Malformed Zotero item version.") from exc
    from tagging.local_api import Version
    return data, Version(version, server_id)


def set_attachment_path(base_url: str, lib_path: str, item_key: str,
                        path: str, version: int, timeout: float = _TIMEOUT_S,
                        api_key: str | None = None) -> int:
    """Re-point a ``linked_file`` attachment at *path*; return the new version.

    ONE field. The attachment's ``title`` — what Zotero displays — is
    deliberately not touched: the maintainer's contract is that the vault
    filename is authoritative and Zotero's display name is nobody's business,
    so a rename must move the POINTER and nothing else. ``filename`` and
    ``md5`` are null for a linked file (verified live, spike S1) and stay
    that way; Zotero stores nothing.

    This exists because the library now holds 5 242 ``linked_file``
    attachments carrying ABSOLUTE vault paths (the 2026-09-06 attach run), so
    every rename breaks a real reference. It was performed by hand nine times
    after the Zotero-preference and Attanger rename incidents; this is that
    call, with a version guard.

    Version-guarded exactly like :func:`set_item_tags`: *version* MUST come
    from a fresh :func:`get_item` and never from a previous write's response
    (a write reports the LIBRARY version, which can already differ from the
    item's own — observed live, a POST returned 77060 while the item was at
    77061 and a PATCH reusing it 412'd).

    Raises :class:`ZoteroVersionConflict` on 412,
    :class:`ZoteroWriteUnsupported` on 501 (the read-only local API before
    Zotero 10), :class:`ZoteroUnavailable` on transport failure.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string.")
    return _patch_attachment_field(base_url, lib_path, item_key, "path", path,
                                   version, timeout, api_key)


def set_attachment_title(base_url: str, lib_path: str, item_key: str,
                         title: str, version: int, timeout: float = _TIMEOUT_S,
                         api_key: str | None = None) -> int:
    """Set an attachment's display ``title``; return the new version.

    The annual cleanup's T4 (2026-09-18, decision D3: an attachment's title
    equals its vault filename). ONE field, like :func:`set_attachment_path`,
    so the revert is the same PATCH with the old title. Measured before it was
    built: 653 of 6,246 personal linked attachments carried a title other than
    their filename, 505 of them "Full Text PDF"/"PDF" from connector saves.

    Version-guarded exactly like :func:`set_attachment_path` (and for the same
    reason: *version* comes from a fresh :func:`get_item`, never from a
    previous write's response).
    """
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be a non-empty string.")
    return _patch_attachment_field(base_url, lib_path, item_key, "title", title,
                                   version, timeout, api_key)


def _patch_attachment_field(base_url, lib_path, item_key, field, value,
                            version, timeout, api_key) -> int:
    """PATCH one attachment field under ``If-Unmodified-Since-Version``."""
    body = json.dumps({field: value}).encode("utf-8")
    req = urllib.request.Request(
        _url(base_url, lib_path, item_key), data=body, method="PATCH",
        headers=_headers(api_key, {
            "Content-Type": "application/json",
            **_version_headers(version),
        }),
    )
    try:
        with _urlopen(req, timeout) as resp:
            _note_backoff(resp.headers)
            new_version = resp.headers.get("Last-Modified-Version")
            return int(new_version) if new_version and new_version.isdigit() else version
    except urllib.error.HTTPError as exc:
        if exc.code == 412:
            raise ZoteroVersionConflict("Attachment changed in Zotero (412).") from exc
        _raise_for_http(exc, "PATCH")
        raise                                   # pragma: no cover — _raise_for_http raises
    except (urllib.error.URLError, OSError) as exc:
        raise ZoteroUnavailable(str(exc)) from exc


def set_item_parent(base_url: str, lib_path: str, item_key: str,
                    parent_key: str, version: int, timeout: float = _TIMEOUT_S,
                    api_key: str | None = None) -> int:
    """Move a CHILD item (attachment or note) under *parent_key*; return the new version.

    The record-merge verb's re-parent (annual cleanup T8, 2026-09-17). ONE
    field — ``parentItem`` — so the child keeps its key, its path, its
    annotations and its own tags; only which item it hangs off changes, and
    the revert is the same PATCH with the old parent. Version-guarded exactly
    like :func:`set_item_tags`: *version* MUST be the child's own current
    version from a fresh read, never a write response's library version.

    Raises :class:`ZoteroVersionConflict` on 412, :class:`ZoteroWriteUnsupported`
    on 501, :class:`ZoteroUnavailable` on transport failure, ``ValueError`` on
    an empty parent key.
    """
    if not isinstance(parent_key, str) or not parent_key.strip():
        raise ValueError("parent_key must be a non-empty string.")
    body = json.dumps({"parentItem": parent_key}).encode("utf-8")
    req = urllib.request.Request(
        _url(base_url, lib_path, item_key), data=body, method="PATCH",
        headers=_headers(api_key, {
            "Content-Type": "application/json",
            **_version_headers(version),
        }),
    )
    try:
        with _urlopen(req, timeout) as resp:
            _note_backoff(resp.headers)
            new_version = resp.headers.get("Last-Modified-Version")
            return int(new_version) if new_version and new_version.isdigit() else version
    except urllib.error.HTTPError as exc:
        if exc.code == 412:
            raise ZoteroVersionConflict("Item changed in Zotero (412).") from exc
        _raise_for_http(exc, "PATCH")
        raise                                   # pragma: no cover — _raise_for_http raises
    except (urllib.error.URLError, OSError) as exc:
        raise ZoteroUnavailable(str(exc)) from exc


#: Fields a metadata write may never carry: structure, identity, children and
#: server-owned stamps. ``extra`` and ``citationKey`` are NOT here — they are
#: ordinary fields that the writer refuses unless EXPLICITLY selected, because
#: Better BibTeX keeps its pinned key in one of them.
METADATA_RESERVED_FIELDS = frozenset({
    "itemType", "parentItem", "key", "version", "collections", "relations",
    "tags", "deleted", "dateAdded", "dateModified", "creators",
})


def set_item_fields(base_url: str, lib_path: str, item_key: str,
                    fields: dict, version: int, timeout: float = _TIMEOUT_S,
                    api_key: str | None = None) -> int:
    """PATCH exactly *fields* on one item; return the new version.

    The metadata executor's ONE write verb (phase 5, 2026-09-16). Only the
    given data fields ride the body — a structural key in *fields* is a
    caller bug and is refused here rather than sent, since Zotero would accept
    some of them silently. Version-guarded exactly like :func:`set_item_tags`:
    *version* MUST come from a fresh :func:`get_item`, never from a previous
    write's response. The caller re-reads the item after the PATCH: Zotero
    normalises on save (observed on 10.0.1: a title-only PATCH promoted an
    ``Extra: Citation Key:`` line into native ``citationKey``), so what landed
    is only known from the readback, never from the request.

    Raises :class:`ZoteroVersionConflict` on 412,
    :class:`ZoteroWriteUnsupported` on 501, :class:`ZoteroUnavailable` on
    transport failure, ``ValueError`` on an empty or reserved field set.
    """
    if not isinstance(fields, dict) or not fields:
        raise ValueError("fields must be a non-empty mapping.")
    reserved = sorted(set(fields) & METADATA_RESERVED_FIELDS)
    if reserved:
        raise ValueError(f"Reserved fields cannot be written: {', '.join(reserved)}.")
    if any(not isinstance(name, str) or not name for name in fields):
        raise ValueError("Field names must be non-empty strings.")
    body = json.dumps(fields, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        _url(base_url, lib_path, item_key), data=body, method="PATCH",
        headers=_headers(api_key, {
            "Content-Type": "application/json",
            **_version_headers(version),
        }),
    )
    try:
        with _urlopen(req, timeout) as resp:
            _note_backoff(resp.headers)
            new_version = resp.headers.get("Last-Modified-Version")
            return int(new_version) if new_version and new_version.isdigit() else version
    except urllib.error.HTTPError as exc:
        if exc.code == 412:
            raise ZoteroVersionConflict("Item changed in Zotero (412).") from exc
        _raise_for_http(exc, "PATCH")
        raise                                   # pragma: no cover — _raise_for_http raises
    except (urllib.error.URLError, OSError) as exc:
        raise ZoteroUnavailable(str(exc)) from exc


def set_item_deleted(base_url: str, lib_path: str, item_key: str,
                     deleted: bool, version: int, timeout: float = _TIMEOUT_S,
                     api_key: str | None = None) -> int:
    """Move an item to Zotero's trash (or back out of it); return the new version.

    ``deleted=True`` trashes, ``deleted=False`` restores — one verb for both
    directions so the undo cannot drift from the do. Version-guarded exactly
    like :func:`set_item_tags`: *version* MUST come from a fresh :func:`get_item`
    and never from a previous write's response (a write reports the LIBRARY
    version, which can already differ from the item's own).

    Raises :class:`ZoteroVersionConflict` on 412, :class:`ZoteroWriteUnsupported`
    on 501 (the read-only local API before Zotero 10), :class:`ZoteroUnavailable`
    on transport failure.
    """
    body = json.dumps({"deleted": 1 if deleted else 0}).encode("utf-8")
    req = urllib.request.Request(
        _url(base_url, lib_path, item_key), data=body, method="PATCH",
        headers=_headers(api_key, {
            "Content-Type": "application/json",
            **_version_headers(version),
        }),
    )
    try:
        with _urlopen(req, timeout) as resp:
            _note_backoff(resp.headers)
            new_version = resp.headers.get("Last-Modified-Version")
            return int(new_version) if new_version and new_version.isdigit() else version
    except urllib.error.HTTPError as exc:
        if exc.code == 412:
            raise ZoteroVersionConflict("Item changed in Zotero (412).") from exc
        _raise_for_http(exc, "PATCH")
        raise                                   # pragma: no cover — _raise_for_http raises
    except (urllib.error.URLError, OSError) as exc:
        raise ZoteroUnavailable(str(exc)) from exc


def get_item_children(base_url: str, lib_path: str, parent_key: str,
                      timeout: float = _TIMEOUT_S, api_key: str | None = None,
                      server_id: str | None = None) -> list[dict]:
    """Read current child paths before a selected-file create/retry.

    Zotero API v3 /items/<key>/children, paginated on the same trusted origin.
    Refuse incomplete or changing results rather than treating them as empty.
    https://www.zotero.org/support/dev/web_api/v3/basics
    """
    from tagging.local_api import read_headers
    extra = read_headers(base_url, server_id)
    children = []
    version = None
    instance = server_id
    for start in range(0, 1000, 100):
        _await_backoff()
        req = urllib.request.Request(
            _url(base_url, lib_path, parent_key) + f'/children?format=json&limit=100&start={start}',
            headers=_headers(api_key, extra), method='GET')
        try:
            with _urlopen(req, timeout) as resp:
                _note_backoff(resp.headers)
                current_instance = resp.headers.get('Zotero-Server-ID')
                if instance is not None and current_instance != instance:
                    raise ZoteroError('Zotero database identity changed during child lookup.')
                instance = current_instance
                current = resp.headers.get('Last-Modified-Version')
                if version is not None and current != version:
                    raise ZoteroError('Zotero changed while checking attachments. Retry.')
                version = current
                raw = resp.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ZoteroError('Zotero attachment response is too large.')
                items = json.loads(raw)
        except urllib.error.HTTPError as exc:
            _raise_for_http(exc, 'GET')
        except (urllib.error.URLError, OSError) as exc:
            raise ZoteroUnavailable(str(exc)) from exc
        except ValueError as exc:
            raise ZoteroError('Malformed Zotero attachment response.') from exc
        if not isinstance(items, list):
            raise ZoteroError('Malformed Zotero attachment list.')
        for item in items:
            data = item.get('data') if isinstance(item, dict) else None
            if not isinstance(data, dict):
                raise ZoteroError('Malformed Zotero child item.')
            children.append(data)
        if len(items) < 100:
            return children
    raise ZoteroError('Too many attachments to verify completely. No attachment created.')


def linked_file_paths(base_url: str, lib_path: str, parent_key: str,
                      timeout: float = _TIMEOUT_S, api_key: str | None = None,
                      server_id: str | None = None) -> set[str]:
    """Return verified live linked paths in one database/version namespace."""
    return {str(data['path']) for data in get_item_children(
        base_url, lib_path, parent_key, timeout=timeout, api_key=api_key, server_id=server_id)
        if data.get('linkMode') == 'linked_file' and not data.get('deleted') and data.get('path')}

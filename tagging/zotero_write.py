"""Zotero tag writer — local API or zotero.org Web API v3, never SQLite.

Two transports, one wire format. Zotero 7+ exposes a **local** HTTP API
(``http://127.0.0.1:23119/api/``) that mirrors Web API v3 and is *mediated by
Zotero itself*, so writing through it cannot corrupt ``zotero.sqlite``. The
**web** transport talks to ``https://api.zotero.org`` with a user API key and
reaches the same library through Zotero's sync.

**Why both exist:** verified live 2026-07-20 against Zotero 9.0.6 — the local
API answers GET but returns **501 "Method not implemented"** for PATCH/PUT
(write support is slated for Zotero 10), so the local transport cannot write
tags today. The identical PATCH against api.zotero.org returned 204, preserved
per-tag ``type`` metadata verbatim, honoured ``If-Unmodified-Since-Version``
(412 on a stale version), and synced down to the desktop app. The web transport
is therefore the working write path until Zotero 10 ships; ``auto`` picks it
whenever a key is present and falls back to local otherwise, so nothing
regresses when the local API gains PATCH.

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


class ZoteroVersionConflict(Exception):
    """The item changed in Zotero since we read it (HTTP 412) — retry."""


class ZoteroError(Exception):
    """Any other API failure (bad status, malformed response)."""


class ZoteroAuthError(ZoteroError):
    """Missing/invalid/insufficient API key (HTTP 401/403) — not retryable."""


class ZoteroRateLimited(ZoteroError):
    """HTTP 429/503 with a server wait hint. Subclasses :class:`ZoteroError`
    so existing ``except ZoteroError`` handlers keep working (→ HTTP 502)."""

    def __init__(self, message: str, retry_after_s: float | None = None):
        super().__init__(message)
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

        Local is optimistically True — it is the correct answer once Zotero 10
        ships PATCH, and a 501 surfaces as :class:`ZoteroWriteUnsupported`
        either way. Web requires a key.
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
    Under ``auto`` a configured API key selects the web transport — because the
    local one cannot write on Zotero ≤ 9 — and its absence falls back to local.
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
    # auto: web only when a key is present (only it can write); else local.
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
    return _opener.open(req, timeout=timeout)


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


def _raise_for_http(exc: urllib.error.HTTPError, what: str):
    """Map an HTTPError onto the module's exception vocabulary."""
    if exc.code in (401, 403):
        raise ZoteroAuthError(
            f"Zotero {what} refused (HTTP {exc.code}) — check the API key's permissions."
        ) from exc
    if exc.code == 501:
        raise ZoteroWriteUnsupported(
            "This Zotero endpoint does not implement writes (HTTP 501). "
            "Zotero's local API is read-only before Zotero 10 — configure a "
            f"{API_KEY_ENV} to write through the web API instead."
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
            f"Zotero {what} rate-limited (HTTP {exc.code}).", retry_after
        ) from exc
    raise ZoteroError(f"Zotero {what} failed: HTTP {exc.code}") from exc


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
    return f"{base_url.rstrip('/')}/api/{lib_path}/items/{item_key}"


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
                       api_key: str | None = None) -> tuple[int, list[dict]]:
    """Return ``(version, raw_tags)`` — the item's tag dicts VERBATIM.

    ``raw_tags`` preserves every per-tag field Zotero stores (notably
    ``type: 1`` marking an *automatic* tag, and colour assignments ride on the
    name): the writer must PATCH the full array back, so dropping fields here
    would silently rewrite metadata on tags the user never touched — the
    pre-2026-07-18 name-only round-trip converted every automatic tag to
    manual on each write. Raises :class:`ZoteroUnavailable` /
    :class:`ZoteroError` on transport / shape problems.
    """
    _await_backoff()
    req = urllib.request.Request(
        _url(base_url, lib_path, item_key), method="GET", headers=_headers(api_key))
    try:
        with _urlopen(req, timeout) as resp:
            _note_backoff(resp.headers)
            payload = json.loads(resp.read().decode("utf-8"))
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
    return version, raw_tags


def tag_names(raw_tags: list[dict]) -> list[str]:
    """Extract the plain name list from raw tag dicts (display / compare)."""
    return [str(t.get("tag", "")).strip() for t in raw_tags]


def get_item_tags(base_url: str, lib_path: str, item_key: str,
                  timeout: float = _TIMEOUT_S,
                  api_key: str | None = None) -> tuple[int, list[str]]:
    """Name-only convenience over :func:`get_item_tags_full`.

    Kept for callers that only *compare* tag sets (the conservative revert
    guard) — anything that will WRITE the array back must use the full variant
    or it reintroduces the metadata-stripping bug documented there.
    """
    version, raw = get_item_tags_full(base_url, lib_path, item_key,
                                      timeout=timeout, api_key=api_key)
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
            "If-Unmodified-Since-Version": str(int(version)),
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
        raise ZoteroUnavailable(str(exc)) from exc

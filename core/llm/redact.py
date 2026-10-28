"""Secret redaction helpers shared by adapters and the security layer.

API keys must never appear in log lines, status payloads, or error
messages that flow back to the UI. The patterns here cover the
common shapes — OpenAI ``sk-...``, Anthropic ``sk-ant-...``, Google
``AIza...``, and generic ``Bearer ...`` headers.
"""
from __future__ import annotations

import logging
import re

_API_KEY_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"),
    # Allow ``_`` / ``-`` so service-account and admin keys
    # (``sk-svcacct-…``, ``sk-admin-…``) are matched, not just bare ``sk-``.
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    # Tavily search keys (the agent web_search tool, 2026-07-22).
    re.compile(r"tvly-[A-Za-z0-9_\-]{16,}"),
    # Google OAuth 2.0 access tokens (Drive/Calendar/Gmail flows) — not caught
    # by the AIza API-key pattern above.
    re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}"),
    # Zhipu / Z.AI keys are ``{id}.{secret}`` — no ``sk-`` prefix, so none of
    # the patterns above caught a raw ZAI key outside a Bearer/header context
    # (2026-07-20 audit M2/B6). Both segments are long alphanumerics; the
    # length floors + word boundaries keep ordinary dotted text (filenames,
    # version pins like ``2.2.0``, ``sha.notext`` markers) from matching.
    re.compile(r"\b[0-9a-f]{24,}\.[A-Za-z0-9]{12,}\b"),
    # Zotero API keys are a bare 24-char alphanumeric run with no prefix, so
    # they can only be matched in context — a bare {24} alnum pattern would
    # redact sha fragments, item keys and ordinary words. One pattern covers the
    # header (``Zotero-API-Key``) and the env var (``ZOTERO_API_KEY``) — they
    # differ only by ``-``/``_`` — across ``:`` OR ``=`` assignment and optional
    # surrounding quotes, so the header form, a quoted env dump
    # (``ZOTERO_API_KEY="…"``) and a headers-dict repr (``'Zotero-api-key': '…'``)
    # are all caught (2026-07-20 audit F9 broadened the earlier two forms). In
    # practice the key only ever travels in a request HEADER (never a URL/query),
    # so the httpx-URL-logging vector does not apply to it; this is defence in
    # depth over that fact, not the sole barrier.
    re.compile(
        r"ZOTERO[_-]API[_-]KEY['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9]{16,}",
        re.IGNORECASE),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}", re.IGNORECASE),
    re.compile(r"x-api-key:\s*[A-Za-z0-9._\-]{20,}", re.IGNORECASE),
    # Generic ``Authorization:`` header value — catches a raw token that is not
    # ``Bearer``-prefixed (the {20,} run never matches the short word ``Bearer``
    # itself, so it complements rather than overlaps the Bearer pattern).
    re.compile(r"authorization:\s*[A-Za-z0-9._\-]{20,}", re.IGNORECASE),
)


def redact(text: str) -> str:
    """Replace anything that looks like an API key with ``<redacted>``."""
    if not text:
        return text
    out = text
    for pat in _API_KEY_PATTERNS:
        out = pat.sub("<redacted>", out)
    return out


class RedactingFilter(logging.Filter):
    """Logging filter that redacts API-key shapes at WRITE time, not read time.

    Third-party loggers bypass app-side sanitisation entirely: ``httpx`` logs
    every request URL at INFO, and the Gemini endpoint carries the API key IN
    the URL query string (``…?key=AIza…``) — so the raw key landed verbatim in
    ``chatekld.log`` (observed live 2026-07-18). ``/api/log/tail`` redacts what
    it *serves*, but the on-disk file (and any copy the user shares for a bug
    report) held the secret. Attaching this filter to the handlers closes that
    hole for every logger, app or third-party, present and future.

    The message is rendered once (``getMessage()``) and ``args`` cleared, so
    %-formatting cannot re-introduce an unredacted value after the filter ran.
    Never raises — a filter exception would swallow the log line entirely,
    which is worse than an unredacted one; on failure the record passes as-is.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 — stdlib API name
        try:
            if record.exc_info:
                record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
                record.exc_info = None
            elif record.exc_text:
                record.exc_text = redact(record.exc_text)
            if record.stack_info:
                record.stack_info = redact(record.stack_info)
            msg = record.getMessage()
            redacted = redact(msg)
            if redacted != msg:
                record.msg = redacted
                record.args = ()
        except Exception:  # noqa: BLE001 — see docstring
            pass
        return True

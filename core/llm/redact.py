"""Secret redaction helpers shared by adapters and the security layer.

API keys must never appear in log lines, status payloads, or error
messages that flow back to the UI. The patterns here cover the
common shapes — OpenAI ``sk-...``, Anthropic ``sk-ant-...``, Google
``AIza...``, and generic ``Bearer ...`` headers.
"""
from __future__ import annotations

import re

_API_KEY_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"),
    # Allow ``_`` / ``-`` so service-account and admin keys
    # (``sk-svcacct-…``, ``sk-admin-…``) are matched, not just bare ``sk-``.
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}", re.IGNORECASE),
    re.compile(r"x-api-key:\s*[A-Za-z0-9._\-]{20,}", re.IGNORECASE),
)


def redact(text: str) -> str:
    """Replace anything that looks like an API key with ``<redacted>``."""
    if not text:
        return text
    out = text
    for pat in _API_KEY_PATTERNS:
        out = pat.sub("<redacted>", out)
    return out

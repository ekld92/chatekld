import re
import logging
from flask import request
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

def origin_is_local() -> bool:
    """Return True only when the request originates from a local browser context."""
    if request.headers.get("X-Requested-With") != "ChatEKLD":
        return False

    origin = request.headers.get("Origin", "")
    referer = request.headers.get("Referer", "")
    allowed_hosts = {"127.0.0.1", "localhost"}
    
    if origin:
        parsed_origin = urlparse(origin)
        return parsed_origin.hostname in allowed_hosts
    if referer:
        parsed_referer = urlparse(referer)
        return parsed_referer.hostname in allowed_hosts

    # Neither Origin nor Referer present (normal for PyWebView's embedded renderer).
    # Use the server-derived remote address rather than the client-supplied Host
    # header, which an attacker can set to "localhost" from a non-local connection.
    return request.remote_addr in {"127.0.0.1", "::1"}

def sanitise_error_msg(error: Exception | str) -> str:
    """Strip sensitive topology details and API keys from errors."""
    msg = str(error)
    msg = re.sub(r'/Users/[\w.-]+/', '/<USER>/', msg)
    msg = re.sub(r'http://(127\.0\.0\.1|localhost):\d+/', 'http://localhost:<PORT>/', msg)
    try:
        from core.llm.redact import redact
        msg = redact(msg)
    except Exception:
        pass
    return msg

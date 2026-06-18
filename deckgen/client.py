"""HTTP + SSE client for the running ChatEKLD app.

Talks to the local API only (``GET /api/obsidian/status``,
``GET /api/obsidian/materials``, ``POST /api/obsidian/chat``). The chat endpoint
streams Server-Sent Events; :meth:`ChatEKLDClient.chat` consumes the stream and
returns an accumulated :class:`ChatResult`.

Security: every endpoint requires the ``X-Requested-With: ChatEKLD`` header and,
with no ``Origin``/``Referer``, a loopback peer (see ``api/security.py``). This
client runs on the same host, sends that header, and sends no ``Origin``/
``Referer`` — so the loopback check accepts it.
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable, Optional

import requests

from .prompts import SYSTEM_PROMPT_LIMIT
# Re-exported so existing callers/tests can keep doing
# ``from deckgen.client import ChatResult``. The class itself lives in a
# requests-free module shared with the in-process runner.
from .result import ChatResult

# Read timeout must exceed the server's per-token / agent wall-clock cap
# (_CHAT_TOKEN_TIMEOUT_S = 300 s in api/routes/vault.py). 360 s matches the
# frontend's _CHAT_TIMEOUT_MS so the server's structured error fires first.
_CONNECT_TIMEOUT_S = 10
_READ_TIMEOUT_S = 360

_PORT_LOG_RE = re.compile(r"Starting Flask on port (\d+)")


class DeckgenClientError(RuntimeError):
    """Raised for transport-level or contract-level failures talking to the app."""


class ChatEKLDClient:
    def __init__(self, base_url: str, *, timeout_read: int = _READ_TIMEOUT_S) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout_read = timeout_read
        self._session = requests.Session()
        # The required CSRF-ish header. No Origin/Referer -> loopback check applies.
        self._session.headers.update({"X-Requested-With": "ChatEKLD"})

    # -- read endpoints -----------------------------------------------------

    def status(self) -> dict:
        return self._get_json("/api/obsidian/status")

    def materials(self) -> dict:
        return self._get_json("/api/obsidian/materials")

    def _get_json(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.get(url, timeout=(_CONNECT_TIMEOUT_S, 30))
        except requests.RequestException as exc:
            raise DeckgenClientError(f"GET {path} failed: {exc}") from exc
        if resp.status_code == 403:
            raise DeckgenClientError(
                f"GET {path} returned 403 Forbidden — is the base URL pointing at the "
                "local ChatEKLD instance? (the request must originate from loopback)"
            )
        if resp.status_code != 200:
            raise DeckgenClientError(f"GET {path} returned HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise DeckgenClientError(f"GET {path} returned non-JSON body") from exc

    # -- chat (SSE) ---------------------------------------------------------

    def chat(
        self,
        message: str,
        *,
        system_prompt: str = "",
        provider: str = "ollama",
        model: str = "",
        embed: str = "",
        agent: bool = True,
        max_iters: int = 6,
        temperature: Optional[float] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        extra: Optional[dict] = None,
    ) -> ChatResult:
        """Run one chat turn and return the accumulated result.

        *on_event* (if given) receives every parsed SSE dict as it arrives — used
        by the CLI to surface stage/info and agent-trace events live.
        """
        if len(system_prompt) > SYSTEM_PROMPT_LIMIT:
            raise DeckgenClientError(
                f"system_prompt is {len(system_prompt)} chars; ChatEKLD caps it at "
                f"{SYSTEM_PROMPT_LIMIT}."
            )

        body: dict = {
            "message": message,
            "provider": provider,
            "agent_enabled": agent,
            "agent_max_iterations": max_iters,
        }
        if model:
            body["llm"] = model
        if embed:
            body["embed"] = embed
        if system_prompt:
            body["system_prompt"] = system_prompt
        if temperature is not None:
            body["temperature"] = temperature
        if extra:
            body.update(extra)

        url = f"{self.base_url}/api/obsidian/chat"
        try:
            resp = self._session.post(
                url,
                json=body,
                stream=True,
                timeout=(_CONNECT_TIMEOUT_S, self._timeout_read),
                headers={"Accept": "text/event-stream"},
            )
        except requests.RequestException as exc:
            raise DeckgenClientError(f"POST /api/obsidian/chat failed: {exc}") from exc

        if resp.status_code == 403:
            raise DeckgenClientError(
                "POST /api/obsidian/chat returned 403 Forbidden — check the base URL "
                "points at the local ChatEKLD instance."
            )
        if resp.status_code != 200:
            raise DeckgenClientError(
                f"POST /api/obsidian/chat returned HTTP {resp.status_code}"
            )

        # requests' iter_lines(decode_unicode=True) yields *bytes* when the
        # response has no charset, which would break the str parsing below. The
        # SSE stream is UTF-8 JSON, so pin it.
        resp.encoding = resp.encoding or "utf-8"
        return self._consume_sse(resp, on_event)

    @staticmethod
    def _consume_sse(resp, on_event: Optional[Callable[[dict], None]]) -> ChatResult:
        result = ChatResult()
        try:
            for raw in resp.iter_lines(decode_unicode=True):
                if raw is None or raw == "":
                    continue  # SSE frame separator / keep-alive
                if not raw.startswith("data:"):
                    continue
                payload = raw[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                try:
                    evt = json.loads(payload)
                except ValueError:
                    continue
                if on_event is not None:
                    try:
                        on_event(evt)
                    except Exception:
                        pass

                if "token" in evt:
                    result.text += evt["token"]
                elif "info" in evt:
                    result.infos.append(evt["info"])
                elif "error" in evt:
                    result.error = evt["error"]
                elif "iteration" in evt:
                    result.iterations = max(result.iterations, int(evt["iteration"]))
                    result.trace.append(evt)
                elif any(k in evt for k in ("thought", "tool_call", "tool_result")):
                    result.trace.append(evt)
        except requests.RequestException as exc:
            raise DeckgenClientError(f"SSE stream read failed: {exc}") from exc
        finally:
            resp.close()
        return result


# ---------------------------------------------------------------------------
# Base-URL / port discovery
# ---------------------------------------------------------------------------

def _default_base_dir() -> str:
    """Mirror core.constants._get_base_dir just enough to find the log file.

    Honours CHATEKLD_BASE_DIR (the test/override hook); otherwise the macOS app
    data dir. We deliberately do NOT import core.constants — deckgen stays
    decoupled from the app.
    """
    override = os.environ.get("CHATEKLD_BASE_DIR", "").strip()
    if override:
        return override
    return os.path.expanduser("~/Library/Application Support/ChatEKLD")


def discover_port_from_log(log_path: Optional[str] = None) -> Optional[int]:
    """Return the most recent port from ``chatekld.log``, or None if not found."""
    if log_path is None:
        log_path = os.path.join(_default_base_dir(), "chatekld.log")
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            last = None
            for line in fh:
                m = _PORT_LOG_RE.search(line)
                if m:
                    last = int(m.group(1))
            return last
    except OSError:
        return None


def resolve_base_url(*, base_url: Optional[str], port: Optional[int]) -> str:
    """Resolve the API base URL from --base-url / --port / log auto-discovery."""
    if base_url:
        return base_url.rstrip("/")
    if port:
        return f"http://127.0.0.1:{port}"
    discovered = discover_port_from_log()
    if discovered:
        return f"http://127.0.0.1:{discovered}"
    raise DeckgenClientError(
        "Could not determine the ChatEKLD port. Pass --port N (shown in the app's "
        "window/log) or --base-url http://127.0.0.1:N. The dynamic port is logged "
        "as 'Starting Flask on port N' in chatekld.log."
    )

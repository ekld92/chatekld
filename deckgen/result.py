"""The accumulated outcome of one chat turn.

Lives in its own module (no third-party imports) so both the HTTP
:class:`~deckgen.client.ChatEKLDClient` and the in-process
:class:`~deckgen.inprocess.InProcessChatRunner` can produce the same shape
without the in-process path having to import ``requests``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ChatResult:
    """Accumulated outcome of one chat turn (HTTP SSE or in-process)."""
    text: str = ""
    infos: list = field(default_factory=list)
    error: Optional[str] = None
    # Agent-mode trace, for verbose logging. Each item is a raw event dict.
    trace: list = field(default_factory=list)
    iterations: int = 0

    @property
    def ok(self) -> bool:
        """True when the turn produced answer text and recorded no error.

        The callers' success predicate: a turn that errored, or one that streamed
        only whitespace (e.g. an empty model reply), is treated as a failure.
        """
        return self.error is None and bool(self.text.strip())

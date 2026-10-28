"""Conservative two-way note state, independent of I/O.

Equality of the current comparable texts is required for ``in_sync``.
Changes since each historical base are separate facts: unchanged divergent
bases remain a conflict. Word/structure counts rank review work; they never
certify equality. The route additionally blocks images, unsupported content
and personal content outside the recognized region. Converter changes keep
old bases for inspection and require a new review.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass

from notesync.compare import region_delta, risk_words

STATES = (
    "in_sync", "pull", "push", "conflict", "unbaselined",
    "missing_obsidian", "missing_zotero", "unparsed", "multiple_notes",
)

_TRAILING_WS = re.compile(r"[ \t]+$", re.M)


def norm_text(text: str | None) -> str:
    """Newline-normalised, trailing-whitespace-free, stripped. The equality the
    state table uses on both sides; NOT a tokenizer."""
    if not text:
        return ""
    return _TRAILING_WS.sub("", text.replace("\r\n", "\n")).strip()


def equivalent(zotero_md: str | None, region: str | None,
               region_rendered: str | None = None) -> bool:
    """Are the two sides equal after the current converter?

    Literal equality first. Failing that, *region_rendered* is the caller's
    ``html_to_md(md_to_html(region))`` — what Zotero would hold, re-read, if
    the region were pushed. Measured 2026-09-18: 633 of 670 unbaselined live
    notes carried the same words as Zotero and differed only in Markdown
    spelling (``n=29`` vs the converter's ``n\\=29``, ``**_x_**`` vs
    ``***x***``, 4- vs 2-space list indents, U+00A0 vs ``&nbsp;``). Without
    this, a push of such a region also left the note reading ``conflict``
    the moment it landed: the base then held two texts that differ only in
    spelling. The word sets must also agree, so a round trip that DROPS
    content from the region can never certify it as equal.
    """
    if norm_text(zotero_md) == norm_text(region):
        return True
    if region_rendered is None:
        return False
    return (norm_text(zotero_md) == norm_text(region_rendered)
            and risk_words(region or "") == risk_words(zotero_md or ""))


def sha256_text(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BaseRecord:
    """What both sides looked like at the last sync of one note.

    ``base_html`` is the Zotero note's stored HTML verbatim (shell included);
    ``base_region`` the Obsidian region bytes. Both are needed for a three-way
    diff, and both are small (1.06 MB of HTML across the whole live library).
    ``zotero_version`` is the note ITEM's version at that time — the value a
    push sends as ``If-Unmodified-Since-Version``.
    """

    citation_key: str
    item_key: str
    note_key: str
    zotero_version: int
    base_html: str
    base_region: str
    synced_at: str
    converter_id: str
    zotero_html_sha256: str = ""
    region_sha256: str = ""
    last_push_version: int = 0

    def to_json(self) -> dict:
        d = asdict(self)
        d["zotero_html_sha256"] = sha256_text(self.base_html)
        d["region_sha256"] = sha256_text(self.base_region)
        d["schema"] = 1
        return d

    @classmethod
    def from_json(cls, data) -> "BaseRecord | None":
        """Strict: a record missing a field, or with a wrong type, is ``None``
        (→ ``unbaselined``), never a partially-filled object."""
        if not isinstance(data, dict) or data.get("schema") != 1:
            return None
        try:
            rec = cls(
                citation_key=str(data["citation_key"]),
                item_key=str(data["item_key"]),
                note_key=str(data["note_key"]),
                zotero_version=int(data["zotero_version"]),
                base_html=str(data["base_html"]),
                base_region=str(data["base_region"]),
                synced_at=str(data["synced_at"]),
                converter_id=str(data["converter_id"]),
                zotero_html_sha256=str(data.get("zotero_html_sha256", "")),
                region_sha256=str(data.get("region_sha256", "")),
                last_push_version=int(data.get("last_push_version", 0) or 0),
            )
        except (KeyError, TypeError, ValueError):
            return None
        # The digests are the tamper/corruption check on the two texts.
        if rec.zotero_html_sha256 and rec.zotero_html_sha256 != sha256_text(rec.base_html):
            return None
        if rec.region_sha256 and rec.region_sha256 != sha256_text(rec.base_region):
            return None
        return rec


@dataclass(frozen=True)
class SyncDecision:
    state: str
    zotero_changed: bool = False
    region_changed: bool = False
    #: The bootstrap counts (``region_delta``), reported for every state so
    #: the listing can rank and the merge view can say how much is at stake.
    missing_words: int = 0
    extra_words: int = 0
    baselined: bool = False


def bootstrap_state(zotero_md_now: str, region_now: str,
                    region_rendered: str | None = None) -> SyncDecision:
    """Classification with NO base: exact conservative equality first.

    ``missing`` words are Zotero's that the region lacks ("something to
    pull"); ``extra`` are the region's that Zotero lacks, markup included
    ("something to push"). Counts are hints only; matching normalized text is required before a
    base may be written without a human decision.
    """
    missing, extra = region_delta(region_now, zotero_md_now)
    if equivalent(zotero_md_now, region_now, region_rendered):
        state = "in_sync"
    elif missing and not extra:
        state = "pull"
    elif extra and not missing:
        state = "push"
    else:
        state = "conflict"
    return SyncDecision(state, bool(missing), bool(extra), missing, extra, baselined=False)


def classify(*, base: BaseRecord | None, zotero_md_now: str, zotero_md_base: str | None,
             region_now: str, converter_id: str,
             region_rendered: str | None = None) -> SyncDecision:
    """The state table. ``zotero_md_base`` is the converter's rendering of
    ``base.base_html`` (computed by the caller so this module stays free of
    the converter); ignored when *base* is ``None``.

    A base written by another converter is treated as absent: its
    ``base_region`` is still the user's bytes, but "did Zotero change?" would
    be answered by comparing two renderings of different converters, which
    would flag every note.
    """
    if base is None or base.converter_id != converter_id:
        d = bootstrap_state(zotero_md_now, region_now, region_rendered)
        return SyncDecision("unbaselined", d.zotero_changed, d.region_changed,
                            d.missing_words, d.extra_words, baselined=False)
    z_changed = norm_text(zotero_md_now) != norm_text(zotero_md_base or "")
    r_changed = norm_text(region_now) != norm_text(base.base_region)
    missing, extra = region_delta(region_now, zotero_md_now)
    if equivalent(zotero_md_now, region_now, region_rendered):
        state = "in_sync"
    elif not z_changed and not r_changed:
        state = "conflict"
    elif z_changed and not r_changed:
        state = "pull"
    elif r_changed and not z_changed:
        state = "push"
    else:
        state = "conflict"
    return SyncDecision(state, z_changed, r_changed, missing, extra, baselined=True)

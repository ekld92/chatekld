"""Keyless bibliographic sources behind one protocol, stdlib urllib only.

Five sources, none of which takes an API key — a durable design property,
not an accident (the plan's §8): **Crossref** (DOI resolution +
``query.bibliographic`` title matching), **Europe PMC** (life sciences,
preprints — the highest-yield source for this library), **arXiv** (Atom
API), **HAL** and **theses.fr** (French deposits and doctoral theses, for
the visibly institutional no-text tail). Response shapes were verified live
against all five endpoints on 2026-08-19; the recorded fixtures under
``tests/accession/fixtures/`` are those live responses.

The HTTP core mirrors ``tagging/zotero_write.py``, the app's hardened
stdlib client:

* a **no-redirect opener** — stdlib's default handler forwards request
  headers cross-origin and follows https→http, so every redirect is refused
  (no credential rides these requests, but the guarantee stays uniform);
* the response body is **read-capped BEFORE parsing** (2 MB, the
  ``web_tools`` precedent) so a hostile or broken endpoint cannot balloon
  memory;
* HTTP errors surface **status-only** — never the third party's body;
* ``Retry-After`` is honoured by arming a per-host backoff deadline that
  later calls sleep on (bounded), and a 429/503 raises
  :class:`SourceRateLimited` carrying the hint;
* per-source **pacing** (arXiv asks for one request per 3 s; the others get
  a token courtesy interval) shares the same per-host deadline map.

What leaves the machine is a DOI, or a title and a surname — never PDF
bytes, never full page text. The one optional identifying datum is a
contact e-mail for Crossref's polite pool, passed explicitly by the caller
(a config key in the UI phase, default empty), never read from the git
identity or the environment.
"""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from audit.core.bib import normalise_doi

__all__ = [
    "Candidate", "Source", "SourceError", "SourceUnavailable",
    "SourceHTTPError", "SourceRateLimited", "CrossrefSource",
    "EuropePmcSource", "ArxivSource", "HalSource", "ThesesFrSource",
    "default_sources",
]

#: Read cap applied BEFORE any parsing (``core/agent/web_tools.py`` precedent).
_MAX_BODY_BYTES = 2_000_000

#: Default per-call timeout, the ``zotero_write`` figure.
DEFAULT_TIMEOUT_S = 15.0

#: Hard ceiling on any pacing/backoff sleep — a server hint beyond this is
#: truncated rather than stalling the caller (``zotero_write`` discipline).
_MAX_WAIT_S = 5.0

_USER_AGENT_BASE = "CHERQAiN-accession/0.1 (local bibliographic re-linker)"


class SourceError(Exception):
    """Base for every source failure. Messages are status-only by contract."""


class SourceUnavailable(SourceError):
    """The endpoint could not be reached (offline / DNS / timeout)."""


class SourceHTTPError(SourceError):
    """A non-2xx answer. Carries the status code, NEVER the response body."""

    def __init__(self, source: str, status: int):
        super().__init__(f"{source} answered HTTP {status}")
        self.status = status


class SourceRateLimited(SourceHTTPError):
    """HTTP 429/503 with an optional ``Retry-After`` hint (seconds)."""

    def __init__(self, source: str, status: int,
                 retry_after_s: float | None = None):
        super().__init__(source, status)
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class Candidate:
    """One bibliographic record a source proposes for a query.

    ``authors`` are display strings, normalised toward ``"Last, First"``
    where the source's shape allows it (the scorer reduces each to a surname
    key with the bridge's own recipe, which takes the part before the first
    comma when one is present, else the last whitespace token — so plain
    ``"First Last"`` full names corroborate correctly too). ``container`` is
    the journal / venue / institution line for the card.
    """

    source: str
    title: str | None
    authors: tuple[str, ...] = ()
    year: int | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    pmid: str | None = None
    container: str | None = None
    url: str | None = None


# ── hardened HTTP core ─────────────────────────────────────────────────────


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse EVERY redirect (the ``zotero_write`` E1 posture).

    No auth header rides these requests, but the default handler still
    follows https→http and re-sends everything to whatever host the 30x
    names. None of the five APIs legitimately redirects an API call, so we
    fail closed and keep the guarantee uniform across the app's clients.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, f"Refusing redirect to {newurl}", headers, fp)


#: One opener for every request in this module — no redirects, ever. Tests
#: patch :func:`_urlopen` (or this opener) to stay hermetic.
_opener = urllib.request.build_opener(_NoRedirect)


def _urlopen(req, timeout):
    """Open *req* through the no-redirect opener. The tests' patch point."""
    return _opener.open(req, timeout=timeout)


# Per-host earliest-next-call deadlines (monotonic). Pacing and Retry-After
# share this map: a source's courtesy interval advances it after each call,
# and a 429/503's hint advances it further so the next call waits.
_pace_lock = threading.Lock()
_next_call_at: dict[str, float] = {}


def _await_turn(host: str) -> None:
    """Sleep (bounded) until *host*'s earliest-next-call deadline passes."""
    with _pace_lock:
        wait = _next_call_at.get(host, 0.0) - time.monotonic()
    if wait > 0:
        time.sleep(min(wait, _MAX_WAIT_S))


def _advance_turn(host: str, secs: float) -> None:
    if secs <= 0:
        return
    with _pace_lock:
        _next_call_at[host] = max(
            _next_call_at.get(host, 0.0), time.monotonic() + secs)


def _retry_after_s(exc: urllib.error.HTTPError) -> float | None:
    raw = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _http_get(url: str, *, source: str, pace_s: float,
              timeout_s: float, contact_email: str = "") -> bytes:
    """One paced GET; returns the read-capped body bytes.

    Error mapping is status-only: an :class:`urllib.error.HTTPError` becomes
    :class:`SourceHTTPError` / :class:`SourceRateLimited` (arming the host's
    backoff from ``Retry-After``), transport failures become
    :class:`SourceUnavailable`. The third party's response body is never
    included in any raised message.
    """
    host = urllib.parse.urlparse(url).netloc
    _await_turn(host)
    ua = _USER_AGENT_BASE
    if contact_email:
        ua = f"{_USER_AGENT_BASE[:-1]}; mailto:{contact_email})"
    req = urllib.request.Request(url, method="GET", headers={
        "User-Agent": ua,
        "Accept": "application/json, application/atom+xml;q=0.9, */*;q=0.1",
    })
    try:
        with _urlopen(req, timeout_s) as resp:
            body = resp.read(_MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        _advance_turn(host, pace_s)
        if exc.code in (429, 503):
            hint = _retry_after_s(exc)
            _advance_turn(host, hint or 0.0)
            raise SourceRateLimited(source, exc.code, hint) from exc
        raise SourceHTTPError(source, exc.code) from exc
    except (urllib.error.URLError, OSError) as exc:
        _advance_turn(host, pace_s)
        raise SourceUnavailable(f"{source} unreachable") from exc
    _advance_turn(host, pace_s)
    if len(body) > _MAX_BODY_BYTES:
        raise SourceError(f"{source} response exceeded the size cap")
    return body


def _get_json(url: str, **kw) -> object:
    """GET + parse JSON, only after the read cap has been applied."""
    body = _http_get(url, **kw)
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SourceError(f"{kw['source']} returned malformed JSON") from exc


# ── shared parsing helpers ─────────────────────────────────────────────────


def _clean(s: object) -> str | None:
    """Collapse whitespace and normalise to NFC (theses.fr answers NFD —
    the same NFC/NFD trap macOS Finder tags taught this codebase)."""
    if s is None:
        return None
    text = unicodedata.normalize(
        "NFC", re.sub(r"\s+", " ", str(s)).strip())
    return text or None


def _as_int_year(v: object) -> int | None:
    m = re.search(r"(19|20)\d{2}", str(v or ""))
    return int(m.group(0)) if m else None


def _title_or_none(t: str | None) -> str | None:
    """Refuse a "title" that is itself a DOI. Observed live: Crossref's
    records for legacy Elsevier PII-style DOIs (Annales Médico-
    psychologiques back-catalog) carry the DOI string AS the title, no
    authors — a registry stub, not a resolution a reviewer could accept."""
    if t and normalise_doi(t):
        return None
    return t


# ── the protocol ───────────────────────────────────────────────────────────


@dataclass
class Source:
    """Base source: one uniform ``run(query)`` over the four query kinds.

    ``supports`` names the kinds this source can serve; ``run`` on an
    unsupported kind returns ``[]`` so the orchestrator can iterate a mixed
    registry without special cases. ``pace_s`` is the courtesy interval
    between calls to this source's host. Subclasses implement the ``_do_*``
    hooks for the kinds they support.
    """

    contact_email: str = ""
    name: str = field(default="", init=False)
    pace_s: float = field(default=0.2, init=False)
    supports: frozenset[str] = field(default=frozenset(), init=False)
    #: Rescue sources are consulted for title / author+year searches only
    #: when the primary sources found nothing usable — the plan's own phasing
    #: ("HAL and theses.fr … against whatever earlier phases actually failed
    #: to resolve"), and it spares every mainstream paper arXiv's 3-second
    #: courtesy interval. Identifier lookups ignore the flag: an arXiv id
    #: has exactly one home.
    rescue: bool = field(default=False, init=False)
    #: Whether ``_do_title`` accepts a ``container=`` narrowing keyword.
    #: Only Crossref exposes a venue filter today.
    supports_container: bool = field(default=False, init=False)

    def run(self, kind: str, value: str, *, year: str | None = None,
            rows: int = 5, timeout_s: float = DEFAULT_TIMEOUT_S,
            container: str | None = None) -> list[Candidate]:
        """Serve one query kind. ``container`` narrows a TITLE search.

        The container is passed as a keyword a subclass may ignore: only
        Crossref exposes a venue filter, and a source that cannot honour the
        constraint must return its unconstrained answer rather than nothing
        (the caller decides what to do with a weaker result, and silently
        dropping a source would be worse than not narrowing).
        """
        if kind not in self.supports or not value:
            return []
        hook = getattr(self, f"_do_{kind}")
        kwargs = {"year": year, "rows": max(1, min(int(rows), 20)),
                  "timeout_s": timeout_s}
        if kind == "title" and container and self.supports_container:
            kwargs["container"] = container
        return hook(value, **kwargs)

    # subclasses override the hooks they declare in ``supports``
    def _get_json(self, url: str, timeout_s: float) -> object:
        return _get_json(url, source=self.name, pace_s=self.pace_s,
                         timeout_s=timeout_s, contact_email=self.contact_email)


# ── Crossref ───────────────────────────────────────────────────────────────


class CrossrefSource(Source):
    """``api.crossref.org`` — DOI resolution and bibliographic title search.

    ``select=`` trims the response to the fields the card needs (a full
    Crossref work carries its whole reference list — measured multi-hundred-KB
    for review articles). The optional contact e-mail rides the User-Agent,
    which is what routes requests into the documented polite pool; it is a
    courtesy, not a key.
    """

    BASE = "https://api.crossref.org"
    # `subtitle` included since the 2026-08-19 review: Crossref splits many
    # titles at the colon, and without the second half a candidate rendered
    # as a stub ("Bipolarity from ancient to modern times:").
    _SELECT = "DOI,title,subtitle,author,issued,container-title"

    def __init__(self, contact_email: str = ""):
        super().__init__(contact_email)
        self.name = "crossref"
        # Measured live 2026-08-19: a ~25-call burst at 0.2 s drew 429s from
        # the public pool. 1 s keeps a bounded batch polite by construction;
        # Retry-After handling covers whatever throttling remains.
        self.pace_s = 1.0
        self.supports = frozenset({"doi", "title", "authoryear"})
        self.supports_container = True

    def _parse_work(self, work: dict, *,
                    allow_untitled: bool = False) -> Candidate | None:
        # Defensive against shape surprises (a "title" that is a dict, an
        # "issued" that is a list): one malformed record must skip, not crash
        # the whole resolution — `resolve_queries._call` only degrades
        # SourceError, so an escaping TypeError/KeyError here would fail the
        # PDF (and, repeated, abort a batch) instead of degrading the source.
        try:
            title = _title_or_none(_clean((work.get("title") or [None])[0]))
            if not title and not allow_untitled:
                return None
            subtitle = _clean((work.get("subtitle") or [None])[0])
            if title and subtitle and subtitle.casefold() not in title.casefold():
                title = f"{title.rstrip(' :')}: {subtitle}"
            authors = tuple(
                _clean(f"{a.get('family', '')}, {a.get('given', '')}".strip(", "))
                or ""
                for a in work.get("author", []) if isinstance(a, dict)
            )
            parts = (work.get("issued") or {}).get("date-parts") or [[None]]
            doi = normalise_doi(str(work.get("DOI") or ""))
            return Candidate(
                source=self.name, title=title,
                authors=tuple(a for a in authors if a),
                year=_as_int_year(parts[0][0] if parts[0] else None),
                doi=doi,
                container=_clean((work.get("container-title") or [None])[0]),
                url=f"https://doi.org/{doi}" if doi else None,
            )
        except (TypeError, KeyError, AttributeError, IndexError):
            return None

    def _do_doi(self, value, *, year, rows, timeout_s):
        url = f"{self.BASE}/works/{urllib.parse.quote(value, safe='')}"
        try:
            payload = self._get_json(url, timeout_s)
        except SourceHTTPError as exc:
            if exc.status == 404:      # unknown DOI: a clean no, not a failure
                return []
            raise
        msg = payload.get("message") if isinstance(payload, dict) else None
        # `allow_untitled` on the DOI path ONLY (2026-08-26). Measured on the
        # maintainer's 20 EMC entries: 2 of them (10%) return HTTP 200 from a
        # record whose `title[0]` IS the DOI string — a registry stub.
        # `_title_or_none` rightly refuses a DOI-shaped title, but dropping the
        # whole candidate then reported "nothing found" for an identifier
        # Crossref had just CONFIRMED exists. The refusal stays; only the
        # consequence changes, and only here: on a title search a candidate
        # with no title cannot be scored and is genuinely useless, whereas on a
        # DOI lookup the identifier is itself the evidence.
        cand = (self._parse_work(msg, allow_untitled=True)
                if isinstance(msg, dict) else None)
        # Still require the record to carry the identifier we asked for, so a
        # shapeless payload cannot become a confident answer.
        return [cand] if cand and cand.doi else []

    def _query(self, params: dict, rows: int, timeout_s: float):
        params = {**params, "rows": rows, "select": self._SELECT}
        if self.contact_email:
            params["mailto"] = self.contact_email
        url = f"{self.BASE}/works?{urllib.parse.urlencode(params)}"
        payload = self._get_json(url, timeout_s)
        items = ((payload.get("message") or {}).get("items")
                 if isinstance(payload, dict) else None) or []
        out = [self._parse_work(w) for w in items if isinstance(w, dict)]
        return [c for c in out if c]

    def _do_title(self, value, *, year, rows, timeout_s, container=None):
        """Title search, optionally narrowed to a venue (Batch D, 2026-08-26).

        Measured on the maintainer's 20 EMC entries: unconstrained, this lands
        on the right DOI 10 times and returns a CONFIDENT WRONG record 4 times
        — "Personnalité narcissique" matched a Kernberg volume at 0.92,
        "Paranoïa" a Presses de l'Université de Montréal title at 0.80. Adding
        `query.container-title` takes it to 14 right and 1 wrong, and the one
        survivor is a title that genuinely exists in two EMC volumes, which the
        engine's two-candidates-within-margin refusal is built to park.

        Generic clinical nouns are the failure mode, so this is a SAFETY
        improvement as much as a recall one.
        """
        params: dict = {"query.bibliographic": value}
        if container:
            params["query.container-title"] = container
        return self._query(params, rows, timeout_s)

    def _do_authoryear(self, value, *, year, rows, timeout_s):
        params: dict = {"query.author": value}
        y = _as_int_year(year)
        if y:
            params["filter"] = (
                f"from-pub-date:{y - 1}-01-01,until-pub-date:{y + 1}-12-31")
        return self._query(params, rows, timeout_s)


# ── Europe PMC ─────────────────────────────────────────────────────────────


_EPMC_AUTHOR_RE = re.compile(r"^(?P<last>.+?)\s+(?P<initials>[A-Z]{1,4})\.?$")


def _epmc_author(token: str) -> str:
    """``"Smith JR"`` → ``"Smith, JR"`` so the surname key comes out right.

    Europe PMC's ``authorString`` uses "Surname Initials" — the bridge's
    surname recipe would otherwise take the initials as the surname.
    """
    m = _EPMC_AUTHOR_RE.match(token.strip().rstrip("."))
    if m:
        return f"{m.group('last')}, {m.group('initials')}"
    return token.strip()


class EuropePmcSource(Source):
    """``www.ebi.ac.uk/europepmc`` REST search — no registration at all."""

    BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

    def __init__(self, contact_email: str = ""):
        super().__init__(contact_email)
        self.name = "europepmc"
        self.pace_s = 0.2
        self.supports = frozenset({"doi", "title", "authoryear"})

    def _search(self, query: str, rows: int, timeout_s: float):
        params = {"query": query, "format": "json", "pageSize": rows}
        url = f"{self.BASE}?{urllib.parse.urlencode(params)}"
        payload = self._get_json(url, timeout_s)
        results = (((payload.get("resultList") or {}).get("result"))
                   if isinstance(payload, dict) else None) or []
        out: list[Candidate] = []
        for r in results:
            if not isinstance(r, dict):
                continue
            title = _clean(r.get("title"))
            if not title:
                continue
            author_string = str(r.get("authorString") or "").rstrip(". ")
            authors = tuple(
                _epmc_author(t) for t in author_string.split(", ") if t.strip())
            out.append(Candidate(
                source=self.name, title=title.rstrip("."), authors=authors,
                year=_as_int_year(r.get("pubYear")),
                doi=normalise_doi(str(r.get("doi") or "")),
                pmid=_clean(r.get("pmid")),
                container=_clean(r.get("journalTitle")),
            ))
        return out

    @staticmethod
    def _phrase(value: str) -> str:
        return '"%s"' % value.replace('"', " ").strip()

    def _do_doi(self, value, *, year, rows, timeout_s):
        return self._search(f"DOI:{self._phrase(value)}", rows, timeout_s)

    def _do_title(self, value, *, year, rows, timeout_s):
        return self._search(f"TITLE:{self._phrase(value)}", rows, timeout_s)

    def _do_authoryear(self, value, *, year, rows, timeout_s):
        q = f"AUTH:{self._phrase(value)}"
        y = _as_int_year(year)
        if y:
            q += f" AND PUB_YEAR:[{y - 1} TO {y + 1}]"
        return self._search(q, rows, timeout_s)


# ── arXiv ──────────────────────────────────────────────────────────────────


_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"


class ArxivSource(Source):
    """``export.arxiv.org`` Atom API. Etiquette: one request per 3 seconds."""

    BASE = "https://export.arxiv.org/api/query"

    def __init__(self, contact_email: str = ""):
        super().__init__(contact_email)
        self.name = "arxiv"
        self.pace_s = 3.0          # arXiv's documented courtesy interval
        self.supports = frozenset({"arxiv", "title"})
        self.rescue = True

    def _query(self, params: dict, timeout_s: float) -> list[Candidate]:
        url = f"{self.BASE}?{urllib.parse.urlencode(params)}"
        body = _http_get(url, source=self.name, pace_s=self.pace_s,
                         timeout_s=timeout_s, contact_email=self.contact_email)
        try:
            root = ET.fromstring(body.decode("utf-8", errors="replace"))
        except ET.ParseError as exc:
            raise SourceError("arxiv returned malformed XML") from exc
        out: list[Candidate] = []
        for entry in root.findall(f"{_ATOM}entry"):
            title = _clean(entry.findtext(f"{_ATOM}title"))
            if not title:
                continue
            entry_id = str(entry.findtext(f"{_ATOM}id") or "")
            m = re.search(r"/abs/([^v\s]+)", entry_id)
            out.append(Candidate(
                source=self.name, title=title,
                authors=tuple(
                    a for a in (
                        _clean(n.findtext(f"{_ATOM}name"))
                        for n in entry.findall(f"{_ATOM}author"))
                    if a),
                year=_as_int_year(entry.findtext(f"{_ATOM}published")),
                doi=normalise_doi(
                    str(entry.findtext(f"{_ARXIV_NS}doi") or "")),
                arxiv_id=m.group(1) if m else None,
                container="arXiv",
                url=entry_id or None,
            ))
        return out

    def _do_arxiv(self, value, *, year, rows, timeout_s):
        return self._query({"id_list": value, "max_results": 1}, timeout_s)

    def _do_title(self, value, *, year, rows, timeout_s):
        q = 'ti:"%s"' % value.replace('"', " ").strip()
        return self._query({"search_query": q, "max_results": rows}, timeout_s)


# ── HAL ────────────────────────────────────────────────────────────────────


class HalSource(Source):
    """``api.archives-ouvertes.fr`` Solr search — French open deposits."""

    BASE = "https://api.archives-ouvertes.fr/search/"
    _FIELDS = "title_s,authFullName_s,producedDateY_i,doiId_s,docType_s,uri_s"

    def __init__(self, contact_email: str = ""):
        super().__init__(contact_email)
        self.name = "hal"
        self.pace_s = 0.2
        self.supports = frozenset({"doi", "title", "authoryear"})
        self.rescue = True

    def _search(self, q: str, rows: int, timeout_s: float):
        params = {"q": q, "wt": "json", "rows": rows, "fl": self._FIELDS}
        url = f"{self.BASE}?{urllib.parse.urlencode(params)}"
        payload = self._get_json(url, timeout_s)
        docs = (((payload.get("response") or {}).get("docs"))
                if isinstance(payload, dict) else None) or []
        out: list[Candidate] = []
        for d in docs:
            if not isinstance(d, dict):
                continue
            raw_title = d.get("title_s")
            title = _clean(raw_title[0] if isinstance(raw_title, list)
                           and raw_title else raw_title)
            if not title:
                continue
            authors = d.get("authFullName_s") or []
            out.append(Candidate(
                source=self.name, title=title,
                authors=tuple(a for a in (_clean(x) for x in authors) if a),
                year=_as_int_year(d.get("producedDateY_i")),
                doi=normalise_doi(str(d.get("doiId_s") or "")),
                container=_clean(d.get("docType_s")),
                url=_clean(d.get("uri_s")),
            ))
        return out

    @staticmethod
    def _phrase(value: str) -> str:
        return '"%s"' % value.replace('"', " ").strip()

    def _do_doi(self, value, *, year, rows, timeout_s):
        return self._search(f"doiId_s:{self._phrase(value)}", rows, timeout_s)

    def _do_title(self, value, *, year, rows, timeout_s):
        return self._search(f"title_t:{self._phrase(value)}", rows, timeout_s)

    def _do_authoryear(self, value, *, year, rows, timeout_s):
        q = f"authLastName_t:{self._phrase(value)}"
        y = _as_int_year(year)
        if y:
            q += f" AND producedDateY_i:[{y - 1} TO {y + 1}]"
        return self._search(q, rows, timeout_s)


# ── theses.fr ──────────────────────────────────────────────────────────────


class ThesesFrSource(Source):
    """``theses.fr`` v1 API — French doctoral theses.

    Shape verified live 2026-08-19 (``/api/v1/theses/recherche/``). Year
    filtering happens client-side over ``dateSoutenance`` (the API's own
    date filters are form-oriented); ±1 year, matching the corroboration
    window everywhere else.
    """

    BASE = "https://theses.fr/api/v1/theses/recherche/"

    def __init__(self, contact_email: str = ""):
        super().__init__(contact_email)
        self.name = "thesesfr"
        self.pace_s = 0.5
        self.supports = frozenset({"title", "authoryear"})
        self.rescue = True

    def _search(self, q: str, rows: int, timeout_s: float,
                year: str | None = None):
        params = {"q": q, "nombre": rows}
        url = f"{self.BASE}?{urllib.parse.urlencode(params)}"
        payload = self._get_json(url, timeout_s)
        theses = (payload.get("theses")
                  if isinstance(payload, dict) else None) or []
        want_year = _as_int_year(year)
        out: list[Candidate] = []
        for t in theses:
            if not isinstance(t, dict):
                continue
            title = _clean(t.get("titrePrincipal"))
            if not title:
                continue
            t_year = _as_int_year(t.get("dateSoutenance"))
            if want_year and t_year and abs(t_year - want_year) > 1:
                continue
            authors = tuple(
                a for a in (
                    _clean(f"{p.get('nom', '')}, {p.get('prenom', '')}"
                           .strip(", "))
                    for p in (t.get("auteurs") or []) if isinstance(p, dict))
                if a)
            thesis_id = _clean(t.get("id") or t.get("nnt"))
            out.append(Candidate(
                source=self.name, title=title, authors=authors, year=t_year,
                doi=normalise_doi(str(t.get("doi") or "")),
                container=_clean(t.get("etabSoutenanceN")) or "thèse",
                url=f"https://theses.fr/{thesis_id}" if thesis_id else None,
            ))
        return out

    def _do_title(self, value, *, year, rows, timeout_s):
        return self._search(value, rows, timeout_s, year=year)

    def _do_authoryear(self, value, *, year, rows, timeout_s):
        return self._search(value, rows, timeout_s, year=year)


class OpenLibrarySource(Source):
    """``openlibrary.org`` — ISBN lookup for books and chapters (Batch D).

    Keyless. Internet Archive's catalogue, ~30M titles at work and edition
    level. Books and book sections had NO resolver at all before this: the
    shipped ladder is Crossref / Europe PMC / arXiv / HAL / theses.fr, every
    one of them an article catalogue, while 27% of orphan PDFs run over 30
    pages against 8% of mapped ones.

    ISBN only, deliberately. Open Library's title search is broad and noisy
    against a scholarly corpus, and the whole point of this rung is that a
    valid check digit is publisher-asserted identity.
    """

    BASE = "https://openlibrary.org"

    def __init__(self, contact_email: str = ""):
        super().__init__(contact_email)
        self.name = "openlibrary"
        self.pace_s = 0.5
        self.supports = frozenset({"isbn"})

    def _do_isbn(self, value, *, year, rows, timeout_s):
        url = f"{self.BASE}/isbn/{urllib.parse.quote(value, safe='')}.json"
        try:
            payload = self._get_json(url, timeout_s)
        except SourceHTTPError as exc:
            if exc.status == 404:      # unknown ISBN: a clean no
                return []
            raise
        if not isinstance(payload, dict):
            return []
        title = _title_or_none(_clean(payload.get("title")))
        if not title:
            return []
        subtitle = _clean(payload.get("subtitle"))
        if subtitle and subtitle.casefold() not in title.casefold():
            title = f"{title.rstrip(' :')}: {subtitle}"
        # The edition record names authors by key only; resolving each would
        # cost a call per author for a field the card merely displays. The
        # `by_statement` line carries them as text when present.
        by = _clean(payload.get("by_statement")) or ""
        authors = tuple(a.strip() for a in re.split(r",| and ", by) if a.strip())
        return [Candidate(
            source=self.name, title=title, authors=authors[:8],
            year=_as_int_year(payload.get("publish_date")),
            container=_clean((payload.get("publishers") or [None])[0]),
            url=f"{self.BASE}/isbn/{value}",
        )]


class GoogleBooksSource(Source):
    """``www.googleapis.com/books`` — ISBN lookup (Batch D).

    Keyless for volume lookups. Broader ISBN coverage than Open Library,
    especially for recent and non-English editions.

    **This is a Google endpoint**, which is a different privacy proposition
    from a scholarly catalogue: the request discloses that this user holds a
    book with this ISBN. Off by default like every accession source, chosen
    explicitly, and documented in the README's egress note beside Tavily.
    """

    BASE = "https://www.googleapis.com/books/v1/volumes"

    def __init__(self, contact_email: str = ""):
        super().__init__(contact_email)
        self.name = "googlebooks"
        self.pace_s = 0.5
        self.supports = frozenset({"isbn"})

    def _do_isbn(self, value, *, year, rows, timeout_s):
        params = {"q": f"isbn:{value}", "maxResults": 1}
        url = f"{self.BASE}?{urllib.parse.urlencode(params)}"
        payload = self._get_json(url, timeout_s)
        items = (payload.get("items") if isinstance(payload, dict) else None) or []
        for item in items:
            info = (item or {}).get("volumeInfo") if isinstance(item, dict) else None
            if not isinstance(info, dict):
                continue
            title = _title_or_none(_clean(info.get("title")))
            if not title:
                continue
            subtitle = _clean(info.get("subtitle"))
            if subtitle and subtitle.casefold() not in title.casefold():
                title = f"{title.rstrip(' :')}: {subtitle}"
            authors = tuple(
                _clean(a) or "" for a in (info.get("authors") or [])
                if isinstance(a, str))
            return [Candidate(
                source=self.name, title=title,
                authors=tuple(a for a in authors if a)[:8],
                year=_as_int_year(info.get("publishedDate")),
                container=_clean(info.get("publisher")),
                url=_clean(info.get("infoLink")),
            )]
        return []


def default_sources(contact_email: str = "") -> list[Source]:
    """The keyless sources in query order (highest expected yield first).

    The two ISBN sources sit last: they serve a kind nothing else can, so
    their position only decides which answers an ISBN first, and Open Library
    is preferred because it is not a Google endpoint.
    """
    return [
        CrossrefSource(contact_email),
        EuropePmcSource(contact_email),
        ArxivSource(contact_email),
        HalSource(contact_email),
        ThesesFrSource(contact_email),
        OpenLibrarySource(contact_email),
        GoogleBooksSource(contact_email),
    ]


def default_source_names() -> tuple[str, ...]:
    """The registry names in order — the ``accession_sources`` config
    vocabulary (``api/routes/config.py`` keeps a literal copy so loading the
    config route never imports this module; a test pins the two)."""
    return tuple(s.name for s in default_sources())

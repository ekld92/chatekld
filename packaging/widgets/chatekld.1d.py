#!/usr/bin/env python3
# <xbar.title>ChatEKLD — paper of the day</xbar.title>
# <xbar.desc>Menu-bar card fed by ChatEKLD's widget.json snapshot: one paper
# of the day + one completable suggestion + a notion of the day.</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
"""SwiftBar/xbar plugin for ChatEKLD. Stdlib only.

Data source: when ``BASE_DIR/server.json`` names a live server (pid alive), a
fresh ``GET /api/widget`` is preferred (header ``X-Requested-With: ChatEKLD``
— the app's origin guard requires it; loopback only); otherwise the durable
``BASE_DIR/widget.json`` snapshot the app writes at the end of each manual
audit scan (and on the in-app "Refresh widget" button) — that fallback works
with the app CLOSED, so freshness is the last scan, by design.

HARD RULE: never call ``/api/audit/status`` or ``/api/obsidian/status`` —
those DRAIN one-shot message buffers meant for the GUI's own status feed.
``/api/health`` is the only sanctioned liveness probe, and this plugin does
not even need it (the pid check + /api/widget answer are enough).

This plugin never triggers a scan. Its one write action is the "Refresh
snapshot" item, which POSTs ``/api/widget/refresh`` — that rebuilds
``widget.json`` from the scan already in memory and touches nothing else. It
is shown as an inert grey line whenever the app is not running.

Cadence: the ``1d`` in the filename (SwiftBar convention). The app also
pushes ``swiftbar://refreshplugin?name=chatekld`` after each snapshot write,
so the menu updates right after a scan without waiting a day.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import textwrap
import urllib.parse
import urllib.request

APP_NAME = "ChatEKLD"
# The app installs under a DATED bundle name (ChatEKLD_2026-08-06.app), so
# `open -a ChatEKLD` fails. The bundle id is hard-coded by
# install_and_build.sh and is stable across builds.
APP_BUNDLE_ID = "com.chatekld.app"
TIMEOUT_S = 1.5
EXCERPT_WRAP = 52       # chars per submenu line
EXCERPT_MAX_LINES = 8   # submenu depth cap


def base_dir() -> str:
    env = os.environ.get("CHATEKLD_BASE_DIR", "").strip()
    if env:
        return env
    return os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")


def load_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def server_base(bd: str) -> str | None:
    """``http://host:port`` of a live server, else ``None``. No probe request.

    Validates like the app's own ``core/server_info.py::read_server_info``
    (version, int port, LOOPBACK host): ``base`` is later interpolated into a
    SwiftBar ``param6=`` curl argument and fetched with the app header, so a
    corrupt/hostile ``server.json`` must neither inject curl arguments (a
    space-bearing "port") nor point the plugin off-box.
    """
    info = load_json(os.path.join(bd, "server.json"))
    if not isinstance(info, dict) or info.get("version") != 1:
        return None
    if not pid_alive(info.get("pid")):
        return None
    port = info.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        return None
    host = info.get("host") or "127.0.0.1"
    if host not in ("127.0.0.1", "localhost", "::1"):
        return None
    return f"http://{host}:{port}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):  # noqa: D102 — drop every redirect
        return None


def live_payload(base: str | None):
    """Fresh /api/widget payload from a live server, else None. Read-only.

    No redirects (the app never redirects; following one would carry the
    header elsewhere) and a 1 MB read cap — web_tools.py discipline.
    """
    if not base:
        return None
    req = urllib.request.Request(
        f"{base}/api/widget", headers={"X-Requested-With": APP_NAME})
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read(1 << 20).decode("utf-8", "replace"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def esc(text) -> str:
    """SwiftBar line text: | starts the param section; collapse EVERY
    whitespace form (\\n\\r\\v\\f, Unicode separators) — a payload string must
    never fabricate a new menu line, which could carry ``bash=``."""
    return " ".join(str(text or "").split()).replace("|", "/")


def safe_href(uri) -> str | None:
    """An ``href=`` value is interpolated RAW into the SwiftBar param section,
    so it must carry no whitespace/quotes/controls (a space would terminate
    the value and everything after it becomes further params — ``bash=`` runs
    on click) and only expected schemes."""
    s = str(uri or "")
    if not s or not s.lower().startswith(("obsidian://", "http://", "https://")):
        return None
    if any(ch.isspace() or ch in "\"'`" or ord(ch) < 0x20 for ch in s):
        return None
    return s


def group(n) -> str:
    """4411 -> '4 411' (narrow no-break space)."""
    try:
        return f"{int(n):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(n)


def open_app_params() -> str:
    return (f"bash=/usr/bin/open param1=-b param2={APP_BUNDLE_ID} "
            f"terminal=false")


def note_href(bd: str, paper: dict) -> str | None:
    """obsidian:// link for the paper's note.

    Prefers the ``note_uri`` the app now ships in the payload; falls back to
    building one from ``config.json`` + ``note_rel`` so a snapshot written by
    an older build still links.
    """
    uri = safe_href(paper.get("note_uri"))
    if uri:
        return uri
    note_rel = paper.get("note_rel") or ""
    if not isinstance(note_rel, str) or not note_rel:
        return None
    cfg = load_json(os.path.join(bd, "config.json"))
    vault = (cfg or {}).get("obsidian_vault_path") or ""
    if not isinstance(vault, str) or not vault:
        return None
    return safe_href("obsidian://open?path=" + urllib.parse.quote(
        os.path.join(vault, note_rel), safe=""))


def freshness(generated_at: str) -> tuple[str, bool]:
    """Human age of the snapshot + whether it is stale (> 7 days)."""
    try:
        dt = datetime.datetime.fromisoformat(str(generated_at))
        now = datetime.datetime.now(dt.tzinfo) if dt.tzinfo else datetime.datetime.now()
        days = max(0, (now - dt).days)
    except (ValueError, TypeError):
        return "age unknown", True
    if days == 0:
        return "updated today", False
    label = "1 day old" if days == 1 else f"{days} days old"
    return label, days > 7


def print_excerpt(paper: dict) -> None:
    """The excerpt as a submenu of wrapped lines, not a clipped one-liner."""
    text = esc(paper.get("excerpt") or "").strip()
    if not text:
        return
    src = {"summary": "your summary",
           "pdf": "first page"}.get(paper.get("excerpt_source") or "")
    print(f"Excerpt{f' — {src}' if src else ''} | size=11 color=gray")
    lines = textwrap.wrap(text, EXCERPT_WRAP)
    for line in lines[:EXCERPT_MAX_LINES]:
        print(f"-- {line} | size=12")
    if len(lines) > EXCERPT_MAX_LINES:
        print("-- … | size=12 color=gray")


def print_refresh(base: str | None) -> None:
    """Refresh action — a live POST when the app runs, an inert hint when not."""
    if not base:
        print(f"Refresh snapshot — open {APP_NAME} first | size=11 color=gray")
        return
    # The header value carries no space (HTTP allows omitting it after the
    # colon), so no SwiftBar parameter needs quoting.
    print(f"Refresh snapshot | size=11 refresh=true "
          f"bash=/usr/bin/curl param1=-fsS param2=-X param3=POST "
          f"param4=-H param5=X-Requested-With:{APP_NAME} "
          f"param6={base}/api/widget/refresh terminal=false")


def main() -> int:
    bd = base_dir()
    base = server_base(bd)
    payload = live_payload(base) or load_json(os.path.join(bd, "widget.json"))
    if not isinstance(payload, dict) or payload.get("version") != 1:
        print("📚")
        print("---")
        if isinstance(payload, dict):
            print("Please update this widget — the snapshot format changed | color=red")
        else:
            print(f"No snapshot yet — run a scan in {APP_NAME}")
        print(f"Open {APP_NAME} | {open_app_params()}")
        return 0

    # Shape guards: a version-1 payload with a wrong-typed slot must degrade
    # to the empty rendering, not a SwiftBar plugin-error card.
    paper = payload.get("paper")
    paper = paper if isinstance(paper, dict) else {}
    suggestions = payload.get("suggestions")
    suggestions = ([s for s in suggestions if isinstance(s, dict)]
                   if isinstance(suggestions, list) else [])
    notion = payload.get("notion")
    notion = notion if isinstance(notion, dict) else {}
    primary = next((s for s in suggestions if s.get("kind") == "tag_batch"), {})

    # Menu-bar line: the completable batch count (the nudge), not the backlog.
    batch = primary.get("batch") or 0
    print(f"📚 {batch}" if batch else "📚")
    print("---")

    # Paper of the day — title is the one 14pt line, everything else recedes.
    if paper:
        print("Paper of the day | size=11 color=gray")
        href = note_href(bd, paper)
        print(f"{esc(paper.get('title') or paper.get('key'))} | size=14"
              + (f" href={href}" if href else ""))
        # `author_line` is the app's short form ("Jardri et al."); older
        # snapshots only carry the raw "Last, First" list.
        who = paper.get("author_line") or "; ".join(paper.get("authors") or [])
        meta = " · ".join(
            x for x in (who, str(paper.get("year") or "")) if x)
        if meta:
            print(f"{esc(meta)} | size=11 color=gray")
        print_excerpt(paper)
        if href:
            print(f"Open note in Obsidian | size=11 href={href}")
        print("---")

    # One concrete action first, the backlog quiet underneath.
    if primary.get("count"):
        print(f"{esc(primary.get('label') or 'Tag papers')} | size=13 "
              f"{open_app_params()}")
    # Short store labels, matching the desktop card — a compact line reads as
    # context, the spelled-out version reads as a to-do list.
    labels = {"finder_drift": "Finder", "note_drift": "Zotero",
              "unread": "unread"}
    parts = [f"{group(s['count'])} {labels[s['kind']]}"
             for s in suggestions
             if s.get("kind") in labels and s.get("count")]
    if parts:
        print(f"{esc(' · '.join(parts))} | size=11 color=gray")

    if notion.get("tag"):
        print("---")
        print("Notion of the day | size=11 color=gray")
        print(f"#{esc(notion['tag'])} | size=13")
        # Glosses are short; keep them visible instead of hiding a single
        # line behind a submenu.
        for line in textwrap.wrap(esc(notion.get("gloss") or ""), 60)[:3]:
            print(f"{line} | size=11 color=gray")

    print("---")
    age, stale = freshness(payload.get("generated_at"))
    colour = " color=orange" if stale else " color=gray"
    print(f"Snapshot {age} | size=10{colour}")
    print_refresh(base)
    print(f"Open {APP_NAME} | size=11 {open_app_params()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

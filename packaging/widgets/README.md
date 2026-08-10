# ChatEKLD desktop widgets

Two optional companions that surface a **paper of the day**, one completable
suggestion ("Tag 10 papers") and a **notion of the day** from your library, on
your Mac's menu bar and/or desktop.

Both read ONE file the app writes:

    ~/Library/Application Support/ChatEKLD/widget.json

That snapshot is written at the end of each **manual** audit scan and by the
**Refresh widget** button on the app's Audit tab — nothing here schedules or
triggers scans, and the widgets work with the app closed (freshness = your
last scan; each widget shows the snapshot's age).

## SwiftBar (menu bar) — `chatekld.1d.py`

1. Install [SwiftBar](https://github.com/swiftbar/SwiftBar) (or xbar).
2. Copy `chatekld.1d.py` into your SwiftBar plugin folder and make it
   executable:

       cp chatekld.1d.py "<your plugin folder>/"
       chmod +x "<your plugin folder>/chatekld.1d.py"

3. It refreshes daily (the `1d` in the filename) — **and** the app pushes
   `swiftbar://refreshplugin?name=chatekld` after every snapshot write, so
   the menu updates right after a scan. If your SwiftBar names plugins
   differently, the push is best-effort and the daily cadence still applies.

The menu bar shows the batch size, not the backlog. In the menu:

- the **title** opens the paper's note in Obsidian (as does *Open note in
  Obsidian* below it) — shown only when the paper has a note;
- **Excerpt** is a submenu holding the full wrapped text, so nothing is
  clipped mid-word;
- **Refresh snapshot** rebuilds `widget.json` from the scan already in
  memory. It appears as an inert grey line when the app isn't running, since
  the rebuild needs the app. It never starts a scan.

When the app is running, the plugin prefers a fresh `GET /api/widget` from
the local server (discovered via `server.json`, pid-checked); otherwise it
reads the file. It never polls the app's status endpoints.

## Übersicht (desktop card) — `chatekld-widget.jsx`

1. Install [Übersicht](https://github.com/felixhageloh/uebersicht).
2. Copy `chatekld-widget.jsx` into
   `~/Library/Application Support/Übersicht/widgets/`.
3. Delete the `GettingStarted.jsx` widget Übersicht ships with — otherwise
   its sample card sits on your desktop alongside this one.

The card sits on the desktop (visible on show-desktop) and re-reads the file
every 30 minutes.

**It is deliberately opaque and light in both macOS appearances.** It is an
index card: it has to stay readable over any wallpaper, and switching it to
a dark variant would only be legible over half of them. Nothing changes when
your Mac switches to Dark Mode.

### Making the card clickable (optional)

Übersicht widgets are click-through until you enable interaction system-wide:

1. Übersicht → **Preferences** → set an **interaction shortcut** (a modifier
   key you hold to interact with widgets).
2. Grant Übersicht **Accessibility** access in System Settings → Privacy &
   Security → Accessibility.

Then hold that modifier and click the card: the **title** opens the paper's
note in Obsidian (or the app, when the paper has no note), and the **"Tag N
papers"** line opens ChatEKLD. Until you do this, the card is display-only
and the clicks simply never arrive — nothing breaks. SwiftBar's menu is
clickable with no setup at all, so it stays the reliable action surface.

## Notes

- If you run the app with a custom `CHATEKLD_BASE_DIR`, set the same
  variable in SwiftBar's environment, and edit the path at the top of the
  `.jsx` file.
- Both widgets check the snapshot's `version` field and show a "please
  update this widget" line instead of misrendering a newer schema. Fields
  added within version 1 are additive: an older copy of a widget ignores
  them and keeps working.
- "Open ChatEKLD" resolves the app by bundle id (`com.chatekld.app`), not by
  name — the app installs under a dated name (`ChatEKLD_<build-date>.app`), so
  `open -a ChatEKLD` would fail.
- The snapshot is regenerable app data: deleting `widget.json` costs one
  Refresh click (or the next scan), nothing else.

// CHERQAiN — desktop card (Übersicht widget).
//
// Reads BASE_DIR/widget.json — the durable snapshot CHERQAiN writes at the
// end of each manual audit scan and on the in-app "Refresh widget" button.
// It never calls the app's API and never triggers a scan; it re-reads the
// file on a timer, so freshness is your last scan, by design, and the card
// works with the app closed.
//
// Design: "index card" — an OPAQUE warm-paper card. Deliberately opaque and
// deliberately single-appearance: it must stay legible over any wallpaper,
// light or dark, and a physical index card does not have a dark mode.
//
// Clicking is optional. Übersicht only delivers click events once you set an
// interaction shortcut in its preferences AND grant it Accessibility access;
// until then the handlers below simply never fire and the card is a display
// surface. See README.md.
//
// Install: copy this file into ~/Library/Application Support/Übersicht/widgets/
// (see README.md alongside). If you run the app with a custom
// CHATEKLD_BASE_DIR, edit the path in `command` below.

import { run } from "uebersicht";

export const command =
  'cat "$HOME/Library/Application Support/ChatEKLD/widget.json" 2>/dev/null || true';

// Re-read every 30 minutes — a display refresh, not a data refresh (the file
// only changes when the app writes it; the cost of a re-read is one `cat`).
export const refreshFrequency = 30 * 60 * 1000;

export const className = `
  bottom: 40px;
  right: 40px;
  width: 340px;
  font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
  -webkit-font-smoothing: antialiased;

  .card {
    background: #fbfaf7;
    color: #1c1b18;
    border-top: 3px solid #1e4fa3;
    border-radius: 5px;
    padding: 13px 16px 11px;
    box-shadow: 0 3px 14px rgba(0, 0, 0, 0.24), 0 0 0 0.5px rgba(0, 0, 0, 0.10);
  }

  /* Header: what this is, and the paper's call number. */
  .head { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; margin-bottom: 7px; }
  .kick { font-size: 9.5px; letter-spacing: 0.12em; text-transform: uppercase; color: #1e4fa3; font-weight: 600; margin: 0; }
  .callno { font-size: 9.5px; letter-spacing: 0.04em; color: #a9a59c; margin: 0; white-space: nowrap; font-variant-numeric: tabular-nums; }

  /* Paper. Both clamps are what bound the card's height: a long French
     title can no longer push the card off the bottom of the screen. */
  .title { font-size: 15px; line-height: 1.28; font-weight: 600; letter-spacing: -0.005em; margin: 0;
           display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
  .title.clickable:hover { color: #1e4fa3; }
  .meta { font-size: 11px; color: #7b776f; margin: 3px 0 0; }
  .excerpt { font-size: 11.5px; line-height: 1.5; color: #45423c; margin: 9px 0 0;
             border-left: 2px solid #e3e0d8; padding-left: 9px;
             display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
  .src { color: #a9a59c; font-style: italic; }

  .sep { border: 0; border-top: 1px solid #e6e3db; margin: 11px 0 9px; }

  /* One completable batch in accent; the backlog stays quiet underneath. */
  .action { font-size: 13px; font-weight: 600; color: #1e4fa3; letter-spacing: -0.003em; margin: 0; }
  .counts { font-size: 10.5px; color: #86827a; margin: 5px 0 0; font-variant-numeric: tabular-nums; }
  .dot { display: inline-block; width: 5px; height: 5px; border-radius: 50%; vertical-align: 1px; margin-right: 4px; }

  .notion { font-size: 11px; line-height: 1.45; color: #56534c; margin: 10px 0 0; padding-top: 9px; border-top: 1px solid #e6e3db; }
  .notion b { color: #1e4fa3; font-weight: 600; }

  .foot { display: flex; justify-content: space-between; gap: 10px; margin-top: 9px; font-size: 9.5px; color: #b0aca3; }
  .foot .stale { color: #a05a00; }
  .foot .hint { white-space: nowrap; }

  .empty { font-size: 12px; line-height: 1.45; color: #6b6b6b; margin: 0; }
`;

// Per-store colours, matching the app's Card Catalog category tokens.
const COUNTS = [
  { kind: "finder_drift", label: "Finder", colour: "#1e4fa3" },
  { kind: "note_drift", label: "Zotero", colour: "#c8222b" },
  { kind: "unread", label: "unread", colour: "#93690c" },
];

// 4411 -> "4 411" (narrow no-break space). A raw five-figure backlog reads as
// a rebuke; the same number grouped reads as a quantity.
function group(n) {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}

function ageOf(iso) {
  const t = Date.parse(iso || "");
  if (Number.isNaN(t)) return { label: "age unknown", stale: true };
  const days = Math.max(0, Math.floor((Date.now() - t) / 86400000));
  if (days === 0) return { label: "Updated today", stale: false };
  return { label: days === 1 ? "1 day old" : `${days} days old`, stale: days > 7 };
}

// Single-quote a value for /bin/sh.
function shq(s) {
  return "'" + String(s).replace(/'/g, "'\\''") + "'";
}

// The app installs under a DATED name (ChatEKLD_2026-08-06.app), so
// `open -a CHERQAiN` fails; the bundle id is the stable handle and is
// hard-coded by install_and_build.sh.
const APP_BUNDLE_ID = "com.chatekld.app";

function openUri(uri) {
  // shq() makes injection impossible, but the scheme is still payload data:
  // only the obsidian:// deep link the app builds may be opened — anything
  // else (arbitrary scheme handler, a path, a leading-dash flag) falls back
  // to opening the app itself.
  if (typeof run !== "function") return;
  if (typeof uri === "string" && /^obsidian:\/\//.test(uri)) {
    run("open " + shq(uri));
  } else {
    openApp();
  }
}

function openApp() {
  if (typeof run === "function") run("open -b " + shq(APP_BUNDLE_ID));
}

function card(children) {
  return <div className="card">{children}</div>;
}

export const render = ({ output }) => {
  let data = null;
  try {
    data = JSON.parse(output);
  } catch (e) {
    data = null;
  }

  if (!data || typeof data !== "object") {
    return card(
      <div>
        <p className="kick">CHERQAiN</p>
        <p className="empty">No snapshot yet — run a library scan in CHERQAiN.</p>
      </div>
    );
  }
  if (data.version !== 1) {
    return card(
      <div>
        <p className="kick">CHERQAiN</p>
        <p className="empty">Please update this widget — the snapshot format changed.</p>
      </div>
    );
  }

  // Shape guards: a version-1 payload with a wrong-typed slot must degrade
  // to the empty rendering, not throw inside render.
  const paper =
    data.paper && typeof data.paper === "object" && !Array.isArray(data.paper)
      ? data.paper
      : {};
  const suggestions = Array.isArray(data.suggestions)
    ? data.suggestions.filter((s) => s && typeof s === "object")
    : [];
  const primary = suggestions.find((s) => s.kind === "tag_batch") || {};
  const notion =
    data.notion && typeof data.notion === "object" && data.notion.tag
      ? data.notion
      : null;
  const age = ageOf(data.generated_at);
  const srcLabel = { summary: "your summary", pdf: "first page" }[paper.excerpt_source];
  const counts = COUNTS.map((c) => {
    const hit = suggestions.find((s) => s.kind === c.kind);
    return hit && hit.count ? { ...c, count: hit.count } : null;
  }).filter(Boolean);

  return card(
    <div>
      <div className="head">
        <p className="kick">Paper of the day</p>
        {paper.key ? <p className="callno">{paper.key}</p> : null}
      </div>

      {paper.title ? (
        <div>
          {/* Always clickable: with a note it deep-links there, without one
              it falls back to the app, so hover feedback is always honest. */}
          <p
            className="title clickable"
            onClick={() => (paper.note_uri ? openUri(paper.note_uri) : openApp())}
          >
            {paper.title}
          </p>
          <p className="meta">
            {/* author_line is the app's short form ("Jardri et al."); older
                snapshots only carry the raw "Last, First" list. */}
            {paper.author_line || (paper.authors || []).join("; ")}
            {paper.year ? ` · ${paper.year}` : ""}
          </p>
          {paper.excerpt ? (
            <p className="excerpt">
              {paper.excerpt}
              {srcLabel ? <span className="src"> — {srcLabel}</span> : null}
            </p>
          ) : null}
        </div>
      ) : (
        <p className="empty">No paper drawn yet.</p>
      )}

      <hr className="sep" />

      {primary.batch ? (
        <p className="action" onClick={openApp}>
          {primary.label}
        </p>
      ) : (
        <p className="action">Everything in this library is tagged.</p>
      )}
      {counts.length ? (
        <p className="counts">
          {counts.map((c, i) => (
            <span key={c.kind}>
              {i ? " · " : ""}
              <span className="dot" style={{ background: c.colour }} />
              {group(c.count)} {c.label}
            </span>
          ))}
        </p>
      ) : null}

      {notion ? (
        <p className="notion">
          <b>#{notion.tag}</b> — {notion.gloss}
        </p>
      ) : null}

      <div className="foot">
        <span className={age.stale ? "stale" : ""}>{age.label}</span>
        <span className="hint">click to open</span>
      </div>
    </div>
  );
};

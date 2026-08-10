/**
 * Contextual help wiring (Axis ii). A single declarative table of help text —
 * keyed by the id of an existing control input or action button — plus
 * installHelp(), which injects a "?" badge next to each and attaches the
 * matching ui.js primitive: a hover/focus TOOLTIP for a setting, or a click
 * POPOVER for an action (with an explicit "writes to your vault" notice where
 * it applies).
 *
 * A LEAF-safe FEATURE module: imports only ui.js (per the JS Module Hierarchy).
 * The table is the ONE source of truth; adding a Settings control or a write
 * action means adding a row here — and, for a vault-write button, WRITE_ACTION_IDS
 * below (a test ratchet asserts every one of them has a writes:true popover).
 */
import { helpTip, helpPopover, sanitiseHtml, runTour, announceError } from './ui.js';

// [ controlOrButtonId, 'tip' | 'popover', payload ]
//   tip     payload: { text }
//   popover payload: { title, body, writes?, anchor? }  anchor:'label' hangs the
//                    badge off the control's <label> instead of the control
//                    itself (used when a SETTING is too long for a tip).
// Mirrors the [id, key, kind] tuple tables in settings.js. Ids that are absent
// from the current DOM are skipped silently at install time.
export const HELP_ENTRIES = [
    // ── Deck generation knobs (the jargon the user flagged) ──
    ['deck-max-sections', 'tip', {
        text: 'Upper bound on how many sections the outline may produce. Fewer sections = a shorter, faster deck.',
    }],
    ['deck-agent-iters', 'tip', {
        text: 'Max reasoning/tool steps the agent may take per section while gathering material from your vault. Higher = more thorough but slower.',
    }],
    ['deck-section-attempts', 'tip', {
        text: 'How many times a failed section is retried before falling back to a placeholder frame. 1–5, default 3.',
    }],
    ['deck-retry-backoff', 'tip', {
        text: 'Seconds waited (linearly) between section retries, to let a transient provider hiccup clear. 0–30 s.',
    }],
    ['deck-section-max-tokens', 'tip', {
        text: 'Output-length cap per section. Larger allows denser slides but costs more tokens and time.',
    }],
    ['deck-temp', 'tip', {
        text: 'Sampling temperature. Lower is more focused and repeatable; higher is more varied and creative.',
    }],

    // ── Report / Article generation knobs (same semantics as the deck twins) ──
    ['docgen-max-sections', 'tip', {
        text: 'Upper bound on how many sections the outline may produce. Fewer sections = a shorter, faster document.',
    }],
    ['docgen-agent-iters', 'tip', {
        text: 'Max reasoning/tool steps the agent may take per section while gathering material from your vault. Higher = more thorough but slower.',
    }],
    ['docgen-section-attempts', 'tip', {
        text: 'How many times a failed section is retried before falling back to a placeholder. 1–5, default 3.',
    }],
    ['docgen-retry-backoff', 'tip', {
        text: 'Seconds waited (linearly) between section retries, to let a transient provider hiccup clear. 0–30 s.',
    }],
    ['docgen-section-max-tokens', 'tip', {
        text: 'Output-length cap per section. Prose sections run longer than slides, hence the higher default (4096).',
    }],
    ['docgen-temp', 'tip', {
        text: 'Sampling temperature. Lower is more focused and repeatable; higher is more varied and creative.',
    }],

    // ── Provider / timeout plumbing (LLM Settings) ──
    ['set-online-timeout', 'tip', {
        text: 'Time to wait for one online-provider request before giving up on that call.',
    }],
    ['set-online-retries', 'tip', {
        text: 'How many times a transient online-provider error is retried before the request fails.',
    }],
    ['set-local-timeout', 'tip', {
        text: 'Per-call timeout for a local model (Ollama / LM Studio). 0 leaves each path’s own default in place.',
    }],
    ['set-agent-wall-clock', 'tip', {
        text: 'Hard time budget for one agent turn or one deck section. Bounds a run so a stuck model can’t hang the app.',
    }],
    ['set-vision-timeout', 'tip', {
        text: 'Per-image timeout for the vision/OCR model during indexing, so one stuck image can’t stall a long run.',
    }],
    ['set-vision-failure-cooldown', 'tip', {
        text: 'After a failed image call, vision is skipped for this long so a missing model isn’t retried once per image. 0 disables the cooldown.',
    }],
    // POPOVER, not a tip (2026-07-26 UX audit). At 364 chars this entry was
    // 2.2x the longest tip (166) — ~11 lines at the tip's 260px max-width, with
    // no max-height, pointer-events:none (so the pointer can NEVER enter it:
    // WCAG 1.4.13 Hoverable is unmeetable) and no scroll dismissal. It also
    // carries a model identifier the user needs to COPY, which a tip forbids.
    // The popover is focusable, height-bounded, scroll-dismissed and text-
    // selectable — the primitive that already handles exactly this shape.
    // `anchor: 'label'` keeps the badge beside the field NAME (a popover
    // otherwise anchors on the control itself, which for a text input would
    // drop the badge after the box).
    ['set-reranker-model', 'popover', {
        anchor: 'label',
        title: 'Reranker model',
        body: 'HuggingFace repo id of the cross-encoder reranker. BAAI/bge-reranker-v2-m3 is the recommended multilingual (FR/DE/EN) upgrade over the English-trained default. No reindex — reranking is query-time. Change it, then QUIT AND RELAUNCH: offline mode is decided at launch, so a model changed mid-session cannot be fetched and reranking turns itself off instead. The new model (~2.3 GB) downloads on the first query after the relaunch.',
        writes: false,
    }],
    ['set-reranker-device', 'tip', {
        text: 'Where the cross-encoder reranker runs. “auto” uses the best available (MPS on Apple Silicon); “cpu” frees unified memory for the LLM.',
    }],
    ['set-vector-backend', 'tip', {
        text: 'Storage for embeddings. “simple” is the JSON store; “lancedb” is the binary backend with lower RAM and faster cold start. Governs fresh builds only.',
    }],
    ['set-prewarm-enabled', 'tip', {
        text: 'Load the retrieval models at launch. Off defers the multi-GB load to the first chat instead.',
    }],

    // ── Vault chat retrieval / agent knobs (Batch H, 2026-07-22) ──
    ['vault-top-k', 'tip', {
        text: 'How many retrieved chunks reach the model per question. More = broader context but a longer prompt.',
    }],
    ['vault-cutoff', 'tip', {
        text: 'Minimum similarity a chunk needs to be considered (dense-only retrieval). Raise it to drop marginal matches. Inert while the reranker is on (the default) — the reranker’s own ranking decides then.',
    }],
    ['vault-prompt-mode', 'tip', {
        text: 'Answer style: strict sticks to the retrieved notes, balanced allows light inference, exploratory ranges wider, concise keeps it short.',
    }],
    ['vault-temp', 'tip', {
        text: 'Sampling temperature for the answer. Lower = more focused and repeatable.',
    }],
    ['vault-hybrid-enabled', 'tip', {
        text: 'Combine lexical (BM25) and semantic (dense) retrieval, fused by rank. Usually better for names, doses and abbreviations.',
    }],
    ['vault-reranker-enabled', 'tip', {
        text: 'Re-score the candidate pool with a cross-encoder before answering. Slower per question, noticeably better ordering.',
    }],
    ['vault-agent-enabled', 'tip', {
        text: 'Let the model search and read your vault across several steps (vault_search / vault_read_note / vault_list_materials) instead of one retrieval pass.',
    }],
    ['vault-websearch-enabled', 'tip', {
        text: 'Adds a web_search tool to agent mode. Online chat providers only, needs TAVILY_API_KEY in the environment; results are treated as untrusted source text.',
    }],
    ['vault-mmr-enabled', 'tip', {
        text: 'Diversify the retrieved chunks (Maximal Marginal Relevance) so near-duplicates don’t crowd out coverage.',
    }],
    ['vault-query-expansion', 'tip', {
        text: 'Rewrite your question into several variants and fuse their results. Local providers only; costs one extra LLM call.',
    }],
    ['vault-wikilink-enabled', 'tip', {
        text: 'Also consider chunks from notes linked to/from the retrieved ones ([[wikilinks]]), then let the reranker keep the relevant ones.',
    }],
    ['vault-thesaurus-enabled', 'tip', {
        text: 'Expand the query with synonyms from your curated _abreviations.md / _tags.md tables (FR/EN shorthand bridging).',
    }],
    ['vault-primer-enabled', 'tip', {
        text: 'Prepend a small glossary of your vault’s shorthand to the system prompt so the model can read abbreviations in the retrieved notes.',
    }],

    // ── Plain Chat ──
    ['plainchat-input', 'tip', {
        text: 'A direct conversation with the configured model — no vault retrieval, no tools. The transcript lives in this window only (lost on reload; use Export to keep it).',
    }],

    // ── Note Refactor scope knobs ──
    ['refactor-scope', 'tip', {
        text: 'The sub-folder of your vault to analyze. The plan only ever reads and writes inside this scope.',
    }],
    ['refactor-strip-preamble', 'tip', {
        text: 'When an image OCR callout is inlined, drop its descriptive preamble and keep only the transcription. Applies scope-wide.',
    }],

    // ── Actions (click popovers) ──
    ['deck-generate-btn', 'popover', {
        title: 'Generate Deck',
        body: 'Builds a LaTeX Beamer deck from your topic, grounded in the indexed vault and in your template’s house style: outlines, then writes it section by section. Emit-only — it writes a new <slug>/ folder you compile with make; it never edits your vault notes.',
        writes: false,
    }],
    ['docgen-topic', 'tip', {
        text: 'The subject of the article/report. The outline and every section are grounded only in what your indexed vault says about it.',
    }],
    ['docgen-citations', 'tip', {
        text: 'When your template has a bibliography, cite matching entries with \\autocite{key} instead of plain-prose (source: note.md). Unmatched claims still fall back to prose.',
    }],
    ['docgen-generate-btn', 'popover', {
        title: 'Generate document',
        body: 'Builds a LaTeX article/report (prose, not slides) from your topic, grounded in the vault and your template’s house style: outlines, then writes it section by section with cancel-aware retries and resume. Emit-only — it writes a new <slug>/ folder you compile with make; it never edits your vault notes.',
        writes: false,
    }],
    ['docgen-md-generate-btn', 'popover', {
        title: 'Generate a Markdown note',
        body: 'Builds a structured long-form Markdown note (# Title + ## sections) from your topic, grounded in the vault, and writes it as a NEW note in the sub-folder you choose. It never overwrites an existing note.',
        writes: true,
        writesText: 'Writes a new note into your vault — journalled and reversible from Note Refactor → Restore.',
    }],
    ['refactor-run-btn', 'popover', {
        title: 'Run Plan',
        body: 'Read-only analysis of the scoped notes: proposes OCR callouts, formatting fixes and hygiene notes, and shows a preview diff. Runs no vision or LLM calls and writes nothing — nothing changes until you approve and Apply.',
        writes: false,
    }],
    ['refactor-apply-btn', 'popover', {
        title: 'Apply approved',
        body: 'Writes the approved OCR callouts into your notes. Each note is re-checked against the preview (content + WYSIWYG hashes) before writing, and the originals are snapshotted first.',
        writes: true,
        writesText: 'Writes to your vault — journalled and reversible via Restore.',
    }],
    ['refactor-normalize-btn', 'popover', {
        title: 'Fix formatting',
        body: 'Deterministic formatting cleanup — no LLM. Fixes missing blank lines, trailing whitespace and the final newline only; predictable and identical to the preview. Never changes your wording.',
        writes: true,
        writesText: 'Writes to your vault — journalled and reversible via Restore.',
    }],
];

// Static vault-write action buttons. The ratchet in tests/js/helpTable.test.js
// asserts each has a writes:true popover entry above — so a new write button
// without help fails the suite.
export const WRITE_ACTION_IDS = ['refactor-apply-btn', 'refactor-normalize-btn', 'docgen-md-generate-btn'];

/**
 * Find the best anchor to hang a setting's "?" badge on: its <label> (so the
 * badge sits beside the control name), falling back to the control itself.
 */
function _tipAnchorFor(id) {
    const byFor = document.querySelector(`label[for="${id}"]`);
    if (byFor) return byFor;
    const el = document.getElementById(id);
    if (!el) return null;
    const group = el.closest ? el.closest('.control-group, .checkbox-row') : null;
    const label = group ? group.querySelector('label') : null;
    return label || el;
}

/**
 * Inject every help badge. Idempotent (the primitives guard per anchor via a
 * dataset flag) and tolerant of missing ids — call once from app.js init.
 * Returns the number of badges actually attached (tests assert on it).
 */
// ── Help tab (Batch H, 2026-07-22): the in-app user guide ────────────────────

let _guideLoaded = false;

/**
 * Lazily fetch + render static/help/guide.md into the Help tab. Idempotent —
 * app.js calls it on every Help-tab activation; only the first call fetches.
 * Rendered through the vendored `marked` global + ui.js::sanitiseHtml (the
 * same single gate every rendered-markdown surface uses), with a plain-text
 * fallback when marked is unavailable.
 */
export async function loadHelpTab() {
    const host = document.getElementById('help-guide-body');
    if (!host || _guideLoaded) return;
    _guideLoaded = true;
    try {
        const r = await fetch('/static/help/guide.md');
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const md = await r.text();
        // Render into an inner column: .help-guide is the SCROLL container
        // (flex:1 + overflow-y in CSS — the card clips, so the guide must own
        // its own scrollbar), .help-guide-inner the centered reading measure.
        const inner = document.createElement('div');
        inner.className = 'help-guide-inner';
        if (window.marked && typeof window.marked.parse === 'function') {
            inner.innerHTML = sanitiseHtml(window.marked.parse(md));
        } else {
            const pre = document.createElement('pre');
            pre.className = 'help-guide-fallback';
            pre.textContent = md;
            inner.appendChild(pre);
        }
        host.replaceChildren(inner);
    } catch (_e) {
        _guideLoaded = false;   // retry on the next tab activation
        const msg = 'Could not load the guide — re-open the Help tab to retry (see the project README).';
        host.textContent = msg;
        // Project rule: every outcome both visible AND announced — this fetch
        // failure was previously visual-only.
        announceError(msg);
    }
}

export function installHelp(entries = HELP_ENTRIES) {
    let attached = 0;
    for (const [id, kind, payload] of entries) {
        if (kind === 'tip') {
            const anchor = _tipAnchorFor(id);
            if (!anchor) continue;
            const labelText = anchor.tagName === 'LABEL' ? (anchor.textContent || '').trim() : id;
            if (helpTip(anchor, { text: payload.text, labelText })) attached++;
        } else if (kind === 'popover') {
            // Default anchor is the element itself (an action BUTTON — the
            // original use). `anchor: 'label'` opts a settings CONTROL into the
            // same label-side placement tips use, so promoting a long tip to a
            // popover doesn't move its badge. Explicit rather than inferred:
            // _tipAnchorFor on a button would walk to whatever <label> happens
            // to share its .control-group.
            const anchorEl = payload && payload.anchor === 'label'
                ? _tipAnchorFor(id)
                : document.getElementById(id);
            if (!anchorEl) continue;
            if (helpPopover(anchorEl, payload)) attached++;
        }
    }
    return attached;
}

// ── Guided tour (Batch H): one pass over the main views ─────────────────────

const TOUR_STEPS = [
    { tab: 'obsidian', target: 'obsidian-tab', title: 'Obsidian Agent',
      text: 'Index your vault, then ask questions against it. Retrieval knobs under the chat apply to the next Send — no reindex needed. Agent mode lets the model search across several steps; exports carry full provenance.' },
    { tab: 'document', target: 'document-tab', title: 'Single Paper',
      text: 'Upload one PDF and stream a tunable summary. Papers larger than the model window are summarised map-reduce — slower, never silent.' },
    { tab: 'audit', target: 'audit-tab', title: 'Library Audit & Tagging',
      text: 'Run Scan reconciles the vault against Zotero, PDFs and Finder tags. The Tagging report writes tags to all three stores (confirmed, journalled, undoable); Tag Hygiene can retire a tag everywhere.' },
    { tab: 'deck', target: 'deck-tab', title: 'Deck Generator',
      text: 'A topic + instructions becomes a Beamer deck in your template’s style, grounded in the vault. Emit-only: compile with make. Preview, Augment, and Compile & Auto-Fix are opt-in and screened.' },
    { tab: 'docgen', target: 'docgen-tab', title: 'Report / Article',
      text: 'The prose sibling of the deck: a LaTeX article, or a Markdown note written (reversibly) into the vault.' },
    { tab: 'refactor', target: 'refactor-tab', title: 'Note Refactor',
      text: 'Read-only analysis of a sub-folder with preview diffs; every write is confirmed and reversible from Restore. Notes too large for one LLM pass can be edited section by section.' },
    { tab: 'chat', target: 'chat-tab', title: 'Plain Chat',
      text: 'A direct conversation with the configured model — no vault, no tools. Export keeps the transcript with model + usage stamping.' },
    { tab: 'help', target: 'help-guide-body', title: 'Help',
      // The guide loads lazily on tab CLICK; the tour switches tabs via
      // showTab, which skips that loader — the onEnter hook fills the gap so
      // this step never shows an empty guide.
      onEnter: () => { loadHelpTab(); },
      text: 'This guide, the "?" badges on the settings, and ⌘K for the command palette. Enjoy!' },
];

/** Launch the guided tour (never auto-fires — the Help tab button calls this). */
export function startTour() {
    runTour(TOUR_STEPS);
}

/**
 * Shared UI primitives — the activity bar, accessibility announcers, recoverable
 * error boundaries, modal/tab/tablist wiring, the provider badge, clipboard, and
 * the HTML sanitiser. A LEAF module in the JS module hierarchy: it imports only
 * api.js (for logError) and nothing else from the project, so every feature
 * module can depend on it without a circular import. `updateProviderBadge` lives
 * here precisely so config.js and app.js can both import it without a cycle.
 */
import { logError, secureFetch } from './api.js';

const _activeTasks = new Map();
// Item 3.7 (improvement plan 2026-07-04): per-overlay modal state, keyed by
// modal id, in OPEN ORDER (Map iteration order = stacking order). The old
// design kept ONE global trigger/keyHandler/overlayClick slot — so opening a
// second modal overwrote the first's handlers, and any closeModal call (in
// particular refactor.js's delayed setTimeout closes) stripped whatever
// modal happened to be live, killing its Esc/Tab handling and focus restore.
// Invariants (pinned by tests/js/modal.test.js): closing one modal never
// affects another's handlers; closeModal on a not-open overlay is a no-op;
// Esc/Tab act on the TOPMOST modal only; the background leaves inert only
// when the last modal closes.
const _modalState = new Map();   // modalId -> {trigger, keyHandler, overlayClick}
// Background regions made inert (non-interactive + hidden from AT) while a modal
// is open, so focus and screen-reader navigation cannot wander behind the dialog.
const _MODAL_BACKDROP_IDS = ['main-content', 'app-sidebar'];

function _setBackgroundInert(on) {
    _MODAL_BACKDROP_IDS.forEach((id) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.inert = on;
        if (on) el.setAttribute('aria-hidden', 'true');
        else el.removeAttribute('aria-hidden');
    });
}
const _MODAL_FOCUSABLE_SELECTOR = 'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href]';

/** Focusable candidates that are actually VISIBLE (2026-07-22 audit F7).
 *
 * The bare selector matched `display:none` controls (the pull modal's hidden
 * Stop-watching button, settings controls inside closed <details>), so the
 * computed first/last element could be un-focusable — the Tab-wrap guard then
 * never fired and Tab produced dead presses at the modal edges.
 * checkVisibility() is the accurate modern test; the offsetParent fallback
 * covers display:none in older engines/jsdom. */
function _visibleFocusable(root) {
    return Array.from(root.querySelectorAll(_MODAL_FOCUSABLE_SELECTOR)).filter((el) => {
        if (typeof el.checkVisibility === 'function') return el.checkVisibility();
        return el.offsetParent !== null;
    });
}

/**
 * Register a long-running task under `id` with a user-visible `label`. Tasks are
 * keyed by id so concurrent operations each show in the activity bar; call
 * taskEnd(id) to clear one. Re-registering the same id replaces its label.
 */
export function taskBegin(id, label) {
    _activeTasks.set(id, label);
    renderActivityBar();
}

export function taskEnd(id) {
    _activeTasks.delete(id);
    renderActivityBar();
}

function renderActivityBar() {
    const bar = document.getElementById('activity-bar');
    const lbl = document.getElementById('activity-label-text');
    if (!bar || !lbl) return;
    if (_activeTasks.size === 0) {
        bar.style.display = 'none';
        lbl.textContent = '';
    } else {
        bar.style.display = 'block';
        lbl.textContent = [..._activeTasks.values()].join(' · ');
    }
}

export function announceError(message) {
    const el = document.getElementById('sr-error-announcer');
    if (!el) return;
    el.textContent = '';
    window.requestAnimationFrame(() => {
        el.textContent = message || 'An unexpected error occurred.';
    });
}

// Politely announce a non-error status to assistive tech (e.g. "Copied to
// clipboard"). Writes to the visually-hidden role="status" region; the rAF
// re-write forces a re-announcement even if the same text repeats.
export function announceStatus(message) {
    const el = document.getElementById('sr-status-announcer');
    if (!el || !message) return;
    el.textContent = '';
    window.requestAnimationFrame(() => { el.textContent = message; });
}

/**
 * Set a status element's text plus the ARIA role/live-region pair appropriate to
 * its severity: errors become an assertive alert (and are echoed to the global
 * screen-reader announcer) while non-errors are a polite status update.
 */
export function setStatusA11y(el, message, isError) {
    if (!el) return;
    el.textContent = message;
    if (isError) {
        el.setAttribute('role', 'alert');
        el.setAttribute('aria-live', 'assertive');
        announceError(message);
    } else {
        el.setAttribute('role', 'status');
        el.setAttribute('aria-live', 'polite');
    }
}

/**
 * Shared inline confirmation ceremony for destructive, hard-to-reverse actions
 * (Track 6e). Note Refactor confirm-gates every vault write while the deck
 * Apply buttons overwrote a user's .tex on a SINGLE click — same risk class,
 * no ceremony. This renders a one-shot confirm strip right after *anchorEl*:
 * the destructive action only fires from the strip's explicit confirm button,
 * and initial focus lands on CANCEL (never the destructive default). Pure
 * createElement/textContent — messages may embed file paths.
 *
 * Returns the strip element (tests use it); re-invoking while a strip is open
 * replaces it (no stacking). The strip removes itself on either choice.
 */
export function confirmInline(anchorEl, { message, confirmLabel, onConfirm, cancelLabel = 'Cancel', tone = 'danger' }) {
    if (!anchorEl || typeof onConfirm !== 'function') return null;
    const prev = anchorEl.parentElement
        ? anchorEl.parentElement.querySelector(':scope > .inline-confirm-strip')
        : null;
    if (prev) prev.remove();

    const strip = document.createElement('div');
    // tone (Card Catalog, 2026-07-20): 'danger' (default — destructive or
    // hard-to-reverse) keeps the red strip + red confirm button; 'confirm'
    // (an ordinary gated write, e.g. the tagging deck's Accept) renders an
    // accent-toned strip + primary button, so red stays reserved for actions
    // that are actually destructive instead of meaning merely "gated".
    strip.className = tone === 'confirm'
        ? 'inline-confirm-strip tone-confirm'
        : 'inline-confirm-strip';
    strip.setAttribute('role', 'group');
    strip.setAttribute('aria-label', 'Confirm action');

    const msg = document.createElement('div');
    msg.className = 'inline-confirm-msg';
    msg.textContent = message || 'Are you sure?';
    strip.appendChild(msg);

    const row = document.createElement('div');
    row.className = 'inline-confirm-actions';
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'btn btn-outline btn-sm';
    cancel.textContent = cancelLabel;
    cancel.addEventListener('click', () => { strip.remove(); anchorEl.focus(); });
    const yes = document.createElement('button');
    yes.type = 'button';
    yes.className = tone === 'confirm' ? 'btn btn-primary btn-sm' : 'btn btn-danger btn-sm';
    yes.textContent = confirmLabel || 'Confirm';
    yes.addEventListener('click', () => { strip.remove(); onConfirm(); });
    // Cancel first in DOM and focus order — the safe default gets the
    // keyboard; the destructive choice is a deliberate extra Tab away.
    row.appendChild(cancel);
    row.appendChild(yes);
    strip.appendChild(row);

    // The strip owns its keys. Consumers (the tagging deck, notably) bind
    // container-level verb handlers that preventDefault Enter/Space — which
    // cancelled the focused button's native activation and made the strip a
    // keyboard trap (mouse-only confirm on a keyboard-first surface). Stopping
    // propagation here lets the browser's own button activation run while the
    // ancestor verbs never see the key; Escape is the standard dismiss.
    strip.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            e.preventDefault();
            e.stopPropagation();
            strip.remove();
            anchorEl.focus();
            return;
        }
        if (e.key === 'Enter' || e.key === ' ') e.stopPropagation();
    });

    anchorEl.insertAdjacentElement('afterend', strip);
    cancel.focus();
    return strip;
}

// Render a recoverable error into a `.task-error-boundary` container.
// `actions` is an array of {label, onClick, primary?} rendered as buttons.
// Built with createElement/textContent — messages may contain arbitrary
// (sanitised but still untrusted) error text, so never interpolate to HTML.
export function showTaskError(el, message, actions = []) {
    if (!el) return;
    el.innerHTML = '';
    const title = document.createElement('div');
    title.className = 'task-error-title';
    title.textContent = 'Something went wrong';
    const msg = document.createElement('div');
    msg.className = 'task-error-msg';
    msg.textContent = message || 'An unexpected error occurred.';
    el.append(title, msg);
    if (actions.length) {
        const row = document.createElement('div');
        row.className = 'task-error-actions';
        for (const a of actions) {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'btn btn-sm ' + (a.primary ? 'btn-primary' : 'btn-outline');
            b.textContent = a.label;
            // The button is handed to the handler (additive — existing
            // callers take no argument) so an action can anchor a
            // confirmInline strip on itself: the vault error boundary's
            // "Rebuild from scratch" needs the destructive-action ceremony,
            // and a confirm strip must attach to the button the user pressed.
            b.addEventListener('click', () => { if (a.onClick) a.onClick(b); });
            row.appendChild(b);
        }
        el.appendChild(row);
    }
    el.classList.add('visible');
    announceError(message);
}

export function clearTaskError(el) {
    if (!el) return;
    el.classList.remove('visible');
    el.innerHTML = '';
}

function _topModalId() {
    let last = null;
    for (const id of _modalState.keys()) last = id;
    return last;
}

export function openModal(modalId) {
    const overlay = document.getElementById(modalId);
    if (!overlay) return;

    // Re-opening an already-open modal: re-wire listeners but KEEP the
    // original trigger (focus must restore to where the user started, not to
    // something inside the modal).
    const prev = _modalState.get(modalId);
    if (prev) {
        overlay.removeEventListener('mousedown', prev.overlayClick);
        document.removeEventListener('keydown', prev.keyHandler);
        _modalState.delete(modalId);
    }
    const state = { trigger: prev ? prev.trigger : document.activeElement };

    overlay.classList.add('open');
    // Move focus into the dialog BEFORE inerting the background, so focus is
    // never momentarily trapped inside an aria-hidden/inert subtree.
    // Visible elements only (F7): focusing a display:none control is a no-op,
    // which left focus in the inerted background.
    const initialFocusable = _visibleFocusable(overlay);
    if (initialFocusable.length > 0) initialFocusable[0].focus();
    _setBackgroundInert(true);

    // Click on the scrim (the overlay itself, not its .modal child) closes it —
    // the conventional dismissal gesture, alongside Esc.
    state.overlayClick = (e) => { if (e.target === overlay) closeModal(modalId); };
    overlay.addEventListener('mousedown', state.overlayClick);

    // Esc closes the modal; Tab/Shift+Tab cycles inside it so keyboard focus
    // cannot escape into the underlying page while the dialog is open. Both
    // act only while THIS modal is the topmost — with stacked modals, Esc
    // must peel one layer, not all of them.
    state.keyHandler = (e) => {
        if (_topModalId() !== modalId) return;
        if (e.key === 'Escape') {
            e.preventDefault();
            closeModal(modalId);
            return;
        }
        if (e.key === 'Tab') {
            const focusable = _visibleFocusable(overlay);
            if (focusable.length === 0) return;
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            const active = document.activeElement;
            if (e.shiftKey && active === first) {
                e.preventDefault();
                last.focus();
            } else if (!e.shiftKey && active === last) {
                e.preventDefault();
                first.focus();
            }
        }
    };
    document.addEventListener('keydown', state.keyHandler);
    _modalState.set(modalId, state);
}

export function closeModal(modalId) {
    // Item 3.7: strictly a no-op unless THIS modal is registered as open —
    // a delayed closeModal timer (refactor.js closes its status modals after
    // ~1.1 s) must never strip a DIFFERENT live modal's handlers or yank its
    // focus, which is exactly what the old single-global-slot version did.
    const state = _modalState.get(modalId);
    if (!state) return;
    const wasTop = _topModalId() === modalId;
    _modalState.delete(modalId);

    const overlay = document.getElementById(modalId);
    if (overlay) {
        overlay.classList.remove('open');
        overlay.removeEventListener('mousedown', state.overlayClick);
    }
    document.removeEventListener('keydown', state.keyHandler);
    // The background stays inert while ANY modal remains open.
    if (_modalState.size === 0) _setBackgroundInert(false);
    // Restore focus only when the closed modal was on top — closing a lower
    // modal must not steal focus from the one the user is interacting with.
    if (wasTop && state.trigger && typeof state.trigger.focus === 'function') {
        state.trigger.focus();
    }
}

// Scroll memory per tab (Card Catalog, 2026-07-20): tab panels are hidden
// with display:none, which resets every descendant's scrollTop in WebKit —
// so returning to a tab used to land back at the top of every panel. Before
// hiding, remember each scrolled element; after showing, restore. Stale refs
// from re-rendered DOM (tagging/refactor innerHTML swaps) are skipped via
// isConnected.
const _tabScrollMemo = new Map();

export function showTab(tabId) {
    const prev = document.querySelector('.content-area.active');
    if (prev && prev.id !== `${tabId}-tab`) {
        const saved = [];
        prev.querySelectorAll('*').forEach((el) => {
            if (el.scrollTop > 0 || el.scrollLeft > 0) {
                saved.push([el, el.scrollTop, el.scrollLeft]);
            }
        });
        _tabScrollMemo.set(prev.id, saved);
    }

    document.querySelectorAll('.tab').forEach(t => {
        t.classList.remove('active');
        t.setAttribute('aria-selected', 'false');
        t.setAttribute('tabindex', '-1');
    });
    document.querySelectorAll('.content-area').forEach(c => c.classList.remove('active'));

    const activeTab = document.getElementById(`tab-${tabId}`);
    if (activeTab) {
        activeTab.classList.add('active');
        activeTab.setAttribute('aria-selected', 'true');
        activeTab.setAttribute('tabindex', '0');
    }
    const contentArea = document.getElementById(`${tabId}-tab`);
    if (contentArea) {
        contentArea.classList.add('active');
        const saved = _tabScrollMemo.get(contentArea.id);
        if (saved) {
            // rAF: scroll positions only stick once the panel has layout again.
            window.requestAnimationFrame(() => {
                saved.forEach(([el, top, left]) => {
                    if (el.isConnected) { el.scrollTop = top; el.scrollLeft = left; }
                });
            });
        }
    }
}

// Roving-tabindex arrow-key navigation for an ARIA tablist (left/right/up/
// down/home/end). `activate` is invoked with the focused tab element so the
// caller can run its own selection logic (automatic-activation pattern).
export function wireTablistKeys(tablistEl, activate) {
    if (!tablistEl) return;
    tablistEl.addEventListener('keydown', (e) => {
        const tabs = Array.from(tablistEl.querySelectorAll('[role="tab"]'));
        if (!tabs.length) return;
        const current = tabs.indexOf(document.activeElement);
        let next = -1;
        if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = (current + 1) % tabs.length;
        else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = (current - 1 + tabs.length) % tabs.length;
        else if (e.key === 'Home') next = 0;
        else if (e.key === 'End') next = tabs.length - 1;
        else return;
        e.preventDefault();
        const target = tabs[next];
        target.focus();
        if (typeof activate === 'function') activate(target);
    });
}

const _PROVIDER_LABELS = {
    ollama: 'Ollama',
    lm_studio: 'LM Studio',
    openai: 'OpenAI',
    anthropic: 'Anthropic',
    google: 'Google Gemini',
    // C4 (2026-07-13): the OpenAI-compat online providers were missing here and
    // from isOnlineProvider below — a missed registration point of the
    // 2026-07-12 provider axis. Their absence made config.js treat them as LOCAL,
    // disabling the embedding selector while they were active.
    deepseek: 'DeepSeek',
    zai: 'Z.AI',
    openrouter: 'OpenRouter',
};

/**
 * Recolour + relabel the header provider badge for the active provider. Lives in
 * this leaf module so both config.js and app.js can import it without a cycle.
 * An unknown provider falls back to the Ollama label/colour.
 *
 * Item 4.9: CSS variables are used dynamically instead of hardcoded JS hex codes.
 * Safe: Modifies only the styling assignment. Bypasses no functionality.
 * Invariant: Badge colors match --provider-<name> CSS variable resolved by theme.
 */
export function updateProviderBadge(provider) {
    const label = _PROVIDER_LABELS[provider] || provider || 'Ollama';
    const cleanProvider = (provider || 'ollama').replace('_', '-');
    const colorVar = `var(--provider-${cleanProvider}, var(--provider-ollama))`;

    const badge = document.getElementById('provider-badge');
    if (badge) {
        badge.style.color = colorVar;
        badge.style.borderColor = colorVar;
    }
    const dot = document.getElementById('provider-badge-dot');
    if (dot) dot.style.background = colorVar;
    const lbl = document.getElementById('provider-badge-label');
    if (lbl) lbl.textContent = label;
}

// The online (chat-only) providers — must stay in sync with core.config's
// ONLINE_PROVIDER_NAMES / api/routes/config.py _PROVIDER_NAMES. deepseek/zai are
// the OpenAI-compat providers (C4, 2026-07-13).
const _ONLINE_PROVIDERS = new Set(['openai', 'anthropic', 'google', 'deepseek', 'zai', 'openrouter']);

export function isOnlineProvider(provider) {
    return _ONLINE_PROVIDERS.has(provider);
}

// ── Input-draft persistence ─────────────────────────────────────────────────
// Remember the LAST value a user typed into key input/textarea fields so it
// survives a full app quit (red-cross or ⌘Q) — the "keep my last prompt"
// request. Purely client-side localStorage, per field, no server round-trip
// and no config key (mirrors the theme-preference pattern above). Restoring a
// value only fills an EMPTY field, so a value that arrived from config/another
// source is never clobbered.
const _DRAFT_PREFIX = 'chatekld-draft:';

/**
 * Wire a set of ``{id, key}`` fields for last-value persistence.
 * On call: restores each field from localStorage if the field is empty.
 * Then: saves on every ``input`` event (debounced) so the latest typed value
 * is always the one remembered. Missing elements are skipped silently.
 */
export function installDraftPersistence(specs) {
    const timers = new Map();
    specs.forEach(({ id, key }) => {
        const el = document.getElementById(id);
        if (!el) return;
        const storeKey = _DRAFT_PREFIX + (key || id);
        try {
            const saved = localStorage.getItem(storeKey);
            if (saved != null && !el.value) el.value = saved;
        } catch (_) { /* private mode / disabled storage */ }
        el.addEventListener('input', () => {
            clearTimeout(timers.get(id));
            timers.set(id, setTimeout(() => {
                try { localStorage.setItem(storeKey, el.value); } catch (_) { /* ignore */ }
            }, 250));
        });
    });
}

// ── Conversation export ─────────────────────────────────────────────────────
/**
 * Build a downloadable transcript string from a ``[{role, content}]`` history
 * (axis iv). Pure — the caller POSTs the result to /api/export-summary. Returns
 * ``{format, content}`` where ``format`` is normalised to ``md`` or ``txt``.
 */
// One provenance line for an assistant turn (export stamping, 2026-07-22).
// `meta` is the server's {"meta"} frame ({provider, model, …}), `usage` the
// {"usage"} frame ({input_tokens, output_tokens, cost_usd, iteration_count?}).
// Returns '' when the turn carries neither.
function _turnProvenanceLine(turn) {
    const bits = [];
    const meta = turn.meta || null;
    if (meta && (meta.provider || meta.model)) {
        bits.push(`Model: ${[meta.provider, meta.model].filter(Boolean).join(' / ')}`);
        if (meta.agent_enabled) bits.push('agent mode');
    }
    const u = turn.usage || null;
    if (u && (u.input_tokens || u.output_tokens)) {
        bits.push(`${u.input_tokens || 0} in / ${u.output_tokens || 0} out tokens`);
        if (u.cost_usd > 0) bits.push(`$${Number(u.cost_usd).toFixed(4)}`);
        if (u.iteration_count) bits.push(`${u.iteration_count} agent iteration${u.iteration_count !== 1 ? 's' : ''}`);
    }
    return bits.join(' · ');
}

/**
 * Build a downloadable transcript string from a ``[{role, content}]`` history
 * (axis iv). Pure — the caller POSTs the result to /api/export-summary. Returns
 * ``{format, content}`` where ``format`` is normalised to ``md`` or ``txt``.
 *
 * Provenance (2026-07-22): ``opts.header`` is an ordered ``{label: value}``
 * object rendered as an italic header block under the title (export date,
 * provider/model, retrieval knobs). Per-turn ``turn.meta`` / ``turn.usage``
 * (captured from the SSE frames) render as a "Model: …" line; ``turn.sources``
 * accepts plain names (legacy) or ``{source, score}`` objects; ``turn.trace``
 * (agent tool calls, ``[{name, arguments}]``) renders as a compact list.
 */
export function buildConversationExport(history, format, title, opts) {
    const fmt = format === 'txt' ? 'txt' : 'md';
    const lines = [];
    if (fmt === 'md') {
        lines.push(`# ${title}`, '');
    } else {
        lines.push(title, '='.repeat(title.length), '');
    }
    const header = (opts && opts.header) || null;
    if (header) {
        Object.entries(header).forEach(([label, value]) => {
            if (value === undefined || value === null || value === '') return;
            const line = `${label}: ${value}`;
            lines.push(fmt === 'md' ? `*${line}*` : line);
        });
        lines.push('');
    }
    (history || []).forEach((turn) => {
        const who = turn.role === 'user' ? 'You' : 'Assistant';
        if (fmt === 'md') {
            lines.push(`## ${who}`, '', turn.content, '');
        } else {
            lines.push(`### ${who}`, turn.content, '');
        }
        const prov = _turnProvenanceLine(turn);
        if (prov) {
            lines.push(fmt === 'md' ? `*${prov}*` : prov, '');
        }
        // Agent tool trace (2026-07-22): names + argument summaries only —
        // the tool OUTPUTS are untrusted vault/web content and often huge, so
        // the export records what was done, not what came back.
        if (Array.isArray(turn.trace) && turn.trace.length) {
            lines.push(fmt === 'md' ? '*Tool calls:*' : 'Tool calls:');
            turn.trace.forEach((call) => {
                let args = '';
                try { args = JSON.stringify(call.arguments || {}); } catch (_) { args = ''; }
                if (args.length > 200) args = args.slice(0, 200) + '…';
                lines.push(`- ${call.name}(${args})${call.is_error ? ' — error' : ''}`);
            });
            lines.push('');
        }
        // Vault-chat turns carry their retrieval provenance (lot 4.3) — often
        // the most valuable part of the transcript. Plain-chat turns have none.
        if (Array.isArray(turn.sources) && turn.sources.length) {
            const names = turn.sources.map((s) => {
                if (s && typeof s === 'object') {
                    const score = (typeof s.score === 'number') ? ` (${s.score.toFixed(3)})` : '';
                    return `${s.source}${score}`;
                }
                return String(s);
            });
            const src = `Sources: ${names.join(', ')}`;
            lines.push(fmt === 'md' ? `*${src}*` : src, '');
        }
    });
    return { format: fmt, content: lines.join('\n') };
}

// ── Theming ─────────────────────────────────────────────────────────────────
// System-aware 3-way theme (System / Light / Dark). The *preference* is one of
// those three strings, persisted in localStorage; the *resolved* theme is always
// concrete (light|dark) and lives on <html data-theme>. The <head> bootstrap in
// index.html applies the resolved theme pre-paint (no flash); these helpers keep
// it in sync after load and reflect state onto the sidebar segmented control.
const _THEME_KEY = 'chatekld-theme';

export function getThemePreference() {
    try { return localStorage.getItem(_THEME_KEY) || 'system'; } catch (_) { return 'system'; }
}

function _resolveTheme(pref) {
    if (pref === 'light' || pref === 'dark') return pref;
    const mql = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)');
    return (mql && mql.matches) ? 'light' : 'dark';
}

/** Apply `pref` to <html> and reflect it on the segmented control (no persist). */
export function applyTheme(pref) {
    document.documentElement.setAttribute('data-theme', _resolveTheme(pref));
    document.querySelectorAll('.theme-option').forEach((b) => {
        const on = b.dataset.themeOption === pref;
        b.classList.toggle('active', on);
        b.setAttribute('aria-checked', on ? 'true' : 'false');
        b.tabIndex = on ? 0 : -1;
    });
}

/** Persist + apply a theme preference (the window.setTheme entry point). */
export function setTheme(pref) {
    try { localStorage.setItem(_THEME_KEY, pref); } catch (_) { /* private mode */ }
    applyTheme(pref);
}

/**
 * Wire the live theme machinery: track OS appearance changes (only while the
 * preference is "system"), add radiogroup arrow-key navigation, and apply the
 * persisted preference so the control matches the pre-painted theme.
 */
export function initTheme() {
    if (window.matchMedia) {
        const mql = window.matchMedia('(prefers-color-scheme: light)');
        const onChange = () => { if (getThemePreference() === 'system') applyTheme('system'); };
        if (mql.addEventListener) mql.addEventListener('change', onChange);
        else if (mql.addListener) mql.addListener(onChange);  // older WebKit
    }
    const group = document.querySelector('.theme-switch');
    if (group) {
        group.addEventListener('keydown', (e) => {
            const opts = Array.from(group.querySelectorAll('.theme-option'));
            const i = opts.indexOf(document.activeElement);
            let n = -1;
            if (e.key === 'ArrowRight' || e.key === 'ArrowDown') n = (i + 1) % opts.length;
            else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') n = (i - 1 + opts.length) % opts.length;
            else return;
            e.preventDefault();
            opts[n].focus();
            setTheme(opts[n].dataset.themeOption);
        });
    }
    applyTheme(getThemePreference());
}

/**
 * Latest-wins gate for overlapping async UI flows (improvement plan
 * 2026-07-04, item 3.3). Four independent races shared one root cause: an
 * interval/re-entrant async function with no staleness discipline, so a SLOW
 * older response could write the DOM after a newer one (health dots, audit
 * status banner, audit report switcher) or an old run's `finally` could
 * clobber the new run's state (summarizer abort controller).
 *
 * Usage: `const isCurrent = gate.enter();` at the top of each run, then
 * `if (!isCurrent()) return;` after every await before touching the DOM.
 * Entering invalidates every earlier run — last caller wins, unconditionally.
 * Pure closure state, no timers, nothing to dispose.
 */
export function makeLatestGate() {
    let gen = 0;
    return {
        enter() {
            const mine = ++gen;
            return () => mine === gen;
        },
    };
}

export function copyToClipboard(text, btn) {
    navigator.clipboard.writeText(text).then(() => {
        const orig = btn.textContent;
        btn.textContent = '\u2713 Copied';
        announceStatus('Copied to clipboard');
        setTimeout(() => { btn.textContent = orig; }, 2000);
    }).catch(() => {
        const orig = btn.textContent;
        btn.textContent = '\u2717 Copy failed';
        announceStatus('Copy failed');
        setTimeout(() => { btn.textContent = orig; }, 2500);
    });
}

/**
 * Inject a per-block "Copy" button into every `<pre>` of rendered markdown so
 * users can grab a single code block without selecting the whole answer. Call
 * AFTER streaming completes \u2014 the per-token innerHTML re-render would otherwise
 * discard the buttons. Idempotent per <pre>. Copies the block's textContent.
 */
export function enhanceCodeBlocks(containerEl) {
    if (!containerEl) return;
    containerEl.querySelectorAll('pre').forEach((pre) => {
        if (pre.querySelector(':scope > .code-copy-btn')) return;
        const code = pre.querySelector('code');
        if (!code) return;
        pre.classList.add('has-code-copy');
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'code-copy-btn';
        btn.textContent = 'Copy';
        btn.setAttribute('aria-label', 'Copy code block');
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            copyToClipboard(code.textContent || '', btn);
        });
        pre.appendChild(btn);
    });
}

// URI schemes that can execute script or smuggle active content when they land
// in an href/src. Model/vault-controlled markdown reaches the sanitiser, so a
// `[x](javascript:…)` / `vbscript:` link or a `data:text/html` document URI must
// be neutralised. `data:image/...` is intentionally NOT blocked (inline images
// are inert and occasionally legitimate); only the executable `data:text/html`
// form is. Matched after trimming + lowercasing the attribute value.
const _UNSAFE_URI_RE = /^(?:javascript:|vbscript:|data:text\/html)/i;

/**
 * True when a `style` attribute value references an external resource — a
 * render-time beacon vector. De-escapes CSS backslash escapes first so
 * `u\72 l(...)` (CSS hex escape for 'r') and `\75 rl(` cannot smuggle a `url(`
 * past a naive substring test, and matches `image-set(...)` and any embedded
 * `http(s)://` literal (the string-form beacon). Pure; only ever used to DROP
 * an attribute, so a false positive costs a lost inline style, never safety.
 */
export function _styleHasExternalRef(value) {
    const deEscaped = String(value)
        // CSS hex escape: `\XXXXXX` optionally followed by one whitespace.
        .replace(/\\([0-9a-fA-F]{1,6})\s?/g, (m, h) => {
            const cp = parseInt(h, 16);
            return (cp >= 0 && cp <= 0x10FFFF) ? String.fromCodePoint(cp) : m;
        })
        // CSS identifier escape of a non-hex char: `\c` → `c`.
        .replace(/\\([^0-9a-fA-F])/g, '$1');
    return /url\s*\(|image-set|https?:\/\//i.test(deEscaped);
}

/**
 * Best-effort HTML sanitiser for rendered-markdown output. Parses the string in
 * an inert document, strips active/embedding elements (script/style/iframe/
 * object/embed/form), every event-handler (on*) attribute, and any href/src/
 * xlink:href whose value uses a script-bearing URI scheme (javascript:,
 * vbscript:, data:text/html). Returns the cleaned innerHTML. Applied to the
 * output of `marked.parse` before it is assigned anywhere (see vault.js /
 * plainchat.js / refactor.js).
 */
export function sanitiseHtml(html) {
    const doc = new DOMParser().parseFromString(html || '', 'text/html');
    // 2026-07-20 audit A1: `link`, `meta`, `base` were missing — a body-legal
    // `<link rel=stylesheet href=http://attacker/x.css>` loads a stylesheet in
    // the app origin (CSS attribute-selector exfiltration, no click needed), an
    // in-body `<meta http-equiv=refresh>` navigates the whole PyWebView window
    // (the exact "dead page"/exfil class the link interceptor exists to stop —
    // and it never fires because there is no click), and `<base>` rewrites every
    // relative URL. All three carry plain http: values, so the scheme gate below
    // never touched them; they must be removed as elements.
    doc.querySelectorAll(
        'script, style, iframe, object, embed, form, link, meta, base'
    ).forEach(el => el.remove());
    doc.querySelectorAll('*').forEach(el => {
        Array.from(el.attributes).forEach(attr => {
            // Drop any `style` carrying an external reference — a render-time
            // network beacon (background:url(http://attacker/…), or the
            // image-set() form) leaks the viewer's presence with no click and no
            // scheme the URI gate would catch. The check de-escapes CSS
            // backslash escapes first (E5, 2026-07-20 audit): `u\72 l(` and
            // `image-set("http://…")` both evaded the bare `url(` test.
            if (attr.name === 'style' && _styleHasExternalRef(attr.value)) {
                el.removeAttribute(attr.name);
                return;
            }
            // XSS bypass fix (improvement plan 2026-07-04, item 1.2). The HTML
            // URL parser strips ASCII tab/LF/CR ANYWHERE in a URL — including
            // inside the scheme — and trims leading/trailing C0 controls and
            // space, all AFTER entity decoding. So `<a href="jav&#9;ascript:…">`
            // in a vault note or model answer decoded to a literal tab inside
            // the scheme, sailed past the old trimStart()-only normalisation,
            // and executed in the app origin on click (every local API is one
            // fetch away; the write confirms are client-side modals). Safe:
            // the transform only ever REMOVES characters from the TESTED copy
            // before the scheme test — anything the old code judged unsafe is
            // still unsafe (strictly more removals, pure function, no state) —
            // and the surviving attribute value itself is left byte-identical.
            // Invariant (pinned by tests/js/sanitiseHtml.test.js): the string
            // tested against _UNSAFE_URI_RE is the post-preprocessing URL the
            // browser's parser would actually resolve, so no control-character
            // placement can smuggle a blocked scheme past this gate.
            const val = attr.value
                .replace(/^[\u0000-\u0020]+|[\u0000-\u0020]+$/g, '')
                .replace(/[\t\n\r]/g, '')
                .toLowerCase();
            // Strip every on* handler, and any URI-bearing attribute (href/src/
            // xlink:href/etc.) whose scheme can execute script. Checking the
            // value for the scheme (rather than only specific attr names) also
            // covers SVG `xlink:href` and any future URI attribute.
            if (attr.name.startsWith('on') || _UNSAFE_URI_RE.test(val)) {
                el.removeAttribute(attr.name);
            }
        });
    });
    return doc.body.innerHTML;
}

/**
 * Render a row of click-to-insert example-prompt chips for a text field.
 *
 * Lowest-friction "starter prompts" affordance: clicking a chip inserts the
 * example text into the bound field — it fills an EMPTY field, but APPENDS (after
 * a blank line) when the field already holds a draft, so a chip click never
 * silently destroys text the user already typed. It then fires an `input` event
 * (so debounced auto-save / `oninput` listeners react exactly as if the user
 * typed) and focuses the field so it can be edited. Pure DOM — chip labels are
 * set via `textContent`, never innerHTML, so example content can never inject markup.
 *
 * Idempotent: a field is only ever decorated once (guarded by a data-attr), so
 * calling this again on a re-rendered field is a no-op rather than a duplicate
 * row.
 *
 * @param {HTMLElement} targetEl  the <input>/<textarea> the chips fill
 * @param {Array<{label:string,text:string}>} examples
 * @param {{mountEl?:HTMLElement, title?:string}} [opts]  mountEl: where to place
 *        the chip row (default: immediately after targetEl). title: an optional
 *        leading label (e.g. "Examples:").
 * @returns {HTMLElement|null} the chip row, or null if nothing was rendered.
 */
export function renderExampleChips(targetEl, examples, opts = {}) {
    if (!targetEl || !Array.isArray(examples) || examples.length === 0) return null;
    if (targetEl.dataset.exampleChips === '1') return null;  // already decorated
    targetEl.dataset.exampleChips = '1';

    const row = document.createElement('div');
    row.className = 'example-chips-row';
    if (opts.title) {
        const lbl = document.createElement('span');
        lbl.className = 'example-chips-label';
        lbl.textContent = opts.title;
        row.appendChild(lbl);
    }
    for (const ex of examples) {
        if (!ex || !ex.text) continue;
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'example-chip';
        chip.textContent = ex.label || ex.text;
        chip.title = ex.text;  // full text on hover when the label is shorter
        // Track 6c: the document is lang="en" but the example sets are French —
        // per-chip lang keeps screen-reader pronunciation correct.
        if (opts.lang) chip.lang = opts.lang;
        chip.addEventListener('click', () => {
            // Replace only when the field is empty; otherwise append after a blank
            // line so an existing draft is preserved rather than clobbered.
            const existing = (targetEl.value || '').replace(/\s+$/, '');
            targetEl.value = existing ? `${existing}\n\n${ex.text}` : ex.text;
            targetEl.dispatchEvent(new Event('input', { bubbles: true }));
            targetEl.focus();
        });
        row.appendChild(chip);
    }
    if (opts.mountEl) {
        opts.mountEl.appendChild(row);
    } else {
        targetEl.insertAdjacentElement('afterend', row);
    }
    return row;
}

/**
 * Coalesce the streaming markdown re-render (improvement plan 1.1 / 4.4).
 * Parsing + sanitising + innerHTML-ing the WHOLE accumulated answer on every
 * token is O(n²) over the stream and visibly janks long answers. Batch to at
 * most one render per _RENDER_COALESCE_MS; the caller MUST call flush() at
 * stream end (the final full parse) and cancel() on error so a dangling timer
 * never renders into a removed bubble.
 *
 * @param {HTMLElement} targetEl - the element to update
 * @returns {object} { update(text), flush(), cancel() }
 */
export function makeAnswerRenderer(targetEl) {
    const _RENDER_COALESCE_MS = 40;
    let timer = null;
    let latest = '';
    const render = () => {
        timer = null;
        if (typeof marked !== 'undefined' && typeof marked.parse === 'function') {
            targetEl.innerHTML = sanitiseHtml(marked.parse(latest));
        } else {
            targetEl.textContent = latest;
        }
    };
    return {
        update(fullAnswer) {
            latest = fullAnswer;
            if (timer === null) timer = setTimeout(render, _RENDER_COALESCE_MS);
        },
        flush() {
            if (timer !== null) { clearTimeout(timer); timer = null; }
            if (latest) render();
        },
        cancel() {
            if (timer !== null) { clearTimeout(timer); timer = null; }
        },
    };
}

/* ------------------------------------------------------------------ */
/* Link interception — no in-app navigation, ever (WP-L1/L2).          */
/* ------------------------------------------------------------------ */

const _HREF_SCHEME_RE = /^[a-zA-Z][a-zA-Z0-9+.-]*:/;

/**
 * Classify an anchor's href for the global link interceptor.
 *
 * Rendered LLM answers and previews may contain markdown links to vault
 * notes/PDFs (relative paths), external URLs, or arbitrary schemes. A raw
 * click on any of them navigates the whole PyWebView window away from the
 * app — a relative path 404s into a dead page. Kinds:
 *   - ignore : empty/in-page anchors — leave the browser default alone
 *   - url    : http/https — open in the system browser via POST /api/open
 *   - path   : relative file path — reveal in Finder via POST /api/open
 *   - blocked: every other scheme (file:, obsidian:, mailto:, protocol-
 *              relative //…) — swallow the click, navigate nowhere
 *
 * @param {string} href - the raw href attribute value
 * @returns {{kind: 'ignore'|'url'|'path'|'blocked', value: string}}
 */
export function classifyHref(href) {
    const raw = (href || '').trim();
    if (!raw || raw.startsWith('#')) return { kind: 'ignore', value: raw };
    if (raw.startsWith('//')) return { kind: 'blocked', value: raw };
    const m = _HREF_SCHEME_RE.exec(raw);
    if (m) {
        const scheme = m[0].slice(0, -1).toLowerCase();
        if (scheme === 'http' || scheme === 'https') return { kind: 'url', value: raw };
        return { kind: 'blocked', value: raw };
    }
    let path = raw;
    try {
        path = decodeURIComponent(raw);
    } catch (_e) {
        // Malformed percent-encoding — try the raw string as a literal path.
    }
    return { kind: 'path', value: path };
}

/**
 * Install the app-wide click delegate that keeps every anchor from
 * navigating the PyWebView window. Idempotent (call once from app.js init).
 *
 * @param {Document|HTMLElement} rootEl - event delegation root
 */
export function installLinkInterceptor(rootEl = document) {
    if (rootEl._linkInterceptorInstalled) return;
    rootEl._linkInterceptorInstalled = true;
    rootEl.addEventListener('click', (e) => {
        const a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
        if (!a) return;
        const cls = classifyHref(a.getAttribute('href'));
        if (cls.kind === 'ignore') return;
        e.preventDefault();
        if (cls.kind === 'blocked') return;
        const body = cls.kind === 'url' ? { url: cls.value } : { path: cls.value };
        secureFetch('/api/open', { method: 'POST', body: JSON.stringify(body) })
            .then(async (resp) => {
                if (resp && !resp.ok) {
                    let msg = 'Could not open link';
                    try {
                        const data = await resp.json();
                        if (data && data.error) msg = data.error;
                    } catch (_e) { /* non-JSON error body */ }
                    announceError(msg);
                }
            })
            .catch((err) => logError('Link open failed', err));
    });
}

/* ------------------------------------------------------------------ */
/* Contextual help — "?" badges: hover/focus tooltips + click popovers.*/
/* Axis ii (UX). Pure createElement/textContent (help text is app-owned */
/* but treated as untrusted → never innerHTML). Modelled on             */
/* confirmInline: anchor + createElement + dedup-by-remove, keyboard-   */
/* first, dismiss on Escape. A LEAF-safe addition (no new imports).     */
/* ------------------------------------------------------------------ */

let _helpSeq = 0;

// One shared floating tooltip element (role="tooltip"), positioned near the
// active badge on demand. Referenced by the badge's aria-describedby only while
// visible, so AT reads the hint on focus without a per-badge hidden node.
let _helpTipEl = null;
let _helpTipAnchor = null;

function _ensureHelpTipEl() {
    if (_helpTipEl && _helpTipEl.isConnected) return _helpTipEl;
    const el = document.createElement('div');
    el.className = 'help-tip';
    el.id = 'help-tip-shared';
    el.setAttribute('role', 'tooltip');
    el.hidden = true;
    document.body.appendChild(el);
    _helpTipEl = el;
    return el;
}

// Position a fixed-position floating element (tip/popover) centred under its
// anchor and clamped to the viewport, flipping above when it would overflow.
// A no-op in jsdom (getBoundingClientRect is zeroed) — layout only, never asserted.
function _placeHelpFloating(el, anchor, gap = 8) {
    if (typeof anchor.getBoundingClientRect !== 'function') return;
    const r = anchor.getBoundingClientRect();
    const w = el.offsetWidth || 0;
    const h = el.offsetHeight || 0;
    const vw = window.innerWidth || 0;
    const vh = window.innerHeight || 0;
    let left = r.left + r.width / 2 - w / 2;
    if (vw) left = Math.max(12, Math.min(left, vw - w - 12));
    let top = r.bottom + gap;
    if (vh && top + h > vh - 12) top = r.top - h - gap;
    el.style.left = `${Math.max(12, left)}px`;
    el.style.top = `${Math.max(12, top)}px`;
}

// Document-level Escape handler active only while a tip is visible (audit
// #19, WCAG 1.4.13): the badge's own keydown listener only fires when the
// badge is FOCUSED, so Escape while merely hovering did nothing.
let _helpTipDocKeydown = null;

function _showHelpTip(badge, text) {
    const tip = _ensureHelpTipEl();
    tip.textContent = text;
    tip.hidden = false;
    tip.classList.add('show');
    badge.setAttribute('aria-describedby', tip.id);
    _helpTipAnchor = badge;
    _placeHelpFloating(tip, badge);
    if (!_helpTipDocKeydown) {
        _helpTipDocKeydown = (e) => {
            if (e.key === 'Escape') _hideHelpTip(_helpTipAnchor);
        };
        document.addEventListener('keydown', _helpTipDocKeydown, true);
    }
}

function _hideHelpTip(badge) {
    if (_helpTipEl) { _helpTipEl.classList.remove('show'); _helpTipEl.hidden = true; }
    if (badge) badge.removeAttribute('aria-describedby');
    _helpTipAnchor = null;
    if (_helpTipDocKeydown) {
        document.removeEventListener('keydown', _helpTipDocKeydown, true);
        _helpTipDocKeydown = null;
    }
}

/**
 * Attach a "?" help badge to *anchorEl* (usually a control's <label>) that
 * reveals a short, non-interactive tooltip on hover AND keyboard focus. The
 * tooltip is dismissed on blur/mouse-leave and on Escape. Idempotent per anchor.
 *
 * @param {HTMLElement} anchorEl - element to append the badge after
 * @param {{text:string, labelText?:string}} opts - tooltip text (plain), and an
 *        optional name for the badge's aria-label ("Help: <labelText>").
 * @returns {HTMLElement|null} the badge button (tests assert on it).
 */
export function helpTip(anchorEl, { text, labelText } = {}) {
    if (!anchorEl || !text) return null;
    if (anchorEl.dataset.helpBadge === '1') return null;   // already decorated
    anchorEl.dataset.helpBadge = '1';

    const badge = document.createElement('button');
    badge.type = 'button';
    badge.className = 'help-badge';
    badge.textContent = '?';
    badge.setAttribute('aria-label', labelText ? `Help: ${labelText}` : 'Help');

    badge.addEventListener('mouseenter', () => _showHelpTip(badge, text));
    badge.addEventListener('mouseleave', () => _hideHelpTip(badge));
    badge.addEventListener('focus', () => _showHelpTip(badge, text));
    badge.addEventListener('blur', () => _hideHelpTip(badge));
    badge.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') { _hideHelpTip(badge); badge.blur(); }
    });

    anchorEl.insertAdjacentElement('afterend', badge);
    return badge;
}

/**
 * Wire every `.control-hint` to the control it describes via aria-describedby.
 *
 * DEFECT (2026-07-26 UX audit): the template carries 71 `.control-hint`
 * elements and exactly 2 aria-describedby attributes, so a screen-reader user
 * heard "Reranker model (HuggingFace id), edit text" and nothing else — not the
 * restart requirement, not the download size, not a single one of the 71 hints
 * that carry the actual operative instruction. Fixed CENTRALLY (one pass over
 * the DOM) rather than by 71 hand-edits, so a hint added later is covered for
 * free.
 *
 * Association rule: the nearest PRECEDING control in the hint's own
 * `.control-group`, found by walking previous siblings and taking the last
 * control inside the first sibling that contains one. This matters — several
 * groups hold MORE than one control (the agent group is checkbox + slider +
 * web-search checkbox + hint), and describing them all with the last hint would
 * be worse than describing none. Falls back to the group's first control for
 * the hint-above-control layouts.
 *
 * Idempotent: skips a hint already wired, and appends to (never replaces) an
 * existing aria-describedby — helpTip writes that attribute on the BADGE, not
 * the control, so the two never collide, but a hand-authored one must survive.
 *
 * @param {ParentNode} [root=document] - subtree to scan.
 * @returns {number} hints newly wired (tests assert on it).
 */
export function wireControlHints(root = document) {
    let wired = 0;
    let seq = 0;
    root.querySelectorAll('.control-hint').forEach((hint) => {
        if (hint.dataset.hintWired === '1') return;
        const group = hint.closest ? hint.closest('.control-group') : null;
        if (!group) return;
        const isControl = (el) => /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName);
        let target = null;
        for (let sib = hint.previousElementSibling; sib && !target; sib = sib.previousElementSibling) {
            if (isControl(sib)) { target = sib; break; }
            const inner = sib.querySelectorAll('input, select, textarea');
            if (inner.length) target = inner[inner.length - 1];
        }
        if (!target) target = group.querySelector('input, select, textarea');
        if (!target) return;
        hint.dataset.hintWired = '1';
        if (!hint.id) {
            // Skip past any id already taken by hand-authored markup. The
            // isConnected term is the loop's termination guard: a DETACHED hint
            // can never be found by getElementById, which would otherwise spin
            // forever.
            do { hint.id = `control-hint-${++seq}`; }
            while (hint.isConnected && document.getElementById(hint.id) !== hint);
        }
        const prev = target.getAttribute('aria-describedby');
        const ids = prev ? prev.split(/\s+/).filter(Boolean) : [];
        if (!ids.includes(hint.id)) ids.push(hint.id);
        target.setAttribute('aria-describedby', ids.join(' '));
        wired += 1;
    });
    return wired;
}

// Track the single open popover so a second open (or an outside click / Escape)
// closes the prior one — no stacking, mirroring confirmInline's dedup.
let _helpPopoverEl = null;
let _helpPopoverAnchor = null;
let _helpPopoverDismiss = null;
let _helpPopoverScroll = null;

function _closeHelpPopover(restoreFocus = true) {
    if (_helpPopoverDismiss) {
        document.removeEventListener('mousedown', _helpPopoverDismiss, true);
        document.removeEventListener('keydown', _helpPopoverDismiss, true);
        _helpPopoverDismiss = null;
    }
    // D5/#22 (2026-07-13): the popover is position:fixed and placed once, so any
    // scroll (e.g. the settings modal body) would leave it floating over the
    // wrong control — dismiss on scroll rather than trying to re-place it live.
    if (_helpPopoverScroll) {
        window.removeEventListener('scroll', _helpPopoverScroll, true);
        _helpPopoverScroll = null;
    }
    if (_helpPopoverEl) { _helpPopoverEl.remove(); _helpPopoverEl = null; }
    const anchor = _helpPopoverAnchor;
    _helpPopoverAnchor = null;
    if (anchor) {
        anchor.setAttribute('aria-expanded', 'false');
        if (restoreFocus) anchor.focus();
    }
}

/**
 * Attach a "?" help badge that opens a click-triggered popover (role="dialog")
 * describing a richer action — what it does, and (when it writes to the vault)
 * an explicit reversibility notice. Use for buttons/actions, not plain settings.
 * Only one popover is open at a time; Escape / outside-click / re-click dismiss it.
 *
 * @param {HTMLElement} anchorEl - element to append the badge after
 * @param {{title:string, body:string, writes?:boolean, writesText?:string}} opts
 * @returns {HTMLElement|null} the badge button.
 */
export function helpPopover(anchorEl, { title, body, writes = false, writesText } = {}) {
    if (!anchorEl || !title) return null;
    if (anchorEl.dataset.helpBadge === '1') return null;
    anchorEl.dataset.helpBadge = '1';

    const badge = document.createElement('button');
    badge.type = 'button';
    badge.className = 'help-badge';
    badge.textContent = '?';
    badge.setAttribute('aria-label', `Help: ${title}`);
    badge.setAttribute('aria-haspopup', 'dialog');
    badge.setAttribute('aria-expanded', 'false');

    const open = () => {
        // Toggle: a re-click on the same badge closes it.
        if (_helpPopoverAnchor === badge) { _closeHelpPopover(); return; }
        _closeHelpPopover(false);

        const pop = document.createElement('div');
        pop.className = 'help-popover';
        pop.id = `help-popover-${++_helpSeq}`;
        pop.setAttribute('role', 'dialog');
        pop.setAttribute('aria-label', title);

        const head = document.createElement('div');
        head.className = 'help-popover-head';
        const h = document.createElement('div');
        h.className = 'help-popover-title';
        h.textContent = title;
        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'help-popover-close';
        close.setAttribute('aria-label', 'Close');
        close.textContent = '×';
        close.addEventListener('click', () => _closeHelpPopover());
        head.appendChild(h);
        head.appendChild(close);
        pop.appendChild(head);

        const bodyEl = document.createElement('div');
        bodyEl.className = 'help-popover-body';
        bodyEl.textContent = body || '';
        pop.appendChild(bodyEl);

        if (writes) {
            const warn = document.createElement('div');
            warn.className = 'help-popover-writes';
            warn.textContent = writesText || 'Writes to your vault — reversible via Restore.';
            pop.appendChild(warn);
        }

        // Tab-out dismissal (audit #18): this is a NON-modal transient popover
        // — deliberately no aria-modal / focus trap — so when keyboard focus
        // leaves it (Tab past the close button), close it instead of leaving
        // an open dialog floating over whatever now has focus. relatedTarget
        // is null when focus moves to a non-focusable target (e.g. the user
        // click-selects text inside the popover body) — do NOT close then, or
        // copying from the popover would dismiss it mid-selection; the
        // outside-mousedown listener already covers true outside clicks.
        pop.addEventListener('focusout', (e) => {
            const to = e.relatedTarget;
            if (!to || pop.contains(to) || to === badge) return;
            _closeHelpPopover(false);
        });

        document.body.appendChild(pop);
        _helpPopoverEl = pop;
        _helpPopoverAnchor = badge;
        badge.setAttribute('aria-expanded', 'true');
        _placeHelpFloating(pop, badge);

        _helpPopoverDismiss = (e) => {
            if (e.type === 'keydown') {
                if (e.key === 'Escape') _closeHelpPopover();
                return;
            }
            // mousedown outside the popover and its badge closes it.
            if (pop.contains(e.target) || e.target === badge) return;
            _closeHelpPopover(false);
        };
        document.addEventListener('mousedown', _helpPopoverDismiss, true);
        document.addEventListener('keydown', _helpPopoverDismiss, true);
        // D5/#22: dismiss on any scroll (capture catches scrolls in the settings
        // modal body and other scroll containers, not just the window).
        _helpPopoverScroll = () => _closeHelpPopover(false);
        window.addEventListener('scroll', _helpPopoverScroll, true);
        close.focus();
    };

    badge.addEventListener('click', (e) => { e.stopPropagation(); open(); });
    anchorEl.insertAdjacentElement('afterend', badge);
    return badge;
}

// ── Command palette (⌘K) — Batch H, 2026-07-22 ─────────────────────────────
//
// A pure-DOM, dependency-free action palette: ⌘K / Ctrl+K opens it anywhere,
// typing filters the registered actions (accent- and case-insensitive),
// ↑/↓ + Enter (or click) runs one, Escape closes. app.js supplies the action
// registry (module hierarchy: ui.js knows no feature modules).

let _paletteEl = null;
let _paletteActions = [];
let _paletteSel = 0;
let _paletteEsc = null;       // document-level Escape handler while the palette is open
let _palettePrevFocus = null; // element focused before the palette opened

function _paletteFold(s) {
    return String(s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
}

function _paletteClose() {
    if (_paletteEsc) { document.removeEventListener('keydown', _paletteEsc, true); _paletteEsc = null; }
    if (_paletteEl) { _paletteEl.remove(); _paletteEl = null; }
    // Dialog pattern: hand focus back to wherever the user was before ⌘K.
    if (_palettePrevFocus && typeof _palettePrevFocus.focus === 'function' && _palettePrevFocus.isConnected) {
        _palettePrevFocus.focus();
    }
    _palettePrevFocus = null;
}

function _paletteRender(list, input) {
    const box = _paletteEl.querySelector('.cmdk-list');
    while (box.firstChild) box.removeChild(box.firstChild);
    if (!list.length) {
        const empty = document.createElement('div');
        empty.className = 'cmdk-empty';
        empty.textContent = 'No matching action.';
        box.appendChild(empty);
        // No options → no active descendant (a stale id would point at nothing).
        input.removeAttribute('aria-activedescendant');
        return;
    }
    list.forEach((a, i) => {
        const row = document.createElement('div');
        row.className = 'cmdk-item' + (i === _paletteSel ? ' is-selected' : '');
        // Per-row id so the input's aria-activedescendant can point at the
        // selected option (combobox pattern — focus stays on the input).
        row.id = 'cmdk-opt-' + i;
        row.setAttribute('role', 'option');
        row.setAttribute('aria-selected', i === _paletteSel ? 'true' : 'false');
        const label = document.createElement('span');
        label.textContent = a.label;
        row.appendChild(label);
        if (a.hint) {
            const hint = document.createElement('span');
            hint.className = 'cmdk-hint';
            hint.textContent = a.hint;
            row.appendChild(hint);
        }
        // mousedown (not click) so the input never blurs before the action runs.
        row.addEventListener('mousedown', (e) => {
            e.preventDefault();
            _paletteClose();
            try { a.run(); } catch (err) { logError('palette action failed: ' + String(err)); }
        });
        row.addEventListener('mousemove', () => {
            if (_paletteSel !== i) { _paletteSel = i; _paletteRender(list, input); }
        });
        box.appendChild(row);
    });
    // Track the selection for AT on every render (arrow keys / hover / filter).
    input.setAttribute('aria-activedescendant', 'cmdk-opt-' + _paletteSel);
}

function _paletteOpen() {
    if (_paletteEl) { _paletteClose(); return; }
    _palettePrevFocus = document.activeElement;
    const overlay = document.createElement('div');
    overlay.className = 'cmdk-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', 'Command palette');
    const panel = document.createElement('div');
    panel.className = 'cmdk-panel';
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'cmdk-input';
    input.placeholder = 'Type a command… (Esc to close)';
    input.setAttribute('aria-label', 'Search commands');
    // Combobox-with-listbox pattern: focus stays on the input while
    // aria-activedescendant (set per render) names the selected option.
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-expanded', 'true');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-controls', 'cmdk-listbox');
    const list = document.createElement('div');
    list.className = 'cmdk-list';
    list.id = 'cmdk-listbox';
    list.setAttribute('role', 'listbox');
    list.setAttribute('aria-label', 'Matching commands');
    panel.appendChild(input);
    panel.appendChild(list);
    overlay.appendChild(panel);
    document.body.appendChild(overlay);
    _paletteEl = overlay;
    _paletteSel = 0;

    const current = () => {
        const q = _paletteFold(input.value.trim());
        if (!q) return _paletteActions;
        return _paletteActions.filter((a) =>
            _paletteFold(a.label + ' ' + (a.hint || '')).includes(q));
    };
    _paletteRender(current(), input);

    input.addEventListener('input', () => { _paletteSel = 0; _paletteRender(current(), input); });
    input.addEventListener('keydown', (e) => {
        const list_ = current();
        // The input is the palette's only focusable element — swallow Tab so
        // focus can't leave a dialog that claims aria-modal.
        if (e.key === 'Tab') { e.preventDefault(); return; }
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            _paletteSel = Math.min(_paletteSel + 1, Math.max(0, list_.length - 1));
            _paletteRender(list_, input);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            _paletteSel = Math.max(_paletteSel - 1, 0);
            _paletteRender(list_, input);
        } else if (e.key === 'Enter') {
            e.preventDefault();
            const a = list_[_paletteSel];
            if (a) { _paletteClose(); try { a.run(); } catch (err) { logError('palette action failed: ' + String(err)); } }
        }
    });
    overlay.addEventListener('mousedown', (e) => { if (e.target === overlay) _paletteClose(); });
    // Escape is bound at DOCUMENT level while the palette is open (removed in
    // _paletteClose), not on the input — so it still closes the palette even
    // if focus has wandered outside the panel.
    _paletteEsc = (e) => {
        if (e.key === 'Escape') { e.preventDefault(); _paletteClose(); }
    };
    document.addEventListener('keydown', _paletteEsc, true);
    input.focus();
}

/**
 * Register the palette's action list and the global ⌘K / Ctrl+K binding.
 * Call once from app.js with [{label, hint?, run}] rows.
 */
export function installCommandPalette(actions) {
    _paletteActions = Array.isArray(actions) ? actions : [];
    if (document.documentElement.dataset.cmdkWired) return;
    document.documentElement.dataset.cmdkWired = '1';
    document.addEventListener('keydown', (e) => {
        if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
            e.preventDefault();
            _paletteOpen();
        }
    });
}

// ── Guided tour — Batch H, 2026-07-22 ───────────────────────────────────────
//
// A minimal, dependency-free spotlight tour (driver.js was considered; ~80
// in-house lines keep the vendor surface at zero). Steps: [{tab?, target,
// title, text, onEnter?}] — `tab` switches views first, `target` is an element
// id the step outlines, `onEnter` is an optional per-step callback (ui.js is
// feature-blind: it just calls it; help.js uses it to lazy-load the Help
// guide). Never auto-fires; help.js's startTour() launches it from the Help
// tab's "Show me around" button.

let _tourEl = null;
let _tourEsc = null;       // document-level Escape handler while the tour is open
let _tourPrevFocus = null; // element focused before the tour started

function _tourClose() {
    document.querySelectorAll('.tour-highlight').forEach((el) => el.classList.remove('tour-highlight'));
    if (_tourEsc) { document.removeEventListener('keydown', _tourEsc); _tourEsc = null; }
    if (_tourEl) { _tourEl.remove(); _tourEl = null; }
    // Dialog pattern: hand focus back to where the user was before the tour.
    if (_tourPrevFocus && typeof _tourPrevFocus.focus === 'function' && _tourPrevFocus.isConnected) {
        _tourPrevFocus.focus();
    }
    _tourPrevFocus = null;
}

export function runTour(steps) {
    if (!Array.isArray(steps) || !steps.length) return;
    _tourClose();
    _tourPrevFocus = document.activeElement;
    let idx = 0;

    const overlay = document.createElement('div');
    overlay.className = 'tour-overlay';
    // role="dialog" WITHOUT aria-modal: the overlay is pointer-events:none and
    // has no focus trap, so the rest of the page stays fully operable —
    // claiming modality would tell AT the background is inert when it isn't.
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-label', 'Guided tour');
    const card = document.createElement('div');
    card.className = 'tour-card';
    // Focus target per step: the CARD (not the Next button), labelled by the
    // step title/text so SR users hear the content, not just "Next, button".
    card.setAttribute('tabindex', '-1');
    card.setAttribute('aria-labelledby', 'tour-step-title');
    card.setAttribute('aria-describedby', 'tour-step-text');
    overlay.appendChild(card);
    document.body.appendChild(overlay);
    _tourEl = overlay;

    const render = () => {
        const step = steps[idx];
        if (step.tab) { try { showTab(step.tab); } catch (_) { /* view optional */ } }
        // Feature-blind per-step hook (e.g. lazy-load the view the step shows).
        if (typeof step.onEnter === 'function') {
            try { step.onEnter(); } catch (_) { /* a broken hook must not kill the tour */ }
        }
        document.querySelectorAll('.tour-highlight').forEach((el) => el.classList.remove('tour-highlight'));
        const target = step.target ? document.getElementById(step.target) : null;
        if (target) {
            target.classList.add('tour-highlight');
            try { target.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch (_) { /* older engines */ }
        }
        overlay.setAttribute('aria-label', `Guided tour — step ${idx + 1} of ${steps.length}`);
        while (card.firstChild) card.removeChild(card.firstChild);
        const h = document.createElement('strong');
        h.id = 'tour-step-title';
        h.textContent = `${idx + 1}/${steps.length} — ${step.title}`;
        const p = document.createElement('p');
        p.id = 'tour-step-text';
        p.textContent = step.text;
        const row = document.createElement('div');
        row.className = 'tour-actions';
        const mk = (label, fn, primary) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = primary ? 'btn btn-primary btn-sm' : 'btn btn-outline btn-sm';
            b.textContent = label;
            b.addEventListener('click', fn);
            return b;
        };
        if (idx > 0) row.appendChild(mk('Back', () => { idx -= 1; render(); }));
        row.appendChild(mk('Close', _tourClose));
        row.appendChild(mk(idx === steps.length - 1 ? 'Done' : 'Next',
            () => { if (idx === steps.length - 1) { _tourClose(); } else { idx += 1; render(); } }, true));
        card.appendChild(h);
        card.appendChild(p);
        card.appendChild(row);
        card.focus();
    };

    // Escape is bound at DOCUMENT level while the tour is open (removed in
    // _tourClose): the overlay is pointer-events:none, so focus can wander
    // anywhere on the page — an overlay-scoped listener would go deaf then.
    _tourEsc = (e) => {
        if (e.key === 'Escape') { e.preventDefault(); _tourClose(); }
    };
    document.addEventListener('keydown', _tourEsc);
    render();
}

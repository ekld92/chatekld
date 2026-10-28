/**
 * Shared UI primitives — the activity bar, accessibility announcers, recoverable
 * error boundaries, modal/tab/tablist wiring, the provider badge, clipboard, and
 * the HTML sanitiser. A LEAF module in the JS module hierarchy: it imports only
 * api.js (for logError) and nothing else from the project, so every feature
 * module can depend on it without a circular import. `updateProviderBadge` lives
 * here precisely so config.js and app.js can both import it without a cycle.
 */
import { logError, secureFetch } from './api.js';
import DOMPurify from './vendor/dompurify.es.mjs';

// DOMPurify 3.4.14 is vendored locally (no CDN/runtime npm dependency). One
// instance is shared because hook/config state is immutable in this module.
const _domPurify = typeof DOMPurify.sanitize === 'function'
    ? DOMPurify
    : DOMPurify(window);

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
const _MODAL_BACKDROP_IDS = ['main-content', 'app-sidebar', 'activity-bar'];

function _setBackgroundInert(on) {
    _MODAL_BACKDROP_IDS.forEach((id) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.inert = on;
        if (on) el.setAttribute('aria-hidden', 'true');
        else el.removeAttribute('aria-hidden');
    });
}
const _MODAL_FOCUSABLE_SELECTOR = 'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], summary, [tabindex]';

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
        if (el.tabIndex < 0 || el.disabled || el.closest('[inert]')) return false;
        if (typeof el.checkVisibility === 'function') return el.checkVisibility();
        return el.offsetParent !== null;
    });
}

function _focusModal(overlay) {
    const first = _visibleFocusable(overlay)[0];
    if (first) first.focus();
    else {
        // Empty/loading dialogs must still own keyboard focus.
        if (!overlay.hasAttribute('tabindex')) overlay.setAttribute('tabindex', '-1');
        overlay.focus();
    }
}

/**
 * Register a long-running task under `id` with a user-visible `label`. Tasks are
 * keyed by id so concurrent operations each show in the activity bar; call
 * taskEnd(id) to clear one. Re-registering the same id replaces its label.
 */
const _taskRecords = new Map();
const _taskEvents = [];
let _completedTasks = 0;

export function taskBegin(id, label) {
    const previous = _taskRecords.get(id);
    const continuing = _activeTasks.has(id);
    _activeTasks.set(id, label);
    _taskRecords.set(id, {...(continuing ? previous : {}), id, label, state: 'running',
        startedAt: continuing && previous?.state === 'running' ? previous.startedAt : Date.now(),
        updatedAt: Date.now()});
    if (previous?.label !== label || previous?.state !== 'running') activityEvent(id, label);
    renderActivityBar();
}

/** Add structured progress without inferring success from a closed stream. */
export function taskUpdate(id, update = {}) {
    const task = _taskRecords.get(id);
    if (!task) return;
    Object.assign(task, update, {updatedAt: Date.now()});
    if (update.message) activityEvent(id, update.message, update.state || 'info');
    renderActivityBar();
}

export function taskNote(prefix, message, state = null) {
    for (const id of _activeTasks.keys()) if (id.startsWith(prefix)) {
        taskUpdate(id, {message, ...(state ? {state} : {})});
    }
}

export function taskEnd(id, state = null) {
    const wasActive = _activeTasks.has(id);
    _activeTasks.delete(id);
    const task = _taskRecords.get(id);
    if (task) {
        task.state = state || (['completed', 'failed', 'partial', 'unknown', 'cancelled'].includes(task.state) ? task.state : 'ended');
        if (!task.message) task.message = task.state === 'completed' ? 'Completed.' : 'Operation ended — inspect its result.';
        task.finishedAt = Date.now();
        if (wasActive && task.state === 'completed') _completedTasks += 1;
    }
    // Active jobs are never evicted. Recent outcomes are session-only evidence.
    const ended = [..._taskRecords.values()].filter(t => !_activeTasks.has(t.id));
    for (const old of ended.slice(0, Math.max(0, ended.length - 20))) _taskRecords.delete(old.id);
    renderActivityBar();
}

function activityEvent(id, message, state = 'info') {
    _taskEvents.push({id, message: String(message), state, at: Date.now()});
    if (_taskEvents.length > 150) _taskEvents.shift();
}

function renderActivityBar() {
    const bar = document.getElementById('activity-bar');
    const lbl = document.getElementById('activity-label-text');
    if (!bar || !lbl) return;
    const consoleMode = !!document.getElementById('activity-tasks');
    bar.style.display = consoleMode || _activeTasks.size ? 'block' : 'none';
    const needsAttention = [..._taskRecords.values()].filter(t => ['failed', 'partial', 'unknown'].includes(t.state)).length;
    lbl.textContent = [..._activeTasks.values()].join(' · ') || (consoleMode ? 'No active operations' : '');
    if (consoleMode && _completedTasks) lbl.textContent += ` · ✓ ${_completedTasks} completed this session`;
    if (consoleMode && needsAttention) lbl.textContent += ` · ${needsAttention} need attention`;
    const host = document.getElementById('activity-tasks');
    if (!host) return;
    host.replaceChildren();
    const labels = {running: 'Running', cancelling: 'Stopping', completed: 'Completed', ended: 'Ended — see result', failed: 'Needs attention', partial: 'Partly complete', unknown: 'Status unknown', cancelled: 'Cancelled'};
    for (const task of [..._taskRecords.values()].reverse()) {
        const row = document.createElement('div'); row.className = 'activity-task';
        const title = document.createElement('strong'); title.textContent = task.label;
        const state = document.createElement('span'); state.className = 'operation-state'; state.textContent = labels[task.state] || task.state;
        const detail = document.createElement('span'); detail.textContent = task.message || 'Waiting for progress';
        const counts = document.createElement('span');
        counts.textContent = task.total != null ? `${task.completed ?? 0} / ${task.total}` : '';
        row.append(title, state, counts, detail);
        if (task.indeterminate && _activeTasks.has(task.id)) {
            const progress = document.createElement('progress');
            progress.setAttribute('aria-label', task.label);
            row.append(progress);
        } else if (Number.isFinite(task.total) && task.total > 0 && Number.isFinite(task.completed)) {
            const progress = document.createElement('progress'); progress.max = task.total;
            progress.value = Math.min(task.total, Math.max(0, task.completed));
            progress.setAttribute('aria-label', `${task.label}: ${task.completed} of ${task.total}`);
            row.append(progress);
        }
        host.append(row);
    }
    const events = document.getElementById('activity-events');
    if (!events) return;
    const follow = events.scrollHeight - events.scrollTop - events.clientHeight < 12;
    events.replaceChildren();
    for (const event of _taskEvents) {
        const line = document.createElement('div');
        line.textContent = `${new Date(event.at).toLocaleTimeString()}  ${event.state.toUpperCase()}  ${event.message}`;
        events.append(line);
    }
    if (follow) events.scrollTop = events.scrollHeight;
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

/* ── The app's one OUTCOME line ─────────────────────────────────────────
 *
 * Five subsystems each grew their own `_xSay(msg, isError)`, and they had
 * drifted into three conventions: a bare muted <p>, a muted <div> holding a
 * `-status-text` span beside an undo button, and a `.warning-banner` wearing a
 * `banner-neutral` modifier whose whole job was to say "ignore the warning
 * colour". A reader could not tell an outcome from a hint from a warning by
 * looking, because all three were small grey text.
 *
 * `statusLine` is the one shape for "here is what just happened":
 *
 *     glyph (aria-hidden) · text · "3 min ago"
 *
 * Four tones, because a status line genuinely has four states and collapsing
 * them to "error or not" is what made a finished scan wear the warning
 * palette. The GLYPH is the tone's primary channel — colour is never alone.
 *
 * The stamp answers a recorded Known Issue: `#audit-ns-status` and
 * `#audit-zl-status` deliberately survive a report switch, so a line from
 * earlier in the same scan read as if it had just happened. It is computed at
 * PAINT time, not ticked live: repainting is what those two hosts already do
 * on every render, so the number is right whenever it is on screen and the
 * absolute time rides the title for the rest.
 *
 * `is-error` stays the error marker the app's CSS and several tests already
 * key on, so nothing has to learn a second name. This writes only the host's
 * contents — it never touches roles or live regions, because every AT
 * announcement in this app routes through the static #sr-status-announcer.
 *
 * Pinned by tests/js/statusLine.test.js.
 */
const _STATUS_TONES = {
    info: { glyph: '›', cls: 'tone-info' },   // ›
    ok: { glyph: '✓', cls: 'tone-ok' },       // ✓
    warn: { glyph: '⚠', cls: 'tone-warn' },   // ⚠
    error: { glyph: '✕', cls: 'tone-error' }, // ✕
};
const _TONE_CLASSES = Object.values(_STATUS_TONES).map((t) => t.cls);

/** "just now" / "4 min ago" / "2 h ago", or null when there is no stamp or it
 *  is far enough back that a relative reading stops being useful. */
export function relativeStamp(at, now = Date.now()) {
    const t = at instanceof Date ? at.getTime() : Number(at);
    if (!Number.isFinite(t) || t <= 0) return null;
    const secs = Math.round((now - t) / 1000);
    if (secs < 0) return null;             // a clock change, not a future event
    if (secs < 45) return 'just now';
    const mins = Math.round(secs / 60);
    if (mins < 60) return `${mins} min ago`;
    const hours = Math.round(mins / 60);
    if (hours < 24) return `${hours} h ago`;
    return null;                           // older than a day: the title says when
}

export function statusLine(host, { tone = 'info', text = '', at = null } = {}) {
    if (!host) return null;
    const msg = text == null ? '' : String(text);
    host.textContent = '';
    host.classList.remove(..._TONE_CLASSES);
    host.classList.toggle('is-error', tone === 'error' && !!msg);
    if (!msg) { host.classList.remove('status-line'); return host; }
    const spec = _STATUS_TONES[tone] || _STATUS_TONES.info;
    host.classList.add('status-line', spec.cls);
    const glyph = document.createElement('span');
    glyph.className = 'status-glyph';
    glyph.setAttribute('aria-hidden', 'true');
    glyph.textContent = spec.glyph;
    host.appendChild(glyph);
    const body = document.createElement('span');
    body.className = 'status-text';
    body.textContent = msg;
    host.appendChild(body);
    const stamp = relativeStamp(at);
    if (stamp) {
        const when = document.createElement('span');
        when.className = 'status-at';
        when.textContent = stamp;
        try { when.title = new Date(at).toLocaleString(); } catch { /* invalid date */ }
        host.appendChild(when);
    }
    return host;
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

/* ── Restrained gamification primitives ─────────────────────────────────
 *
 * The house rule, and the reason these are so small: nothing here may be
 * load-bearing, and nothing here may nudge anyone toward a WRITE. No streaks,
 * no points, no levels, no badges, no prompt beside a verb. What is allowed is
 * showing the user a true fact about their own library that they would
 * otherwise have to compute: it is going in the right direction, something
 * changed since they last looked, they did some work today.
 *
 * Everything is localStorage and client-side. Every read is wrapped, because
 * a private window throws on access and a decoration must never take a panel
 * down with it.
 */

/** A day-scoped count of things the user did, e.g. "12 triaged today". Resets
 *  itself at midnight by storing the date beside the number — deliberately NOT
 *  a streak, which would turn skipping a day into a penalty. */
export function dailyCounter(key) {
    const today = () => {
        const d = new Date();
        return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    };
    const read = () => {
        try {
            const rec = JSON.parse(localStorage.getItem(key) || 'null');
            return (rec && rec.d === today()) ? (rec.n | 0) : 0;
        } catch { return 0; }
    };
    return {
        count: read,
        bump(by = 1) {
            const n = read() + by;
            try { localStorage.setItem(key, JSON.stringify({ d: today(), n })); } catch { /* private mode */ }
            return n;
        },
    };
}

/**
 * A bounded, self-trimming series of {t, n} samples under one localStorage key.
 *
 * `id` is what makes a sample a sample: the caller passes the identity of the
 * thing measured (a scan's finished_at), and re-reading the SAME scan never
 * adds a point. Without that, opening the tab twice would draw a flat line and
 * call it a trend.
 */
export function trendSeries(key, max = 10) {
    const read = () => {
        try {
            const v = JSON.parse(localStorage.getItem(key) || '[]');
            return Array.isArray(v) ? v.filter((p) => p && typeof p.n === 'number') : [];
        } catch { return []; }
    };
    return {
        values: read,
        /** Record one sample. Returns the series. A repeated `id` is ignored. */
        record(id, n) {
            const all = read();
            if (!id || !Number.isFinite(n)) return all;
            if (all.length && all[all.length - 1].id === id) return all;
            all.push({ id, n, t: Date.now() });
            const trimmed = all.slice(-max);
            try { localStorage.setItem(key, JSON.stringify(trimmed)); } catch { /* private mode */ }
            return trimmed;
        },
        clear() { try { localStorage.removeItem(key); } catch { /* private mode */ } },
    };
}

/**
 * An inline SVG sparkline over *values*, in `currentColor`.
 *
 * `role="img"` with a caller-supplied `label`: the shape is decoration and the
 * numbers are the content, so a reader who cannot see the line loses nothing.
 * A series of fewer than two points draws nothing rather than a dot that looks
 * like a measurement.
 */
export function sparkline(values, { label = '', width = 96, height = 20 } = {}) {
    const nums = (values || []).map(Number).filter(Number.isFinite);
    if (nums.length < 2) return null;
    const max = Math.max(...nums);
    const min = Math.min(...nums);
    const span = max - min || 1;
    const dx = width / (nums.length - 1);
    const pad = 2;
    const y = (n) => pad + (height - 2 * pad) * (1 - (n - min) / span);
    const points = nums.map((n, i) => `${(i * dx).toFixed(1)},${y(n).toFixed(1)}`).join(' ');
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'sparkline');
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.setAttribute('width', String(width));
    svg.setAttribute('height', String(height));
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', label || `${nums.length} readings, now ${nums[nums.length - 1]}`);
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
    line.setAttribute('points', points);
    line.setAttribute('fill', 'none');
    line.setAttribute('stroke', 'currentColor');
    line.setAttribute('stroke-width', '1.5');
    line.setAttribute('stroke-linejoin', 'round');
    line.setAttribute('stroke-linecap', 'round');
    svg.appendChild(line);
    // The latest reading is marked, so "where are we now" does not depend on
    // tracing the line to its right-hand end.
    const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    dot.setAttribute('cx', ((nums.length - 1) * dx).toFixed(1));
    dot.setAttribute('cy', y(nums[nums.length - 1]).toFixed(1));
    dot.setAttribute('r', '2');
    dot.setAttribute('fill', 'currentColor');
    svg.appendChild(dot);
    return svg;
}

/**
 * Show a one-shot fact, once, ever. `key` burns on FIRST SUCCESSFUL render —
 * never before — because the historical defect here was spending the flag on a
 * run that had no number to show, making the real milestone permanently
 * unshowable (2026-08-18).
 */
export function milestoneOnce(key, hostEl, { title, detail, announce = true } = {}) {
    if (!hostEl || !title) return null;
    try { if (localStorage.getItem(key)) return null; } catch { return null; }
    const strip = document.createElement('div');
    strip.className = 'milestone-strip';
    strip.setAttribute('role', 'status');
    const body = document.createElement('span');
    const strong = document.createElement('strong');
    strong.textContent = title;
    body.appendChild(strong);
    if (detail) body.appendChild(document.createTextNode(` — ${detail}`));
    strip.appendChild(body);
    hostEl.insertAdjacentElement('afterend', strip);
    if (announce) announceStatus(`${title}${detail ? `. ${detail}` : ''}`);
    try { localStorage.setItem(key, '1'); } catch { /* private mode: may repeat */ }
    return strip;
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
export function confirmInline(anchorEl, { message, confirmLabel, onConfirm, cancelLabel = 'Cancel', tone = 'danger', checkbox = null }) {
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

    let checkboxInput = null;
    if (checkbox && typeof checkbox === 'object' && checkbox.label) {
        const opt = document.createElement('label');
        opt.className = 'inline-confirm-option';
        checkboxInput = document.createElement('input');
        checkboxInput.type = 'checkbox';
        checkboxInput.checked = checkbox.defaultChecked !== false;
        const span = document.createElement('span');
        span.textContent = checkbox.label;
        opt.append(checkboxInput, span);
        strip.appendChild(opt);
    }

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
    if (anchorEl.closest('.audit-daily-active')) {
        cancel.tabIndex = 0;
        yes.tabIndex = 0;
        if (checkboxInput) checkboxInput.tabIndex = 0;
    }
    yes.addEventListener('click', () => {
        const checked = checkboxInput ? !!checkboxInput.checked : undefined;
        strip.remove();
        if (checkboxInput) onConfirm({ checkboxChecked: checked });
        else onConfirm();
        // 2026-09-03: Cancel and Escape both restore focus to the anchor;
        // CONFIRM left it on <body>, so a keyboard user who committed a write
        // had to Tab from the top of the document to reach the next row. The
        // accession panel had to build its own focus chain (_accFocusAfter)
        // precisely because of this. Deliberately conservative: only when the
        // anchor SURVIVED the write and nothing claimed focus during
        // onConfirm — a consumer that moves focus itself, or that re-renders
        // the anchor away, is left exactly as it was.
        if (anchorEl.isConnected && document.activeElement === document.body) {
            anchorEl.focus();
        }
    });
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

function _syncModalLayers() {
    const top = _topModalId();
    let layer = 1000; // .modal-overlay's base layer in styles.css
    for (const id of _modalState.keys()) {
        const overlay = document.getElementById(id);
        if (!overlay) continue;
        overlay.style.zIndex = String(layer++);
        overlay.inert = id !== top;
        if (id !== top) overlay.setAttribute('aria-hidden', 'true');
        else overlay.removeAttribute('aria-hidden');
    }
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
    overlay.inert = false;
    overlay.removeAttribute('aria-hidden');
    // Move focus into the dialog BEFORE inerting the background, so focus is
    // never momentarily trapped inside an aria-hidden/inert subtree.
    // Visible elements only (F7): focusing a display:none control is a no-op,
    // which left focus in the inerted background.
    _focusModal(overlay);
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
            if (focusable.length === 0) { e.preventDefault(); _focusModal(overlay); return; }
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            const active = document.activeElement;
            if (!overlay.contains(active)) {
                e.preventDefault();
                (e.shiftKey ? last : first).focus();
            } else if (e.shiftKey && active === first) {
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
    _syncModalLayers();
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
    // A child may outlive its invoking dialog (e.g. a delayed completion).
    // Preserve the return chain instead of later focusing a hidden control.
    for (const remaining of _modalState.values()) {
        if (overlay?.contains(remaining.trigger)) remaining.trigger = state.trigger;
    }
    if (overlay) {
        overlay.classList.remove('open');
        overlay.style.removeProperty('z-index');
        overlay.inert = false;
        overlay.removeAttribute('aria-hidden');
        overlay.removeEventListener('mousedown', state.overlayClick);
    }
    _syncModalLayers();
    document.removeEventListener('keydown', state.keyHandler);
    // The background stays inert while ANY modal remains open.
    if (_modalState.size === 0) _setBackgroundInert(false);
    // Restore focus only when the closed modal was on top — closing a lower
    // modal must not steal focus from the one the user is interacting with.
    if (wasTop && state.trigger && typeof state.trigger.focus === 'function') {
        state.trigger.focus();
    }
    if (wasTop) {
        const top = document.getElementById(_topModalId());
        if (top && !top.contains(document.activeElement)) _focusModal(top);
    }
}

// Scroll memory per tab (Card Catalog, 2026-07-20): tab panels are hidden
// with display:none, which resets every descendant's scrollTop in WebKit —
// so returning to a tab used to land back at the top of every panel. Before
// hiding, remember each scrolled element; after showing, restore. Stale refs
// from re-rendered DOM (tagging/refactor innerHTML swaps) are skipped via
// isConnected.
const _tabScrollMemo = new Map();

/* PERF (2026-09-02): leaving a tab no longer walks every descendant.
 * Before: showTab ran prev.querySelectorAll('*') and read scrollTop on every
 *   node in the outgoing panel just to find the handful that had scrolled.
 *   With the audit inventory rendered that is one NodeList and one layout
 *   read per node over tens of thousands of nodes, on every tab switch.
 * Change: one passive capture-phase 'scroll' listener records WHICH elements
 *   actually scroll, per panel; showTab reads only those.
 * Safe because: an element's scrollTop can only be non-zero after a scroll
 *   event fired for it (user or programmatic, including our own restore), so
 *   the recorded set is a superset of what the full walk would have found.
 *   Detached and non-scrolled entries are filtered exactly as before, and the
 *   restore path (isConnected + rAF) is untouched.
 * Lock/thread contract: n/a (browser).
 * Pinned by: tests/js/tabScrollMemo.test.js (red before the change:
 *   the walk still queried '*'). */
const _scrolledInPanel = new Map();

document.addEventListener('scroll', (e) => {
    const el = e.target;
    if (!el || typeof el.closest !== 'function') return;
    const panel = el.closest('.content-area');
    if (!panel || !panel.id) return;
    let set = _scrolledInPanel.get(panel.id);
    if (!set) { set = new Set(); _scrolledInPanel.set(panel.id, set); }
    set.add(el);
}, true);

function _scrollCandidates(panel) {
    const set = _scrolledInPanel.get(panel.id);
    if (!set) return [];
    const out = [];
    for (const el of set) {
        if (el.isConnected && panel.contains(el)) out.push(el);
        else set.delete(el);
    }
    return out;
}

export function showTab(tabId) {
    const navigation = new CustomEvent('chatekld:navigate', {detail: {tabId}, cancelable: true});
    if (!document.dispatchEvent(navigation)) return;
    const prev = document.querySelector('.content-area.active');
    if (prev && prev.id !== `${tabId}-tab`) {
        const saved = [];
        _scrollCandidates(prev).forEach((el) => {
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
        // A feature-blind activation signal (the module hierarchy forbids
        // ui.js knowing about audit.js): a panel whose first paint is
        // expensive listens for this instead of doing the work at launch.
        try {
            document.dispatchEvent(new CustomEvent('chatekld:tabshown',
                                                   { detail: { tabId } }));
        } catch (e) { /* CustomEvent unavailable — nothing to defer */ }
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
        // `hidden` tabs (the Maintenance tab on a public build) are skipped:
        // focus() on a hidden element is a no-op, so including them would make
        // arrow-key navigation appear to swallow a keypress at that position.
        const tabs = Array.from(tablistEl.querySelectorAll('[role="tab"]'))
            .filter((t) => !t.hidden);
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
        // Plain Chat attachments (2026-08-29). An exported conversation that
        // silently omitted "and a 40-page PDF" would misrepresent what the
        // answer was written from — the same reason the tool trace is here.
        // The files' TEXT is deliberately not exported (it is the user's own
        // document, often huge); what is recorded is which files rode the turn
        // and how much of each the model actually received, so a truncated
        // attachment cannot read later as a whole one.
        if (Array.isArray(turn.attachments) && turn.attachments.length) {
            const names = turn.attachments.map((a) => {
                if (!a || typeof a !== 'object') return String(a);
                const chars = Number(a.attached_chars);
                if (!Number.isFinite(chars)) return String(a.filename || '');
                const full = Number(a.chars);
                const size = (a.truncated && Number.isFinite(full))
                    ? `first ${chars} of ${full} chars`
                    : `${chars} chars`;
                return `${a.filename} (${size})`;
            });
            const att = `Attachments: ${names.join(', ')}`;
            lines.push(fmt === 'md' ? `*${att}*` : att, '');
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
                    // Paper label (2026-09-23): "path — Smith et al., 2020, Title".
                    const label = (s.paper && typeof s.paper.label === 'string' && s.paper.label.trim())
                        ? ` — ${s.paper.label.trim()}` : '';
                    return `${s.source}${label}${score}`;
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

const _MARKDOWN_TAGS = [
    'a', 'p', 'br', 'hr', 'blockquote', 'pre', 'code', 'em', 'strong', 'del',
    'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'img', 'span',
];
const _MARKDOWN_ATTRS = [
    'href', 'src', 'alt', 'title', 'class', 'colspan', 'rowspan', 'align',
    'start', 'reversed',
];
const _LINK_SCHEMES = new Set(['http:', 'https:', 'mailto:', 'obsidian:', 'zotero:']);

function _browserUrlValue(raw) {
    return String(raw || '')
        .replace(/^[\u0000-\u0020]+|[\u0000-\u0020]+$/g, '')
        .replace(/[\t\n\r]/g, '');
}

function _allowedLink(raw) {
    const value = _browserUrlValue(raw);
    if (!value || value.startsWith('\\') || value.startsWith('//')) return false;
    if (value.startsWith('#') || value.startsWith('/') || value.startsWith('./')
            || value.startsWith('../')) return true;
    const match = /^([a-z][a-z0-9+.-]*:)/i.exec(value);
    return !match || _LINK_SCHEMES.has(match[1].toLowerCase());
}

function _allowedImage(raw) {
    const value = _browserUrlValue(raw);
    if (!value || value.startsWith('\\') || value.startsWith('//')) return false;
    if (/^https?:/i.test(value)) return false;
    if (/^data:/i.test(value)) {
        return /^data:image\/(?:png|gif|jpe?g|webp);base64,[a-z0-9+/=\s]+$/i.test(value);
    }
    if (/^blob:/i.test(value)) return true;
    return !/^[a-z][a-z0-9+.-]*:/i.test(value);
}

// The schemes the app's own link interceptor knows how to route, plus the
// relative/anchor forms Markdown produces. Replacing DOMPurify's default URI
// regexp (rather than setting ALLOW_UNKNOWN_PROTOCOLS) keeps the maintained
// sanitizer as the gate: the previous arrangement switched its URI check OFF so
// obsidian:/zotero: would survive, then re-implemented scheme matching in app
// code — reinstating by hand the denylist posture adopting DOMPurify was meant
// to retire, and covering only the two attributes the follow-up pass looked at.
const _ALLOWED_URI_REGEXP =
    /^(?:(?:https?|mailto|obsidian|zotero):|[^a-z]|[a-z+.\-]+(?:[^a-z+.\-:]|$))/i;

// marked emits `class="language-x"` on fenced code and nothing else. Untrusted
// Markdown could otherwise borrow ANY class in the app stylesheet — including
// overlay/positioning classes — which is a UI-redress primitive, not styling.
const _ALLOWED_CLASS = /^language-[\w.+-]+$/;

// One hook, registered once, applies the app's narrower policy DURING
// sanitization. It used to run afterwards over a re-parsed copy of the output;
// see sanitiseHtml for why that round trip is gone.
_domPurify.addHook('afterSanitizeAttributes', (node) => {
    if (!node || typeof node.hasAttribute !== 'function') return;
    if (node.nodeName === 'A' && node.hasAttribute('href')
            && !_allowedLink(node.getAttribute('href'))) {
        node.removeAttribute('href');
    }
    if (node.nodeName === 'IMG'
            && !_allowedImage(node.getAttribute('src'))) {
        // Removed outright rather than stripped: a src-less <img> renders as a
        // broken-image placeholder, which is worse than nothing for a source we
        // are refusing on privacy grounds. This also drops an <img> that never
        // had a src — the previous pass selected `img[src]` and left those in
        // place. Deliberate: marked never emits one, and it renders as nothing.
        node.remove();
        return;
    }
    if (node.hasAttribute('class')) {
        const kept = String(node.getAttribute('class') || '')
            .split(/\s+/).filter((name) => _ALLOWED_CLASS.test(name));
        if (kept.length) node.setAttribute('class', kept.join(' '));
        else node.removeAttribute('class');
    }
});

/**
 * Allowlist sanitizer for every rendered-Markdown surface.
 *
 * DOMPurify owns namespace/parser mutation safety and URL scheme policy; the
 * hook above adds the app's narrower privacy rules (no remote image sources, no
 * borrowed stylesheet classes) while the document is still DOMPurify's.
 *
 * The returned string goes straight to `innerHTML`, which is DOMPurify's
 * supported usage. It deliberately does NOT take the previous detour of
 * re-parsing the sanitized string with DOMParser, mutating that copy and
 * re-serialising it: an extra parse/serialize round trip after sanitization is
 * the shape mXSS exploits, and DOMPurify documents it as unsafe. Links are
 * inert until a user activates them; styles, SVG/MathML, media, forms and
 * ids/names are absent by construction.
 */
export function sanitiseHtml(html) {
    return _domPurify.sanitize(String(html || ''), {
        ALLOWED_TAGS: _MARKDOWN_TAGS,
        ALLOWED_ATTR: _MARKDOWN_ATTRS,
        ALLOW_DATA_ATTR: false,
        ALLOW_ARIA_ATTR: false,
        ALLOWED_URI_REGEXP: _ALLOWED_URI_REGEXP,
        RETURN_TRUSTED_TYPE: false,
    });
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
export function makeAnswerRenderer(targetEl, onRender) {
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
        // PERF (2026-09-02): the optional after-render hook exists so a caller
        // that must follow the growing answer (scrolling a chat pane to the
        // bottom) does it HERE, inside the 40 ms coalesced render, instead of
        // wrapping update() and forcing a layout read per streamed token.
        // Safe because it runs on exactly the ticks that changed the DOM, so
        // the pane is pinned to the bottom at every paint the user can see.
        if (typeof onRender === 'function') {
            try { onRender(); } catch (e) { /* a hook must not break a stream */ }
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
/**
 * Is the user mid-selection? Used to tell a text drag from a click.
 *
 * Defensive: `getSelection` is absent in some embedding contexts and jsdom
 * returns a Selection with no ranges, so anything unexpected reads as "no
 * selection" — the interceptor must keep working when the API does not.
 *
 * @returns {boolean} true when a non-empty selection exists
 */
function _hasTextSelection() {
    try {
        const sel = window.getSelection && window.getSelection();
        return !!sel && !sel.isCollapsed && String(sel).length > 0;
    } catch (_e) {
        return false;
    }
}

export function installLinkInterceptor(rootEl = document) {
    if (rootEl._linkInterceptorInstalled) return;
    rootEl._linkInterceptorInstalled = true;
    rootEl.addEventListener('click', (e) => {
        const a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
        if (!a) return;
        // A drag-selection that begins and ends inside one anchor produces a
        // click ON that anchor, so without this guard selecting a sentence that
        // happens to contain a link fired /api/open — a Finder reveal or a
        // browser tab — instead of selecting text. Harmless before text
        // selection was enabled (launch.py's text_select); a daily annoyance
        // after it. A real click always leaves the selection collapsed.
        if (_hasTextSelection()) return;
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
let _helpTipHideTimer = null;

function _queueHideHelpTip() {
    clearTimeout(_helpTipHideTimer);
    // Allow the pointer to cross the small gap between badge and tooltip.
    _helpTipHideTimer = setTimeout(() => {
        if (_helpTipAnchor && document.activeElement !== _helpTipAnchor
            && !_helpTipAnchor.matches(':hover') && !(_helpTipEl && _helpTipEl.matches(':hover'))) {
            _hideHelpTip(_helpTipAnchor);
        }
    }, 200);
}

function _ensureHelpTipEl() {
    if (_helpTipEl && _helpTipEl.isConnected) return _helpTipEl;
    const el = document.createElement('div');
    el.className = 'help-tip';
    el.id = 'help-tip-shared';
    el.setAttribute('role', 'tooltip');
    el.hidden = true;
    el.addEventListener('mouseenter', () => clearTimeout(_helpTipHideTimer));
    el.addEventListener('mouseleave', _queueHideHelpTip);
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
    clearTimeout(_helpTipHideTimer);
    if (_helpTipAnchor && _helpTipAnchor !== badge) _helpTipAnchor.removeAttribute('aria-describedby');
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
    clearTimeout(_helpTipHideTimer);
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
 * tooltip stays readable while badge or tip is hovered, or badge focused;
 * Escape dismisses immediately. Idempotent per anchor.
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
    badge.addEventListener('mouseleave', _queueHideHelpTip);
    badge.addEventListener('focus', () => _showHelpTip(badge, text));
    badge.addEventListener('blur', _queueHideHelpTip);
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
    // Undo the background inert — unless a real modal still owns it.
    if (_modalState.size === 0) _setBackgroundInert(false);
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
    // aria-modal must be TRUE in behavior, not just claimed (2026-08-18):
    // Tab was trapped but the SR virtual cursor could wander into content
    // the dialog declared inert. Same background-inert as openModal; undone
    // in _paletteClose.
    _setBackgroundInert(true);
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
            try {
                // Explicit behavior:'smooth' is NOT overridden by the global
                // reduced-motion CSS kill — gate it ourselves (2026-08-18).
                const reduce = typeof window.matchMedia === 'function'
                    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
                target.scrollIntoView({ block: 'center', behavior: reduce ? 'auto' : 'smooth' });
            } catch (_) { /* older engines */ }
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

// ---------------------------------------------------------------------------
// Table sort + filter (2026-08-19)
//
// ONE primitive for every report table (audit.js's 16 report renders and
// tagging.js's inventory table), feature-blind and dependency-free — the
// confirmInline / runTour precedent.
//
// It works at the DOM level (reordering the existing <tr> nodes, hiding the
// non-matching ones) rather than sorting a row ARRAY and re-rendering through
// the owning module's string builder. That is a deliberate inversion of the
// obvious design, for two reasons:
//
//   * Re-rendering destroys per-row STATE that lives in the DOM and nowhere
//     else. The tagging table's write cell holds a text <input> the user may
//     be typing a tag into; a sort click mid-typing would silently discard it.
//     Moving the existing nodes preserves every binding, checkbox and caret by
//     construction — there is no rehydration step to get wrong.
//   * Filtering never re-renders at all, so it cannot re-enter the owning
//     module's seeding/wiring passes (the accession body's `_acc.seeded`
//     default-check pass, `_wireRowActions`, the `_accessionAttach` handover).
//     A data-level filter would have to be proven safe against each of them on
//     every keystroke.
//
// Wire-once is a DATASET MARKER on the table element, never a module flag: a
// module-scoped boolean stays true after the node is replaced and silently
// leaves the new table unwired (the audit search-bar defect class).

/**
 * Accent- and case-fold a string for comparison.
 *
 * Mirrors `audit/search.py::fold` (NFD, drop combining marks, casefold) — the
 * vault is bilingual FR/EN on an NFD-filename volume, so "Éluard" must sort
 * and match with "E" on both sides of every comparison.
 */
export function foldText(s) {
    return String(s == null ? '' : s)
        .normalize('NFD')
        .replace(/[̀-ͯ]/g, '')
        .toLowerCase();
}

const _TABLE_FILTER_DEBOUNCE_MS = 200;
/** Below this many rows a filter box is noise, not help. */
const _TABLE_CONTROLS_MIN_ROWS = 8;
/** Cells sampled per column when guessing whether it sorts numerically. */
const _TABLE_TYPE_SAMPLE = 60;

/**
 * The value one cell sorts on.
 *
 * An explicit `data-sort-value` wins — for cells whose visible text is
 * truncated, formatted ("1.5 MB") or a count followed by verb buttons.
 * Otherwise the cell's text WITHOUT its buttons, so a column ends up ordered
 * by its data and not by the affordances every row shares (which would make
 * the sort a no-op). Same rule as the filter haystack, deliberately.
 */
function _cellSortValue(tr, index) {
    const td = tr.cells[index];
    if (!td) return '';
    const explicit = td.getAttribute('data-sort-value');
    if (explicit !== null) return explicit.trim();
    if (!td.querySelector('button')) return td.textContent.trim();
    const clone = td.cloneNode(true);
    clone.querySelectorAll('button').forEach((b) => b.remove());
    return clone.textContent.trim();
}

const _NUMERICISH = /^-?\d[\d\s,]*(\.\d+)?$/;

function _valuesAreNumeric(values) {
    let seen = 0;
    for (const v of values) {
        if (!v) continue;
        if (!_NUMERICISH.test(v)) return false;
        seen += 1;
        if (seen >= _TABLE_TYPE_SAMPLE) break;
    }
    return seen > 0;
}

function _numOf(v) {
    const n = parseFloat(String(v).replace(/[\s,]/g, ''));
    return Number.isFinite(n) ? n : null;
}

/** A <th> is sortable when it carries visible text of its own. A header that
 *  is empty (the verb columns) or only a screen-reader label (the harmony
 *  select column) has nothing to order by. `data-sort="off"` opts out. */
function _thIsSortable(th) {
    if (th.getAttribute('data-sort') === 'off') return false;
    const clone = th.cloneNode(true);
    clone.querySelectorAll('.sr-only').forEach((el) => el.remove());
    return clone.textContent.trim().length > 0;
}

function _thLabel(th) {
    const clone = th.cloneNode(true);
    clone.querySelectorAll('.th-sort-arrow').forEach((el) => el.remove());
    return clone.textContent.trim();
}

/**
 * Attach A→Z sorting and a text filter to one rendered table.
 *
 * @param {HTMLTableElement} table
 * @param {object} [o]
 *   - label      accessible name for the filter input
 *   - mountBefore element to insert the control strip before (default: the
 *                table). Pass the table's scroll wrapper when it has one, or
 *                the strip rides the horizontal scroll out of view.
 *   - noun       what a row is called in the count line (default "rows")
 *   - minRows    skip installation below this many rows (default 8)
 *   - initial    {sortKey, dir, query} to restore after an owner re-render
 *   - onState    called with the current {sortKey, dir, query} on every change
 * @returns {object|null} controller, or null when nothing was installed
 */
/**
 * Swallow file drops that land outside a real drop zone.
 *
 * WHY THIS IS NOT COSMETIC. An unhandled `drop` makes the browser NAVIGATE to
 * the dropped file — and in PyWebView that replaces the entire app window with
 * a PDF viewer, with no back button and no way to return short of relaunching.
 * The two real drop zones (the Plain Chat panel and the Single Paper upload
 * box) each `preventDefault` their own `dragover`, and plainchat.js's comment
 * names this exact hazard — but a drop that misses them by a few pixels, or
 * lands on the sidebar, the header, or any other tab, still destroyed the
 * session. Guarding one element while the rest of the window stays live is a
 * mitigation that only works when the user aims correctly.
 *
 * Document-level and LAST-resort: it only acts on an event nothing else
 * handled. A real drop zone calls `preventDefault` in its own handler, which
 * runs first (it is deeper in the tree, and these listeners are not capturing),
 * so `defaultPrevented` is already true by the time this sees it and the zone's
 * behaviour is untouched.
 *
 * Idempotent via a dataset marker on <html> rather than a module flag — a
 * module boolean stays true across a re-import while the listeners it claims to
 * have installed may not exist (the audit search-bar defect class).
 */
export function installGlobalDropGuard() {
    const root = document.documentElement;
    if (!root || root.dataset.dropGuard === '1') return;
    root.dataset.dropGuard = '1';
    const isFileDrag = (e) => !!(e.dataTransfer
        && [...(e.dataTransfer.types || [])].includes('Files'));
    // dragover must be prevented too: without it the browser never fires a
    // cancellable `drop` at all, so preventing only `drop` is not enough.
    for (const type of ['dragover', 'drop']) {
        document.addEventListener(type, (e) => {
            if (e.defaultPrevented || !isFileDrag(e)) return;
            e.preventDefault();
            if (type === 'drop' && e.dataTransfer) {
                // Say why nothing happened, rather than appearing to ignore
                // the file. The user meant to attach it somewhere.
                announceStatus('Drop a file on the chat panel or the upload box '
                    + 'to attach it.');
            }
        });
    }
}


export function installTableControls(table, o = {}) {
    if (!table || table.dataset.tableControls === '1') return null;
    const tbody = table.tBodies && table.tBodies[0];
    const head = table.tHead && table.tHead.rows[0];
    if (!tbody || !head) return null;
    // The shared empty-state row (`_tbodyHtml`) is not data, and neither is an
    // injected detail/diff panel (`data-row-attachment`) — it belongs to the
    // row above it and is sorted, hidden and counted with that row.
    const dataRows = [...tbody.rows].filter(
        (r) => !r.querySelector('.audit-empty-row')
            && !r.hasAttribute('data-row-attachment'));
    if (dataRows.length < (o.minRows == null ? _TABLE_CONTROLS_MIN_ROWS : o.minRows)) return null;
    table.dataset.tableControls = '1';

    const noun = o.noun || 'rows';
    const state = {
        sortKey: (o.initial && o.initial.sortKey) || null,
        dir: (o.initial && o.initial.dir) || 'asc',
        query: (o.initial && o.initial.query) || '',
    };
    const order = new Map(dataRows.map((tr, i) => [tr, i]));   // stable tiebreak
    const haystack = new WeakMap();                            // lazily folded
    const numeric = new Map();                                 // index -> bool

    // ---- filter --------------------------------------------------------
    const wrap = document.createElement('div');
    wrap.className = 'table-controls';
    const input = document.createElement('input');
    input.type = 'search';
    input.className = 'table-filter';
    input.value = state.query;
    input.setAttribute('aria-label', o.label || `Filter these ${noun}`);
    input.placeholder = 'Filter…';
    const count = document.createElement('span');
    count.className = 'table-filter-count';
    wrap.appendChild(input);
    wrap.appendChild(count);
    const before = (o.mountBefore && o.mountBefore.parentNode) ? o.mountBefore : table;
    before.parentNode.insertBefore(wrap, before);

    let emptyRow = null;
    const showEmpty = (on) => {
        if (on && !emptyRow) {
            emptyRow = document.createElement('tr');
            emptyRow.className = 'table-filter-empty';
            const td = document.createElement('td');
            td.colSpan = 99;
            td.className = 'muted';
            td.textContent = `No ${noun} match this filter.`;
            emptyRow.appendChild(td);
            tbody.appendChild(emptyRow);
        } else if (!on && emptyRow) {
            emptyRow.remove();
            emptyRow = null;
        }
    };

    /** Row text WITHOUT its verb buttons: "[open]" is an affordance, not data,
     *  and matching it would make "open" select every row.
     *
     *  Plus the FULL value of any truncated cell. `.cell-title` is the house
     *  class for "clipped for width, whole value in the title attribute" — a
     *  90-character slice of a paper title, typically. Searching only the
     *  visible text there returns "no match" for a word that is genuinely in
     *  the row, which is a silently wrong answer rather than a missing
     *  feature. Only that class is consulted: `title` on other elements is
     *  explanatory tooltip prose ("Present in both stores under different
     *  spellings…") and folding it in would make whole reports match it. */
    const rowText = (tr) => {
        let cached = haystack.get(tr);
        if (cached === undefined) {
            const clone = tr.cloneNode(true);
            clone.querySelectorAll('button').forEach((b) => b.remove());
            let text = clone.textContent;
            for (const td of tr.querySelectorAll('td.cell-title[title]')) {
                text += ` ${td.getAttribute('title')}`;
            }
            cached = foldText(text.replace(/\s+/g, ' '));
            haystack.set(tr, cached);
        }
        return cached;
    };

    /** Rows a caller injected UNDER a data row — an expanded detail drawer or
     *  a diff panel — marked `data-row-attachment`.
     *
     *  They are looked up live rather than taken from `dataRows`, which is a
     *  snapshot taken at install time and therefore cannot contain a row that
     *  did not exist yet. Before this existed, sorting `appendChild`-moved
     *  every data row to the end of the tbody and left an injected row exactly
     *  where it was — so it floated to the TOP, detached from its own parent
     *  and sitting under an unrelated one. On the Zotero-region diff that meant
     *  reading note A's proposed changes immediately above note B's [update]
     *  button. Filtering had the mirror bug: the panel was never hidden, so a
     *  query matching nothing still left a stranded diff on screen. */
    const attachmentsOf = (tr) => {
        const out = [];
        let n = tr.nextElementSibling;
        while (n && n.hasAttribute && n.hasAttribute('data-row-attachment')) {
            out.push(n);
            n = n.nextElementSibling;
        }
        return out;
    };

    const applyFilter = (announce) => {
        const tokens = foldText(state.query).split(/\s+/).filter(Boolean);
        let shown = 0;
        for (const tr of dataRows) {
            const hit = !tokens.length || tokens.every((t) => rowText(tr).includes(t));
            // PERF (2026-09-02): write `hidden` (and walk for attachments)
            // only when the row's visibility actually CHANGES. Before, every
            // keystroke assigned `hidden` to all 6 000 rows and ran the
            // sibling walk for each of them, whether or not anything moved.
            // Safe because assigning the value a row already has is a no-op
            // by definition; the shown count is still derived from `hit`, so
            // the count line, the empty state and the announcement are
            // unchanged. Pinned by tests/js/tableSort.test.js.
            if (tr.hidden === hit) {
                tr.hidden = !hit;
                // An attachment carries its parent's visibility, never its own
                // text: matching a diff panel's CONTENT would surface a row
                // whose data does not match the query at all.
                for (const at of attachmentsOf(tr)) at.hidden = !hit;
            }
            if (hit) shown += 1;
        }
        if (tokens.length) {
            count.textContent = `${shown} of ${dataRows.length} ${noun}`;
            showEmpty(shown === 0);
        } else {
            count.textContent = '';
            showEmpty(false);
        }
        // Announced through the STATIC announcer — a freshly-minted live
        // region per table is the documented anti-pattern.
        if (announce && tokens.length) {
            announceStatus(`${shown} of ${dataRows.length} ${noun} match.`);
        }
    };

    let debounce = null;
    input.addEventListener('input', () => {
        state.query = input.value;
        if (debounce) clearTimeout(debounce);
        debounce = setTimeout(() => {
            debounce = null;
            applyFilter(true);
            if (o.onState) o.onState({ ...state });
        }, _TABLE_FILTER_DEBOUNCE_MS);
    });
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && input.value) {
            e.preventDefault();
            e.stopPropagation();
            input.value = '';
            state.query = '';
            if (debounce) { clearTimeout(debounce); debounce = null; }
            applyFilter(false);
            if (o.onState) o.onState({ ...state });
        }
    });

    // ---- sort ----------------------------------------------------------
    const ths = [...head.cells];
    const applySort = () => {
        for (const th of ths) {
            if (!th.dataset.sortKey) continue;
            const active = th.dataset.sortKey === state.sortKey;
            th.setAttribute('aria-sort', active
                ? (state.dir === 'asc' ? 'ascending' : 'descending')
                : 'none');
            const arrow = th.querySelector('.th-sort-arrow');
            if (arrow) arrow.textContent = active ? (state.dir === 'asc' ? '▲' : '▼') : '↕';
        }
        if (!state.sortKey) return;
        const th = ths.find((t) => t.dataset.sortKey === state.sortKey);
        if (!th) return;
        const idx = th.cellIndex;
        // Keys are extracted ONCE per sort, not per comparison: at ~6k rows a
        // comparator that re-read (and cloned) cells would do O(n log n)
        // DOM work for what is one pass of data.
        const key = new Map();
        for (const tr of dataRows) key.set(tr, _cellSortValue(tr, idx));
        if (!numeric.has(idx)) numeric.set(idx, _valuesAreNumeric(key.values()));
        const isNum = numeric.get(idx);
        const sign = state.dir === 'asc' ? 1 : -1;
        const folded = new Map();
        if (!isNum) for (const tr of dataRows) folded.set(tr, foldText(key.get(tr)));
        const sorted = [...dataRows].sort((a, b) => {
            const va = key.get(a);
            const vb = key.get(b);
            // Blanks always sink, in BOTH directions — "no value" is not the
            // smallest value, and floating them to the top on a descending
            // sort would bury the rows the user asked to see.
            if (!va !== !vb) return va ? -1 : 1;
            let c = 0;
            if (va && vb) {
                if (isNum) {
                    const na = _numOf(va);
                    const nb = _numOf(vb);
                    c = (na == null || nb == null) ? 0 : na - nb;
                } else {
                    const fa = folded.get(a);
                    const fb = folded.get(b);
                    c = fa < fb ? -1 : (fa > fb ? 1 : 0);
                }
            }
            if (c) return c * sign;
            return order.get(a) - order.get(b);
        });
        // Moving the EXISTING nodes: bindings, checkbox state and any text a
        // user has typed into a row survive the reorder untouched.
        const frag = document.createDocumentFragment();
        for (const tr of sorted) {
            // Read the attachments BEFORE moving the parent: appendChild
            // detaches it, after which nextElementSibling no longer answers
            // the question.
            const attached = attachmentsOf(tr);
            frag.appendChild(tr);
            for (const at of attached) frag.appendChild(at);
        }
        tbody.appendChild(frag);
        if (emptyRow) tbody.appendChild(emptyRow);
    };

    for (const th of ths) {
        if (!_thIsSortable(th)) continue;
        const key = `c${th.cellIndex}`;
        th.dataset.sortKey = key;
        th.setAttribute('aria-sort', 'none');
        const label = _thLabel(th);
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'th-sort';
        while (th.firstChild) btn.appendChild(th.firstChild);
        const arrow = document.createElement('span');
        arrow.className = 'th-sort-arrow';
        arrow.setAttribute('aria-hidden', 'true');
        arrow.textContent = '↕';
        btn.appendChild(arrow);
        th.appendChild(btn);
        btn.addEventListener('click', () => {
            if (state.sortKey === key) {
                state.dir = state.dir === 'asc' ? 'desc' : 'asc';
            } else {
                state.sortKey = key;
                state.dir = 'asc';
            }
            applySort();
            announceStatus(`Sorted by ${label}, ${state.dir === 'asc' ? 'A to Z' : 'Z to A'}.`);
            if (o.onState) o.onState({ ...state });
        });
    }

    if (state.sortKey) applySort();
    if (state.query) applyFilter(false);

    return {
        state: () => ({ ...state }),
        clearFilter() {
            if (!state.query) return;
            input.value = '';
            state.query = '';
            applyFilter(false);
            if (o.onState) o.onState({ ...state });
        },
    };
}

/* ---------------------------------------------------------------------------
 * Shared side-by-side diff (2026-08-29)
 *
 * ONE diff look for the app's two "here is what would change to a file you own"
 * surfaces: the Zotero-region update preview (audit.js) and Note Refactor's
 * detail pane (refactor.js). It lives here, beside confirmInline and
 * installTableControls, because both callers may import only ui.js + api.js.
 *
 * WHY CLIENT-SIDE. It takes two TEXTS, not a server-computed diff, and that is
 * what lets one renderer serve both callers with no payload change at all:
 * /api/notesync/update-preview already ships `current_region` + `proposed_region`
 * and the refactor plan frame already ships the full `original` + `proposed`
 * bodies. Server-side opcodes would have meant growing two payloads (one of them
 * the app's heaviest) and maintaining a second alignment implementation.
 *
 * WHY WORD-LEVEL. Measured over the live library's 16 actionable Zotero-region
 * updates (2026-08-29): `replace` is the dominant opcode — 52 blocks covering
 * 178 lines, against 14 deleted and 31 inserted — and the similarity of the
 * paired lines is strongly BIMODAL: 32 pairs at >=0.9 (the same line lightly
 * reworded) and 26 at <=0.1 (unrelated lines merely adjacent in one block),
 * with only 4 in between. Line-level-only would therefore paint 178 lines as
 * wholesale changes when half of them differ by a few words; and highlighting
 * words in the <=0.1 pairs would be confetti. _DIFF_WORD_MIN_RATIO sits in the
 * empty middle of that distribution, so the gate is decided by the data.
 *
 * NEVER innerHTML: both sides are vault / Zotero content. Every line and every
 * word run is its own element with textContent, the discipline
 * refactor.js::_renderDiff already follows and tests/js/sanitiseHtml.test.js
 * exists to protect.
 *
 * COLOUR IS NEVER THE SOLE CHANNEL: each cell carries a visible `-`/`+` sign,
 * so the state survives monochrome rendering and reaches a screen reader in the
 * unified-diff vocabulary (WCAG 1.4.1). Line numbers are aria-hidden.
 *
 * NO HORIZONTAL SCROLL: cells wrap (`pre-wrap` + `overflow-wrap: anywhere`)
 * rather than scrolling sideways. These are prose notes, not code, so wrapping
 * loses nothing — and it is what keeps the two columns inside a report table
 * cell without widening the document (tests/browser/viewport.spec.js asserts no
 * document-level horizontal overflow at 960px).
 */

/** Line-alignment cell budget. Beyond it the core is reported as one wholesale
 *  replacement rather than aligned — honest, and never wrong, just coarser.
 *  Sized from the live vault (2026-08-29): reference-note regions run to 175
 *  lines (median 6) and whole notes to 3,399 (p99 678), but the common
 *  prefix/suffix is stripped first, so this bounds the number of lines that
 *  actually DIFFER. A note with over ~1,000 differing lines is a full rewrite,
 *  which is exactly what the degraded rendering says. 1e6 Uint32 cells = 4 MB. */
const _DIFF_LINE_BUDGET = 1000 * 1000;
/** Same, per line, for the word pass. A line with more tokens than this gets no
 *  word highlighting (it still renders as a changed line). */
const _DIFF_TOKEN_BUDGET = 250 * 250;
/** Below this similarity a paired line gets NO word highlighting — see the
 *  bimodal measurement above. */
const _DIFF_WORD_MIN_RATIO = 0.5;
/** Unchanged lines kept either side of a change before a run is folded. */
const _DIFF_CONTEXT = 3;
/** Rows actually built into the DOM. `_DIFF_LINE_BUDGET` bounds the ALIGNMENT
 *  cost and nothing else: past it the core is reported as one wholesale
 *  `replace`, which `_diffRows` then expands into `min(aCore, bCore)` rows —
 *  still thousands. Note Refactor renders whole-note diffs, and the live vault
 *  runs to 3,399 lines, so a heavily reworded long note built ~20k elements
 *  synchronously on the main thread (plus one Uint32Array per changed line for
 *  the word pass). Truncating and SAYING SO is the house idiom; silently
 *  janking the window for seconds is not. */
const _DIFF_MAX_ROWS = 1500;

/** Words, whitespace runs, and single other characters.
 *  Every character of the input falls into exactly one token, so joining the
 *  tokens reproduces the line byte-for-byte — the property that lets the word
 *  pass re-emit user text without losing any of it. */
const _DIFF_TOKEN_RE = /[\p{L}\p{N}_]+|\s+|[^\p{L}\p{N}_\s]/gu;

function _diffTokens(line) {
    return line.match(_DIFF_TOKEN_RE) || [];
}

/** Longest-common-subsequence opcodes over two arrays, difflib's vocabulary:
 *  a list of {tag: equal|replace|delete|insert, i1, i2, j1, j2} covering both
 *  inputs exactly. Returns {ops, degraded}; `degraded` means the core exceeded
 *  `budget` and is reported as one `replace`. */
function _lcsOps(a, b, budget) {
    const shared = Math.min(a.length, b.length);
    let p = 0;
    while (p < shared && a[p] === b[p]) p++;
    let s = 0;
    while (s < shared - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s++;

    const aCore = a.slice(p, a.length - s);
    const bCore = b.slice(p, b.length - s);
    const ops = [];
    if (p) ops.push({ tag: 'equal', i1: 0, i2: p, j1: 0, j2: p });

    let degraded = false;
    if (aCore.length && bCore.length && aCore.length * bCore.length > budget) {
        degraded = true;
        ops.push({ tag: 'replace', i1: p, i2: p + aCore.length, j1: p, j2: p + bCore.length });
    } else if (aCore.length || bCore.length) {
        for (const op of _lcsCore(aCore, bCore)) {
            ops.push({
                tag: op.tag,
                i1: op.i1 + p, i2: op.i2 + p,
                j1: op.j1 + p, j2: op.j2 + p,
            });
        }
    }
    if (s) {
        ops.push({
            tag: 'equal',
            i1: a.length - s, i2: a.length,
            j1: b.length - s, j2: b.length,
        });
    }
    return { ops, degraded };
}

/** LCS opcodes over two arrays with no common prefix/suffix. Classic DP: the
 *  table is filled from the end so the forward walk can pick the branch that
 *  keeps the longest remaining subsequence. */
function _lcsCore(a, b) {
    const n = a.length;
    const m = b.length;
    if (!n) return m ? [{ tag: 'insert', i1: 0, i2: 0, j1: 0, j2: m }] : [];
    if (!m) return [{ tag: 'delete', i1: 0, i2: n, j1: 0, j2: 0 }];

    const w = m + 1;
    const dp = new Uint32Array((n + 1) * w);
    for (let i = n - 1; i >= 0; i--) {
        for (let j = m - 1; j >= 0; j--) {
            dp[i * w + j] = a[i] === b[j]
                ? dp[(i + 1) * w + j + 1] + 1
                : Math.max(dp[(i + 1) * w + j], dp[i * w + j + 1]);
        }
    }

    // Forward walk, one step per element, then coalesce equal-tag runs.
    const blocks = [];
    const step = (tag, i, j) => {
        const last = blocks[blocks.length - 1];
        if (last && last.tag === tag) {
            if (tag !== 'insert') last.i2++;
            if (tag !== 'delete') last.j2++;
            return;
        }
        blocks.push({
            tag,
            i1: i, i2: i + (tag === 'insert' ? 0 : 1),
            j1: j, j2: j + (tag === 'delete' ? 0 : 1),
        });
    };
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
        if (a[i] === b[j]) { step('equal', i, j); i++; j++; }
        else if (dp[(i + 1) * w + j] >= dp[i * w + j + 1]) { step('delete', i, j); i++; }
        else { step('insert', i, j); j++; }
    }
    while (i < n) { step('delete', i, j); i++; }
    while (j < m) { step('insert', i, j); j++; }

    // Adjacent delete+insert is one `replace`, as difflib reports it — that is
    // what lets the renderer put the two versions of a line on ONE row.
    const out = [];
    for (let k = 0; k < blocks.length; k++) {
        const cur = blocks[k];
        const nxt = blocks[k + 1];
        if (cur.tag === 'delete' && nxt && nxt.tag === 'insert') {
            out.push({ tag: 'replace', i1: cur.i1, i2: cur.i2, j1: nxt.j1, j2: nxt.j2 });
            k++;
        } else if (cur.tag === 'insert' && nxt && nxt.tag === 'delete') {
            out.push({ tag: 'replace', i1: nxt.i1, i2: nxt.i2, j1: cur.j1, j2: cur.j2 });
            k++;
        } else {
            out.push(cur);
        }
    }
    return out;
}

/** Token-level ops for one changed line pair, or null when the pair is too
 *  dissimilar (or too long) for word highlighting to help. Similarity is
 *  weighted by the CHARACTER length of non-whitespace tokens, so re-indenting a
 *  line does not read as a rewrite and a one-word change in a long line does
 *  not read as one either. */
function _wordDiff(before, after) {
    const at = _diffTokens(before);
    const bt = _diffTokens(after);
    if (at.length * bt.length > _DIFF_TOKEN_BUDGET) return null;
    const { ops } = _lcsOps(at, bt, _DIFF_TOKEN_BUDGET);
    const weight = (t) => (t.trim() ? t.length : 0);
    let common = 0;
    for (const op of ops) {
        if (op.tag !== 'equal') continue;
        for (let k = op.i1; k < op.i2; k++) common += weight(at[k]);
    }
    let total = 0;
    for (const t of at) total += weight(t);
    for (const t of bt) total += weight(t);
    const ratio = total ? (2 * common) / total : 1;
    return ratio < _DIFF_WORD_MIN_RATIO ? null : { ops, at, bt };
}

/** Aligned rows for two texts: one row per visual line pair.
 *  kind ∈ equal | chg | del | ins; `words` is the word pass for a `chg` row. */
function _diffRows(before, after) {
    const a = before.split('\n');
    const b = after.split('\n');
    const { ops, degraded } = _lcsOps(a, b, _DIFF_LINE_BUDGET);
    // When the alignment degraded, the pairing inside the single wholesale
    // `replace` is POSITIONAL and therefore arbitrary — marking words in it
    // would assert a relationship the aligner just failed to establish. It is
    // also the only shape that can put thousands of token diffs in one render.
    const wordPass = !degraded;
    const rows = [];
    for (const op of ops) {
        if (op.tag === 'equal') {
            for (let k = 0; k < op.i2 - op.i1; k++) {
                rows.push({
                    kind: 'equal',
                    an: op.i1 + k + 1, bn: op.j1 + k + 1,
                    a: a[op.i1 + k], b: b[op.j1 + k],
                });
            }
            continue;
        }
        if (op.tag === 'delete') {
            for (let i = op.i1; i < op.i2; i++) rows.push({ kind: 'del', an: i + 1, a: a[i] });
            continue;
        }
        if (op.tag === 'insert') {
            for (let j = op.j1; j < op.j2; j++) rows.push({ kind: 'ins', bn: j + 1, b: b[j] });
            continue;
        }
        // replace: pair positionally (difflib's own reading of a replace block),
        // then spill whatever is left as pure deletes or inserts.
        const pairs = Math.min(op.i2 - op.i1, op.j2 - op.j1);
        for (let k = 0; k < pairs; k++) {
            const av = a[op.i1 + k];
            const bv = b[op.j1 + k];
            rows.push({
                kind: 'chg',
                an: op.i1 + k + 1, bn: op.j1 + k + 1,
                a: av, b: bv,
                words: wordPass ? _wordDiff(av, bv) : null,
            });
        }
        for (let i = op.i1 + pairs; i < op.i2; i++) rows.push({ kind: 'del', an: i + 1, a: a[i] });
        for (let j = op.j1 + pairs; j < op.j2; j++) rows.push({ kind: 'ins', bn: j + 1, b: b[j] });
    }
    return { rows, degraded };
}

/** Group a row list into render items, folding long unchanged runs.
 *  A run at either end keeps context on its inward side only — there is nothing
 *  above the first change or below the last one to orient against. */
function _foldRows(rows, context) {
    const items = [];
    let i = 0;
    while (i < rows.length) {
        if (rows[i].kind !== 'equal') { items.push({ fold: false, row: rows[i] }); i++; continue; }
        let j = i;
        while (j < rows.length && rows[j].kind === 'equal') j++;
        const run = rows.slice(i, j);
        const head = i === 0 ? 0 : context;
        const tail = j === rows.length ? 0 : context;
        if (run.length <= head + tail + 1) {
            for (const r of run) items.push({ fold: false, row: r });
        } else {
            for (let k = 0; k < head; k++) items.push({ fold: false, row: run[k] });
            items.push({ fold: true, rows: run.slice(head, run.length - tail) });
            for (let k = run.length - tail; k < run.length; k++) items.push({ fold: false, row: run[k] });
        }
        i = j;
    }
    return items;
}

const _DIFF_SIGN = { equal: ' ', chg: '±', del: '−', ins: '+' };

/** One side of one row: line number (decorative) + sign + the text itself. */
function _diffCell(row, side) {
    const cell = document.createElement('div');
    const present = side === 'a' ? row.a !== undefined : row.b !== undefined;
    cell.className = `diff-sxs-cell side-${side}`;
    if (!present) {
        cell.classList.add('is-void');
        cell.setAttribute('aria-hidden', 'true');
        return cell;
    }
    const changed = row.kind === 'chg' || (side === 'a' ? row.kind === 'del' : row.kind === 'ins');
    if (changed) cell.classList.add(side === 'a' ? 'is-del' : 'is-add');

    const num = document.createElement('span');
    num.className = 'diff-sxs-ln';
    num.setAttribute('aria-hidden', 'true');
    num.textContent = String(side === 'a' ? row.an : row.bn);
    cell.appendChild(num);

    const sign = document.createElement('span');
    sign.className = 'diff-sxs-sign';
    // The sign is the non-colour channel, so it must say what THIS side did:
    // a changed pair is a removal on the left and an addition on the right.
    sign.textContent = !changed ? _DIFF_SIGN.equal
        : (side === 'a' ? _DIFF_SIGN.del : _DIFF_SIGN.ins);
    cell.appendChild(sign);

    const text = document.createElement('span');
    text.className = 'diff-sxs-text';
    const raw = side === 'a' ? row.a : row.b;
    if (row.kind === 'chg' && row.words) {
        const { ops, at, bt } = row.words;
        const toks = side === 'a' ? at : bt;
        for (const op of ops) {
            const from = side === 'a' ? op.i1 : op.j1;
            const to = side === 'a' ? op.i2 : op.j2;
            if (to <= from) continue;
            const chunk = toks.slice(from, to).join('');
            if (op.tag === 'equal') {
                text.appendChild(document.createTextNode(chunk));
            } else {
                const mark = document.createElement('span');
                mark.className = 'diff-sxs-word';
                mark.textContent = chunk;
                text.appendChild(mark);
            }
        }
    } else {
        text.textContent = raw;
    }
    cell.appendChild(text);
    return cell;
}

/**
 * Render a GitHub-style side-by-side diff of two texts as a detached element.
 *
 * `opts`: `beforeLabel` / `afterLabel` (column headings), `context` (unchanged
 * lines kept around a change before folding), `emptyMessage` (shown when the
 * two texts are identical).
 *
 * The caller owns placement; nothing here reads or writes global state, so the
 * same element type serves a report-table cell and a detail pane.
 */
export function renderSideBySideDiff(before, after, opts = {}) {
    const a = typeof before === 'string' ? before : '';
    const b = typeof after === 'string' ? after : '';
    const wrap = document.createElement('div');
    wrap.className = 'diff-sxs';

    if (a === b) {
        const p = document.createElement('p');
        p.className = 'diff-sxs-empty';
        p.textContent = opts.emptyMessage || 'No changes.';
        wrap.appendChild(p);
        return wrap;
    }

    const { rows, degraded } = _diffRows(a, b);
    const removed = rows.filter((r) => r.kind === 'del' || r.kind === 'chg').length;
    const added = rows.filter((r) => r.kind === 'ins' || r.kind === 'chg').length;

    wrap.setAttribute('role', 'group');
    wrap.setAttribute('aria-label',
        `Differences: ${removed} line(s) removed, ${added} line(s) added.`);

    const beforeLabel = opts.beforeLabel || 'Before';
    const afterLabel = opts.afterLabel || 'After';
    const head = document.createElement('div');
    head.className = 'diff-sxs-head';
    for (const [side, label] of [['a', beforeLabel], ['b', afterLabel]]) {
        const h = document.createElement('div');
        h.className = `diff-sxs-col-label side-${side}`;
        h.textContent = label;
        head.appendChild(h);
    }
    wrap.appendChild(head);

    // Below the stacking breakpoint the two columns become one interleaved
    // column, at which point a PAIR of column headings labels nothing — and
    // these headings carry the load-bearing counts (the caller passes "N
    // word(s) not in Zotero" / "N word(s) imported"), so a reader would take
    // them as describing whichever half sat under them. CSS hides the head
    // there and shows this single sentence instead; both are built here so the
    // text has one source.
    const caption = document.createElement('div');
    caption.className = 'diff-sxs-caption';
    caption.textContent = `${beforeLabel} → ${afterLabel}`;
    wrap.appendChild(caption);

    if (degraded) {
        const note = document.createElement('p');
        // Distinct modifier classes: a very long diff can be BOTH degraded
        // (alignment gave up) and clipped (too many blocks to draw), and the
        // two say different things, so neither may be mistaken for the other.
        note.className = 'diff-sxs-note is-degraded';
        note.textContent = 'Too many lines differ to align them — '
            + 'showing the two versions as a whole replacement.';
        wrap.appendChild(note);
    }

    const body = document.createElement('div');
    body.className = 'diff-sxs-body';
    const context = Number.isInteger(opts.context) && opts.context >= 0
        ? opts.context : _DIFF_CONTEXT;

    const rowEl = (row) => {
        const el = document.createElement('div');
        el.className = `diff-sxs-row is-${row.kind}`;
        el.appendChild(_diffCell(row, 'a'));
        el.appendChild(_diffCell(row, 'b'));
        return el;
    };

    // The cap applies to what is actually RENDERED, i.e. AFTER folding — not to
    // the aligned row list. A 3,000-line note with three edits folds to a
    // handful of rows and a couple of buttons, so capping earlier would clip a
    // diff that costs nothing to draw AND tell the reader lines were withheld
    // when they were not. (Caught by the existing "does not degrade a big note
    // whose edits are few" test, which is exactly the case.)
    const items = _foldRows(rows, context);
    const clipped = items.length > _DIFF_MAX_ROWS;
    const shownItems = clipped ? items.slice(0, _DIFF_MAX_ROWS) : items;
    if (clipped) {
        const note = document.createElement('p');
        note.className = 'diff-sxs-note is-clipped';
        // Counted in the units the reader sees. Named exactly, so they know
        // what they are NOT looking at rather than believing the diff ended
        // where the scrolling did.
        note.textContent = `This diff is too long to draw in full — showing `
            + `the first ${_DIFF_MAX_ROWS} of ${items.length} blocks. Open the `
            + 'file to review the rest.';
        // appendChild, not insertBefore(note, body): `body` is not in the
        // wrapper yet at this point, so insertBefore threw. Appending here
        // puts the note above the rows anyway, which is where a warning
        // about what is missing belongs.
        wrap.appendChild(note);
    }

    for (const item of shownItems) {
        if (!item.fold) { body.appendChild(rowEl(item.row)); continue; }
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'diff-sxs-fold';
        btn.textContent = `Show ${item.rows.length} unchanged line(s)`;
        btn.setAttribute('aria-expanded', 'false');
        // Toggle, not a one-way `replaceWith`: on a long note the expanded run
        // can be hundreds of lines, and there was no way back short of
        // re-rendering the whole diff. The rows are kept and hidden rather than
        // rebuilt so re-collapsing costs nothing.
        let shownRows = null;
        btn.addEventListener('click', () => {
            if (shownRows) {
                for (const el of shownRows) el.remove();
                shownRows = null;
                btn.textContent = `Show ${item.rows.length} unchanged line(s)`;
                btn.setAttribute('aria-expanded', 'false');
                announceStatus(`${item.rows.length} unchanged line(s) hidden.`);
                return;
            }
            shownRows = item.rows.map(rowEl);
            const frag = document.createDocumentFragment();
            for (const el of shownRows) frag.appendChild(el);
            btn.after(frag);
            btn.textContent = `Hide ${item.rows.length} unchanged line(s)`;
            btn.setAttribute('aria-expanded', 'true');
            announceStatus(`${item.rows.length} unchanged line(s) shown.`);
        });
        body.appendChild(btn);
    }
    wrap.appendChild(body);
    return wrap;
}

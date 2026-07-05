/**
 * Shared UI primitives — the activity bar, accessibility announcers, recoverable
 * error boundaries, modal/tab/tablist wiring, the provider badge, clipboard, and
 * the HTML sanitiser. A LEAF module in the JS module hierarchy: it imports only
 * api.js (for logError) and nothing else from the project, so every feature
 * module can depend on it without a circular import. `updateProviderBadge` lives
 * here precisely so config.js and app.js can both import it without a cycle.
 */
import { logError } from './api.js';

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
export function confirmInline(anchorEl, { message, confirmLabel, onConfirm, cancelLabel = 'Cancel' }) {
    if (!anchorEl || typeof onConfirm !== 'function') return null;
    const prev = anchorEl.parentElement
        ? anchorEl.parentElement.querySelector(':scope > .inline-confirm-strip')
        : null;
    if (prev) prev.remove();

    const strip = document.createElement('div');
    strip.className = 'inline-confirm-strip';
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
    yes.className = 'btn btn-danger btn-sm';
    yes.textContent = confirmLabel || 'Confirm';
    yes.addEventListener('click', () => { strip.remove(); onConfirm(); });
    // Cancel first in DOM and focus order — the safe default gets the
    // keyboard; the destructive choice is a deliberate extra Tab away.
    row.appendChild(cancel);
    row.appendChild(yes);
    strip.appendChild(row);

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
            b.addEventListener('click', () => { if (a.onClick) a.onClick(); });
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
    const initialFocusable = overlay.querySelectorAll(_MODAL_FOCUSABLE_SELECTOR);
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
            const focusable = Array.from(overlay.querySelectorAll(_MODAL_FOCUSABLE_SELECTOR));
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

export function showTab(tabId) {
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
    if (contentArea) contentArea.classList.add('active');
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

export function isOnlineProvider(provider) {
    return provider === 'openai' || provider === 'anthropic' || provider === 'google';
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
    doc.querySelectorAll('script, style, iframe, object, embed, form').forEach(el => el.remove());
    doc.querySelectorAll('*').forEach(el => {
        Array.from(el.attributes).forEach(attr => {
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

import { logError } from './api.js';

const _activeTasks = new Map();
let _modalTrigger = null;
let _modalKeyHandler = null;
const _MODAL_FOCUSABLE_SELECTOR = 'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href]';

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

export function openModal(modalId) {
    _modalTrigger = document.activeElement;
    const overlay = document.getElementById(modalId);
    if (!overlay) return;
    overlay.classList.add('open');
    const initialFocusable = overlay.querySelectorAll(_MODAL_FOCUSABLE_SELECTOR);
    if (initialFocusable.length > 0) initialFocusable[0].focus();

    // Esc closes the modal; Tab/Shift+Tab cycles inside it so keyboard focus
    // cannot escape into the underlying page while the dialog is open.
    if (_modalKeyHandler) {
        document.removeEventListener('keydown', _modalKeyHandler);
    }
    _modalKeyHandler = (e) => {
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
    document.addEventListener('keydown', _modalKeyHandler);
}

export function closeModal(modalId) {
    const overlay = document.getElementById(modalId);
    if (overlay) overlay.classList.remove('open');
    if (_modalKeyHandler) {
        document.removeEventListener('keydown', _modalKeyHandler);
        _modalKeyHandler = null;
    }
    if (_modalTrigger) {
        _modalTrigger.focus();
        _modalTrigger = null;
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

const _PROVIDER_COLORS = {
    ollama: '#34c759',
    lm_studio: '#007aff',
    openai: '#10a37f',
    anthropic: '#d97757',
    google: '#4285f4',
};

export function updateProviderBadge(provider) {
    const label = _PROVIDER_LABELS[provider] || provider || 'Ollama';
    const color = _PROVIDER_COLORS[provider] || '#34c759';

    const badge = document.getElementById('provider-badge');
    if (badge) {
        badge.style.color = color;
        badge.style.borderColor = color;
    }
    const dot = document.getElementById('provider-badge-dot');
    if (dot) dot.style.background = color;
    const lbl = document.getElementById('provider-badge-label');
    if (lbl) lbl.textContent = label;
}

export function isOnlineProvider(provider) {
    return provider === 'openai' || provider === 'anthropic' || provider === 'google';
}

export function copyToClipboard(text, btn) {
    navigator.clipboard.writeText(text).then(() => {
        const orig = btn.textContent;
        btn.textContent = '\u2713 Copied';
        setTimeout(() => { btn.textContent = orig; }, 2000);
    }).catch(() => {
        const orig = btn.textContent;
        btn.textContent = '\u2717 Copy failed';
        setTimeout(() => { btn.textContent = orig; }, 2500);
    });
}

export function sanitiseHtml(html) {
    const doc = new DOMParser().parseFromString(html || '', 'text/html');
    doc.querySelectorAll('script, style, iframe, object, embed, form').forEach(el => el.remove());
    doc.querySelectorAll('*').forEach(el => {
        Array.from(el.attributes).forEach(attr => {
            if (attr.name.startsWith('on') || attr.value.toLowerCase().trimStart().startsWith('javascript:')) {
                el.removeAttribute(attr.name);
            }
        });
    });
    return doc.body.innerHTML;
}

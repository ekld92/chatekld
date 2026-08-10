/**
 * Plain Chat panel — a RAG-free, multi-turn conversation with the configured
 * LLM. The client owns the conversation: it keeps the {role, content} history
 * array and sends the last N turns on every send. The server is stateless.
 *
 * Imports only from ui.js + api.js (JS Module Hierarchy rule). `marked` is a
 * global loaded via a vendored <script> tag, referenced the same way vault.js
 * does — never imported.
 */
import { secureFetch, consumeSSE } from './api.js';
import { sanitiseHtml, copyToClipboard, enhanceCodeBlocks, showTaskError, clearTaskError, makeAnswerRenderer, announceStatus, announceError, buildConversationExport } from './ui.js';

let _history = [];          // {role: 'user'|'assistant', content: string}[]
let _isQuerying = false;

const _MAX_TURNS = 20;     // mirrors the backend cap
// No agent wall-clock here — the stream's only time guard is the server-side
// consumer get (floor 300 + 30 margin = 330s), so the frontend abort is floored
// above it to keep the timeout chain ordered (server consumer ≤ frontend abort).
const _CHAT_STALL_FLOOR_S = 300;
const _CHAT_TIMEOUT_MARGIN_S = 60;
// Muted text shown when the model returns nothing. Display-only — it is NEVER
// pushed into `_history`, so it cannot be re-sent to the model as a fake
// assistant turn on the next request (the backend deliberately sends no
// placeholder token for the same reason).
const _NO_RESPONSE_MSG = '(No response from the model.)';

// Temperature and system prompt are NOT sent from here: the server resolves
// them from config (chat_temperature / chat_system_prompt) on every request, so
// a change made in the LLM Settings modal takes effect on the next Send with no
// reload (mirrors the paper_* Single-Paper pattern). Caching them client-side
// at init would go stale the moment the user edited Settings.

function _abortMs() {
    return (_CHAT_STALL_FLOOR_S + _CHAT_TIMEOUT_MARGIN_S) * 1000;
}

// Toggle the Send button while a turn is in flight. The `_isQuerying` guard
// already blocks a concurrent send functionally; this is the matching visual
// affordance (mirrors the vault Send button being disabled mid-request).
function _setSending(on) {
    const btn = document.getElementById('plainchat-send-btn');
    if (btn) btn.disabled = !!on;
}

function _attachCopyButton(messageEl, getText, ariaLabel) {
    // Idempotent: a re-render mid-stream should not stack buttons.
    const existing = messageEl.querySelector(':scope > .copy-btn');
    if (existing) existing.remove();
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'copy-btn';
    btn.textContent = 'Copy';
    btn.setAttribute('aria-label', ariaLabel || 'Copy message');
    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const text = typeof getText === 'function' ? getText() : String(getText || '');
        if (text) copyToClipboard(text, btn);
    });
    messageEl.appendChild(btn);
    return btn;
}


// Surface a recoverable error in the shared boundary. `retry`, when given,
// becomes a "Retry" button; a "Dismiss" button always clears it.
function _showError(message, retry) {
    const el = document.getElementById('plainchat-error-boundary');
    const actions = [];
    if (typeof retry === 'function') {
        actions.push({ label: 'Retry', primary: true, onClick: () => { clearTaskError(el); retry(); } });
    }
    actions.push({ label: 'Dismiss', onClick: () => clearTaskError(el) });
    showTaskError(el, message, actions);
}

/**
 * Send the input as a new user turn and stream the assistant reply. Pushes the
 * user turn into `_history` before the request (so it is part of the sent
 * context), sends only `_history.slice(-20)`, and consumes the SSE `{info}` /
 * `{token}` / `{error}` stream. On success the assistant turn is recorded; on
 * error the un-answered user turn is rolled back so a retry re-sends it exactly
 * once; an empty-but-clean stream shows a muted placeholder that is NOT recorded.
 */
export async function chatPlain(retryText) {
    if (_isQuerying) return;
    const input = document.getElementById('plainchat-input');
    // Item 3.5: a Retry passes the failed message DIRECTLY (string guard —
    // DOM handlers call this with an Event) so it never overwrites a fresh
    // draft the user typed after the failure.
    const fromRetry = typeof retryText === 'string' && retryText.trim() !== '';
    const text = fromRetry ? retryText.trim() : (input ? input.value.trim() : '');
    if (!text) return;
    // Resolve the transcript container BEFORE locking the UI. It is appended to
    // unconditionally below (outside the try/finally), so a missing element here
    // would throw with `_isQuerying`/Send-disabled already set and no finally to
    // clear them — permanently wedging the panel. Bail cleanly instead.
    const chat = document.getElementById('plainchat-history');
    if (!chat) return;

    _isQuerying = true;
    _setSending(true);
    clearTaskError(document.getElementById('plainchat-error-boundary'));

    const userMsg = document.createElement('div');
    userMsg.className = 'message message-user';
    userMsg.textContent = text;
    chat.appendChild(userMsg);
    _attachCopyButton(userMsg, () => text, 'Copy message');
    if (input && !fromRetry) input.value = '';   // a retry leaves the draft untouched

    // Push the user turn before the request so it is part of the sent history;
    // it is rolled back on failure so a retry does not duplicate it.
    _history.push({ role: 'user', content: text });

    const botMsg = document.createElement('div');
    botMsg.className = 'message message-bot';
    botMsg.innerHTML = '<span class="typing-indicator"><span></span></span>';
    chat.appendChild(botMsg);
    chat.scrollTop = chat.scrollHeight;
    const answerRenderer = makeAnswerRenderer(botMsg);
    // Bind the scroll update to the renderer
    const _oldUpdate = answerRenderer.update.bind(answerRenderer);
    answerRenderer.update = (fullAnswer) => {
        _oldUpdate(fullAnswer);
        chat.scrollTop = chat.scrollHeight;
    };

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), _abortMs());

    // Track 6b: announce the generation lifecycle (streamed tokens are
    // otherwise silent to AT; errors announce via showTaskError).
    announceStatus('Generating response…');

    let fullAnswer = '';
    // Export provenance (2026-07-22): the server announces the resolved
    // provider/model ({"meta"}) and, post-stream, the real token/cost figures
    // ({"usage"}); both are stored on the assistant turn for MD/TXT exports.
    let turnMeta = null;
    let turnUsage = null;
    try {
        // The server reads only role/content; the provenance fields riding on
        // stored turns are ignored by _validate_messages.
        const body = { messages: _history.slice(-_MAX_TURNS) };

        await consumeSSE('/api/plainchat', {
            method: 'POST',
            signal: controller.signal,
            body: JSON.stringify(body),
        }, {
            onInfo: (info) => {
                const note = document.createElement('div');
                note.className = 'message message-info';
                note.textContent = info;
                chat.insertBefore(note, botMsg);
                chat.scrollTop = chat.scrollHeight;
                // Announced via the STATIC live region — a role minted in the
                // same tick as the textContent write is unreliable in WebKit
                // (the liveRegions ratchet class; parity with vault.js onInfo).
                announceStatus(info);
            },
            onToken: (token) => {
                if (fullAnswer === '') botMsg.innerHTML = '';
                fullAnswer += token;
                answerRenderer.update(fullAnswer);
            },
            onOther: (frame) => {
                if (frame && frame.meta) turnMeta = frame.meta;
                if (frame && frame.usage) turnUsage = frame.usage;
            },
            onError: (errMsg) => { throw new Error(errMsg); }
        });

        answerRenderer.flush();

        if (fullAnswer) {
            const turn = { role: 'assistant', content: fullAnswer };
            if (turnMeta) turn.meta = turnMeta;
            if (turnUsage) turn.usage = turnUsage;
            _history.push(turn);
            _attachCopyButton(botMsg, () => fullAnswer, 'Copy answer');
            enhanceCodeBlocks(botMsg);
        } else {
            botMsg.textContent = _NO_RESPONSE_MSG;
        }
        announceStatus('Response ready.');
    } catch (e) {
        if (fullAnswer) {
            answerRenderer.flush();
            const note = document.createElement('div');
            note.className = 'muted';
            note.textContent = '⚠ Response interrupted — the text above is partial.';
            botMsg.appendChild(note);
            _attachCopyButton(botMsg, () => fullAnswer, 'Copy answer');
        } else {
            answerRenderer.cancel();
            botMsg.remove();
        }
        if (_history.length && _history[_history.length - 1].role === 'user') _history.pop();
        // 2026-07-20 audit A2: test the DOMException NAME (consumeSSE now
        // re-throws the abort untouched), not e.message — which was never
        // 'AbortError' even before, so the friendly branch was dead.
        const msg = e.name === 'AbortError'
            ? 'Response timed out. The model may be overloaded — please try again.'
            : ('Error: ' + e.message);
        _showError(msg, () => {
            if (!fullAnswer) userMsg.remove();
            chatPlain(text);
        });
    } finally {
        clearTimeout(timeout);
        _isQuerying = false;
        _setSending(false);
        chat.scrollTop = chat.scrollHeight;
    }
}

/**
 * Start a fresh conversation: clear the _history array and the rendered transcript
 * (the conversation is ephemeral — nothing is persisted). Refuses mid-stream.
 */
export function newChat() {
    // Refuse while a query is in flight so the in-progress bot message is not
    // orphaned mid-stream.
    if (_isQuerying) {
        // C5/#43 (2026-07-13): announce instead of silently doing nothing
        // (parity with vault chat's "wait for the current response" hint).
        announceStatus('Wait for the current response before starting a new chat.');
        return;
    }
    _history = [];
    const chat = document.getElementById('plainchat-history');
    if (chat) chat.innerHTML = '';
    clearTaskError(document.getElementById('plainchat-error-boundary'));
}

/**
 * Export the current plain-chat conversation to Downloads as md or txt
 * (axis iv). The browser-owned ``_history`` array is already a full transcript;
 * this serialises it and reuses the /api/export-summary writer.
 */
export async function exportPlainConversation(format, btn) {
    // C5/#17 (2026-07-13): flash the outcome ON the clicked button so a sighted
    // user sees success/failure — the SR-only announcements alone were invisible.
    const flash = (label) => {
        if (!btn) return;
        const orig = btn.dataset.origLabel || btn.textContent;
        btn.dataset.origLabel = orig;
        btn.textContent = label;
        setTimeout(() => { btn.textContent = btn.dataset.origLabel || orig; }, 2500);
    };
    if (!_history.length) {
        announceStatus('No conversation to export yet.');
        flash('Nothing to export');
        return;
    }
    // Header: export date + the last answered turn's provider/model (the
    // conversation may span a provider switch; per-turn lines carry the rest).
    const lastMeta = [..._history].reverse().find((t) => t.meta)?.meta || null;
    const header = { 'Exported': new Date().toISOString() };
    if (lastMeta) header['Model'] = [lastMeta.provider, lastMeta.model].filter(Boolean).join(' / ');
    const { format: fmt, content } = buildConversationExport(
        _history, format, 'Plain Chat Conversation', { header });
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    try {
        // raw: true — see vault.js's exporter: no Summary_ prefix, no second
        // injected heading; the transcript already carries its own title.
        const r = await secureFetch('/api/export-summary', {
            method: 'POST',
            body: JSON.stringify({ format: fmt, filename: `PlainChat_${stamp}`, content, raw: true }),
        });
        const d = await r.json().catch(() => ({}));
        if (r.ok) {
            announceStatus(`Exported to ${d.path || 'Downloads'}`);
            flash('Exported ✓');
        } else {
            // Failures go through the assertive error region, not the polite
            // status one — parity with the rest of the app's error surfacing.
            announceError(d.error || `Export failed (${r.status})`);
            flash('Export failed');
        }
    } catch (err) {
        announceError(String(err));
        flash('Export failed');
    }
}

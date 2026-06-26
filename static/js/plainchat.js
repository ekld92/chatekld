/**
 * Plain Chat panel — a RAG-free, multi-turn conversation with the configured
 * LLM. The client owns the conversation: it keeps the {role, content} history
 * array and sends the last N turns on every send. The server is stateless.
 *
 * Imports only from ui.js + api.js (JS Module Hierarchy rule). `marked` is a
 * global loaded via a vendored <script> tag, referenced the same way vault.js
 * does — never imported.
 */
import { secureFetch, readSSE } from './api.js';
import { sanitiseHtml, copyToClipboard, showTaskError, clearTaskError } from './ui.js';

let history = [];          // {role: 'user'|'assistant', content: string}[]
let _isQuerying = false;

const _MAX_TURNS = 20;     // mirrors the backend cap
// No agent wall-clock here — the stream's only time guard is the server-side
// consumer get (floor 300 + 30 margin = 330s), so the frontend abort is floored
// above it to keep the timeout chain ordered (server consumer ≤ frontend abort).
const _CHAT_STALL_FLOOR_S = 300;
const _CHAT_TIMEOUT_MARGIN_S = 60;
// Muted text shown when the model returns nothing. Display-only — it is NEVER
// pushed into `history`, so it cannot be re-sent to the model as a fake
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

function _renderAnswer(botMsg, fullAnswer) {
    // marked is vendored locally; a load failure must never cost the user the
    // answer — fall back to plain text rather than throwing mid-stream.
    if (typeof marked !== 'undefined' && typeof marked.parse === 'function') {
        botMsg.innerHTML = sanitiseHtml(marked.parse(fullAnswer));
    } else {
        botMsg.textContent = fullAnswer;
    }
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
 * user turn into `history` before the request (so it is part of the sent
 * context), sends only `history.slice(-20)`, and consumes the SSE `{info}` /
 * `{token}` / `{error}` stream. On success the assistant turn is recorded; on
 * error the un-answered user turn is rolled back so a retry re-sends it exactly
 * once; an empty-but-clean stream shows a muted placeholder that is NOT recorded.
 */
export async function chatPlain() {
    if (_isQuerying) return;
    const input = document.getElementById('plainchat-input');
    const text = input ? input.value.trim() : '';
    if (!text) return;

    _isQuerying = true;
    _setSending(true);
    clearTaskError(document.getElementById('plainchat-error-boundary'));
    const chat = document.getElementById('plainchat-history');

    const userMsg = document.createElement('div');
    userMsg.className = 'message message-user';
    userMsg.textContent = text;
    chat.appendChild(userMsg);
    _attachCopyButton(userMsg, () => text, 'Copy message');
    if (input) input.value = '';

    // Push the user turn before the request so it is part of the sent history;
    // it is rolled back on failure so a retry does not duplicate it.
    history.push({ role: 'user', content: text });

    const botMsg = document.createElement('div');
    botMsg.className = 'message message-bot';
    botMsg.innerHTML = '<span class="typing-indicator"><span></span></span>';
    chat.appendChild(botMsg);
    chat.scrollTop = chat.scrollHeight;

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), _abortMs());

    let fullAnswer = '';
    try {
        const body = { messages: history.slice(-_MAX_TURNS) };

        const resp = await secureFetch('/api/plainchat', {
            method: 'POST',
            signal: controller.signal,
            body: JSON.stringify(body),
        });
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            throw new Error(data.error || 'Chat request failed.');
        }

        for await (const payload of readSSE(resp)) {
            if (payload.error) {
                throw new Error(payload.error);
            } else if (payload.info) {
                const note = document.createElement('div');
                note.className = 'message message-info';
                note.setAttribute('role', 'status');
                note.textContent = payload.info;
                chat.insertBefore(note, botMsg);
                chat.scrollTop = chat.scrollHeight;
            } else if (payload.token) {
                if (fullAnswer === '') botMsg.innerHTML = '';
                fullAnswer += payload.token;
                _renderAnswer(botMsg, fullAnswer);
                chat.scrollTop = chat.scrollHeight;
            }
        }

        if (fullAnswer) {
            history.push({ role: 'assistant', content: fullAnswer });
            // Attach the copy button only after streaming completes so the
            // per-token innerHTML re-render does not clobber it. Copies the raw
            // markdown rather than the rendered HTML.
            _attachCopyButton(botMsg, () => fullAnswer, 'Copy answer');
        } else {
            // Empty-but-clean stream: the model produced no tokens. Settle the
            // bubble (replacing the typing indicator) with a muted placeholder
            // that is deliberately NOT recorded in `history` — so it cannot be
            // re-sent as a fake assistant turn next request.
            botMsg.textContent = _NO_RESPONSE_MSG;
        }
    } catch (e) {
        botMsg.remove();
        // Roll back the un-answered user turn so a retry re-sends it exactly once.
        if (history.length && history[history.length - 1].role === 'user') history.pop();
        const msg = e.name === 'AbortError'
            ? 'Response timed out. The model may be overloaded — please try again.'
            : ('Error: ' + e.message);
        _showError(msg, () => {
            userMsg.remove();
            if (input) input.value = text;
            chatPlain();
        });
    } finally {
        clearTimeout(timeout);
        _isQuerying = false;
        _setSending(false);
        chat.scrollTop = chat.scrollHeight;
    }
}

/**
 * Start a fresh conversation: clear the history array and the rendered transcript
 * (the conversation is ephemeral — nothing is persisted). Refuses mid-stream.
 */
export function newChat() {
    // Refuse while a query is in flight so the in-progress bot message is not
    // orphaned mid-stream.
    if (_isQuerying) return;
    history = [];
    const chat = document.getElementById('plainchat-history');
    if (chat) chat.innerHTML = '';
    clearTaskError(document.getElementById('plainchat-error-boundary'));
}

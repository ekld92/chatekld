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
import { sanitiseHtml, copyToClipboard, enhanceCodeBlocks, showTaskError, clearTaskError, makeAnswerRenderer, announceStatus, announceError, buildConversationExport, confirmInline } from './ui.js';

let _chatController = null;
let _stopRequested = false;

export function stopChat() {
    if (!_chatController) return;
    _stopRequested = true;
    const button = document.getElementById('plainchat-stop-btn');
    if (button) {
        if (document.activeElement === button) document.getElementById('plainchat-input')?.focus();
        button.disabled = true; button.textContent = 'Stop requested…';
    }
    announceStatus('Stop requested. The provider may still be finishing its current operation.');
    _chatController.abort();
}

let _history = [];          // {role: 'user'|'assistant', content: string}[]
let _isQuerying = false;

// Files attached to the turn CURRENTLY BEING COMPOSED. Each entry is the
// server's own answer from POST /api/chat-attachment — including how much of
// the file was actually kept — never anything this file inferred.
// Emptied when the turn goes on record, NOT when it is sent: a send that fails
// and rolls its user turn back must leave the attachments in the composer, or
// the retry would silently ask about a document it no longer carries.
let _attachments = [];
let _uploading = 0;

const _MAX_TURNS = 20;     // mirrors the backend cap
// Mirrors CHAT_ATTACHMENT_MAX_PER_MESSAGE. Enforced client-side ONLY to tell
// the user before they pay for an extraction; the server re-checks and is the
// authority (a client cannot widen it).
const _MAX_ATTACHMENTS = 4;
// Mirrors CHAT_ATTACHMENT_REQUEST_MAX_CHARS. Same rule as _MAX_ATTACHMENTS: the
// server re-checks and is the authority, this exists only so the user is told
// BEFORE paying for an extraction. It matters here because the two server
// bounds do not compose — at the default 120 000-character per-file cap, two
// files already exceed this, so "up to four files" is reachable only for small
// ones, and the refusal used to arrive after four extractions had been paid for.
const _MAX_REQUEST_CHARS = 240000;
// The untrusted wrapper (preamble + nonce'd tags + the truncation note) adds a
// few hundred characters per file that `attached_chars` does not count. Round
// up generously: this is a warning, and warning slightly early is better than
// letting the user pay for an extraction the server will then refuse.
const _WRAPPER_OVERHEAD_CHARS = 400;

function _overBudgetMessage(tail) {
    return 'The files in this conversation add up to more text than one request '
        + `can carry (${Math.round(_MAX_REQUEST_CHARS / 1000)}k characters). ${tail}`;
}

/** Characters the next send would carry: the composer plus every recorded turn.
 *  The server sums across the whole 20-turn window, not just this message. */
function _projectedAttachmentChars() {
    let total = 0;
    const add = (list) => {
        for (const a of list || []) {
            const n = Number(a && a.attached_chars);
            if (Number.isFinite(n)) total += n + _WRAPPER_OVERHEAD_CHARS;
        }
    };
    for (const turn of _history.slice(-_MAX_TURNS)) add(turn.attachments);
    add(_attachments.filter((a) => !a.pending));
    return total;
}
// Muted text shown when the model returns nothing. Display-only — it is NEVER
// pushed into `_history`, so it cannot be re-sent to the model as a fake
// assistant turn on the next request (the backend deliberately sends no
// placeholder token for the same reason).
const _NO_RESPONSE_MSG = '(No response from the model.)';

// Temperature and system prompt are NOT sent from here: the server resolves
// them from config (chat_temperature / chat_system_prompt) on every request, so
// a change made in the Settings modal takes effect on the next Send with no
// reload (mirrors the paper_* Single-Paper pattern). Caching them client-side
// at init would go stale the moment the user edited Settings.

// Toggle the Send button while a turn is in flight. The `_isQuerying` guard
// already blocks a concurrent send functionally; this is the matching visual
// affordance (mirrors the vault Send button being disabled mid-request).
function _setSending(on) {
    const stop = document.getElementById('plainchat-stop-btn');
    if (stop) {
        if (!_isQuerying && document.activeElement === stop) document.getElementById('plainchat-input')?.focus();
        stop.hidden = !_isQuerying; stop.disabled = false; stop.textContent = 'Stop';
    }
    // Fold in `_uploading` FIRST. Attaching runs an extraction (a bounded PDF
    // subprocess, or a vision call), so both buttons are held while one is in
    // flight: sending mid-upload would ask about a document the request does
    // not yet carry, and attaching mid-upload opens a SECOND concurrent
    // extraction — which `_uploadOne` documents as impossible ("sequential by
    // caller"). It was: `addAttachments` calls `_setSending(_isQuerying)`,
    // which is false when idle, and the attach line below read the RAW
    // argument while only the Send line saw the folded value (2026-08-30).
    on = on || _uploading > 0;
    const attachBtn = document.getElementById('plainchat-attach-btn');
    if (attachBtn) attachBtn.disabled = !!on;
    const btn = document.getElementById('plainchat-send-btn');
    if (!btn) return;
    // Disabling the focused element drops focus to <body> (2026-08-18):
    // park it on the input first so keyboard/SR position survives.
    if (on && document.activeElement === btn) {
        const input = document.getElementById('plainchat-input');
        if (input) input.focus();
    }
    btn.disabled = !!on;
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
/* --- Attachments ---------------------------------------------------------
 *
 * A file attached here is EXTRACTED TO TEXT BY THE SERVER at upload time
 * (services/attachment_service.py) and referenced afterwards by id. This panel
 * never holds or sends file bytes, and never sends extracted text: the server
 * owns what reaches the model, the same posture as the refactor tab's staged
 * proposals.
 *
 * Uploading on ATTACH rather than on SEND is deliberate. It costs a visible
 * wait, and buys the user the answer to "did it actually read this?" before
 * they spend a turn on it — including how much of a long document was kept.
 * Deferring to send time would hide a failed extraction inside a chat request.
 */

// Mirrors CHAT_ATTACHMENT_EXTS and the file input's `accept`. `.jpeg`
// was missing here while both of those accepted it, so the one place
// that TELLS the user what works under-reported it.
const _ACCEPTED_HINT = '.pdf, .png, .jpg, .jpeg, .md, .txt';

function _fmtChars(n) {
    return n >= 1000 ? `${Math.round(n / 1000)}k chars` : `${n} chars`;
}

/** Repaint the composer chip strip from `_attachments`. Pure DOM, no innerHTML:
 *  filenames are user-controlled. */
function _renderAttachmentStrip() {
    const strip = document.getElementById('plainchat-attachments');
    if (!strip) return;
    // Remember which chip's Remove button had focus. The strip is wiped and
    // rebuilt on every upload, so a user tabbed onto a × when the next file
    // lands was dropped to <body> — the same focus-loss the audit deck's
    // `_accFocusAfter` chain exists to prevent, on a surface that had no
    // equivalent. Restored by attachment id, because the chip's POSITION can
    // change between renders while its identity does not.
    const active = document.activeElement;
    const refocusId = (active && strip.contains(active)
                       && active.dataset && active.dataset.attId)
        ? active.dataset.attId : null;
    strip.textContent = '';
    for (const att of _attachments) {
        const chip = document.createElement('span');
        chip.className = 'chat-attachment-chip';
        if (att.pending) chip.classList.add('is-pending');

        const name = document.createElement('span');
        name.className = 'chip-name';
        name.textContent = att.filename;
        // The full name in a tooltip, because the chip clips at 22ch and the
        // distinguishing part of a citation-key filename is often the tail.
        name.title = att.filename;
        chip.appendChild(name);

        const meta = document.createElement('span');
        meta.className = 'chip-meta';
        meta.textContent = att.pending ? 'reading…' : _fmtChars(att.attached_chars);
        chip.appendChild(meta);

        if (att.truncated) {
            const warn = document.createElement('span');
            warn.className = 'chip-trunc';
            // Stated on the chip, not only in the transient status line: this
            // is a standing fact about what the model can answer from.
            //
            // A PAGE-bounded read is reported in pages, because the character
            // total then describes only what was EXTRACTED, not the document —
            // saying "first 45k of 45k chars" about 151 pages of a 2,000-page
            // PDF would read as "all of it".
            warn.textContent = att.pages_read
                ? `· first ${att.pages_read} of ${att.pages_total} pages`
                : `· first ${_fmtChars(att.attached_chars)} of ${_fmtChars(att.chars)}`;
            warn.title = 'Only the beginning of this file is sent. Raise '
                + '"Attachment text limit" in Settings to send more.';
            chip.appendChild(warn);
        }

        if (!att.pending) {
            const rm = document.createElement('button');
            rm.type = 'button';
            rm.className = 'chip-remove';
            rm.textContent = '×';
            rm.setAttribute('aria-label', `Remove attachment ${att.filename}`);
            rm.dataset.attId = att.attachment_id;
            rm.addEventListener('click', () => _removeAttachment(att.attachment_id));
            chip.appendChild(rm);
        }
        strip.appendChild(chip);
    }
    if (refocusId) {
        // Compared by VALUE rather than built into a selector: `CSS.escape`
        // does not exist in jsdom (so the whole restore threw there, silently
        // taking the rebuilt strip with it), and an id should not have to be
        // selector-safe for focus to survive a repaint.
        const back = [...strip.querySelectorAll('.chip-remove')]
            .find((b) => b.dataset.attId === refocusId);
        // Only if that chip still exists. If the focused attachment was the one
        // removed, focus belongs on the composer, not on a neighbour's × the
        // user never aimed at.
        if (back) back.focus();
        else {
            const input = document.getElementById('plainchat-input');
            if (input) input.focus();
        }
    }
}

/** The read-only echo of what rode a turn, rendered under the sent message so
 *  the transcript still says which files the answer was written from. */
function _renderTurnAttachments(messageEl, attachments) {
    const strip = document.createElement('div');
    strip.className = 'chat-attachments';
    for (const att of attachments) {
        const chip = document.createElement('span');
        chip.className = 'chat-attachment-chip';
        const name = document.createElement('span');
        name.className = 'chip-name';
        name.textContent = att.filename;
        name.title = att.filename;
        chip.appendChild(name);
        const meta = document.createElement('span');
        meta.className = 'chip-meta';
        meta.textContent = att.truncated
            ? (att.pages_read
                ? `first ${att.pages_read} of ${att.pages_total} pages`
                : `first ${_fmtChars(att.attached_chars)} of ${_fmtChars(att.chars)}`)
            : _fmtChars(att.attached_chars);
        chip.appendChild(meta);
        strip.appendChild(chip);
    }
    messageEl.appendChild(strip);
}

/** Drop the composer strip WITHOUT deleting the rows: a turn already on record
 *  re-sends its ids on every following turn, so the server must still resolve
 *  them. Orphans are swept by the server's 24 h TTL. */
function _clearComposerAttachments() {
    _attachments = [];
    _renderAttachmentStrip();
}

async function _removeAttachment(id) {
    const i = _attachments.findIndex((a) => a.attachment_id === id);
    if (i === -1) return;
    const [gone] = _attachments.splice(i, 1);
    _renderAttachmentStrip();
    announceStatus(`Removed attachment ${gone.filename}.`);
    // Best-effort: the strip is already updated, and the server's TTL sweeps a
    // row whose DELETE failed. Failing the removal visibly would be worse — the
    // user asked for it gone from THIS turn, which has happened.
    try {
        await secureFetch(`/api/chat-attachment/${encodeURIComponent(id)}`, { method: 'DELETE' });
    } catch (_) { /* swept by TTL */ }
}

/** Upload one file and put its chip in the strip.
 *
 *  Sequential by caller, so a multi-file drop cannot open N concurrent
 *  extractions (a PDF extraction is a bounded subprocess and an image is a
 *  vision call — both are expensive). That was only true WITHIN one
 *  `addAttachments` call until 2026-08-30: `_setSending` left the Attach button
 *  enabled during an upload, so a second click or drop started a concurrent
 *  one. See the `_uploading` fold at the top of `_setSending` — it is what
 *  makes this sentence true. */
async function _uploadOne(file) {
    const placeholder = { attachment_id: `pending-${Date.now()}-${Math.random()}`,
                          filename: file.name, pending: true };
    _attachments.push(placeholder);
    _renderAttachmentStrip();
    const drop = () => {
        const i = _attachments.indexOf(placeholder);
        if (i !== -1) _attachments.splice(i, 1);
    };
    try {
        const form = new FormData();
        form.append('file', file);
        // Raw fetch, NOT secureFetch: secureFetch forces
        // `Content-Type: application/json`, which on a FormData body suppresses
        // the browser's own multipart boundary and makes the part unparseable
        // server-side. The origin-guard header is supplied by hand instead —
        // the same exception summarizer.js's upload documents.
        const r = await fetch('/api/chat-attachment', {
            method: 'POST',
            headers: { 'X-Requested-With': 'ChatEKLD' },
            body: form,
        });
        const d = await r.json().catch(() => ({}));
        drop();
        if (!r.ok) {
            _renderAttachmentStrip();
            announceError(d.error || `Could not attach ${file.name} (${r.status})`);
            _showError(d.error || `Could not attach ${file.name}.`);
            return;
        }
        _attachments.push(d);
        _renderAttachmentStrip();
        announceStatus(d.truncated
            ? `Attached ${d.filename}: the first ${d.attached_chars} of ${d.chars} characters.`
            : `Attached ${d.filename}, ${d.attached_chars} characters.`);
    } catch (e) {
        drop();
        _renderAttachmentStrip();
        announceError(String(e));
        _showError(`Could not attach ${file.name}: ${e.message}`);
    }
}

/** Upload a FileList sequentially, refusing to exceed the server's per-message
 *  cap client-side so the user is told before paying for an extraction. */
async function addAttachments(fileList) {
    const files = [...(fileList || [])];
    if (!files.length) return;
    if (_isQuerying) {
        announceStatus('Wait for the current response before attaching a file.');
        return;
    }
    _uploading += 1;
    _setSending(_isQuerying);
    try {
        for (const file of files) {
            if (_attachments.length >= _MAX_ATTACHMENTS) {
                announceError(`At most ${_MAX_ATTACHMENTS} files per message. `
                    + `${file.name} was not attached.`);
                _showError(`At most ${_MAX_ATTACHMENTS} files per message.`);
                break;
            }
            // Refuse here rather than let the server refuse the SEND: by then
            // the extraction has been paid for, and if the file that pushed the
            // conversation over the line is on an earlier turn there is nothing
            // left to remove.
            if (_projectedAttachmentChars() >= _MAX_REQUEST_CHARS) {
                _showError(_overBudgetMessage(`${file.name} was not attached.`));
                break;
            }
            await _uploadOne(file);
            // A file's extracted length is only known AFTER the extraction, so
            // the pre-check above cannot see the one that tips the balance. Say
            // so now, while the chip is still in the composer and removing it is
            // one click, rather than letting the send 400 later.
            if (_projectedAttachmentChars() > _MAX_REQUEST_CHARS) {
                _showError(_overBudgetMessage(
                    'Remove an attachment before sending.'));
            }
        }
    } finally {
        _uploading -= 1;
        _setSending(_isQuerying);
    }
}

/** Wire the Attach button, the hidden file input and drag-and-drop.
 *  Called once from app.js. Idempotent via a dataset marker — a module-scope
 *  boolean would stay true after the node was replaced and silently leave the
 *  new input unwired (the audit search-bar defect class). */
export function initAttachments() {
    const btn = document.getElementById('plainchat-attach-btn');
    const input = document.getElementById('plainchat-file');
    const panel = document.getElementById('chat-tab');
    if (!btn || !input || !panel || input.dataset.wired === '1') return;
    input.dataset.wired = '1';

    btn.addEventListener('click', () => input.click());
    input.addEventListener('change', async () => {
        const files = input.files;
        // Reset FIRST so re-picking the same file fires `change` again; the
        // FileList is captured above and survives the reset.
        const captured = [...(files || [])];
        input.value = '';
        await addAttachments(captured);
    });

    // Drag and drop over the whole panel. dragover must be prevented or the
    // browser navigates the window to the dropped file — which in PyWebView
    // replaces the app with a PDF viewer and no way back.
    let depth = 0;
    panel.addEventListener('dragenter', (e) => {
        if (!e.dataTransfer || ![...e.dataTransfer.types].includes('Files')) return;
        e.preventDefault();
        depth += 1;
        panel.classList.add('chat-drop-active');
    });
    panel.addEventListener('dragover', (e) => {
        if (!e.dataTransfer || ![...e.dataTransfer.types].includes('Files')) return;
        e.preventDefault();
    });
    panel.addEventListener('dragleave', () => {
        // Counted, not cleared on the first leave: dragging across a child
        // element fires leave/enter pairs, so a naive handler flickers.
        depth = Math.max(0, depth - 1);
        if (!depth) panel.classList.remove('chat-drop-active');
    });
    panel.addEventListener('drop', async (e) => {
        if (!e.dataTransfer || ![...e.dataTransfer.types].includes('Files')) return;
        e.preventDefault();
        depth = 0;
        panel.classList.remove('chat-drop-active');
        await addAttachments(e.dataTransfer.files);
    });
    btn.title = `Attach a file to this message (${_ACCEPTED_HINT})`;
}

export async function chatPlain(retryText, opts = {}) {
    if (_isQuerying) return;
    // An extraction still running means the composer holds a PLACEHOLDER whose
    // id the server has never minted. The Send button is disabled while one is
    // in flight, but the Enter handler on #plainchat-input calls this directly,
    // so the button is not the guard — this is (2026-08-29 review). Without it
    // the turn goes out carrying `pending-<ts>-<rand>`, the server finds no row
    // and reports it as an attachment that EXPIRED, and the bogus id stays in
    // _history and re-reports on every later turn. Announce rather than
    // silently no-op, like newChat() does.
    if (_uploading > 0) {
        announceStatus('Wait for the file to finish reading before sending.');
        return;
    }
    const input = document.getElementById('plainchat-input');
    // Item 3.5: a Retry passes the failed message DIRECTLY (string guard —
    // DOM handlers call this with an Event) so it never overwrites a fresh
    // draft the user typed after the failure.
    const fromRetry = typeof retryText === 'string' && retryText.trim() !== '';
    const text = fromRetry ? retryText.trim() : (input ? input.value.trim() : '');
    if (!text) return;
    // Retry after a KEPT partial (2026-08-12 adjudicated #5): the question is
    // already on screen above the interrupted answer AND already recorded in
    // _history — re-rendering and re-recording it doubled both.
    const questionOnRecord = fromRetry && opts.questionOnRecord === true;
    // Resolve the transcript container BEFORE locking the UI. It is appended to
    // unconditionally below (outside the try/finally), so a missing element here
    // would throw with `_isQuerying`/Send-disabled already set and no finally to
    // clear them — permanently wedging the panel. Bail cleanly instead.
    const chat = document.getElementById('plainchat-history');
    if (!chat) return;

    _isQuerying = true;
    _setSending(true);
    clearTaskError(document.getElementById('plainchat-error-boundary'));

    let userMsg = null;
    if (!questionOnRecord) {
        userMsg = document.createElement('div');
        userMsg.className = 'message message-user';
        userMsg.textContent = text;
        chat.appendChild(userMsg);
        _attachCopyButton(userMsg, () => text, 'Copy message');
        // Committed attachments only. A placeholder id is a CLIENT invention the
        // server never minted, so it must never be serialised, echoed or
        // exported as a real one. DELIBERATELY UNPINNED: the _uploading guard
        // above makes this unreachable, so no test can drive it without
        // disabling that guard — it is here so a future change to the guard
        // cannot resurrect the wire-level half of the defect, not because it
        // does anything today. Do not "simplify" it away.
        const sendable = _attachments.filter((a) => !a.pending);
        if (sendable.length) _renderTurnAttachments(userMsg, sendable);
        // Push the user turn before the request so it is part of the sent
        // history; it is rolled back on an EMPTY failure so a retry does not
        // duplicate it (a kept partial keeps it — see the catch).
        // `attachment_ids` rides the TURN, not the request, so a document
        // attached three turns ago is still in context on the fourth: the
        // server re-inflates it from the id every time. `attachments` is the
        // display/export metadata; the server reads neither it nor any other
        // field beyond role/content/attachment_ids.
        const turn = { role: 'user', content: text };
        if (sendable.length) {
            turn.attachment_ids = sendable.map((a) => a.attachment_id);
            turn.attachments = sendable.map((a) => ({ ...a }));
        }
        _history.push(turn);
    }
    if (input && !fromRetry) input.value = '';   // a retry leaves the draft untouched

    const botMsg = document.createElement('div');
    botMsg.className = 'message message-bot';
    botMsg.innerHTML = '<span class="typing-indicator"><span></span></span>';
    chat.appendChild(botMsg);
    chat.scrollTop = chat.scrollHeight;
    // The scroll rides the renderer's own coalesced tick (~25/s) rather than
    // every streamed token: reading scrollHeight forces a layout, and doing
    // that per token was work the 40 ms coalescer already exists to avoid.
    const answerRenderer = makeAnswerRenderer(botMsg, () => {
        chat.scrollTop = chat.scrollHeight;
    });

    const controller = new AbortController();
    _chatController = controller;
    _stopRequested = false;

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
        const sent = _history.slice(-(questionOnRecord ? _MAX_TURNS - 1 : _MAX_TURNS));
        if (questionOnRecord) {
            // Re-ask without re-recording: the recorded pair already holds the
            // question + its [interrupted] partial. The WIRE copy appends the
            // question as the final user turn so the model has something to
            // answer; history and the export keep a single rendition.
            sent.push({ role: 'user', content: text });
        }
        const body = { messages: sent };

        await consumeSSE('/api/plainchat', {
            serverBudget: true,
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
            // The user turn is on record and carries the ids, so the composer
            // strip is done. Not deleted server-side: the recorded turn is
            // re-sent on every following turn and the ids must still resolve.
            _clearComposerAttachments();
            _attachCopyButton(botMsg, () => fullAnswer, 'Copy answer');
            enhanceCodeBlocks(botMsg);
        } else {
            botMsg.textContent = _NO_RESPONSE_MSG;
        }
        announceStatus(fullAnswer ? 'Response ready.' : 'No response from the model.');
    } catch (e) {
        if (fullAnswer) {
            answerRenderer.flush();
            const note = document.createElement('div');
            note.className = 'muted';
            note.textContent = '⚠ Response interrupted — the text above is partial.';
            botMsg.appendChild(note);
            _attachCopyButton(botMsg, () => fullAnswer, 'Copy answer');
            // Record the visible exchange (2026-08-12 adjudicated #5,
            // vault.js's pattern): the user turn STAYS — a follow-up
            // ("développe ce point") needs its context — and the partial is
            // recorded as an [interrupted] assistant turn so the export
            // keeps it too. Pre-fix the user turn was popped and no
            // assistant turn pushed, leaving no trace of a visible exchange.
            const turn = { role: 'assistant', content: fullAnswer + '\n[interrupted]' };
            if (turnMeta) turn.meta = turnMeta;
            if (turnUsage) turn.usage = turnUsage;
            _history.push(turn);
            _clearComposerAttachments();
        } else {
            answerRenderer.cancel();
            botMsg.remove();
            // Roll back the un-answered user turn only when THIS attempt
            // pushed it — an empty retry after a kept partial must not pop
            // the recorded exchange it was retrying.
            if (!questionOnRecord && _history.length
                    && _history[_history.length - 1].role === 'user') {
                _history.pop();
            }
        }
        // 2026-07-20 audit A2: test the DOMException NAME (consumeSSE now
        // re-throws the abort untouched), not e.message — which was never
        // 'AbortError' even before, so the friendly branch was dead.
        const msg = e.name === 'AbortError'
            ? (_stopRequested ? 'Stop requested — the response is interrupted. The provider may still be finishing its current operation.' : 'Response timed out. The model may be overloaded — please try again.')
            : ('Error: ' + e.message);
        _showError(msg, () => {
            if (!fullAnswer && userMsg) userMsg.remove();
            // With a kept partial, the question stays on screen and on
            // record — the retry must not re-render or re-record it.
            chatPlain(text, { questionOnRecord: !!fullAnswer || questionOnRecord });
        });
    } finally {
        _chatController = null;
        _isQuerying = false;
        _setSending(false);
        chat.scrollTop = chat.scrollHeight;
    }
}

/**
 * Start a fresh conversation: clear the _history array and the rendered transcript
 * (the conversation is ephemeral — nothing is persisted). Refuses mid-stream.
 */
export function newChat(confirmed = false) {
    const hasTranscript = _history.length || document.querySelector('#plainchat-history .message');
    if (!_isQuerying && hasTranscript && !confirmed) {
        confirmInline(document.getElementById('plainchat-new-btn'), {
            message: 'Discard this conversation? Export it first if you want to keep it.',
            confirmLabel: 'Discard conversation',
            onConfirm: () => newChat(true),
        });
        return;
    }
    // Refuse while a query is in flight so the in-progress bot message is not
    // orphaned mid-stream.
    if (_isQuerying) {
        // C5/#43 (2026-07-13): announce instead of silently doing nothing
        // (parity with vault chat's "wait for the current response" hint).
        announceStatus('Wait for the current response before starting a new chat.');
        return;
    }
    // Anything still in the composer strip was never sent, so nothing will ever
    // reference it again — delete those rows rather than leaving them to the
    // TTL. Attachments already on a RECORDED turn are deliberately not deleted
    // here either way: the transcript is being discarded, and the TTL is the
    // right sweeper for rows a discarded conversation left behind.
    const orphans = _attachments.filter((a) => !a.pending);
    _attachments = [];
    _renderAttachmentStrip();
    for (const a of orphans) {
        secureFetch(`/api/chat-attachment/${encodeURIComponent(a.attachment_id)}`,
                    { method: 'DELETE' }).catch(() => {});
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

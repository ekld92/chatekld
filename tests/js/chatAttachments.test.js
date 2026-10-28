// Plain Chat attachments (plainchat.js, 2026-08-29).
//
// The server extracts an attached file to text and hands back an id; this panel
// holds ids and metadata and never file bytes or extracted text. What is pinned
// here is the client half of the four properties the Python suite pins on the
// server half — plus three failure modes that live only in the browser:
//
//   1. MULTIPART MUST NOT GO THROUGH secureFetch. secureFetch forces
//      `Content-Type: application/json`, which on a FormData body suppresses
//      the browser's own multipart boundary and makes the part unparseable
//      server-side. Nothing in Python can see this.
//   2. A FAILED SEND MUST KEEP THE ATTACHMENTS IN THE COMPOSER. The user turn is
//      rolled back on an empty failure, so a retry that had dropped them would
//      silently re-ask about a document it no longer carries — and the answer
//      would look perfectly normal.
//   3. dragover MUST BE PREVENTED. Without it the browser navigates to the
//      dropped file, which in PyWebView replaces the app with a PDF viewer and
//      no way back.
//
// Filenames are user-controlled, so they are rendered with textContent.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const enc = new TextEncoder();

function sseOnce(frames) {
    const payload =
        frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('') + 'data: [DONE]\n\n';
    let sent = false;
    return {
        ok: true,
        status: 200,
        body: {
            getReader: () => ({
                read: async () => (sent
                    ? { value: undefined, done: true }
                    : ((sent = true), { value: enc.encode(payload), done: false })),
                releaseLock: () => {},
                cancel: async () => {},
            }),
        },
    };
}

const jsonRes = (body, ok = true, status = 200) => ({
    ok, status, json: async () => body,
});

/** An upload answer as the server actually shapes it. */
const uploaded = (over = {}) => jsonRes({
    attachment_id: 'att-1', filename: 'paper.pdf', kind: 'pdf',
    chars: 40000, attached_chars: 40000, truncated: false, ...over,
});

function fakeFile(name) {
    // jsdom has File; the panel only ever reads .name and hands it to FormData.
    return new File(['x'], name, { type: 'application/octet-stream' });
}

/** Drive the real Attach flow: wire, then fire `change` on the file input. */
async function attach(plain, files) {
    plain.initAttachments();
    const input = document.getElementById('plainchat-file');
    Object.defineProperty(input, 'files', { value: files, configurable: true });
    input.dispatchEvent(new Event('change'));
    // Let the sequential upload loop settle.
    await new Promise((r) => setTimeout(r, 10));
}

describe('Plain Chat attachments', () => {
    let fetchMock;
    beforeEach(() => {
        document.body.innerHTML = `
          <div id="chat-tab">
            <div id="plainchat-history"></div>
            <div id="plainchat-error-boundary"></div>
            <div id="plainchat-attachments"></div>
            <input type="file" id="plainchat-file" multiple>
            <button id="plainchat-attach-btn"></button>
            <input type="text" id="plainchat-input">
            <button id="plainchat-send-btn"></button>
          </div>
          <div id="sr-status-announcer" role="status"></div>`;
        // plainchat.js holds module-scope state (_attachments, _history), so
        // every test re-imports it — the taggingScope trap.
        vi.resetModules();
        fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
    });
    afterEach(() => vi.unstubAllGlobals());

    describe('upload', () => {
        it('posts multipart WITHOUT a JSON content-type', async () => {
            fetchMock.mockResolvedValue(uploaded());
            const plain = await import('../../static/js/plainchat.js');
            await attach(plain, [fakeFile('paper.pdf')]);

            const [url, opts] = fetchMock.mock.calls[0];
            expect(url).toBe('/api/chat-attachment');
            expect(opts.body).toBeInstanceOf(FormData);
            expect(opts.headers['X-Requested-With']).toBe('ChatEKLD');
            // The whole point: a JSON content-type here suppresses the
            // multipart boundary and the server sees no file part.
            expect(opts.headers['Content-Type']).toBeUndefined();
        });

        it('shows a chip naming the file and how much of it was kept', async () => {
            fetchMock.mockResolvedValue(uploaded({ filename: 'rapport.pdf', attached_chars: 12000 }));
            const plain = await import('../../static/js/plainchat.js');
            await attach(plain, [fakeFile('rapport.pdf')]);
            const strip = document.getElementById('plainchat-attachments');
            expect(strip.textContent).toContain('rapport.pdf');
            expect(strip.textContent).toContain('12k chars');
        });

        it('states truncation ON THE CHIP, not only in a transient status line', async () => {
            fetchMock.mockResolvedValue(uploaded({
                chars: 400000, attached_chars: 120000, truncated: true }));
            const plain = await import('../../static/js/plainchat.js');
            await attach(plain, [fakeFile('long.pdf')]);
            const chip = document.querySelector('.chat-attachment-chip .chip-trunc');
            // A standing fact about what the model can answer from.
            expect(chip).not.toBeNull();
            expect(chip.textContent).toContain('120k');
            expect(chip.textContent).toContain('400k');
        });

        it('renders a hostile filename as text, never as markup', async () => {
            const nasty = '<img src=x onerror=alert(1)>.pdf';
            fetchMock.mockResolvedValue(uploaded({ filename: nasty }));
            const plain = await import('../../static/js/plainchat.js');
            await attach(plain, [fakeFile(nasty)]);
            const strip = document.getElementById('plainchat-attachments');
            expect(strip.querySelector('img')).toBeNull();
            expect(strip.textContent).toContain(nasty);
        });

        it('surfaces the server refusal and attaches nothing', async () => {
            fetchMock.mockResolvedValue(jsonRes(
                { error: 'Unsupported file type.' }, false, 400));
            const plain = await import('../../static/js/plainchat.js');
            await attach(plain, [fakeFile('diagram.svg')]);
            expect(document.querySelectorAll('.chat-attachment-chip')).toHaveLength(0);
            expect(document.getElementById('plainchat-error-boundary').textContent)
                .toContain('Unsupported file type.');
        });

        it('refuses past the per-message cap before paying for an extraction', async () => {
            fetchMock.mockImplementation(async () => uploaded(
                { attachment_id: `att-${fetchMock.mock.calls.length}` }));
            const plain = await import('../../static/js/plainchat.js');
            await attach(plain, [1, 2, 3, 4, 5].map((i) => fakeFile(`f${i}.txt`)));
            // Four uploaded, the fifth refused WITHOUT a request.
            expect(fetchMock.mock.calls.filter(
                ([u]) => u === '/api/chat-attachment')).toHaveLength(4);
            expect(document.querySelectorAll('.chat-attachment-chip')).toHaveLength(4);
        });

        it('refuses past the CHARACTER budget before paying for an extraction', async () => {
            // The two server bounds do not compose: at the default 120 000-char
            // per-file cap, two files already exceed the 240 000-char request
            // budget, so "up to four files" is reachable only for small ones.
            // Before this the user paid for every extraction and learned at
            // SEND time -- and if the file that pushed it over sat on an
            // earlier turn, "remove an attachment" could not reach it.
            const plain = await import('../../static/js/plainchat.js');
            fetchMock.mockResolvedValue(uploaded(
                { attachment_id: 'att-big', chars: 120000, attached_chars: 120000 }));
            await attach(plain, [fakeFile('one.pdf')]);
            await attach(plain, [fakeFile('two.pdf')]);
            expect(document.querySelectorAll(
                '#plainchat-attachments .chat-attachment-chip')).toHaveLength(2);
            fetchMock.mockClear();
            await attach(plain, [fakeFile('three.pdf')]);
            // Not uploaded at all -- that is the point: no extraction was paid for.
            expect(fetchMock).not.toHaveBeenCalled();
            expect(document.querySelectorAll(
                '#plainchat-attachments .chat-attachment-chip')).toHaveLength(2);
            expect(document.getElementById('plainchat-error-boundary').textContent)
                .toMatch(/more text than one request can carry/i);
        });

        it('counts EARLIER turns toward the budget, not just the composer', async () => {
            // The server sums across the whole 20-turn window, so a document
            // attached three turns ago still occupies the budget.
            const plain = await import('../../static/js/plainchat.js');
            fetchMock.mockResolvedValue(uploaded(
                { attachment_id: 'att-big', chars: 200000, attached_chars: 200000 }));
            await attach(plain, [fakeFile('huge.pdf')]);
            fetchMock.mockClear();
            fetchMock.mockResolvedValue(sseOnce([{ token: 'answered' }]));
            document.getElementById('plainchat-input').value = 'q';
            await plain.chatPlain();
            await new Promise((r) => setTimeout(r, 60));
            // Composer is now empty -- the file lives only on the recorded turn.
            expect(document.querySelectorAll(
                '#plainchat-attachments .chat-attachment-chip')).toHaveLength(0);
            fetchMock.mockClear();
            fetchMock.mockResolvedValue(uploaded(
                { attachment_id: 'att-2', chars: 100000, attached_chars: 100000 }));
            await attach(plain, [fakeFile('next.pdf')]);
            // A file's extracted length is unknowable before the extraction, so
            // this one is uploaded and THEN flagged -- while the chip is still
            // in the composer and removing it is one click, rather than at send
            // time when an earlier turn's file cannot be reached at all.
            expect(document.getElementById('plainchat-error-boundary').textContent)
                .toMatch(/more text than one request can carry/i);
            // And a further attach is refused outright, unpaid for.
            fetchMock.mockClear();
            await attach(plain, [fakeFile('third.pdf')]);
            expect(fetchMock).not.toHaveBeenCalled();
        });

        it('removes a chip and tells the server', async () => {
            fetchMock.mockResolvedValue(uploaded());
            const plain = await import('../../static/js/plainchat.js');
            await attach(plain, [fakeFile('paper.pdf')]);
            fetchMock.mockClear();
            fetchMock.mockResolvedValue(jsonRes({ ok: true }));
            document.querySelector('.chip-remove').click();
            await new Promise((r) => setTimeout(r, 5));
            expect(document.querySelectorAll('.chat-attachment-chip')).toHaveLength(0);
            const [url, opts] = fetchMock.mock.calls[0];
            expect(url).toBe('/api/chat-attachment/att-1');
            expect(opts.method).toBe('DELETE');
        });
    });

    describe('sending', () => {
        async function attachThenSend(plain, sendRes) {
            fetchMock.mockResolvedValue(uploaded());
            await attach(plain, [fakeFile('paper.pdf')]);
            fetchMock.mockClear();
            fetchMock.mockResolvedValue(sendRes);
            document.getElementById('plainchat-input').value = 'what does it say?';
            await plain.chatPlain();
            await new Promise((r) => setTimeout(r, 60));
            return JSON.parse(fetchMock.mock.calls[0][1].body);
        }

        it('sends ids, never file text', async () => {
            const plain = await import('../../static/js/plainchat.js');
            const body = await attachThenSend(plain, sseOnce([{ token: 'it says things' }]));
            expect(body.messages[0].attachment_ids).toEqual(['att-1']);
            // The panel has no file text to send, and must not invent a field
            // for one: the server owns what reaches the model.
            expect(JSON.stringify(body)).not.toContain('extracted_text');
        });

        it('keeps the file in context on the NEXT turn', async () => {
            const plain = await import('../../static/js/plainchat.js');
            await attachThenSend(plain, sseOnce([{ token: 'first answer' }]));
            fetchMock.mockClear();
            fetchMock.mockResolvedValue(sseOnce([{ token: 'second answer' }]));
            document.getElementById('plainchat-input').value = 'and the method?';
            await plain.chatPlain();
            await new Promise((r) => setTimeout(r, 60));
            const body = JSON.parse(fetchMock.mock.calls[0][1].body);
            // Turn 1 still carries its ids, so the server re-inflates the file.
            expect(body.messages[0].attachment_ids).toEqual(['att-1']);
            // And the new turn carries none — the file was not re-attached.
            expect(body.messages[2].attachment_ids).toBeUndefined();
        });

        it('clears the composer once the turn is on record', async () => {
            const plain = await import('../../static/js/plainchat.js');
            await attachThenSend(plain, sseOnce([{ token: 'answered' }]));
            expect(document.querySelectorAll(
                '#plainchat-attachments .chat-attachment-chip')).toHaveLength(0);
        });

        it('KEEPS the composer attachments when the send fails outright', async () => {
            // The user turn is rolled back on an empty failure, so the retry
            // must still carry the file. Dropping it here would produce a
            // perfectly normal-looking answer about a document that was never
            // sent — the failure mode this test exists for.
            const plain = await import('../../static/js/plainchat.js');
            fetchMock.mockResolvedValue(uploaded());
            await attach(plain, [fakeFile('paper.pdf')]);
            fetchMock.mockClear();
            fetchMock.mockResolvedValue(sseOnce([{ error: 'provider down' }]));
            document.getElementById('plainchat-input').value = 'q';
            await plain.chatPlain();
            await new Promise((r) => setTimeout(r, 60));
            expect(document.querySelectorAll(
                '#plainchat-attachments .chat-attachment-chip')).toHaveLength(1);
        });

        it('echoes the attachments under the sent message', async () => {
            const plain = await import('../../static/js/plainchat.js');
            await attachThenSend(plain, sseOnce([{ token: 'answered' }]));
            const userMsg = document.querySelector('#plainchat-history .message-user');
            expect(userMsg.textContent).toContain('paper.pdf');
        });

        /** Start an upload and leave it hanging. Returns the release fn. */
        async function startSlowUpload(plain) {
            let release;
            fetchMock.mockImplementation(() => new Promise((r) => { release = r; }));
            plain.initAttachments();
            const input = document.getElementById('plainchat-file');
            Object.defineProperty(input, 'files',
                { value: [fakeFile('slow.pdf')], configurable: true });
            input.dispatchEvent(new Event('change'));
            await new Promise((r) => setTimeout(r, 5));
            return () => release(uploaded());
        }

        it('REFUSES to send while an extraction is still running', async () => {
            // The Send button is disabled, but #plainchat-input's onkeydown
            // calls chatPlain() directly, so the button is not the guard. Before
            // the fix this sent a turn carrying the CLIENT-invented
            // `pending-<ts>-<rand>` placeholder id: the server found no row and
            // announced it as an attachment that had EXPIRED, the transcript
            // showed "slow.pdf undefined chars", and the bogus id stayed in
            // _history and re-reported on every later turn.
            const plain = await import('../../static/js/plainchat.js');
            await startSlowUpload(plain);
            document.getElementById('plainchat-input').value = 'what does it say?';
            await plain.chatPlain();
            await new Promise((r) => setTimeout(r, 40));
            const sends = fetchMock.mock.calls.filter(
                (c) => String(c[0]).includes('/api/plainchat'));
            expect(sends).toHaveLength(0);
            expect(document.getElementById('sr-status-announcer').textContent)
                .toMatch(/finish reading/i);
            // The question is still in the composer, so Enter again after the
            // upload lands asks it exactly once.
            expect(document.getElementById('plainchat-input').value)
                .toBe('what does it say?');
        });

        it('sends the REAL id once the extraction lands, never a pending one', async () => {
            const plain = await import('../../static/js/plainchat.js');
            const release = await startSlowUpload(plain);
            release();
            await new Promise((r) => setTimeout(r, 20));
            fetchMock.mockClear();
            fetchMock.mockResolvedValue(sseOnce([{ token: 'ok' }]));
            document.getElementById('plainchat-input').value = 'q';
            await plain.chatPlain();
            await new Promise((r) => setTimeout(r, 60));
            const body = JSON.parse(fetchMock.mock.calls[0][1].body);
            expect(body.messages[0].attachment_ids).toEqual(['att-1']);
            // A placeholder id must never reach the wire, the transcript or an
            // export: the server never minted it.
            expect(JSON.stringify(body)).not.toContain('pending-');
            expect(document.getElementById('plainchat-history').textContent)
                .not.toContain('pending-');
        });

        it('re-enables Send after an upload that FAILS', async () => {
            // The refusal above is gated on _uploading, so a leaked counter
            // would wedge the panel permanently.
            const plain = await import('../../static/js/plainchat.js');
            fetchMock.mockResolvedValue(jsonRes({ error: 'nope' }, false, 400));
            await attach(plain, [fakeFile('bad.pdf')]);
            expect(document.getElementById('plainchat-send-btn').disabled).toBe(false);
            fetchMock.mockClear();
            fetchMock.mockResolvedValue(sseOnce([{ token: 'ok' }]));
            document.getElementById('plainchat-input').value = 'q';
            await plain.chatPlain();
            await new Promise((r) => setTimeout(r, 60));
            expect(fetchMock.mock.calls.filter(
                (c) => String(c[0]).includes('/api/plainchat'))).toHaveLength(1);
        });

        it('holds Send while an extraction is still running', async () => {
            // Sending mid-upload would ask about a document the request does
            // not yet carry, and the answer would look entirely normal.
            const plain = await import('../../static/js/plainchat.js');
            let release;
            fetchMock.mockImplementation(() => new Promise((r) => { release = r; }));
            plain.initAttachments();
            const input = document.getElementById('plainchat-file');
            Object.defineProperty(input, 'files',
                { value: [fakeFile('slow.pdf')], configurable: true });
            input.dispatchEvent(new Event('change'));
            await new Promise((r) => setTimeout(r, 5));
            expect(document.getElementById('plainchat-send-btn').disabled).toBe(true);
            release(uploaded());
            await new Promise((r) => setTimeout(r, 10));
            expect(document.getElementById('plainchat-send-btn').disabled).toBe(false);
        });
        // NOTE: the assertion above is about the BUTTON only, and stayed true
        // throughout the 2026-08-29 defect — the Enter key never touches it.
        // The behavioural half is 'REFUSES to send while an extraction is still
        // running' above; keep both, they fail for different reasons.
    });

    describe('drag and drop', () => {
        it('prevents the browser navigating to a dropped file', async () => {
            const plain = await import('../../static/js/plainchat.js');
            plain.initAttachments();
            const panel = document.getElementById('chat-tab');
            const ev = new Event('dragover', { bubbles: true, cancelable: true });
            ev.dataTransfer = { types: ['Files'], files: [] };
            panel.dispatchEvent(ev);
            // Without this the window navigates to the file, which in PyWebView
            // replaces the app with a PDF viewer and no way back.
            expect(ev.defaultPrevented).toBe(true);
        });

        it('ignores a drag that carries no files', async () => {
            const plain = await import('../../static/js/plainchat.js');
            plain.initAttachments();
            const panel = document.getElementById('chat-tab');
            const ev = new Event('dragover', { bubbles: true, cancelable: true });
            ev.dataTransfer = { types: ['text/plain'], files: [] };
            panel.dispatchEvent(ev);
            expect(ev.defaultPrevented).toBe(false);
        });

        it('uploads a dropped file', async () => {
            fetchMock.mockResolvedValue(uploaded());
            const plain = await import('../../static/js/plainchat.js');
            plain.initAttachments();
            const panel = document.getElementById('chat-tab');
            const ev = new Event('drop', { bubbles: true, cancelable: true });
            ev.dataTransfer = { types: ['Files'], files: [fakeFile('dropped.pdf')] };
            panel.dispatchEvent(ev);
            await new Promise((r) => setTimeout(r, 10));
            expect(fetchMock.mock.calls.some(([u]) => u === '/api/chat-attachment')).toBe(true);
        });
    });

    describe('wiring', () => {
        it('is idempotent via a dataset marker on the element, not a module flag', async () => {
            // A module-scope boolean stays true after the node is replaced and
            // would silently leave the new input unwired (the audit search-bar
            // defect class).
            const src = readFileSync(join(ROOT, 'static/js/plainchat.js'), 'utf8');
            expect(src).toContain("input.dataset.wired");
            const plain = await import('../../static/js/plainchat.js');
            plain.initAttachments();
            plain.initAttachments();
            fetchMock.mockResolvedValue(uploaded());
            const input = document.getElementById('plainchat-file');
            Object.defineProperty(input, 'files',
                { value: [fakeFile('a.pdf')], configurable: true });
            input.dispatchEvent(new Event('change'));
            await new Promise((r) => setTimeout(r, 10));
            // Wired twice would upload the same file twice.
            expect(fetchMock.mock.calls.filter(
                ([u]) => u === '/api/chat-attachment')).toHaveLength(1);
        });

        it('offers exactly the extensions the server accepts', () => {
            // A drift class with the provider-registry shape: the picker's
            // `accept` list and the server's allowlist are written in different
            // files, and a mismatch is silent in BOTH directions — an extension
            // the picker hides can never be attached, and one it offers is
            // refused only after the user chose the file.
            const html = readFileSync(join(ROOT, 'templates/index.html'), 'utf8');
            const m = /id="plainchat-file"[^>]*accept="([^"]+)"/.exec(html);
            expect(m, 'the file input must declare an accept list').not.toBeNull();
            const offered = m[1].split(',').map((x) => x.trim()).sort();
            const consts = readFileSync(join(ROOT, 'core/constants.py'), 'utf8');
            const server = [];
            for (const name of ['CHAT_ATTACHMENT_TEXT_EXTS', 'CHAT_ATTACHMENT_IMAGE_EXTS',
                                'CHAT_ATTACHMENT_PDF_EXTS']) {
                const block = new RegExp(`${name} = frozenset\\(\\{([^}]*)\\}`).exec(consts);
                expect(block, name).not.toBeNull();
                for (const q of block[1].matchAll(/"(\.[a-z]+)"/g)) server.push(q[1]);
            }
            expect(offered).toEqual(server.sort());
        });

        it('adds no inline handler to the template', () => {
            // The CSP already carries 111 'unsafe-hashes' entries, each of which
            // authorises that exact handler string on ANY element.
            const html = readFileSync(join(ROOT, 'templates/index.html'), 'utf8');
            // Checked per NEW element, not over a slice: the composer's
            // pre-existing `onkeydown` on #plainchat-input is already hashed
            // and is not this feature's to remove.
            for (const id of ['plainchat-attach-btn', 'plainchat-file',
                              'plainchat-attachments']) {
                const at = html.indexOf(`id="${id}"`);
                expect(at, id).toBeGreaterThan(-1);
                const tag = html.slice(html.lastIndexOf('<', at), html.indexOf('>', at));
                expect(tag, id).not.toMatch(/\son[a-z]+=/);
            }
        });
    });
});

describe('export records what rode each turn', () => {
    it('names the files and how much of each the model received', async () => {
        const { buildConversationExport } = await import('../../static/js/ui.js');
        const { content } = buildConversationExport([
            { role: 'user', content: 'summarise it',
              attachments: [{ filename: 'paper.pdf', chars: 40000,
                              attached_chars: 40000, truncated: false }] },
            { role: 'assistant', content: 'It argues…' },
        ], 'md', 'Plain Chat Conversation');
        // An export that omitted "and a 40-page PDF" would misrepresent what
        // the answer was written from.
        expect(content).toContain('Attachments: paper.pdf (40000 chars)');
    });

    it('cannot make a truncated attachment read as a whole one', async () => {
        const { buildConversationExport } = await import('../../static/js/ui.js');
        const { content } = buildConversationExport([
            { role: 'user', content: 'q',
              attachments: [{ filename: 'long.pdf', chars: 400000,
                              attached_chars: 120000, truncated: true }] },
        ], 'txt', 'T');
        expect(content).toContain('first 120000 of 400000 chars');
    });

    it('leaves a turn with no attachments exactly as before', async () => {
        const { buildConversationExport } = await import('../../static/js/ui.js');
        const { content } = buildConversationExport(
            [{ role: 'user', content: 'q' }], 'md', 'T');
        expect(content).not.toContain('Attachments:');
    });
});

describe('Plain Chat attachments — extraction is held exclusively', () => {
    beforeEach(() => {
        document.body.innerHTML = `
          <div id="chat-tab">
            <div id="plainchat-history"></div>
            <div id="plainchat-error-boundary"></div>
            <div id="plainchat-attachments"></div>
            <input type="file" id="plainchat-file" multiple>
            <button id="plainchat-attach-btn"></button>
            <input type="text" id="plainchat-input">
            <button id="plainchat-send-btn"></button>
          </div>
          <div id="sr-status-announcer" role="status"></div>`;
        vi.resetModules();
    });
    afterEach(() => vi.unstubAllGlobals());

    // `_setSending` read the RAW argument for the attach button and folded in
    // `_uploading` only for Send. `addAttachments` calls
    // `_setSending(_isQuerying)`, which is false when idle, so Attach stayed
    // live during an upload — and a second click opened a CONCURRENT
    // extraction, which `_uploadOne`'s own docstring says is impossible
    // ("sequential by caller"). Each one is a spawned PDF subprocess or a
    // vision call (2026-08-30).
    it('holds BOTH Attach and Send during an upload, and restores both after', async () => {
        let release;
        const gate = new Promise((r) => { release = r; });
        vi.stubGlobal('fetch', vi.fn(async () => {
            await gate;
            return uploaded();
        }));
        const plain = await import('../../static/js/plainchat.js');
        plain.initAttachments();
        const attachBtn = document.getElementById('plainchat-attach-btn');
        const sendBtn = document.getElementById('plainchat-send-btn');

        // Driven through the real UI path — `addAttachments` is not exported,
        // and the button state is exactly what a second CLICK would consult.
        const input = document.getElementById('plainchat-file');
        Object.defineProperty(input, 'files', {
            value: [fakeFile('paper.pdf')], configurable: true });
        input.dispatchEvent(new Event('change'));
        await new Promise((r) => setTimeout(r, 5));
        expect(attachBtn.disabled, 'Attach must be held during an extraction').toBe(true);
        expect(sendBtn.disabled, 'Send must be held during an extraction').toBe(true);

        release();
        await new Promise((r) => setTimeout(r, 20));
        expect(attachBtn.disabled).toBe(false);
        expect(sendBtn.disabled).toBe(false);
    });

    it('reports a page-bounded read in PAGES, never as a character total', async () => {
        // On a sparse long PDF the character total describes only what was
        // extracted, so "first 45k of 45k chars" would read as "all of it".
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue(uploaded({
            filename: 'huge.pdf', chars: 45000, attached_chars: 45000,
            truncated: true, pages_read: 151, pages_total: 2000,
        })));
        const plain = await import('../../static/js/plainchat.js');
        await attach(plain, [fakeFile('huge.pdf')]);
        const strip = document.getElementById('plainchat-attachments').textContent;
        expect(strip).toContain('first 151 of 2000 pages');
        expect(strip).not.toContain('45k of 45k');
    });

    it('lists .jpeg among the accepted types it advertises', async () => {
        // The input `accept` and the server both take .jpeg; the one place that
        // TELLS the user what works omitted it.
        vi.stubGlobal('fetch', vi.fn());
        const plain = await import('../../static/js/plainchat.js');
        plain.initAttachments();
        expect(document.getElementById('plainchat-attach-btn').title)
            .toContain('.jpeg');
    });
});

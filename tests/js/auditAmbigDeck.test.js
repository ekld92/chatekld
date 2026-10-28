// Pins the ambiguous-PDF triage deck (audit.js).
//
// Two classes are guarded here:
//   1. WRITE CEREMONY — the pre-deck table assigned a citation key on a single
//      unconfirmed click with no way back. Every deck write (assign, not-in-bib,
//      undo) must go through confirmInline, and undo must POST the `clear`
//      action that bridge.clear_curation implements.
//   2. PDF PREVIEW TRANSPORT — the app-level origin guard 403s any /api route
//      without X-Requested-With, so an <iframe src="/api/audit/pdf">CANNOT
//      work. The bytes must be fetched with the header and rendered from a
//      blob: URL (the same constraint deckPreview/taggingPdf pin elsewhere).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { selectAuditReport } from '../../static/js/audit.js';
import fs from 'node:fs';
import path from 'node:path';

function jsonOk(payload) {
    return { ok: true, status: 200, json: async () => payload };
}

const REPORT = {
    name: 'ambiguous_pdfs',
    count: 2,
    rows: [
        {
            pdf: { abs: '/v/att/a.pdf', rel: 'att/a.pdf', name: 'a.pdf' },
            candidates: [
                { citation_key: 'smith2020', title: 'Cardiac outcomes' },
                { citation_key: 'smith2021', title: 'Later work' },
            ],
        },
        {
            pdf: { abs: '/v/att/b.pdf', rel: 'att/b.pdf', name: 'b.pdf' },
            candidates: [{ citation_key: 'jones2019', title: 'Something' }],
        },
    ],
};

const DOM = `
  <div id="audit-summary" style="display:none;"></div>
  <div id="audit-report-tabs" style="display:none;"></div>
  <div id="audit-search-bar" style="display:none;">
    <input id="audit-search-input"><button id="audit-search-clear"></button>
  </div>
  <div id="audit-report-meta"></div>
  <div id="audit-search-results" style="display:none;"></div>
  <div id="audit-report-body"></div>
  <div id="audit-empty-state"></div>
  <div id="sr-status-announcer" role="status"></div>
`;

/** Click the confirm button inside the inline confirm strip. */
function acceptConfirm() {
    const strip = document.querySelector('.inline-confirm-strip');
    expect(strip, 'no confirm strip appeared').toBeTruthy();
    const buttons = [...strip.querySelectorAll('button')];
    // ui.js puts Cancel first in DOM/focus order; the confirm is the other one.
    buttons[buttons.length - 1].click();
}

describe('ambiguous-PDF triage deck', () => {
    let fetchMock;

    beforeEach(async () => {
        document.body.innerHTML = DOM;
        // This vitest environment does not ship a working localStorage.
        const store = new Map([['audit_ambig_view', 'deck']]);
        vi.stubGlobal('localStorage', {
            getItem: (k) => (store.has(k) ? store.get(k) : null),
            setItem: (k, v) => store.set(k, String(v)),
            removeItem: (k) => store.delete(k),
        });
        fetchMock = vi.fn(async (url) => {
            if (String(url).includes('/api/audit/reports/ambiguous_pdfs')) {
                return jsonOk(REPORT);
            }
            return jsonOk({ ok: true });
        });
        vi.stubGlobal('fetch', fetchMock);
        if (!URL.createObjectURL) URL.createObjectURL = () => {};
        if (!URL.revokeObjectURL) URL.revokeObjectURL = () => {};
        vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:mock-audit-pdf');
        vi.spyOn(URL, 'revokeObjectURL');
        await selectAuditReport('ambiguous_pdfs');
        fetchMock.mockClear();
    });

    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('renders the first card with its candidates', () => {
        const card = document.getElementById('audit-ambig-card');
        expect(card.textContent).toContain('a.pdf');
        expect(card.textContent).toContain('smith2020');
        expect(card.textContent).toContain('smith2021');
        expect(document.getElementById('audit-ambig-progress').textContent)
            .toContain('PDF 1 of 2');
    });

    it('assign is confirm-gated and posts the selected key, then advances', async () => {
        document.querySelector('[data-ambig="assign"]').click();
        // Nothing written on the first click — the ceremony must intervene.
        expect(fetchMock).not.toHaveBeenCalled();

        acceptConfirm();
        await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
        const [url, opts] = fetchMock.mock.calls[0];
        expect(String(url)).toContain('/api/audit/mapping');
        expect(JSON.parse(opts.body))
            .toEqual({ pdf: 'att/a.pdf', citation_key: 'smith2020', confirm: true });

        await vi.waitFor(() => {
            expect(document.getElementById('audit-ambig-card').textContent)
                .toContain('b.pdf');
        });
    });

    it('number keys select a different candidate before assigning', async () => {
        const deck = document.getElementById('audit-ambig-deck');
        deck.dispatchEvent(new KeyboardEvent('keydown', { key: '2', bubbles: true }));
        document.querySelector('[data-ambig="assign"]').click();
        acceptConfirm();
        await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
        expect(JSON.parse(fetchMock.mock.calls[0][1].body).citation_key)
            .toBe('smith2021');
    });

    it('not-in-bib posts no_match', async () => {
        document.querySelector('[data-ambig="no-match"]').click();
        acceptConfirm();
        await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
        expect(JSON.parse(fetchMock.mock.calls[0][1].body))
            .toEqual({ pdf: 'att/a.pdf', no_match: true, confirm: true });
    });

    it('undo is confirm-gated, posts clear, and returns to the undone card', async () => {
        document.querySelector('[data-ambig="assign"]').click();
        acceptConfirm();
        await vi.waitFor(() => {
            expect(document.getElementById('audit-ambig-card').textContent)
                .toContain('b.pdf');
        });
        fetchMock.mockClear();

        document.querySelector('[data-ambig="undo"]').click();
        expect(fetchMock).not.toHaveBeenCalled();   // ceremony required
        acceptConfirm();
        await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
        expect(JSON.parse(fetchMock.mock.calls[0][1].body))
            .toEqual({ pdf: 'att/a.pdf', clear: true, confirm: true });

        // Lands back on the card whose decision was reversed.
        await vi.waitFor(() => {
            expect(document.getElementById('audit-ambig-card').textContent)
                .toContain('a.pdf');
        });
    });

    it('a failed undo keeps the entry retryable (peek, not pop)', async () => {
        document.querySelector('[data-ambig="assign"]').click();
        acceptConfirm();
        // Wait for the write to COMPLETE (the card advances), not merely to be
        // issued — the undo entry is pushed after the response.
        await vi.waitFor(() => {
            expect(document.getElementById('audit-ambig-card').textContent)
                .toContain('b.pdf');
        });

        fetchMock.mockImplementation(async () => ({
            ok: false, status: 500, json: async () => ({ error: 'boom' }),
        }));
        document.querySelector('[data-ambig="undo"]').click();
        acceptConfirm();
        await vi.waitFor(() => {
            expect(document.getElementById('audit-ambig-result').textContent)
                .toContain('boom');
        });
        // Still offered, not silently spent.
        expect(document.querySelector('[data-ambig="undo"]').disabled).toBe(false);
    });

    it('the PDF panel fetches with the header and renders a blob: iframe', async () => {
        fetchMock.mockImplementation(async () => ({
            ok: true, status: 200,
            blob: async () => new Blob(['%PDF-1.4'], { type: 'application/pdf' }),
            json: async () => ({}),
        }));
        document.querySelector('[data-ambig="pdf"]').click();
        await vi.waitFor(() => {
            expect(document.querySelector('.audit-ambig-frame')).toBeTruthy();
        });
        const [url, opts] = fetchMock.mock.calls[0];
        expect(String(url)).toContain('/api/audit/pdf?rel=att%2Fa.pdf');
        expect(opts.headers['X-Requested-With']).toBe('ChatEKLD');
        const frame = document.querySelector('.audit-ambig-frame');
        expect(frame.getAttribute('src')).toBe('blob:mock-audit-pdf');
        expect(frame.getAttribute('src')).not.toContain('/api/');
    });

    it('changing card closes the panel and revokes the blob URL', async () => {
        fetchMock.mockImplementation(async () => ({
            ok: true, status: 200,
            blob: async () => new Blob(['%PDF-1.4'], { type: 'application/pdf' }),
            json: async () => ({}),
        }));
        document.querySelector('[data-ambig="pdf"]').click();
        await vi.waitFor(() => {
            expect(document.querySelector('.audit-ambig-frame')).toBeTruthy();
        });
        document.querySelector('[data-ambig="skip"]').click();
        expect(document.querySelector('.audit-ambig-frame')).toBeNull();
        expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:mock-audit-pdf');
    });

    it('source ratchet: the deck never points an iframe src at /api', () => {
        const src = fs.readFileSync(
            path.resolve(__dirname, '../../static/js/audit.js'), 'utf8');
        expect(src).not.toMatch(/\.src\s*=\s*['"`]\/api\//);
        expect(src).toContain('/api/audit/pdf?rel=');
        expect(src).toContain('URL.createObjectURL');
    });

    // Finding 1: navigation is not busy-gated, so the user can move while a
    // write is in flight. The response must NOT blindly advance again (that
    // silently skipped an un-triaged card).
    it('does not skip a card when navigating during an in-flight write', async () => {
        let resolveWrite;
        fetchMock.mockImplementation((url) => {
            if (String(url).includes('/api/audit/mapping')) {
                return new Promise((res) => { resolveWrite = () => res(jsonOk({ ok: true })); });
            }
            return jsonOk(REPORT);   // re-select loads
        });
        // Begin assigning card 0 (a.pdf); the POST is left pending.
        document.querySelector('[data-ambig="assign"]').click();
        acceptConfirm();
        // Skip forward to card 1 (b.pdf) while the write is still in flight.
        document.querySelector('[data-ambig="skip"]').click();
        expect(document.getElementById('audit-ambig-card').textContent).toContain('b.pdf');
        // The write now lands — it must land on the SAME card the user moved to,
        // not advance a second time (which, with 2 rows, would jump to "done").
        resolveWrite();
        await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
        await Promise.resolve();
        expect(document.getElementById('audit-ambig-card').textContent).toContain('b.pdf');
    });

    // The manual link picker (2026-08-19), dropped into this deck too: the
    // bridge parks a PDF here because author+year matched SEVERAL keys — and
    // the right key may be none of them, which the candidate list alone can
    // never express.
    it('L opens the bibliography picker and assigns a key outside the candidates', async () => {
        fetchMock.mockImplementation(async (url) => {
            const u = String(url);
            if (u.includes('/api/audit/reports/ambiguous_pdfs')) return jsonOk(REPORT);
            if (u.includes('/api/audit/inventory')) {
                return jsonOk({
                    summary: {},
                    records: [
                        { citation_key: 'vonkanel2019', title: 'Sleep', first_author: 'von Känel, R', year: '2019', has_bib_entry: true },
                        { citation_key: 'smith2020', title: 'Cardiac outcomes', first_author: 'Smith, J', year: '2020', has_bib_entry: true },
                    ],
                });
            }
            return jsonOk({ ok: true });
        });
        document.getElementById('audit-ambig-deck')
            .dispatchEvent(new KeyboardEvent('keydown', { key: 'l', bubbles: true }));
        const input = await vi.waitFor(() => {
            const el = document.querySelector('.acc-keypicker-input');
            expect(el).toBeTruthy();
            return el;
        });
        await vi.waitFor(() => {
            expect(document.querySelector('.acc-keypicker-count').textContent)
                .toContain('Type to search');
        });
        input.value = 'kanel';
        input.dispatchEvent(new Event('input'));
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
        acceptConfirm();
        await vi.waitFor(() => {
            const call = fetchMock.mock.calls.find(
                ([u, o]) => String(u).includes('/api/audit/mapping') && o);
            expect(call).toBeTruthy();
            // A key that is NOT one of this row's two candidates, and no
            // `source` annotation — hand curation is not an accession.
            expect(JSON.parse(call[1].body)).toEqual({
                pdf: 'att/a.pdf', citation_key: 'vonkanel2019', confirm: true,
            });
        });
    });

    // Finding 3: the undo stack is keyed to a scan snapshot and must survive a
    // deck↔table view toggle (same scan). The old code wiped it on every payload.
    it('keeps the undo stack across a view toggle within one scan', async () => {
        document.querySelector('[data-ambig="assign"]').click();
        acceptConfirm();
        await vi.waitFor(() => expect(
            document.getElementById('audit-ambig-card').textContent).toContain('b.pdf'));
        // Re-select the report (what a deck↔table toggle does) — same scan.
        await selectAuditReport('ambiguous_pdfs');
        const undoBtn = document.querySelector('[data-ambig="undo"]');
        expect(undoBtn).toBeTruthy();
        expect(undoBtn.disabled).toBe(false);   // stack preserved, not wiped
    });
});

// The table view of the same report (bulk overview). Its write verbs DISABLE
// themselves on success — and disabling the focused element drops focus to
// <body>, so a keyboard user working down the rows had their next Tab restart
// from the top of the page after every assignment. Paired with the accession
// table's re-render focus loss in the deferral registry: fixed together.
describe('ambiguous-PDF table view — focus after a write', () => {
    let fetchMock;

    beforeEach(async () => {
        document.body.innerHTML = DOM;
        const store = new Map([['audit_ambig_view', 'table']]);
        vi.stubGlobal('localStorage', {
            getItem: (k) => (store.has(k) ? store.get(k) : null),
            setItem: (k, v) => store.set(k, String(v)),
            removeItem: (k) => store.delete(k),
        });
        fetchMock = vi.fn(async (url) => {
            if (String(url).includes('/api/audit/reports/ambiguous_pdfs')) {
                return jsonOk(REPORT);
            }
            return jsonOk({ ok: true });
        });
        vi.stubGlobal('fetch', fetchMock);
        await selectAuditReport('ambiguous_pdfs');
        fetchMock.mockClear();
    });

    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('assigning a key moves focus to the next row, never to <body>', async () => {
        const btns = [...document.querySelectorAll('[data-action="pick-key"]')];
        expect(btns.length).toBe(2);
        btns[0].focus();
        btns[0].click();
        acceptConfirm();
        await vi.waitFor(() => expect(btns[0].disabled).toBe(true));
        await vi.waitFor(() => {
            expect(document.activeElement.tagName).not.toBe('BODY');
        });
        expect(document.activeElement).toBe(btns[1]);
    });

    it('the LAST row keeps focus in its own row rather than dropping it', async () => {
        const btns = [...document.querySelectorAll('[data-action="no-match"]')];
        btns[btns.length - 1].click();
        acceptConfirm();
        await vi.waitFor(() => expect(btns[btns.length - 1].disabled).toBe(true));
        await vi.waitFor(() => {
            expect(document.activeElement.tagName).not.toBe('BODY');
        });
        expect(document.activeElement.getAttribute('data-action')).toBe('reveal');
    });
});

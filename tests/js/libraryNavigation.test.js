import { beforeEach, afterEach, expect, it, vi } from 'vitest';

beforeEach(() => {
    vi.resetModules();
    document.body.innerHTML = '<div id="audit-report-tabs"></div><div id="audit-report-meta"></div><div id="audit-report-body"></div><div id="audit-empty-state"></div>';
    vi.stubGlobal('localStorage', {getItem: () => null, setItem: () => {}, removeItem: () => {}});
});
afterEach(() => vi.unstubAllGlobals());

it('does not infer missing attachments from an unavailable observation', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ok: true, json: async () => ({
        freshness: {available: false}, populations: {references: 1, files: 0, attachments: 0},
        documents: [{id: 'r', kind: 'reference', title: 'Retained reference', attachments: []}],
    })})));
    const {selectAuditReport} = await import('../../static/js/audit.js');
    await selectAuditReport('library_documents');
    expect(document.body.textContent).toContain('Retained reference');
    const select = document.querySelector('select'); select.value = 'no_attachment'; select.dispatchEvent(new Event('change'));
    expect(document.body.textContent).toContain('0 entries shown');
    select.value = ''; select.dispatchEvent(new Event('change'));
    expect(document.body.textContent).toContain('Retained reference');
});

it('shows references without files, filters populations and escapes source titles without a scan', async () => {
    const payload = {freshness: {checked_at: 100, available: true}, populations: {references: 1, files: 1, attachments: 0}, documents: [
        {id: 'native:1', kind: 'reference', title: '<img src=x onerror=alert(1)>', files: [], attachments: []},
        {id: 'file:a.pdf', kind: 'file', title: 'a.pdf', row: {mapping: {keys: []}, attachment_state: 'none_observed'}},
    ]};
    const fetch = vi.fn(async () => ({ok: true, json: async () => payload}));
    vi.stubGlobal('fetch', fetch);
    const {selectAuditReport} = await import('../../static/js/audit.js');
    await selectAuditReport('library_documents');
    const host = document.querySelector('#audit-report-body');
    expect(host.textContent).toContain('1 references and 1 physical files');
    expect(host.querySelector('img')).toBeNull();
    const select = host.querySelector('select');
    select.value = 'reference'; select.dispatchEvent(new Event('change'));
    expect(host.textContent).toContain('1 entries shown');
    expect([...host.querySelectorAll('button')].some(b => b.textContent === 'file: a.pdf')).toBe(false);
    expect(host.getAttribute('aria-label')).toBe('Library documents');
    expect(fetch.mock.calls.every(([url, options]) => String(url).includes('/api/audit/library/documents') && !options?.method)).toBe(true);
});

it('routes a document filter and retains it when returning to Documents', async () => {
    const payload = {freshness: {}, populations: {references: 1, files: 1}, documents: [
        {id: 'r', kind: 'reference', title: 'Reference', attachments: []},
        {id: 'f', kind: 'file', title: 'File', row: {mapping: {keys: ['a', 'b']}, parent_state: 'ambiguous'}},
    ]};
    vi.stubGlobal('fetch', vi.fn(async () => ({ok: true, json: async () => payload})));
    const {selectAuditReport} = await import('../../static/js/audit.js');
    await selectAuditReport('library_documents:ambiguous');
    expect(document.querySelector('select').value).toBe('ambiguous');
    expect(document.body.textContent).toContain('1 entries shown');
    await selectAuditReport('library_documents');
    expect(document.querySelector('select').value).toBe('ambiguous');
    expect(document.body.textContent).toContain('Advanced decisions and historical reports');
});

it('reviews exact bibliography fields without executing metadata writes or rendering source HTML', async () => {
    const campaign = {id: 'fixture', kind: 'bibliography', proposals: {input: {provider: 'ollama', model: 'fixture', max_tokens: 128}}, outcomes: {
        generation: {status: 'proposed', proposals: [{id: 'r', fields: [{field: 'title', current: 'old', proposed: 'new', evidence: 'digest', excerpt_verified: true, source: {text: '<img src=x>'}}]}]},
    }};
    const fetch = vi.fn(async () => ({ok: true, json: async () => ({freshness: {}, note_campaigns: [campaign]})}));
    vi.stubGlobal('fetch', fetch);
    document.body.insertAdjacentHTML('beforeend', '<p id="library-campaign-review-status" role="status"></p>');
    const {selectAuditReport} = await import('../../static/js/audit.js');
    await selectAuditReport('library_history');
    expect(document.querySelector('img')).toBeNull();
    expect(document.body.textContent).toContain('Semantic support: unverified');
    const choice = document.querySelector('fieldset select'); choice.value = 'approve';
    const save = [...document.querySelectorAll('button')].find(b => b.textContent === 'Review selected field decisions');
    save.click();
    expect(document.body.textContent).toContain('Explain support for every approved field');
    // D5: the message also reaches the STATIC live region.
    await vi.waitFor(() => expect(document.getElementById('library-campaign-review-status').textContent).toContain('Explain support for every approved field'));
    const areas = document.querySelectorAll('fieldset textarea'); areas[1].value = 'Verified the title in its full source context.';
    save.click();
    const confirm = [...document.querySelectorAll('button')].find(b => b.textContent === 'Record decisions');
    expect(confirm).toBeTruthy(); confirm.click();
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    const [url, options] = fetch.mock.calls[1];
    expect(url).toBe('/api/audit/library/bibliography/fixture/decisions');
    expect(JSON.parse(options.body).decisions).toEqual([{record_id: 'r', field: 'title', evidence: 'digest', approve: true, support_assessment: 'supported', reason: areas[1].value, reviewed_value: 'new'}]);
});

it('loads note conflicts only on request and refuses unavailable observations as absence', async () => {
    const payload = {freshness: {}, populations: {references: 1, files: 0}, documents: [
        {id: 'r', kind: 'reference', citation_key: 'key', title: 'Reference', attachments: []},
    ]};
    const fetch = vi.fn(async url => String(url).includes('/notesync/')
        ? {ok: true, json: async () => ({rows: [{citation_key: 'key', state: 'conflict'}]})}
        : {ok: true, json: async () => payload});
    vi.stubGlobal('fetch', fetch);
    const {selectAuditReport} = await import('../../static/js/audit.js');
    await selectAuditReport('library_documents');
    expect(fetch).toHaveBeenCalledTimes(1);
    const select = document.querySelector('select'); select.value = 'note_conflict'; select.dispatchEvent(new Event('change'));
    await vi.waitFor(() => expect(document.body.textContent).toContain('Note comparison read at'));
    expect(document.body.textContent).toContain('1 entries shown');
    fetch.mockImplementationOnce(async () => ({ok: false, json: async () => ({error: 'fixture offline'})}));
    [...document.querySelectorAll('button')].find(b => b.textContent === 'Read current note comparison').click();
    await vi.waitFor(() => expect(document.body.textContent).toContain('Empty results do not establish absence of conflicts'));
});

it('discloses Crossref acquisition separately before any model request', async () => {
    const fetch = vi.fn(async () => ({ok: true, json: async () => ({freshness: {}, results: []})}));
    vi.stubGlobal('fetch', fetch);
    const {selectAuditReport} = await import('../../static/js/audit.js');
    await selectAuditReport('library_checks');
    const fields = document.querySelectorAll('.library-campaign input');
    fields[0].value = 'ollama'; fields[1].value = 'fixture'; fields[2].checked = true; fields[3].value = '128';
    document.querySelector('.library-campaign textarea').value = JSON.stringify([{id: 'r', doi: '10.1234/x', current: {title: 'old'}, sources: []}]);
    [...document.querySelectorAll('button')].find(b => b.textContent === 'Review generation request').click();
    expect(document.body.textContent).toContain('Also send the selected DOI values to Crossref');
    expect(fetch).toHaveBeenCalledTimes(1);
});

it('renders the storage control as a table with manual guidance and no raw JSON', async () => {
    const payload = {freshness: {}, goal: 'Zero PDFs in Zotero storage or cloud.', destination: '/vault/biblio_articles',
        population: {attachments: 2}, observation: {checked_at: 100, available: true}, counts: {stored: 1, linked_ok: 1},
        findings: [{attachment_key: 'STORED01', parent_key: 'P', parent_title: '<b>Parent</b>', citation_key: 'smith2020', finding: 'stored',
                    path: 'storage:paper.pdf', guidance: ['Open the item in Zotero.', 'Use Attanger → Move attachment.']}],
        excluded: {embedded_image: 12, note_child: 2, web_snapshot: 3},
        results: [], writes: false, applicable: false, conversion_offered: false};
    const fetch = vi.fn(async () => ({ok: true, json: async () => payload}));
    vi.stubGlobal('fetch', fetch);
    const {selectAuditReport} = await import('../../static/js/audit.js');
    await selectAuditReport('library_storage');
    const host = document.querySelector('#audit-report-body');
    expect(fetch.mock.calls.every(([url, options]) => String(url).includes('/api/audit/library/storage') && !options?.method)).toBe(true);
    expect(host.querySelector('pre')).toBeNull();
    expect(host.querySelector('b')).toBeNull();
    expect(host.textContent).toContain('Stored in Zotero');
    expect(host.textContent).toContain('smith2020');
    expect(host.querySelectorAll('table tbody tr').length).toBe(1);
    expect([...host.querySelectorAll('li')].map(li => li.textContent)).toEqual(['Open the item in Zotero.', 'Use Attanger → Move attachment.']);
    expect(host.textContent).toContain('1 to correct by hand');
    expect(host.textContent).toContain('Excluded, not in this control: 12 image(s) embedded in Zotero notes; 2 other attachment(s) whose parent is a note; 3 web snapshot(s).');
});

it('renders every Library view without a raw JSON block and with the contractual vocabulary', async () => {
    const payloads = {
        overview: {freshness: {checked_at: 1758000000, scan_at: 1758003600}, populations: {references: 3, files: 2, attachments: 1}, coverage: {scan_only_entries: 1}},
        checks: {freshness: {checked_at: 1758000000}, counts: {review: 1}, results: [{id: 'file:a.pdf', pdf: 'a.pdf', status: 'review', method: 'deterministic', contradictions: [], uncertainties: ['PDF identity not independently confirmed'], sources: {mapping: {keys: ['k'], pending: false}, bibliography_records: [{doi: '10.1/x', title: 'T'}]}}]},
        links: {freshness: {}, scope: 'known notes', syntax_scope: 'wiki + md', inspected_notes: 1, known_notes: 1, complete: true, notes: [{note: 'n.md', status: 'candidate', links: [{target: 'a.pdf', status: 'candidate', candidates: ['file:a.pdf'], basis: 'saved basename'}]}]},
        history: {freshness: {}, note_campaigns: [{id: 'c1', kind: 'notes', created_at: 1700000000, scope: '/v', proposals: {k1: {}}, outcomes: {k1: {ok: true, status: 'undone', at: 1700000100}}}],
                  pdf_operations: [{id: 'op1', key: 'k1', status: 'complete', at: 1700000000, steps: [{rel: 'a_1.pdf', action: 'delete', status: 'complete', recovery: '/r/0.pdf'}], plan: {survivor: {rel: 'a.pdf'}}}]},
        documents: {freshness: {checked_at: 100}, populations: {references: 1, files: 1}, documents: [
            {id: 'f', kind: 'file', title: 'a.pdf', row: {mapping: {keys: ['k']}, state: 'Needs identification', pdf: {rel: 'a.pdf'}, physical: {sha256: 'x'}}}]},
    };
    vi.stubGlobal('fetch', vi.fn(async (url) => ({ok: true, json: async () => payloads[String(url).split('/library/')[1].split('?')[0]] || {rows: []}})));
    const {selectAuditReport} = await import('../../static/js/audit.js');
    const host = document.querySelector('#audit-report-body');
    for (const view of ['overview', 'checks', 'links', 'history', 'documents']) {
        await selectAuditReport('library_' + view);
        expect(host.querySelector('pre'), view).toBeNull();
        expect(host.textContent, view).not.toContain('{"');
    }
    await selectAuditReport('library_overview');
    expect(host.textContent).toContain('2 physical PDF files in the bibliography folder — scan of');
    expect(host.textContent).toContain('3 references (Zotero items and scan records) — Zotero observation of');
    await selectAuditReport('library_checks');
    expect(host.textContent).toContain('Review required: 1');
    expect(host.textContent).toContain('PDF identity not independently confirmed');
    await selectAuditReport('library_history');
    expect(host.textContent).toContain('Undone on both notes');
    expect(host.textContent).toContain('a_1.pdf'); // the per-step results table renders the file and its result
    expect(host.textContent).toContain('Complete');
    await selectAuditReport('library_documents');
    expect(host.textContent).toContain('Identity to review');
    expect(host.textContent).not.toContain('Needs identification');
});

it('selects exactly one report tab when a redirected tab opens a Documents filter', async () => {
    // 2026-09-17 review, D1: both redirected tabs matched no data-report, so
    // every tab got tabindex=-1 and keyboard users lost the tablist.
    document.getElementById('audit-report-tabs').innerHTML = `
        <button class="audit-report-tab" role="tab" id="audit-rtab-inventory" data-report="inventory" tabindex="0">Inventory</button>
        <button class="audit-report-tab" role="tab" id="audit-rtab-pdf_review" data-report="library_documents:identity" tabindex="-1">PDF identification review</button>
        <button class="audit-report-tab" role="tab" id="audit-rtab-ambiguous_pdfs" data-report="library_documents:ambiguous" tabindex="-1">Ambiguous PDFs</button>`;
    vi.stubGlobal('fetch', vi.fn(async () => ({ok: true, json: async () => ({freshness: {}, populations: {}, documents: []})})));
    const {selectAuditReport} = await import('../../static/js/audit.js');
    for (const [name, id] of [['library_documents:identity', 'audit-rtab-pdf_review'], ['library_documents:ambiguous', 'audit-rtab-ambiguous_pdfs']]) {
        await selectAuditReport(name);
        const focusable = [...document.querySelectorAll('.audit-report-tab[tabindex="0"]')];
        expect(focusable.map(t => t.id)).toEqual([id]);
        expect([...document.querySelectorAll('.audit-report-tab[aria-selected="true"]')].map(t => t.id)).toEqual([id]);
        expect(document.getElementById('audit-report-body').getAttribute('aria-labelledby')).toBe(id);
    }
});

it('prints arbitrary scalars verbatim and dates only date-named fields', async () => {
    // 2026-09-17 review, D2: every leaf string went through the state
    // vocabulary (a title "Recovery" read as an inspection state) and every
    // number over 1e9 rendered as a date (a 2 GB file size).
    const payload = {freshness: {}, populations: {references: 1, files: 0}, documents: [
        {id: 'r', kind: 'reference', title: 'Recovery', note: 'Recovery', attachments: [], physical: {size: 2000000000, checked_at: 1789600000}},
    ]};
    vi.stubGlobal('fetch', vi.fn(async () => ({ok: true, json: async () => payload})));
    const {selectAuditReport} = await import('../../static/js/audit.js');
    await selectAuditReport('library_documents');
    [...document.querySelectorAll('button')].find(b => b.textContent.startsWith('reference: Recovery')).click();
    const text = document.body.textContent;
    expect(text).toContain('2000000000');
    expect(text).not.toContain('Observation or operation requires inspection');
    expect(text).toContain(new Date(1789600000 * 1000).toLocaleString());
});

it('renders structured bibliography values as names and facts, never JSON', async () => {
    // 2026-09-17 review, D3: `Current: ${JSON.stringify(field.current)}`.
    const creators = [{firstName: 'Ada', lastName: 'Test', creatorType: 'author'}];
    const campaign = {id: 'fixture', kind: 'bibliography', proposals: {input: {provider: 'ollama', model: 'fixture', max_tokens: 128}}, outcomes: {
        generation: {status: 'proposed', proposals: [{id: 'r', fields: [{field: 'creators', current: creators, proposed: [...creators, {firstName: 'Bo', lastName: 'Two'}], evidence: 'digest', excerpt_verified: true, issues: [{kind: 'note', detail: 'x'}]}]}]},
    }};
    vi.stubGlobal('fetch', vi.fn(async () => ({ok: true, json: async () => ({freshness: {}, note_campaigns: [campaign]})})));
    const {selectAuditReport} = await import('../../static/js/audit.js');
    await selectAuditReport('library_history');
    const legend = [...document.querySelectorAll('fieldset legend')].find(l => l.textContent === 'creators');
    const panel = legend.parentElement;
    const shown = [...panel.querySelectorAll('p')].map(p => p.textContent).filter(t => t.startsWith('Current') || t.startsWith('Proposed')).join('\n');
    expect(shown).toContain('Ada Test');
    expect(shown).toContain('Bo Two');
    expect(shown).not.toMatch(/[{}"]/);
});

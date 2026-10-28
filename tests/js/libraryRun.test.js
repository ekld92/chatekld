// "Update my library" (static/js/libraryRun.js) — behaviour, not source text.
//
// What is pinned: nothing is written without ticking rows and confirming; the
// session model is sent only when tags are asked for; nothing is pre-ticked;
// the approvals name units (never paths the server does not own) and the
// mirror's reviewed digest with its units; an uncertain outcome is stated;
// the online/local disclosure follows the chosen provider.
import { beforeEach, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const { fetch, sse, confirms } = vi.hoisted(() => ({ fetch: vi.fn(), sse: vi.fn(), confirms: [] }));
vi.mock('../../static/js/api.js', () => ({ secureFetch: fetch, consumeSSE: sse }));
vi.mock('../../static/js/ui.js', () => ({
    taskBegin: vi.fn(), taskEnd: vi.fn(), openModal: vi.fn(),
    confirmInline: (btn, opts) => confirms.push(opts),
    announceStatus: () => {},
    statusLine: (host, { text }) => { host.textContent = text; return host; },
    relativeStamp: () => 'just now',
}));
import { mountLibraryRun } from '../../static/js/libraryRun.js';

const tick = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const ok = (body) => ({ ok: true, json: async () => body });

const REVIEW = {
    observation: { initialized: false, new: { new_entry_with_pdf: ['attachment:1:A', 'entry:1:E'] } },
    blocked: [],
    renames: [{ unit: 'attachment:1:A', rel: 'Jones - 2026.pdf', name: 'Jones - 2026.pdf',
        proposal: 'jones_2026.pdf', can_apply: true }],
    tags: [{ unit: 'attachment:1:A', citation_key: 'jones2026', source: 'pdf', from_cache: false,
        sent: { online: false, provider: 'ollama' }, proposed: [{ tag: 'dep' }, { tag: 'mood' }] }],
    mirror: { plan_digest: 'digest-1', to_create: 1, to_refresh: 0, to_baseline: 1, listed: {} },
    mirror_units: ['note:1:N'],
};

const sessionCall = () => fetch.mock.calls.find((c) => c[0] === '/api/library/sessions');

function stubRun(review = REVIEW, applyResult = null, models = { models: ['llama3.1:8b', 'gpt-4o-mini'], configured: 'llama3.1:8b' }) {
    fetch.mockImplementation(async (url) => String(url).includes('/api/tagging/zotero-available')
        ? ok({writable: true, mode: 'auto'}) : String(url).startsWith('/api/models')
        ? ok({ ...models, provider: new URL('http://x' + url).searchParams.get('provider') })
        : ok({ session: { id: 'S1' } }));
    sse.mockImplementation(async (url, opts, handlers) => {
        if (url.endsWith('/analyze')) handlers.onOther({ library_analyze: review });
        if (url.endsWith('/apply')) { handlers.onOpen?.(); handlers.onOther({ library_apply: applyResult }); }
    });
}

beforeEach(() => {
    // Each test owns its browser session; earlier analyses must not trigger
    // automatic recovery fetches in the next test.
    vi.unstubAllGlobals();
    try { localStorage.clear(); } catch (_) { /* storage unavailable */ }
    fetch.mockReset(); sse.mockReset(); confirms.length = 0;
    document.body.innerHTML = '<div id="host"></div>';
});

it('retains the next preparation choice when the section is remounted', () => {
    stubRun();
    const host = document.getElementById('host');
    mountLibraryRun(host);
    document.getElementById('library-online-yes').click();
    mountLibraryRun(host);
    expect(document.getElementById('library-online-yes').checked).toBe(true);
    document.getElementById('library-online-no').click();
});

it('previews a direct search update without scan, observation, analysis or writes', async () => {
    stubRun();
    mountLibraryRun(document.getElementById('host'));
    fetch.mockClear();
    let detail;
    const listener = event => { detail = event.detail; event.detail.waitUntil(Promise.resolve()); };
    document.addEventListener('library-index-preview', listener);
    try {
        document.getElementById('library-daily-update').click(); await tick();
        expect(detail).toMatchObject({prepareOnly: false, onlineChoice: false});
        expect(detail.anchor.id).toBe('library-daily-update');
        expect(fetch).not.toHaveBeenCalled();
        expect(sse).not.toHaveBeenCalled();
        document.getElementById('library-online-yes').click();
        document.getElementById('library-daily-update').click(); await tick();
        expect(detail.onlineChoice).toBe(true);
        document.getElementById('library-online-no').click();
    } finally { document.removeEventListener('library-index-preview', listener); }
});

it('keeps both entry actions disabled until the preview resolves and while extraction is active', async () => {
    stubRun();
    mountLibraryRun(document.getElementById('host'));
    let release;
    const listener = event => event.detail.waitUntil(new Promise(resolve => { release = resolve; }));
    document.addEventListener('library-index-preview', listener);
    try {
        const update = document.getElementById('library-daily-update');
        const prepare = document.getElementById('library-daily-prepare');
        update.click(); await tick();
        expect(update.disabled).toBe(true);
        expect(prepare.disabled).toBe(true);
        release(); await tick();
        expect(update.disabled).toBe(false);
        document.dispatchEvent(new CustomEvent('library-extraction-busy', {detail: {busy: true}}));
        expect(update.disabled).toBe(true);
        expect(prepare.disabled).toBe(true);
        document.dispatchEvent(new CustomEvent('library-extraction-busy', {detail: {busy: false}}));
        expect(update.disabled).toBe(false);
    } finally { document.removeEventListener('library-index-preview', listener); }
});

it('keeps daily buttons and choices in the explicit keyboard tab order', async () => {
    stubRun();
    mountLibraryRun(document.getElementById('host'));
    expect(document.getElementById('library-daily-prepare').getAttribute('tabindex')).toBe('0');
    expect(document.getElementById('library-online-yes').getAttribute('tabindex')).toBe('0');
    document.getElementById('library-run-analyse').click(); await tick();
    expect(document.querySelector('[data-kind="tag"]').getAttribute('tabindex')).toBe('0');
    expect(document.getElementById('library-run-apply').getAttribute('tabindex')).toBe('0');
});

it('blocks selected Zotero writes when Desktop authorization is missing', async () => {
    stubRun();
    const base = fetch.getMockImplementation();
    fetch.mockImplementation((url, ...args) => String(url).includes('/api/tagging/zotero-available')
        ? ok({writable: false, mode: 'auto', note: 'Authorize Desktop first.'}) : base(url, ...args));
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    document.querySelector('[data-kind="tag"]').checked = true;
    document.getElementById('library-run-apply').click(); await tick();
    expect(confirms).toHaveLength(0);
    expect(document.getElementById('library-zotero-access').textContent).toContain('Authorize Desktop');
    expect(document.activeElement.textContent).toContain('Authorize Zotero Desktop');
    expect(document.querySelector('#library-run-review button:not([hidden])')?.isConnected).toBe(true);
    expect(sse.mock.calls.filter(([url]) => url.endsWith('/apply'))).toHaveLength(0);
});

it('keeps a failed retry access warning and authorization beside its result', async () => {
    stubRun(REVIEW, {session: 'S1', tags: [{unit: 'attachment:1:A', citation_key: 'jones2026',
        tags: ['dep'], status: 'failed', targets: {finder: 'done', zotero: 'failed'}}]});
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    document.querySelector('[data-kind="rename"]').checked = true;
    document.getElementById('library-run-apply').click(); await tick();
    await confirms[0].onConfirm(); await tick();
    const base = fetch.getMockImplementation();
    fetch.mockImplementation((url, ...args) => String(url).includes('/api/tagging/zotero-available')
        ? ok({writable: false, mode: 'auto', note: 'Authorize Desktop first.'}) : base(url, ...args));
    [...document.querySelectorAll('#library-run-results button')].find(b => b.textContent.includes('Retry failed tag')).click();
    await tick();
    expect(document.getElementById('library-retry-access').textContent).toContain('Authorize Desktop');
    expect(document.activeElement.id).toBe('library-retry-authorize');
    expect(confirms).toHaveLength(1);
});

it('offers an explicit retry for failed destinations only', async () => {
    stubRun(REVIEW, {session: 'S1', tags: [{unit: 'attachment:1:A', citation_key: 'jones2026',
        tags: ['dep'], status: 'failed', targets: {finder: 'done', zotero: 'failed'}}]});
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    document.querySelector('[data-kind="tag"]').checked = true;
    document.getElementById('library-run-apply').click(); await tick();
    await confirms[0].onConfirm(); await tick();
    const button = [...document.querySelectorAll('#library-run-results button')].find(b => b.textContent.includes('Retry failed tag'));
    expect(button).toBeTruthy();
    button.click(); await tick();
    expect(confirms[1].message).toContain('zotero');
    sse.mockImplementation(async (url, opts, handlers) => {
        handlers.onOpen?.();
        handlers.onOther({library_apply: {tags: [{unit: 'attachment:1:A', status: 'done', targets: {zotero: 'done'}}]}});
    });
    await confirms[1].onConfirm(); await tick();
    const body = JSON.parse(sse.mock.calls.at(-1)[1].body);
    expect(body.approvals.tags[0]).toMatchObject({retry_failed_only: true,
        targets: {finder: false, zotero: true, note: false}});
    expect([...document.querySelectorAll('#library-run-results button')].find(b => b.textContent === 'Review search index update').disabled).toBe(true);
});

it('analysing without tags sends no model and pre-ticks nothing', async () => {
    stubRun();
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    expect(JSON.parse(sessionCall()[1].body)).toEqual({});
    expect(JSON.parse(sse.mock.calls[0][1].body)).toEqual({ session: 'S1', propose_tags: false });
    // Rows carry data-kind; the three tag DESTINATION boxes are ticked by
    // maintainer decision (2026-09-22) and are not rows.
    const boxes = [...document.querySelectorAll('#library-run-review input[type=checkbox][data-kind]')];
    expect(boxes.length).toBeGreaterThan(0);
    expect(boxes.every((b) => !b.checked)).toBe(true);
});

it('asking for tags without a model writes nothing', async () => {
    stubRun();
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-tags').checked = true;
    document.getElementById('library-run-model').value = '';
    document.getElementById('library-run-analyse').click(); await tick();
    expect(sessionCall()).toBeUndefined();
    expect(sse).not.toHaveBeenCalled();
    expect(document.getElementById('library-run-status').textContent).toContain('Choose the model');
});

it('the session model rides the session, with its limit', async () => {
    stubRun();
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-tags').checked = true;
    const provider = document.getElementById('library-run-provider');
    provider.value = 'openai'; provider.dispatchEvent(new Event('change'));
    document.getElementById('library-run-model').value = 'gpt-4o-mini';
    document.getElementById('library-run-budget').value = '0.5';
    expect(document.getElementById('library-run-online-note').textContent).toContain('sent to openai');
    document.getElementById('library-run-analyse').click(); await tick();
    expect(JSON.parse(sessionCall()[1].body)).toEqual({ llm: {
        provider: 'openai', model: 'gpt-4o-mini', budget_usd: 0.5 } });
});

it('applying with nothing ticked asks for a row and sends nothing', async () => {
    stubRun();
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    document.getElementById('library-run-apply').click(); await tick();
    expect(confirms).toHaveLength(0);
    expect(sse).toHaveBeenCalledTimes(1);
});

it('apply is confirmed, names the destinations, and sends units and the reviewed digest', async () => {
    stubRun(REVIEW, { renames: [{ unit: 'attachment:1:A', from: 'Jones - 2026.pdf', to: 'jones_2026.pdf', status: 'done' }],
        tags: [{ unit: 'attachment:1:A', status: 'uncertain', error: 'bad gateway' }], mirror: null });
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    document.getElementById('library-run-select-all').click();
    document.getElementById('library-run-apply').click(); await tick();
    expect(confirms).toHaveLength(1);
    expect(confirms[0].message).toContain('Zotero entry');
    expect(confirms[0].message).toContain('git');
    expect(sse).toHaveBeenCalledTimes(1); // nothing before the confirm
    await confirms[0].onConfirm(); await tick();
    const body = JSON.parse(sse.mock.calls[1][1].body);
    expect(body).toMatchObject({ session: 'S1', confirm: true });
    expect(body.approvals.renames).toEqual([{ unit: 'attachment:1:A', rel: 'Jones - 2026.pdf', new_name: 'jones_2026.pdf' }]);
    expect(body.approvals.tags).toEqual([{ unit: 'attachment:1:A', citation_key: 'jones2026', tags: ['dep', 'mood'],
        targets: { finder: false, zotero: true, note: false } }]);
    expect(body.approvals.mirror).toEqual({ plan_digest: 'digest-1', units: ['note:1:N'], create_keys: [] });
    expect(body.approvals.pdf_links).toEqual([]);
    const results = document.getElementById('library-run-results').textContent;
    expect(results).toContain('uncertain');
    expect(results).toContain('will not be retried automatically');
    expect([...document.querySelectorAll('#library-run-results button')].find(b => b.textContent === 'Review search index update').disabled).toBe(true);
    expect([...document.querySelectorAll('#library-run-review button')].find(b => b.textContent.startsWith('Skip selected')).disabled).toBe(true);
});

it('the first run says the library became the starting point', async () => {
    stubRun({ ...REVIEW, observation: { initialized: true, new: {} }, renames: [], tags: [], mirror: {} });
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    expect(document.getElementById('library-run-review').textContent).toContain('starting point');
});

it('renders data through textContent only', () => {
    const src = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', '..',
        'static', 'js', 'libraryRun.js'), 'utf8');
    expect(src).not.toMatch(/innerHTML|insertAdjacentHTML|outerHTML/);
});

// Lot 1 (2026-09-22): a real "no spending limit" option.
it('"no spending limit" sends no_limit and never a budget', async () => {
    stubRun();
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-tags').checked = true;
    const provider = document.getElementById('library-run-provider');
    provider.value = 'openai'; provider.dispatchEvent(new Event('change'));
    document.getElementById('library-run-model').value = 'gpt-4o-mini';
    const budget = document.getElementById('library-run-budget');
    budget.value = '0.5';
    const noLimit = document.getElementById('library-run-no-limit');
    noLimit.checked = true; noLimit.dispatchEvent(new Event('change'));
    expect(budget.value).toBe('');
    expect(budget.disabled).toBe(true);
    document.getElementById('library-run-analyse').click(); await tick();
    expect(JSON.parse(sessionCall()[1].body)).toEqual({ llm: {
        provider: 'openai', model: 'gpt-4o-mini', no_limit: true } });
    expect(document.getElementById('library-run-unpriced')).toBeNull();
});

it('the limit controls exist only for an online choice', () => {
    mountLibraryRun(document.getElementById('host'));
    const cost = document.getElementById('library-run-cost');
    expect(cost.hidden).toBe(true);
    const provider = document.getElementById('library-run-provider');
    provider.value = 'openai'; provider.dispatchEvent(new Event('change'));
    expect(cost.hidden).toBe(false);
    provider.value = 'ollama'; provider.dispatchEvent(new Event('change'));
    const model = document.getElementById('library-run-model');
    model.value = 'gpt-oss:120b-cloud'; model.dispatchEvent(new Event('input'));
    expect(cost.hidden).toBe(false);
});

// Lot 2 (2026-09-22): pdf: links group, tag destinations, results.
const PDF_REVIEW = { ...REVIEW, pdf_links: { counts: {}, rows: [
    { citation_key: 'a2020', state: 'absent', expected: ['a_2020.pdf'], listed: [], note: 'a2020.md', selectable: true },
    { citation_key: 'b2020', state: 'dirty', expected: ['b_2020.pdf'], listed: ['hand'], note: 'b2020.md', selectable: false,
      reason: 'a pdf: list this app did not write; edit it by hand' },
] } };

it('pdf: link rows are selectable only when the writer can act, and ride the approvals', async () => {
    stubRun(PDF_REVIEW, { mirror: null, pdf_links: [{ citation_key: 'a2020', status: 'done', changed: true }],
        tags: [], renames: [], notes_written: 1 });
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    const boxes = [...document.querySelectorAll('input[data-kind=pdf]')];
    expect(boxes.map((b) => b.dataset.key)).toEqual(['a2020']);
    expect(document.getElementById('library-run-review').textContent).toContain('did not write');
    boxes[0].checked = true;
    document.getElementById('library-run-apply').click(); await tick();
    expect(confirms[0].message).toContain('pdf: list into 1 note');
    await confirms[0].onConfirm(); await tick();
    const body = JSON.parse(sse.mock.calls[1][1].body);
    expect(body.approvals.pdf_links).toEqual(['a2020']);
    expect(Object.keys(body.approvals)).toEqual(['mirror', 'pdf_links', 'tags', 'renames']);
    const results = document.getElementById('library-run-results').textContent;
    expect(results).toContain('PDF links');
    expect(results).toContain('uncommitted');
});

it('Zotero is the default destination and optional destinations remain selectable', async () => {
    stubRun(REVIEW, { mirror: null, pdf_links: [], tags: [], renames: [] });
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    for (const k of ['finder', 'zotero', 'note']) expect(document.getElementById(`library-run-target-${k}`).checked).toBe(k === 'zotero');
    document.getElementById('library-run-target-finder').checked = true;
    document.getElementById('library-run-target-note').checked = false;
    document.querySelector('input[data-kind=tag]').checked = true;
    document.getElementById('library-run-apply').click(); await tick();
    expect(confirms[0].message).toContain('Finder tags, Zotero entry');
    expect(confirms[0].message).not.toContain('Obsidian note');
    await confirms[0].onConfirm(); await tick();
    const body = JSON.parse(sse.mock.calls[1][1].body);
    expect(body.approvals.tags[0].targets).toEqual({ finder: true, zotero: true, note: false });
    // A destination box is not a row: select-all never ticks or counts it.
    expect(document.querySelectorAll('input[type=checkbox][data-kind]').length).toBe(4);
});

// Lot 5 (2026-09-22): the layout.
it('the Model block appears only when tags are wanted, lists the provider’s models and prefills the configured one', async () => {
    stubRun();
    mountLibraryRun(document.getElementById('host'));
    const block = document.getElementById('library-run-model-block');
    expect(block.hidden).toBe(true);
    expect(fetch.mock.calls).toEqual([['/api/obsidian/readiness', {signal: expect.any(AbortSignal)}]]);
    const tags = document.getElementById('library-run-tags');
    tags.checked = true; tags.dispatchEvent(new Event('change')); await tick();
    expect(block.hidden).toBe(false);
    // By URL, not position: the price list (audit remediation 2026-09-23) is
    // fetched beside the model list.
    const modelCalls = () => fetch.mock.calls.map((c) => c[0]).filter((u) => u.startsWith('/api/models'));
    expect(modelCalls()[0]).toBe('/api/models?provider=ollama');
    const model = document.getElementById('library-run-model');
    expect(model.value).toBe('llama3.1:8b');
    expect([...document.querySelectorAll('#library-run-models option')].map((o) => o.value)).toEqual(['llama3.1:8b', 'gpt-4o-mini']);
    expect(document.getElementById('library-run-model-hint').textContent).toContain('2 model(s)');
    // A typed value survives a later list; the provider change refetches.
    model.value = 'mine'; model.dispatchEvent(new Event('input'));
    const provider = document.getElementById('library-run-provider');
    provider.value = 'openai'; provider.dispatchEvent(new Event('change')); await tick();
    expect(modelCalls()[1]).toBe('/api/models?provider=openai');
    expect(model.value).toBe('mine');
    // The list is free text with suggestions, never a closed select.
    expect(model.getAttribute('list')).toBe('library-run-models');
});

it('the intro is one sentence with a collapsed "How to undo" and recovery above the form', () => {
    vi.stubGlobal('localStorage', { getItem: () => 'S9', setItem() {}, removeItem() {} });
    mountLibraryRun(document.getElementById('host'));
    const host = document.getElementById('host');
    const intro = host.querySelector('p.control-hint');
    expect(intro.textContent).not.toContain('Restore');
    const undo = host.querySelector('details.library-run-undo');
    expect(undo.open).toBe(false);
    expect(undo.textContent).toContain('Settings → History and Restore');
    expect(undo.textContent).toContain('git');
    const recovery = host.querySelector('.library-run-recovery');
    expect(recovery.compareDocumentPosition(host.querySelector('fieldset.library-run-model')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(recovery.querySelector('button').textContent).toBe('Recover last run');
    vi.unstubAllGlobals();
});

it('every review group is a table with its count, its own All/None, and select-all names the groups', async () => {
    stubRun(PDF_REVIEW);
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    const heads = [...document.querySelectorAll('fieldset .library-run-group-head h4')].map((h) => h.textContent);
    expect(heads).toEqual(['Names (1)', 'PDF links in notes (1)', 'Tags (1)', 'Notes mirror (1)']);
    const tables = document.querySelectorAll('fieldset table.library-run-group');
    expect(tables.length).toBe(4);
    for (const t of tables) expect([...t.querySelectorAll('th')].map((x) => x.textContent).length).toBe(4);
    // The Tags group's own All ticks its chips and nothing else.
    const tagsHead = [...document.querySelectorAll('.library-run-group-head')].find((h) => h.textContent.includes('Tags'));
    tagsHead.querySelector('button').click();
    expect([...document.querySelectorAll('input[data-kind=tag]')].every((b) => b.checked)).toBe(true);
    expect(document.querySelector('input[data-kind=rename]').checked).toBe(false);
    tagsHead.querySelectorAll('button')[1].click();
    expect([...document.querySelectorAll('input[data-kind=tag]')].some((b) => b.checked)).toBe(false);
    expect(document.getElementById('library-run-select-all').textContent).toBe('Select all (names, PDF links, tags, the notes mirror)');
});

it('results are grouped in apply order under a one-line summary, in plain words', async () => {
    stubRun(REVIEW, { mirror: { status: 'done', created: 1, refreshed: 0 },
        pdf_links: [{ citation_key: 'a2020', status: 'done', changed: false }],
        tags: [{ unit: 'attachment:1:A', status: 'failed', targets: { finder: 'done', zotero: 'failed' }, error: '412' }],
        renames: [{ unit: 'attachment:1:A', from: 'a.pdf', to: 'b.pdf', status: 'uncertain', tracker: 'identity' }] });
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    document.getElementById('library-run-select-all').click();
    document.getElementById('library-run-apply').click(); await tick();
    await confirms[0].onConfirm(); await tick();
    const results = document.getElementById('library-run-results');
    expect(results.querySelector('.library-run-summary').textContent).toBe('2 done · 1 failed · 1 uncertain');
    expect([...results.querySelectorAll('h5')].map((h) => h.textContent)).toEqual(['Notes mirror', 'PDF links in notes', 'Tags', 'Names']);
    const outcomes = [...results.querySelectorAll('td[data-status]')].map((td) => td.textContent);
    expect(outcomes[0]).toBe('Done');
    expect(outcomes[1]).toBe('Done');
    expect(outcomes[2]).toBe('Failed — finder: done, zotero: failed; 412');
    expect(outcomes[3]).toBe('Uncertain — check before retrying');
    expect(results.textContent).toContain('already current');
    expect(results.textContent).toContain('identity');
});

// Review 2026-09-22 (each proven red on f7869935).
it('the mirror approval names the notes it creates, and ticked tags that did not land are shown', async () => {
    stubRun({ ...REVIEW, mirror: { ...REVIEW.mirror, create_keys: ['jones2026'] } },
        { mirror: { status: 'done', created: 1, refreshed: 0,
            note_tags_unwritten: { jones2026: 'the tag vocabulary (_tags.md) could not be read' } },
          pdf_links: [], tags: [], renames: [] });
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    document.getElementById('library-run-select-all').click();
    document.getElementById('library-run-apply').click(); await tick();
    await confirms[0].onConfirm(); await tick();
    const body = JSON.parse(sse.mock.calls[1][1].body);
    expect(body.approvals.mirror.create_keys).toEqual(['jones2026']);
    const results = document.getElementById('library-run-results');
    const banner = [...results.querySelectorAll('.warning-banner')].find((b) => b.textContent.includes('were not written'));
    expect(banner.textContent).toContain('jones2026: the tag vocabulary (_tags.md) could not be read');
});

// Audit remediation 2026-09-23 (L1/N4/L10): what the run left undone is shown,
// not only what failed. Red before: none of these three lines existed.
it('notes left open, pdf: lists not written and a scan stop are shown', async () => {
    stubRun(REVIEW, { renames: [], tags: [], pdf_links: [], stopped: 'scan_running',
        blocked: 'An audit scan is running; nothing more was applied.',
        pdf_links_unwritten: { new2026: 'the pdf: list could not be written (dirty_list)' },
        mirror: { status: 'done', created: 1, refreshed: 0,
            open: [{ unit: 'note:1:N', citation_key: 'late2026', reason: 'rescan needed' }] } });
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    document.getElementById('library-run-select-all').click();
    document.getElementById('library-run-apply').click(); await tick();
    await confirms[0].onConfirm(); await tick();
    const results = document.getElementById('library-run-results').textContent;
    expect(results).toContain('late2026: rescan needed');
    expect(results).toContain('new2026: the pdf: list could not be written');
    expect(results).toContain('An audit scan is running');
});

// Audit remediation 2026-09-23 (L2): an uncertain row can be resolved by
// hand. Red before: the row had no control, and nothing cleared the state.
it('an uncertain row offers "Mark resolved…" and posts one confirmed resolution', async () => {
    stubRun(REVIEW, { session: 'S1', renames: [], mirror: null,
        tags: [{ unit: 'attachment:1:A', status: 'uncertain', error: 'bad gateway' }] });
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    document.getElementById('library-run-select-all').click();
    document.getElementById('library-run-apply').click(); await tick();
    await confirms[0].onConfirm(); await tick();
    const mark = [...document.querySelectorAll('#library-run-results button')]
        .find((b) => b.textContent === 'Mark resolved…');
    expect(mark).toBeTruthy();
    mark.click();
    const landed = [...document.querySelectorAll('#library-run-results button')]
        .find((b) => b.textContent === 'It landed');
    fetch.mockImplementation(async () => ok({ unit: { id: 'attachment:1:A' } }));
    landed.click();
    expect(confirms).toHaveLength(2);           // nothing posted before the confirm
    await confirms[1].onConfirm(); await tick();
    const call = fetch.mock.calls.find((c) => c[0] === '/api/library/steps');
    expect(JSON.parse(call[1].body)).toEqual({ session: 'S1', unit: 'attachment:1:A',
        step: 'tag', status: 'done', confirm: true });
    expect(document.getElementById('library-run-results').textContent).toContain('resolved by hand');
});

// Audit remediation 2026-09-23 (L3/L4): an unpriced online model runs only
// under "No spending limit", and a spend that could not be recorded is said.
it('an unpriced online model hides the limit and requires "No spending limit"', async () => {
    stubRun();
    fetch.mockImplementation(async (url) => String(url) === '/api/pricing'
        ? ok({ models: { 'gpt-4o-mini': { input: 0.15, output: 0.6 } } })
        : String(url).startsWith('/api/models') ? ok({ models: [], configured: '' })
        : ok({ session: { id: 'S1' } }));
    mountLibraryRun(document.getElementById('host'));
    const tags = document.getElementById('library-run-tags');
    tags.checked = true; tags.dispatchEvent(new Event('change')); await tick();
    const provider = document.getElementById('library-run-provider');
    provider.value = 'openai'; provider.dispatchEvent(new Event('change')); await tick();
    const model = document.getElementById('library-run-model');
    const budget = document.getElementById('library-run-budget');
    model.value = 'gpt-4o-mini'; model.dispatchEvent(new Event('input'));
    expect(budget.hidden).toBe(false);
    model.value = 'some-unpriced-model'; model.dispatchEvent(new Event('input'));
    expect(budget.hidden).toBe(true);
    document.getElementById('library-run-analyse').click(); await tick();
    expect(sessionCall()).toBeUndefined();
    expect(document.getElementById('library-run-status').textContent).toContain('No spending limit');
    document.getElementById('library-run-no-limit').click();
    document.getElementById('library-run-analyse').click(); await tick();
    expect(JSON.parse(sessionCall()[1].body)).toEqual({ llm: {
        provider: 'openai', model: 'some-unpriced-model', no_limit: true } });
});

it('a proposal whose cost was not recorded is announced in the review', async () => {
    stubRun({ ...REVIEW, tags: [{ ...REVIEW.tags[0], spend_error: true, spend: { usd: 0.01, unknown: true } }] });
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    expect(document.getElementById('library-run-review').textContent).toContain('could not be recorded');
});

// ── Audit remediation 2026-09-23, Batch 6 (UX). Each was red on a7a171d9. ──

async function analysed(review = REVIEW, applyImpl = null) {
    stubRun(review);
    if (applyImpl) {
        const base = sse.getMockImplementation();
        sse.mockImplementation(async (url, opts, h) => (url.endsWith('/apply') ? applyImpl(url, opts, h) : base(url, opts, h)));
    }
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
}

it('U1 an apply refused before the run began leaves the review editable, with no "may have been written"', async () => {
    await analysed(REVIEW, async (url, opts, h) => { h.onError('A library run is already in progress.', { error: 'x' }); });
    document.getElementById('library-run-select-all').click();
    document.getElementById('library-run-apply').click(); await tick();
    await confirms[0].onConfirm(); await tick();
    const host = document.getElementById('host');
    expect(host.textContent).not.toMatch(/may already have been written|Run finished/);
    expect(document.getElementById('library-run-apply').disabled).toBe(false);
    expect(document.getElementById('library-run-status').textContent).toContain('already in progress');
});

it('U2 a remount restores the saved review when no apply followed it', async () => {
    vi.stubGlobal('localStorage', { getItem: () => 'S9', setItem() {}, removeItem() {} });
    fetch.mockImplementation(async (url) => String(url).startsWith('/api/library/sessions/S9')
        ? ok({ session: { id: 'S9', last_analysis: { at: 200, result: REVIEW }, applies: [{ at: 100, result: { renames: [] } }] } })
        : ok({}));
    mountLibraryRun(document.getElementById('host')); await tick();
    expect(document.getElementById('library-run-review').textContent).toContain('Restored review');
    expect(document.getElementById('library-run-apply')).not.toBeNull();
    vi.unstubAllGlobals();
});

it('U4 a failed analysis keeps its error on screen and does not replace the saved session', async () => {
    const set = vi.fn();
    vi.stubGlobal('localStorage', { getItem: () => null, setItem: set, removeItem() {} });
    fetch.mockResolvedValue(ok({ session: { id: 'S1' } }));
    sse.mockImplementation(async (url, opts, h) => { h.onError('Observation failed: disk'); });
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    expect(document.getElementById('library-run-status').textContent).toContain('Observation failed');
    expect(set).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
});

it('U5 Stop aborts the running analysis', async () => {
    let signal = null;
    fetch.mockResolvedValue(ok({ session: { id: 'S1' } }));
    sse.mockImplementation((url, opts) => new Promise((resolve, reject) => {
        signal = opts.signal;
        opts.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
    }));
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    const stop = document.getElementById('library-run-stop');
    expect(stop.hidden).toBe(false);
    stop.click(); await tick();
    expect(signal.aborted).toBe(true);
    expect(document.getElementById('library-run-status').textContent).toContain('Stopped');
    expect(stop.hidden).toBe(true);
});

it('U6 progress frames drive a progress bar', async () => {
    fetch.mockResolvedValue(ok({ session: { id: 'S1' } }));
    let seen = null;
    sse.mockImplementation(async (url, opts, h) => {
        h.onInfo('Proposing tags for k… (3/10)');
        seen = document.getElementById('library-run-progress');
        seen = { hidden: seen.hidden, value: seen.value, max: seen.max };
        h.onOther({ library_analyze: REVIEW });
    });
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    expect(seen).toEqual({ hidden: false, value: 3, max: 10 });
});

it('U7 a mirror in the selection is confirmed in the danger tone; renames alone are not', async () => {
    await analysed();
    document.getElementById('library-run-select-all').click();
    document.getElementById('library-run-apply').click(); await tick();
    expect(confirms[0].tone).toBe('danger');
    confirms.length = 0;
    document.getElementById('library-run-select-none').click();
    document.querySelector('input[data-kind=rename]').checked = true;
    document.getElementById('library-run-apply').click(); await tick();
    expect(confirms[0].tone).toBe('confirm');
});

it('U8 "Select all" only selects; "Select none" clears', async () => {
    await analysed();
    const boxes = () => [...document.querySelectorAll('#library-run-review input[type=checkbox][data-kind]')];
    document.getElementById('library-run-select-all').click();
    document.getElementById('library-run-select-all').click();
    expect(boxes().every((b) => b.checked)).toBe(true);
    document.getElementById('library-run-select-none').click();
    expect(boxes().some((b) => b.checked)).toBe(false);
});

it('U13 the model controls have visible labels', () => {
    stubRun();
    mountLibraryRun(document.getElementById('host'));
    for (const id of ['library-run-provider', 'library-run-model', 'library-run-budget']) {
        expect(document.querySelector(`label[for="${id}"]`), id).not.toBeNull();
        expect(document.getElementById(id).hasAttribute('aria-label'), id).toBe(false);
    }
});

it('U15 the mirror group has no All/None and the tag cell has no arrow glyph', async () => {
    await analysed();
    const mirrorHead = [...document.querySelectorAll('.library-run-group-head')].find((h) => h.textContent.includes('Notes mirror'));
    expect(mirrorHead.querySelector('button')).toBeNull();
    expect(document.getElementById('library-run-review').textContent).not.toContain('tick tags →');
});

it('U3 an apply that finishes while the panel is hidden still paints its result', async () => {
    document.body.innerHTML = '<div class="content-area active" id="area"><div id="host"></div></div>';
    await analysed(REVIEW, async (url, opts, h) => { h.onOpen(); h.onOther({ library_apply: { renames: [], tags: [], mirror: null } }); });
    document.getElementById('library-run-select-all').click();
    document.getElementById('library-run-apply').click(); await tick();
    document.getElementById('area').classList.remove('active');   // the user moved on
    await confirms[0].onConfirm(); await tick();
    const heading = document.querySelector('#library-run-results h4');
    expect(heading).not.toBeNull();
    expect(document.activeElement).not.toBe(heading);
});

it('U14 the result time is local with the UTC instant in the tooltip', async () => {
    await analysed(REVIEW, async (url, opts, h) => { h.onOpen(); h.onOther({ library_apply: { renames: [], tags: [], mirror: null } }); });
    document.getElementById('library-run-select-all').click();
    document.getElementById('library-run-apply').click(); await tick();
    await confirms[0].onConfirm(); await tick();
    const heading = document.querySelector('#library-run-results h4');
    expect(heading.textContent).not.toContain('UTC');
    expect(heading.title).toMatch(/UTC$/);
});

it('shows provider diagnostics and a next step without starting a retry', async () => {
    stubRun();
    mountLibraryRun(document.getElementById('host'));
    document.dispatchEvent(new CustomEvent('library-extraction-status', {detail:{
        state:'extraction_incomplete', global_failure:true,
        source_errors:[{source:'figure.png', status:'authentication_failed', failure_scope:'global',
            provider:'deepseek', model:'deepseek-flash', http_status:401, provider_code:'invalid_api_key',
            error_detail:'API key rejected <script>untrusted</script>', next_step:'Check the provider key, then explicitly retry.'}]
    }}));
    const exceptions = document.getElementById('library-extraction-exceptions');
    expect(exceptions.open).toBe(true);
    expect(exceptions.textContent).toContain('HTTP 401');
    expect(exceptions.textContent).toContain('API key rejected <script>untrusted</script>');
    expect(exceptions.textContent).toContain('Next: Check the provider key');
    expect(exceptions.querySelector('script')).toBeNull();
    expect(sse).not.toHaveBeenCalled();
});

it('reports index readiness even when every selected source was deferred', () => {
    stubRun();
    const host = document.getElementById('host');
    mountLibraryRun(host);
    for (const [state, bm25, expected] of [
        ['completed_with_omissions', 'preparing', 'Search update is finishing'],
        ['completed_with_omissions', 'ready', 'Search updated with pending documents'],
        ['index_incomplete', null, 'saved recovery evidence'],
        ['extraction_incomplete', null, 'Preparation stopped'],
    ]) {
        document.dispatchEvent(new CustomEvent('library-extraction-status', {detail: {
            state, bm25_status: bm25, source_count: 3, deferred_count: 3,
            failure_scope: 'global',
        }}));
        expect(host.querySelector('#library-preparation-next').textContent).toContain(expected);
    }
});

it('blocked review controls remain locked after analysis releases busy state', async () => {
    stubRun({...REVIEW, blocked: ['Uncertain prior write'], renames: [], tags: [], mirror: {}});
    mountLibraryRun(document.getElementById('host'));
    document.getElementById('library-run-analyse').click(); await tick();
    const buttons = [...document.querySelectorAll('#library-run-review button')];
    expect(buttons.find(b => b.textContent.startsWith('Skip selected')).disabled).toBe(true);
    expect(buttons.find(b => b.textContent === 'Review search index update').disabled).toBe(true);
    expect(document.getElementById('library-daily-update').disabled).toBe(true);
    expect(document.getElementById('library-skip-proposals').disabled).toBe(true);
});

it('a remounted observation cannot open a preparation confirmation', async () => {
    let resolve;
    fetch.mockImplementation(url => String(url).endsWith('/sessions')
        ? new Promise(r => { resolve = r; }) : Promise.resolve(ok({})));
    const host = document.getElementById('host');
    mountLibraryRun(host);
    const preview = vi.fn();
    document.addEventListener('library-index-preview', preview);
    try {
        document.getElementById('library-daily-prepare').click(); await tick();
        mountLibraryRun(host);
        resolve(ok({session: {id: 'departed'}})); await tick();
        expect(preview).not.toHaveBeenCalled();
        expect(fetch.mock.calls.some(([url]) => String(url).endsWith('/observe'))).toBe(false);
    } finally { document.removeEventListener('library-index-preview', preview); }
});

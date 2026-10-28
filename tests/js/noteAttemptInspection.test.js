import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {dirname, join} from 'node:path';
import {describe, it, expect, vi} from 'vitest';

const source = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '../../static/js/audit.js'), 'utf8');
const handler = source.slice(source.indexOf('async function _noteSyncInspectCreate('),
    source.indexOf('\nfunction _noteSyncCreateZotero('));

function loadRender() {
    const body = source.slice(source.indexOf('function _renderCreateAttempts('),
        source.indexOf('\nfunction _renderTwoWaySync('));
    const escape = text => String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
    return new Function('_esc', '_attr', body + '; return _renderCreateAttempts;')(escape, escape);
}

describe('note attempt inspection', () => {
    // Behavioural, over the REAL rendered markup. The earlier version drove a
    // literal `{disabled, getAttribute}` object with no `closest`, so every
    // `btn.closest?.('.notesync-attempt')?.…` lookup short-circuited and the
    // test passed while asserting only that the screen-reader announcer fired —
    // exactly the hostless case that made the sync-table button render nothing.
    function mount() {
        document.body.innerHTML = loadRender()({rows: [{citation_key: 'paper',
            status: 'acknowledged', parent: 'PARENT23', note_key: 'NOTE2345', in_inventory: true}]});
        return document.querySelector('[data-action="notesync-inspect-create"]');
    }

    // `_nsSay` is what the handler calls now (2026-09-19): it paints
    // `#audit-ns-status` AND announces. Inject it, or the evaluated slice
    // throws a ReferenceError that its own `catch` swallows into `_showError`
    // — the failure would look like a transport error, not a missing binding.
    function handlerWith(fetch, error = vi.fn(), announce = vi.fn()) {
        return {run: new Function('secureFetch', '_showError', 'announceStatus', '_nsSay',
            handler + '; return _noteSyncInspectCreate;')(fetch, error, announce, announce),
            error, announce};
    }

    it('writes the outcome where the user can SEE it, not only to the announcer', async () => {
        const button = mount();
        const {run, announce} = handlerWith(vi.fn().mockResolvedValue({ok: true, json: async () => ({
            remote: 'changed', source: 'unchanged', target: {parent: 'PARENT23'}, note_key: 'NOTE2345'})}));
        await run(button);
        const shown = document.querySelector('.notesync-attempt-result').textContent;
        expect(shown).toContain('NOTE2345');
        expect(shown).toContain('Automatic retry and undo remain blocked');
        expect(announce).toHaveBeenCalledWith(shown);
        expect(button.disabled).toBe(false);
    });

    it('reveals the repair verbs only for an actionable inspection', async () => {
        const button = mount();
        const {run} = handlerWith(vi.fn().mockResolvedValue({ok: true, json: async () => ({
            remote: 'matches_acknowledgement', source: 'unchanged', frontmatter: 'missing_keys',
            target: {parent: 'PARENT23'}, note_key: 'NOTE2345',
            evidence_token: 'tok', source_sha256: 'sha'})}));
        await run(button);
        expect(document.querySelector('.notesync-attempt-repair').hidden).toBe(false);
        expect(document.querySelector('.notesync-attempt-keys').hidden).toBe(false);
        expect(document.querySelector('.notesync-attempt-keys').dataset.token).toBe('tok');
        // `already_keyed` is the resolve gate; `missing_keys` must not open it.
        expect(document.querySelector('.notesync-attempt-resolve').hidden).toBe(true);
        expect(document.querySelector('.notesync-attempt-satisfied').hidden).toBe(false);
        expect(document.querySelector('.notesync-attempt-retry').hidden).toBe(true);
        expect(document.querySelector('.notesync-attempt-adopt').hidden).toBe(true);
    });

    it('offers trash-gated retry and adopt only for their remotes', async () => {
        const button = mount();
        const fetch = vi.fn()
            .mockResolvedValueOnce({ok: true, json: async () => ({
                remote: 'trashed', source: 'unchanged', target: {parent: 'PARENT23'},
                note_key: 'NOTE2345', evidence_token: 'tok'})})
            .mockResolvedValueOnce({ok: true, json: async () => ({
                remote: 'unknown_outcome', source: 'missing', target: {parent: 'PARENT23'},
                evidence_token: 'tok2'})});
        const {run} = handlerWith(fetch);
        await run(button);
        expect(document.querySelector('.notesync-attempt-retry').hidden).toBe(false);
        expect(document.querySelector('.notesync-attempt-adopt').hidden).toBe(true);
        expect(document.querySelector('.notesync-attempt-uncertain').hidden).toBe(true);
        await run(button);
        expect(document.querySelector('.notesync-attempt-adopt').hidden).toBe(false);
        expect(document.querySelector('.notesync-attempt-uncertain').hidden).toBe(false);
        expect(document.querySelector('.notesync-attempt-retry').hidden).toBe(true);
    });

    it('names the frontmatter obstacle when no repair is offered', async () => {
        const button = mount();
        const {run} = handlerWith(vi.fn().mockResolvedValue({ok: true, json: async () => ({
            remote: 'matches_acknowledgement', source: 'unchanged', frontmatter: 'key_conflict',
            target: {parent: 'PARENT23'}, note_key: 'NOTE2345', evidence_token: 'tok'})}));
        await run(button);
        const shown = document.querySelector('.notesync-attempt-result').textContent;
        // Without this the row reads "matches its creation acknowledgement",
        // offers nothing, and never says why -- while the note points at a
        // DIFFERENT Zotero item, which set_frontmatter_keys calls a fact to surface.
        expect(shown).toMatch(/different Zotero item/i);
        expect(document.querySelector('.notesync-attempt-keys').hidden).toBe(true);
        expect(document.querySelector('.notesync-attempt-resolve').hidden).toBe(true);
    });

    it('reports an unavailable inspection without leaving the button disabled', async () => {
        const button = mount();
        const {run, error} = handlerWith(vi.fn().mockRejectedValue(new Error('offline')));
        await run(button);
        expect(error).toHaveBeenCalledWith('Error: offline');
        expect(button.disabled).toBe(false);
    });

    it('is emitted from the retained-attempts section only', () => {
        // The two-way sync table has no `.notesync-attempt` host, so a button
        // there can never render its own result. Exactly one emission site.
        expect(source.match(/data-action="notesync-inspect-create"/g)).toHaveLength(1);
        const section = source.slice(source.indexOf('function _renderCreateAttempts('),
            source.indexOf('\nfunction _renderTwoWaySync('));
        expect(section).toContain('data-action="notesync-inspect-create"');
    });
});

it('renders retained attempts independently of sync rows and disables absent inventory actions', () => {
    const body = source.slice(source.indexOf('function _renderCreateAttempts('), source.indexOf('\nfunction _renderTwoWaySync('));
    const escape = text => String(text).replaceAll('<', '&lt;').replaceAll('>', '&gt;');
    const render = new Function('_esc', '_attr', body + '; return _renderCreateAttempts;')(escape, escape);
    const html = render({rows: [
        {citation_key: 'in-sync-paper', status: 'acknowledged', parent: 'PARENT23', note_key: 'NOTE2345', in_inventory: true},
        {citation_key: '<removed>', status: 'uncertain', parent: 'OTHER234', in_inventory: false}
    ]});
    expect(html).toContain('in-sync-paper');
    expect(html).toContain('NOTE2345');
    expect(html).toContain('&lt;removed&gt;');
    expect(html.match(/data-action="notesync-inspect-create"/g)).toHaveLength(1);
    expect(html.match(/hidden class="audit-link-btn notesync-attempt-repair"/g)).toHaveLength(2);
    expect(html).toContain('manual review required');
    expect(html).toContain('notesync-attempt-result');
});

it('journal repair waits for explicit confirmation and sends the inspected token', async () => {
    const body = source.slice(source.indexOf('function _noteSyncRepairCreate('), source.indexOf('\nfunction _noteSyncCreateZotero('));
    const fetch = vi.fn().mockResolvedValue({ok: true, json: async () => ({op_id: 'saved', status: 'existing'})});
    let confirmation;
    const run = new Function('confirmInline', 'secureFetch', '_showError', 'announceStatus', '_loadReport',
        body + '; return _noteSyncRepairCreate;')((btn, options) => { confirmation = options; }, fetch, vi.fn(), vi.fn(), vi.fn());
    run({getAttribute: () => 'paper', dataset: {token: 'reviewed'}});
    expect(fetch).not.toHaveBeenCalled();
    await confirmation.onConfirm();
    expect(fetch.mock.calls[0][0]).toBe('/api/notesync/repair-create-journal');
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({citation_key: 'paper', evidence_token: 'reviewed', confirm: true});
});

it('key repair confirms the exact parent/note and sends the full-file preview hash', async () => {
    const body = source.slice(source.indexOf('function _noteSyncRepairKeys('), source.indexOf('\nfunction _noteSyncRepairCreate('));
    const fetch = vi.fn().mockResolvedValue({ok: true, json: async () => ({status: 'applied'})});
    let confirmation;
    const run = new Function('confirmInline', 'secureFetch', '_showError', 'announceStatus', '_loadReport',
        body + '; return _noteSyncRepairKeys;')((btn, options) => { confirmation = options; }, fetch, vi.fn(), vi.fn(), vi.fn());
    run({getAttribute: () => 'paper', dataset: {token: 'reviewed', sha: 'source-hash', parent: 'PARENT23', note: 'NOTE2345'}});
    expect(fetch).not.toHaveBeenCalled();
    expect(confirmation.message).toContain('PARENT23');
    expect(confirmation.message).toContain('NOTE2345');
    await confirmation.onConfirm();
    expect(fetch.mock.calls[0][0]).toBe('/api/notesync/repair-create-keys');
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({citation_key: 'paper', evidence_token: 'reviewed', source_sha256: 'source-hash', confirm: true});
});

it('resolution requires confirmation and preserves the inspected evidence and source hash', async () => {
    const body = source.slice(source.indexOf('function _noteSyncResolveCreate('), source.indexOf('\nfunction _noteSyncMarkSatisfied('));
    const fetch = vi.fn().mockResolvedValue({ok: true, json: async () => ({ok: true})});
    let confirmation;
    const run = new Function('confirmInline', 'secureFetch', '_showError', 'announceStatus', '_loadReport',
        body + '; return _noteSyncResolveCreate;')((btn, options) => { confirmation = options; }, fetch, vi.fn(), vi.fn(), vi.fn());
    run({getAttribute: () => 'paper', dataset: {token: 'reviewed', sha: 'source-hash'}});
    expect(fetch).not.toHaveBeenCalled();
    expect(confirmation.message).toContain('does not release the creation block');
    expect(confirmation.confirmLabel).toBe('Mark bookkeeping done');
    await confirmation.onConfirm();
    expect(fetch.mock.calls[0][0]).toBe('/api/notesync/resolve-create-attempt');
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({citation_key: 'paper', evidence_token: 'reviewed', source_sha256: 'source-hash', confirm: true});
});

it('allow-retry confirm posts to allow-create-retry', async () => {
    const body = source.slice(source.indexOf('function _noteSyncAllowRetry('), source.indexOf('\nfunction _noteSyncAdoptKey('));
    const fetch = vi.fn().mockResolvedValue({ok: true, json: async () => ({ok: true})});
    let confirmation;
    const run = new Function('confirmInline', 'secureFetch', '_showError', 'announceStatus', '_loadReport',
        body + '; return _noteSyncAllowRetry;')((btn, options) => { confirmation = options; }, fetch, vi.fn(), vi.fn(), vi.fn());
    run({getAttribute: () => 'paper', dataset: {token: 'reviewed'}});
    expect(confirmation.message).toMatch(/trash/i);
    await confirmation.onConfirm();
    expect(fetch.mock.calls[0][0]).toBe('/api/notesync/allow-create-retry');
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
        citation_key: 'paper', evidence_token: 'reviewed', confirm: true});
});

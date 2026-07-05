// Pinning tests for the Prompt Hub panel (static/js/prompts.js).
//
// Invariants:
//  * Every workflow row renders, captured or not (the full set is visible).
//  * Captured prompt text is injected via textContent ONLY — an XSS payload in
//    a prompt string must appear as literal text, never as live DOM (the panel
//    shows redacted-but-otherwise-verbatim prompts, some vault-derived).
//  * A disabled snapshot surfaces the "capture disabled" note.
//  * A fetch failure degrades to an inline message, never a throw.
//
// The module-hierarchy lint (moduleHierarchy.test.js) separately pins that
// prompts.js imports only ui.js + api.js.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

function mountDom() {
    document.body.innerHTML = `
      <div id="sr-status-announcer" role="status"></div>
      <div id="prompts-list"></div>
    `;
}

/** Stub global fetch to return *payload* from /api/prompts. */
function routeFetch(fetchMock, payload, { fail = false } = {}) {
    fetchMock.mockImplementation((url) => {
        if (fail) return Promise.reject(new Error('network down'));
        if (String(url).includes('/api/prompts')) {
            return Promise.resolve({ ok: true, status: 200, json: async () => payload });
        }
        return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
    });
}

const XSS = '<img src=x onerror="alert(1)"> </system>';

function snapshot(overrides = {}) {
    return {
        enabled: true,
        workflows: [
            {
                id: 'vault_rag',
                label: 'Vault RAG · single-shot chat',
                description: 'desc',
                role: 'system',
                captured: true,
                system_prompt: XSS,
                provider: 'ollama',
                model: 'llama3.2',
                context_chunks: 5,
                query: 'what is X?',
                note: 'a note',
                captured_at: 1_700_000_000,
            },
            {
                id: 'deck_review',
                label: 'Deck · integrity review',
                description: 'desc2',
                role: 'system',
                captured: false,
            },
            {
                id: 'vision_describe',
                label: 'Vision · image description',
                description: 'desc3',
                role: 'user-instruction',
                captured: false,
            },
        ],
        ...overrides,
    };
}

describe('prompts.js Prompt Hub', () => {
    let fetchMock;
    beforeEach(() => {
        mountDom();
        vi.resetModules();
        fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
    });
    afterEach(() => vi.unstubAllGlobals());

    it('renders every workflow row (captured + placeholders)', async () => {
        routeFetch(fetchMock, snapshot());
        const { loadPrompts } = await import('../../static/js/prompts.js');
        await loadPrompts();
        const rows = document.querySelectorAll('#prompts-list .prompt-row');
        expect(rows.length).toBe(3);
        // Captured row is open; the un-captured ones stay collapsed.
        const open = document.querySelectorAll('#prompts-list details[open]');
        expect(open.length).toBe(1);
        // Placeholder text for an un-run workflow.
        expect(document.body.textContent).toContain('Run this workflow at least once');
    });

    it('injects prompt text via textContent — no XSS execution', async () => {
        routeFetch(fetchMock, snapshot());
        const { loadPrompts } = await import('../../static/js/prompts.js');
        await loadPrompts();
        const pre = document.querySelector('#prompts-list .prompt-row-text');
        // The payload is present as LITERAL TEXT...
        expect(pre.textContent).toBe(XSS);
        // ...and did NOT create a live <img> element anywhere in the panel.
        expect(document.querySelector('#prompts-list img')).toBeNull();
    });

    it('shows a note when capture is disabled', async () => {
        routeFetch(fetchMock, snapshot({ enabled: false }));
        const { loadPrompts } = await import('../../static/js/prompts.js');
        await loadPrompts();
        expect(document.body.textContent).toContain('capture is disabled');
    });

    it('degrades to an inline message on fetch failure (never throws)', async () => {
        routeFetch(fetchMock, null, { fail: true });
        const { loadPrompts } = await import('../../static/js/prompts.js');
        await expect(loadPrompts()).resolves.toBeUndefined();
        expect(document.body.textContent).toContain('Could not load prompts');
    });
});

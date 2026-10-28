import {beforeEach, expect, it, vi} from 'vitest';
const {fetch} = vi.hoisted(() => ({fetch: vi.fn()}));
vi.mock('../../static/js/api.js', () => ({secureFetch: fetch, consumeSSE: vi.fn()}));
import {mountLibraryReadiness, extractionPreviewCounts} from '../../static/js/libraryRun.js';

beforeEach(() => { document.body.innerHTML = '<div id="host"></div>'; fetch.mockReset(); });
const tick = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };

it('distinguishes a saved manifest from all five unknown live counts', async () => {
    fetch.mockResolvedValue({ok: true, json: async () => ({recorded_sources: 12,
        message: 'Saved index metadata records an incomplete update.',
        manifest_at: 'yesterday', indexed_at: 'today', observed_at: 'now'})});
    mountLibraryReadiness(document.getElementById('host'));
    await tick();
    expect(document.body.textContent).toContain('Saved manifest: 12 distinct sources with chunks');
    expect(document.body.textContent).toContain('incomplete update');
    expect(document.body.textContent).toContain('current index: unknown');
    expect(document.body.textContent).toContain('pending or changed: unknown');
    expect(document.body.textContent).toContain('Manifest recorded: yesterday; index recorded: today');
    expect(fetch.mock.calls).toEqual([['/api/obsidian/readiness', {signal: expect.any(AbortSignal)}]]);
});

it('failed refresh clears the previous count and remains actionable', async () => {
    fetch.mockResolvedValueOnce({ok: true, json: async () => ({recorded_sources: 7})})
        .mockResolvedValueOnce({ok: false});
    mountLibraryReadiness(document.getElementById('host'));
    await tick();
    document.querySelector('button').click();
    await tick();
    expect(document.body.textContent).not.toContain('7 distinct');
    expect(document.body.textContent).toContain('Search evidence unavailable');
    expect(document.querySelector('button').disabled).toBe(false);
});

it('zero remains a snapshot count while missing preparation evidence is unknown', () => {
    const text = extractionPreviewCounts({source_count: 0, reused_count: 20});
    expect(text).toContain('unknown discovered');
    expect(text).toContain('0 selected for preparation');
    expect(text).toContain('20 complete caches reused');
    expect(text).toContain('unknown previously deferred');
    expect(text).toContain('not counts of searchable documents');
});

it('a stalled read releases refresh after its deadline without starting work', async () => {
    vi.useFakeTimers();
    try {
        fetch.mockImplementation((_url, {signal}) => new Promise((_resolve, reject) => {
            signal.addEventListener('abort', () => reject(new DOMException('Timed out', 'AbortError')));
        }));
        mountLibraryReadiness(document.getElementById('host'));
        expect(document.querySelector('button').disabled).toBe(true);
        await vi.advanceTimersByTimeAsync(10000);
        expect(document.querySelector('button').disabled).toBe(false);
        expect(document.body.textContent).toContain('Search evidence unavailable');
        expect(fetch).toHaveBeenCalledTimes(1);
    } finally { vi.useRealTimers(); }
});

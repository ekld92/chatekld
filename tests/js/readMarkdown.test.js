import {describe, it, expect, vi, beforeEach, afterEach} from 'vitest';
import {readFileSync} from 'node:fs';

// Exercise the shipped parser, the shared sanitizer and the real Read lifecycle.
const parserModule = {exports: {}};
new Function('module', 'exports', readFileSync('static/js/vendor/marked.min.js', 'utf8'))(parserModule, parserModule.exports);

function response(frames) {
    return {ok: true, body: new ReadableStream({start(c) {
        c.enqueue(new TextEncoder().encode(frames.map(f => 'data: ' + JSON.stringify(f) + '\n\n').join('') + 'data: [DONE]\n\n'));
        c.close();
    }})};
}

describe('Read Markdown display and raw exports', () => {
    let summarizer, frames, fetchMock, writeText;
    beforeEach(async () => {
        vi.resetModules();
        document.body.innerHTML = `
          <input id="pdf-upload" type="file">
          <div id="upload-overlay"></div>
          <div id="summary-view"></div>
          <div id="document-summary-content"></div>
          <div id="doc-error-boundary"></div>
          <button id="summarise-btn"></button>
          <button id="export-summary-btn"></button>
          <button id="export-summary-md-btn"></button>
          <select id="model-select"><option value="m">m</option></select>
          <select id="preset-select"><option value="p">p</option></select>
          <select id="report-type-select"><option value="r">r</option></select>
          <select id="audience-select"><option value="a">a</option></select>
          <select id="language-select"><option value="fr">fr</option></select>
          <input id="doc-focus-question" value="">
          <input id="doc-temp" value="0.3">
          <input id="doc-predict" value="4096">
          <input id="doc-ctx" value="32768">
          <input id="doc-top-p" value="0.9">
          <input id="doc-repeat-penalty" value="1.1">
          <textarea id="doc-system-prompt"></textarea>
        
          <button id="summary-copy-btn">Copy</button>
          <div id="summarise-notices"></div>
        `;
        vi.stubGlobal('marked', parserModule.exports);
        writeText = vi.fn(async () => {});
        vi.stubGlobal('navigator', {clipboard: {writeText}});
        frames = [];
        fetchMock = vi.fn(async (url) => {
            if (url === '/api/upload') return {ok: true, json: async () => ({upload_id: 'fixture', filename: 'fixture.pdf'})};
            if (url === '/api/summarise') return response(frames);
            return {ok: true, json: async () => ({})};
        });
        vi.stubGlobal('fetch', fetchMock);
        summarizer = await import('../../static/js/summarizer.js');
        Object.defineProperty(document.getElementById('pdf-upload'), 'files', {
            value: [new File(['fixture'], 'fixture.pdf', {type: 'application/pdf'})]
        });
        await summarizer.uploadPDF();
    });
    afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

    it('renders Markdown safely while copy and export keep exact model source', async () => {
        const raw = '# Findings\n\n**Evidence** and $n=681$.\n\n- First\n- Second\n\n<script>bad()</script><img src="x" onerror="bad()">';
        frames = [{token: raw.slice(0, 10)}, {token: raw.slice(10)}];
        await summarizer.summarisePDF();
        const content = document.getElementById('document-summary-content');
        expect(content.querySelector('h1').textContent).toBe('Findings');
        expect(content.querySelector('strong').textContent).toBe('Evidence');
        expect(content.querySelectorAll('li')).toHaveLength(2);
        expect(content.querySelector('script')).toBeNull();
        expect(content.querySelector('[onerror]')).toBeNull();
        summarizer.copySummary();
        expect(writeText).toHaveBeenCalledWith(raw);
        await summarizer.exportSummary('md');
        const call = fetchMock.mock.calls.find(([url]) => url === '/api/export-summary');
        expect(JSON.parse(call[1].body).content).toBe(raw);
    });

    it('flushes and exports a partial on error without exporting the error notice', async () => {
        const raw = '**Partial evidence**';
        frames = [{token: raw}, {error: 'Provider disconnected'}];
        await summarizer.summarisePDF();
        expect(document.querySelector('#document-summary-content strong').textContent).toBe('Partial evidence');
        expect(document.getElementById('summarise-notices').textContent).toContain('partial');
        expect(document.getElementById('export-summary-md-btn').disabled).toBe(false);
        await summarizer.exportSummary('md');
        const call = fetchMock.mock.calls.find(([url]) => url === '/api/export-summary');
        expect(JSON.parse(call[1].body).content).toBe(raw);
    });

    it('falls back to plain source when the parser is unavailable', async () => {
        vi.stubGlobal('marked', undefined);
        frames = [{token: '**Evidence**'}];
        await summarizer.summarisePDF();
        expect(document.getElementById('document-summary-content').textContent).toBe('**Evidence**');
    });

    it('does not copy readiness messages or a cleared summary', async () => {
        summarizer.copySummary();
        expect(writeText).not.toHaveBeenCalled();
        frames = [{token: '**Evidence**'}];
        await summarizer.summarisePDF();
        await summarizer.resetUpload();
        summarizer.copySummary();
        expect(writeText).not.toHaveBeenCalled();
    });
    it('cancels pending render timers when New Paper invalidates the view', async () => {
        const api = await import('../../static/js/api.js');
        let finish;
        vi.spyOn(api, 'consumeSSE').mockImplementation((_url, _options, callbacks) => {
            callbacks.onToken('**Old summary**');
            return new Promise(resolve => { finish = resolve; });
        });
        vi.useFakeTimers();
        const run = summarizer.summarisePDF();
        await summarizer.resetUpload();
        const content = document.getElementById('document-summary-content');
        content.textContent = 'New paper view';
        await vi.advanceTimersByTimeAsync(100);
        finish();
        await run;
        expect(content.textContent).toBe('New paper view');
        expect(document.getElementById('summarise-btn').disabled).toBe(true);
    });

});

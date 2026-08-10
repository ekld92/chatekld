// Link interception (WP-L1/L2): no anchor click may navigate the PyWebView
// window. classifyHref decides where a click goes — http/https to the system
// browser, relative vault paths to a Finder reveal, every other scheme
// swallowed — and installLinkInterceptor preventDefaults everything except
// in-page anchors. The failure this pins against: a relative link the LLM
// emitted navigated the whole window to a Flask 404 (the "dead page").
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { classifyHref, installLinkInterceptor } from '../../static/js/ui.js';

describe('classifyHref', () => {
    it('classifies http/https as url', () => {
        expect(classifyHref('https://example.com/x')).toEqual({ kind: 'url', value: 'https://example.com/x' });
        expect(classifyHref('http://example.com')).toEqual({ kind: 'url', value: 'http://example.com' });
        expect(classifyHref('HTTPS://EXAMPLE.COM')).toEqual({ kind: 'url', value: 'HTTPS://EXAMPLE.COM' });
    });

    it('classifies relative paths as path (percent-decoded)', () => {
        expect(classifyHref('notes/note.md')).toEqual({ kind: 'path', value: 'notes/note.md' });
        expect(classifyHref('notes/my%20note.md')).toEqual({ kind: 'path', value: 'notes/my note.md' });
        expect(classifyHref('folder/file.pdf')).toEqual({ kind: 'path', value: 'folder/file.pdf' });
    });

    it('keeps a malformed percent-encoding as a literal path', () => {
        expect(classifyHref('notes/50%.md')).toEqual({ kind: 'path', value: 'notes/50%.md' });
    });

    it('blocks every non-http scheme', () => {
        for (const href of [
            'file:///etc/passwd',
            'obsidian://open?vault=x',
            'javascript:alert(1)',
            'mailto:a@b.c',
            'ftp://host/x',
        ]) {
            expect(classifyHref(href).kind, href).toBe('blocked');
        }
    });

    it('blocks protocol-relative URLs', () => {
        expect(classifyHref('//evil.com/x').kind).toBe('blocked');
    });

    it('ignores empty hrefs and in-page anchors', () => {
        expect(classifyHref('').kind).toBe('ignore');
        expect(classifyHref('#section-2').kind).toBe('ignore');
        expect(classifyHref(null).kind).toBe('ignore');
    });
});

describe('installLinkInterceptor', () => {
    let fetchMock;

    beforeEach(() => {
        fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true }) });
        vi.stubGlobal('fetch', fetchMock);
        document.body.innerHTML = '';
    });

    afterEach(() => {
        vi.unstubAllGlobals();
    });

    function clickAnchor(href) {
        const root = document.createElement('div');
        // A fresh delegation root per test — `document` would accumulate the
        // idempotence flag across tests.
        const a = document.createElement('a');
        a.setAttribute('href', href);
        a.textContent = 'link';
        root.appendChild(a);
        document.body.appendChild(root);
        installLinkInterceptor(root);
        const evt = new window.MouseEvent('click', { bubbles: true, cancelable: true });
        a.dispatchEvent(evt);
        return evt;
    }

    it('prevents navigation and posts a relative path to /api/open', () => {
        const evt = clickAnchor('notes/my%20note.md');
        expect(evt.defaultPrevented).toBe(true);
        expect(fetchMock).toHaveBeenCalledTimes(1);
        const [url, opts] = fetchMock.mock.calls[0];
        expect(url).toBe('/api/open');
        expect(JSON.parse(opts.body)).toEqual({ path: 'notes/my note.md' });
    });

    it('prevents navigation and posts an https URL to /api/open', () => {
        const evt = clickAnchor('https://example.com/paper');
        expect(evt.defaultPrevented).toBe(true);
        expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ url: 'https://example.com/paper' });
    });

    it('swallows blocked schemes without any request', () => {
        const evt = clickAnchor('file:///etc/passwd');
        expect(evt.defaultPrevented).toBe(true);
        expect(fetchMock).not.toHaveBeenCalled();
    });

    it('leaves in-page anchors alone', () => {
        const evt = clickAnchor('#top');
        expect(evt.defaultPrevented).toBe(false);
        expect(fetchMock).not.toHaveBeenCalled();
    });

    it('is idempotent per root (no double POST on repeat install)', () => {
        const root = document.createElement('div');
        const a = document.createElement('a');
        a.setAttribute('href', 'note.md');
        root.appendChild(a);
        document.body.appendChild(root);
        installLinkInterceptor(root);
        installLinkInterceptor(root);
        a.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
        expect(fetchMock).toHaveBeenCalledTimes(1);
    });
});

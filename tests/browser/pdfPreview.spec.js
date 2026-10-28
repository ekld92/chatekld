import { expect, test } from '@playwright/test';

/**
 * The regression this file exists for.
 *
 * Every PDF preview in the app (audit inventory/accession/ambiguous decks, the
 * Tagging deck's `V` panel, Deck and Report "Preview PDF") fetches bytes with
 * the origin header and hands an object URL to an <iframe> — an `/api/` src is
 * 403'd by the origin guard, so blob: is not a stylistic choice.
 *
 * A blob: document INHERITS the embedder's CSP, and the browser renders an
 * embedded PDF through a native plugin. Shipping `object-src 'none'` therefore
 * blanked all six surfaces at once while every Python and jsdom test stayed
 * green: jsdom has no CSP engine and no PDF renderer, so only a real browser
 * can observe this. Assert it against the policy the real server sends.
 */

// Smallest structurally valid PDF; content is irrelevant, the plugin decision
// is made from the blob's MIME type and the inherited policy.
const TINY_PDF =
    '%PDF-1.1\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n' +
    '2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n' +
    '3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 99 99]>>endobj\n' +
    'trailer<</Root 1 0 R>>\n%%EOF\n';

test('the served CSP grants blob: to every directive a PDF preview needs', async ({ page }) => {
    const response = await page.goto('/');
    const csp = response.headers()['content-security-policy'];
    expect(csp, 'the app shell must ship an enforced CSP').toBeTruthy();

    const directives = Object.fromEntries(
        csp.split('; ').map((part) => {
            const [name, ...rest] = part.split(' ');
            return [name, rest.join(' ')];
        }),
    );
    // frame-src/child-src admit the frame; object-src admits the PDF viewer
    // instantiated inside it. All three, or the preview is a white page.
    for (const name of ['frame-src', 'child-src', 'object-src']) {
        expect(directives[name] || '', `${name} must grant blob:`).toContain('blob:');
    }

    // WebKit's blocker, asserted here for completeness even though THIS engine
    // cannot demonstrate it: the blob document inherits this policy and is
    // itself framed, so frame-ancestors tells it to refuse our own page.
    // Chromium renders it regardless, which is precisely how the regression
    // reached a user — the authoritative assertion (with the WKWebView
    // measurement behind it) is in tests/test_security_hardening.py.
    expect(directives).not.toHaveProperty('frame-ancestors');
});

test('a blob: PDF frame loads without a CSP violation', async ({ page }) => {
    await page.goto('/');

    // Reproduce the app's exact transport in the real engine and let the engine
    // — not a parsed header — decide whether the policy permits it.
    const violations = await page.evaluate(async (pdf) => {
        const seen = [];
        document.addEventListener('securitypolicyviolation', (event) => {
            seen.push(`${event.effectiveDirective}:${event.blockedURI}`);
        });

        const url = URL.createObjectURL(new Blob([pdf], { type: 'application/pdf' }));
        const frame = document.createElement('iframe');
        const loaded = new Promise((resolve) => {
            frame.addEventListener('load', resolve, { once: true });
            setTimeout(resolve, 3000);
        });
        frame.src = url;
        document.body.appendChild(frame);
        await loaded;

        // <object> is the element the inherited object-src governs directly, so
        // it fails loudly on the exact directive that caused the regression.
        const obj = document.createElement('object');
        obj.data = url;
        obj.type = 'application/pdf';
        document.body.appendChild(obj);
        await new Promise((resolve) => setTimeout(resolve, 500));

        frame.remove();
        obj.remove();
        URL.revokeObjectURL(url);
        return seen;
    }, TINY_PDF);

    expect(violations, 'a blob: PDF must not be blocked by the app CSP').toEqual([]);
});

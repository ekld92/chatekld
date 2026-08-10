// Adversarial vectors for ui.js::sanitiseHtml (improvement plan 2026-07-04,
// item 1.2). The defect: the scheme test ran against trimStart()-only input,
// but the HTML URL parser strips tab/LF/CR ANYWHERE in a URL (after entity
// decoding) and trims leading/trailing C0 controls + space — so
// `jav&#9;ascript:` decoded to a tab inside the scheme, passed the regex, and
// executed in the app origin. Invariant pinned here: the sanitiser judges the
// same post-preprocessing URL the browser would resolve, so no
// control-character placement smuggles javascript:/vbscript:/data:text/html
// through, while legitimate http(s)/mailto/data:image values survive intact.
import { describe, it, expect } from 'vitest';
import { sanitiseHtml } from '../../static/js/ui.js';

/** Return the first <a>'s href attribute after sanitisation (null if stripped). */
function hrefAfter(html) {
    const div = document.createElement('div');
    div.innerHTML = sanitiseHtml(html);
    const a = div.querySelector('a, img, area');
    if (!a) return null;
    return a.getAttribute('href') ?? a.getAttribute('src');
}

describe('sanitiseHtml — control-character scheme smuggling (Track 1.2)', () => {
    const smuggled = [
        // Entity-encoded tab/LF/CR inside the scheme — the original bypass.
        ['tab entity in scheme', '<a href="jav&#9;ascript:alert(1)">x</a>'],
        ['LF entity in scheme', '<a href="jav&#10;ascript:alert(1)">x</a>'],
        ['CR entity in scheme', '<a href="jav&#13;ascript:alert(1)">x</a>'],
        // Literal control characters (what the DOM holds after decoding).
        ['literal tab in scheme', '<a href="java\tscript:alert(1)">x</a>'],
        ['literal newline in scheme', '<a href="java\nscript:alert(1)">x</a>'],
        // Leading C0 control before the scheme (URL parser trims it).
        ['leading C0 control', '<a href="\u0001javascript:alert(1)">x</a>'],
        // Mixed case + tab.
        ['mixed case with tab', '<a href="JaVa\tScRiPt:alert(1)">x</a>'],
        // data:text/html with a tab inside the media type.
        ['tab inside data:text/html', '<a href="data:text\t/html,<script>alert(1)</script>">x</a>'],
        // vbscript with entity tab.
        ['vbscript with tab entity', '<a href="vb&#9;script:msgbox(1)">x</a>'],
        // src attribute, not just href.
        ['img src javascript', '<img src="jav&#10;ascript:alert(1)">'],
    ];
    it.each(smuggled)('strips %s', (_name, html) => {
        expect(hrefAfter(html)).toBeNull();
    });

    const alreadyBlocked = [
        ['plain javascript:', '<a href="javascript:alert(1)">x</a>'],
        ['leading-space javascript:', '<a href="  javascript:alert(1)">x</a>'],
        ['uppercase JAVASCRIPT:', '<a href="JAVASCRIPT:alert(1)">x</a>'],
        ['data:text/html', '<a href="data:text/html,<script>x</script>">x</a>'],
        ['vbscript:', '<a href="vbscript:msgbox(1)">x</a>'],
    ];
    it.each(alreadyBlocked)('still strips %s (no regression of the old gate)', (_name, html) => {
        expect(hrefAfter(html)).toBeNull();
    });

    const legitimate = [
        ['https link', '<a href="https://example.com/page?a=1">x</a>', 'https://example.com/page?a=1'],
        ['relative link', '<a href="notes/psy.md">x</a>', 'notes/psy.md'],
        ['mailto', '<a href="mailto:x@y.org">x</a>', 'mailto:x@y.org'],
        ['inline data image', '<img src="data:image/png;base64,AAAA">', 'data:image/png;base64,AAAA'],
    ];
    it.each(legitimate)('keeps %s byte-identical', (_name, html, expected) => {
        expect(hrefAfter(html)).toBe(expected);
    });

    it('keeps active-content stripping intact (script/iframe/on*)', () => {
        const out = sanitiseHtml(
            '<p onclick="alert(1)">t</p><script>alert(1)</script><iframe src="x"></iframe>');
        expect(out).not.toContain('onclick');
        expect(out).not.toContain('<script');
        expect(out).not.toContain('<iframe');
        expect(out).toContain('<p>t</p>');
    });

    it('leaves "javascript:" as visible TEXT content untouched', () => {
        // Only attribute values are URL-parsed; prose about javascript: is safe.
        const out = sanitiseHtml('<p>use javascript: carefully</p>');
        expect(out).toContain('use javascript: carefully');
    });

    // 2026-07-20 audit A1: these carry plain http: values, so the scheme gate
    // never touched them — they must be removed as ELEMENTS / attributes.
    it('strips <link>, <meta>, and <base> elements', () => {
        const out = sanitiseHtml(
            '<link rel="stylesheet" href="http://attacker/x.css">'
            + '<meta http-equiv="refresh" content="0;url=http://attacker">'
            + '<base href="http://attacker/">'
            + '<p>kept</p>');
        expect(out).not.toContain('<link');
        expect(out).not.toContain('<meta');
        expect(out).not.toContain('<base');
        expect(out).toContain('<p>kept</p>');
    });

    it('drops a style attribute that carries a url() beacon', () => {
        const out = sanitiseHtml('<p style="background:url(http://attacker/b.png)">t</p>');
        expect(out).not.toContain('url(');
        expect(out).not.toContain('attacker');
        expect(out).toContain('<p');
        expect(out).toContain('t</p>');
    });

    it('keeps a benign style attribute without url()', () => {
        const out = sanitiseHtml('<p style="color:red">t</p>');
        expect(out).toContain('color:red');
    });

    // E5 (2026-07-20 audit): the bare `url(` substring test was evadable.
    it('drops a style with a CSS-escaped url() (u\\72 l form)', () => {
        // `\72 ` is the CSS hex escape for 'r', so `u\72 l(` decodes to `url(`.
        const out = sanitiseHtml('<p style="background:u\\72 l(http://attacker/b.png)">t</p>');
        expect(out).not.toContain('attacker');
        expect(out).not.toMatch(/style=/);
    });

    it('drops a style using image-set() to beacon', () => {
        const out = sanitiseHtml('<p style="background:image-set(&quot;http://attacker/x&quot; 1x)">t</p>');
        expect(out).not.toContain('attacker');
        expect(out).not.toMatch(/style=/);
    });
});

describe('_styleHasExternalRef', () => {
    it('flags url(), image-set, escaped url, and embedded http(s)', async () => {
        const { _styleHasExternalRef } = await import('../../static/js/ui.js');
        for (const v of [
            'background:url(http://x/y)',
            'background:image-set("http://x" 1x)',
            'background:u\\72 l(http://x)',
            'x:\\75 rl(http://x)',
            'content:"https://x/y"',
        ]) {
            expect(_styleHasExternalRef(v), v).toBe(true);
        }
    });
    it('does not flag benign inline styles', () => {
        // imported lazily above; re-import is cached.
        return import('../../static/js/ui.js').then(({ _styleHasExternalRef }) => {
            for (const v of ['color:red', 'margin:0 auto', 'font-weight:600']) {
                expect(_styleHasExternalRef(v), v).toBe(false);
            }
        });
    });
});

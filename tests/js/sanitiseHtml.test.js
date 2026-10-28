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

    it('drops all style attributes because rendered Markdown needs none', () => {
        const out = sanitiseHtml('<p style="color:red">t</p>');
        expect(out).not.toMatch(/style=/);
        expect(out).toContain('<p>t</p>');
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

describe('sanitiseHtml — maintained allowlist and no-beacon policy', () => {
    it('drops remote and protocol-relative images but keeps relative/inline bitmaps', () => {
        const out = sanitiseHtml(
            '<img alt="remote" src="https://attacker.test/pixel.png">'
            + '<img alt="proto" src="//attacker.test/pixel.png">'
            + '<img alt="local" src="attachments/local.png">'
            + '<img alt="inline" src="data:image/png;base64,AAAA">');
        expect(out).not.toContain('attacker.test');
        expect(out).toContain('attachments/local.png');
        expect(out).toContain('data:image/png;base64,AAAA');
    });

    it('removes SVG/MathML namespaces and mutated active descendants', () => {
        const vectors = [
            '<svg><a xlink:href="javascript:alert(1)"><text>x</text></a></svg>',
            '<math><mtext><img src=x onerror=alert(1)></mtext></math>',
            '<svg><foreignObject><iframe srcdoc="<script>x</script>"></iframe></foreignObject></svg>',
            '<math><annotation-xml encoding="text/html"><script>x</script></annotation-xml></math>',
        ];
        for (const vector of vectors) {
            const out = sanitiseHtml(vector);
            expect(out).not.toMatch(/svg|math|xlink|script|iframe|onerror/i);
        }
    });

    it('drops srcset, ids/names, forms, media and malformed namespace attributes', () => {
        const out = sanitiseHtml(
            '<img src="local.png" srcset="https://attacker/x 2x" id="clobber">'
            + '<form><input name="location"></form>'
            + '<video autoplay src="https://attacker/v"></video>'
            + '<p xmlns:x="urn:x" x:href="javascript:x">kept</p>');
        expect(out).not.toMatch(/srcset|id=|name=|form|input|video|autoplay|xmlns|x:href/i);
        expect(out).not.toContain('attacker');
        expect(out).toContain('kept');
    });

    it('allows only explicit supported link schemes', () => {
        const out = sanitiseHtml(
            '<a href="obsidian://open?vault=x">obsidian</a>'
            + '<a href="zotero://select/items/x">zotero</a>'
            + '<a href="file:///etc/passwd">file</a>'
            + '<a href="//attacker/x">protocol relative</a>'
            + '<a href="java&#x0d;script:x">smuggled</a>');
        const host = document.createElement('div');
        host.innerHTML = out;
        const links = [...host.querySelectorAll('a')];
        expect(links[0].getAttribute('href')).toBe('obsidian://open?vault=x');
        expect(links[1].getAttribute('href')).toBe('zotero://select/items/x');
        expect(links.slice(2).every((a) => !a.hasAttribute('href'))).toBe(true);
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

describe('policy is applied inside DOMPurify, not over a re-parsed copy', () => {
    it('drops stylesheet classes untrusted Markdown tries to borrow', () => {
        // `class` is allowed so fenced code keeps `language-x`. Unrestricted, a
        // note or model reply could apply any app class — including overlay and
        // positioning ones — which is UI redress, not styling.
        const out = sanitiseHtml(
            '<p class="modal-overlay audit-link-btn">x</p>'
            + '<pre><code class="language-python">y</code></pre>');
        expect(out).not.toContain('modal-overlay');
        expect(out).not.toContain('audit-link-btn');
        expect(out).toContain('language-python');
    });

    it('keeps the app link schemes and still refuses the rest', () => {
        expect(sanitiseHtml('<a href="obsidian://open?vault=x">o</a>'))
            .toContain('obsidian://open');
        expect(sanitiseHtml('<a href="zotero://select/items/1">z</a>'))
            .toContain('zotero://select');
        for (const href of ['javascript:alert(1)', 'vbscript:x',
                            'file:///etc/passwd', 'data:text/html,<b>']) {
            expect(sanitiseHtml(`<a href="${href}">x</a>`)).not.toContain(href);
        }
    });

    it('does not re-parse and re-serialise DOMPurify output', async () => {
        // Source ratchet. The extra DOMParser round trip after sanitization is
        // the mXSS shape DOMPurify warns against; keeping the policy in a hook
        // is what removed it, so a reintroduction should fail here.
        const fs = await import('node:fs/promises');
        const src = await fs.readFile('static/js/ui.js', 'utf8');
        const body = src.slice(src.indexOf('export function sanitiseHtml'),
                               src.indexOf('export function renderExampleChips'));
        expect(body).not.toMatch(/DOMParser/);
        expect(body).not.toMatch(/ALLOW_UNKNOWN_PROTOCOLS/);
    });
});

// Online-provider registration ratchet (2026-08-08, added with OpenRouter).
//
// The C4 bug (2026-07-13): DeepSeek/Z.AI were registered server-side and in
// index.html but NOT in ui.js's _ONLINE_PROVIDERS / _PROVIDER_LABELS, so the
// client treated them as LOCAL and disabled the embedding selector while they
// were active. Nothing failed the suite. This lint closes that gap: the three
// client-side registries must name the SAME online-provider set, sourced from
// the HTML "Online (requires API key)" optgroups (both the sidebar selector
// and the Settings fallback selector list every online provider, so they are
// the ground truth a user actually sees).
//
// Static analysis over the real sources (moduleHierarchy.test.js precedent —
// no jsdom): the registries are plain literals, so regexes are reliable; if a
// registry is ever rewritten in a shape these regexes miss, the extraction
// helpers throw rather than silently pass on an empty set.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (p) => readFileSync(join(ROOT, p), 'utf8');

function extract(re, src, what) {
    const m = src.match(re);
    if (!m) throw new Error(`could not locate ${what} — registry shape changed?`);
    return m[1];
}

// Entry regexes are digit-safe ([a-z0-9_]) and the <option> ones
// attribute-safe (2026-08-08 review B3): with the old [a-z_] class a provider
// id carrying a digit vanished from EVERY extracted set consistently, so the
// parity checks passed while the ratchet silently stopped covering it — the
// worst failure mode a lint can have.

/** Provider ids inside a JS object-literal body ("openai: 'x'," / quoted keys). */
function objectKeys(body) {
    return [...body.matchAll(/^\s*['"]?([a-z0-9_]+)['"]?\s*:/gm)].map((m) => m[1]);
}

const uiSrc = read('static/js/ui.js');
const configSrc = read('static/js/config.js');
const htmlSrc = read('templates/index.html');

const onlineSet = [...extract(
    /_ONLINE_PROVIDERS\s*=\s*new Set\(\[([^\]]*)\]\)/, uiSrc, 'ui.js _ONLINE_PROVIDERS',
).matchAll(/['"]([a-z0-9_]+)['"]/g)].map((m) => m[1]);

const labelKeys = objectKeys(extract(
    /_PROVIDER_LABELS\s*=\s*\{([\s\S]*?)\};/, uiSrc, 'ui.js _PROVIDER_LABELS'));

const modelKeyProviders = objectKeys(extract(
    /_ONLINE_MODEL_KEYS\s*=\s*\{([\s\S]*?)\};/, configSrc, 'config.js _ONLINE_MODEL_KEYS'));

/** Option values of the sidebar "Online (requires API key)" optgroup. */
function htmlOnlineProviders() {
    const body = extract(
        /<optgroup label="Online \(requires API key\)">([\s\S]*?)<\/optgroup>/,
        htmlSrc, 'index.html Online optgroup');
    return [...body.matchAll(/<option value="([^"]+)"[^>]*>/g)].map((m) => m[1]);
}

/** Option values of the Settings fallback-provider selector (flat, no optgroup). */
function htmlFallbackProviders() {
    const body = extract(
        /<select id="set-fallback-provider">([\s\S]*?)<\/select>/,
        htmlSrc, 'index.html set-fallback-provider select');
    return [...body.matchAll(/<option value="([^"]+)"[^>]*>/g)].map((m) => m[1]);
}

describe('online-provider registration parity (the missing C4 ratchet)', () => {
    it('index.html online optgroup, config.js model keys and ui.js online set agree', () => {
        const htmlSet = htmlOnlineProviders();
        expect([...onlineSet].sort()).toEqual([...htmlSet].sort());
        expect([...modelKeyProviders].sort()).toEqual([...htmlSet].sort());
    });

    it('every online provider has a badge label (and locals keep theirs)', () => {
        for (const p of onlineSet) expect(labelKeys, p).toContain(p);
        expect(labelKeys).toContain('ollama');
        expect(labelKeys).toContain('lm_studio');
    });

    it('the fallback-provider selector offers every online provider', () => {
        const fallback = htmlFallbackProviders();
        for (const p of onlineSet) expect(fallback, p).toContain(p);
    });

    it('openrouter is registered online client-side (2026-08-08)', () => {
        expect(onlineSet).toContain('openrouter');
        expect(modelKeyProviders).toContain('openrouter');
    });
});

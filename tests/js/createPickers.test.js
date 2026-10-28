import {it, expect, vi, afterEach} from 'vitest';

afterEach(() => vi.unstubAllGlobals());

for (const [module, prefix, augment] of [['deck', 'deck', 'pickAugmentDeck'], ['docgen', 'docgen', 'pickAugmentDoc']]) {
    for (const fn of ['pickTemplateFile', 'pickOutDir', augment]) {
        it(`${module} ${fn} explains an unavailable native picker`, async () => {
            document.body.innerHTML = `<div id="${prefix}-status"></div><div id="${prefix}-aug-status"></div>`;
            vi.stubGlobal('fetch', vi.fn(async () => ({ok:false, status:503,
                json: async () => ({error:'No active window'})})));
            const api = await import(`../../static/js/${module}.js`);
            await api[fn]();
            const status = document.getElementById(prefix + (fn === augment ? '-aug-status' : '-status'));
            expect(status.textContent).toContain('No active window');
            expect(status.textContent).toContain('enter the path manually');
        });
    }
}

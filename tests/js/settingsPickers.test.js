import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

function okJson(body) {
    return { ok: true, status: 200, json: async () => body };
}

describe('settings & audit native path pickers and unsaved state', () => {
    beforeEach(() => {
        vi.resetModules();
        vi.useFakeTimers();
    });

    afterEach(() => {
        vi.useRealTimers();
        vi.unstubAllGlobals();
    });

    it('Browse buttons in Settings populate archive directory inputs and trigger debounced save', async () => {
        document.body.innerHTML = `
            <div id="sr-status-announcer" role="status"></div>
            <div id="sr-error-announcer" role="alert"></div>
            <div id="settings-modal">
                <input type="text" id="set-history-archive-dir" value="" />
                <button type="button" id="set-history-archive-dir-browse">Browse…</button>
                <input type="text" id="set-tagging-archive-dir" value="" />
                <button type="button" id="set-tagging-archive-dir-browse">Browse…</button>
                <div id="settings-status" role="status"></div>
            </div>
        `;
        const posts = [];
        vi.stubGlobal('fetch', vi.fn(async (url, opts = {}) => {
            const u = String(url);
            if (u.includes('/api/config/native-pick-folder')) {
                return okJson({ ok: true, path: '/Users/alice/Archive' });
            }
            if (u.includes('/api/config') && opts.method === 'POST') {
                posts.push(JSON.parse(opts.body));
                return okJson({ ok: true, dropped: [] });
            }
            return okJson({});
        }));

        const settings = await import('../../static/js/settings.js');
        settings.initSettings({});

        document.getElementById('set-history-archive-dir-browse').click();
        await vi.waitFor(() => {
            expect(document.getElementById('set-history-archive-dir').value).toBe('/Users/alice/Archive');
        });

        await vi.advanceTimersByTimeAsync(450);
        expect(posts.length).toBeGreaterThanOrEqual(1);
        expect(posts.at(-1).refactor_archive_dir).toBe('/Users/alice/Archive');
    });

    it('Audit Settings Browse buttons populate Zotero SQLite/storage inputs and mark unsaved changes', async () => {
        document.body.innerHTML = `
            <div id="sr-status-announcer" role="status"></div>
            <div id="sr-error-announcer" role="alert"></div>
            <div id="audit-settings-modal">
                <input type="text" id="audit-attachments-subdir" value="Z_attachments" />
                <input type="text" id="audit-biblio-articles-subdir" value="biblio_articles" />
                <input type="text" id="audit-zotero-notes-subdir" value="Z_Zotero_Notes" />
                <input type="text" id="audit-master-bib-path" value="_master.bib" />
                <input type="text" id="audit-zotero-sqlite" value="" />
                <button type="button" id="audit-zotero-sqlite-browse">Browse…</button>
                <input type="text" id="audit-zotero-storage" value="" />
                <button type="button" id="audit-zotero-storage-browse">Browse…</button>
                <input type="text" id="audit-biblio-skip-prefix" value="z_item" />
                <input type="number" id="audit-annotations-read-threshold" value="5" />
                <input type="number" id="audit-near-dup-ratio" value="88" />
                <input type="text" id="audit-no-pdf-types" value="" />
                <input type="checkbox" id="audit-drift-include-parent-tags" />
                <div id="audit-settings-status" role="status" aria-live="polite"></div>
            </div>
        `;
        const auditPosts = [];
        vi.stubGlobal('fetch', vi.fn(async (url, opts = {}) => {
            const u = String(url);
            if (u.includes('/api/config/native-pick-file')) {
                return okJson({ ok: true, path: '/Users/alice/Zotero/zotero.sqlite' });
            }
            if (u.includes('/api/config/native-pick-folder')) {
                return okJson({ ok: true, path: '/Users/alice/Zotero/storage' });
            }
            if (u.includes('/api/audit/config') && opts.method === 'POST') {
                auditPosts.push(JSON.parse(opts.body));
                return okJson({ ok: true });
            }
            if (u.includes('/api/audit/config')) {
                return okJson({
                    audit_attachments_subdir: 'Z_attachments',
                    audit_biblio_articles_subdir: 'biblio_articles',
                    audit_zotero_notes_subdir: 'Z_Zotero_Notes',
                    audit_master_bib_path: '_master.bib',
                    audit_zotero_sqlite: '~/Zotero/zotero.sqlite',
                    audit_zotero_storage: '~/Zotero/storage',
                });
            }
            if (u.includes('/api/audit/status')) {
                return okJson({ state: 'idle', has_results: false });
            }
            return okJson({});
        }));

        const audit = await import('../../static/js/audit.js');
        await audit.initAuditTab();

        const statusEl = document.getElementById('audit-settings-status');
        expect(statusEl.textContent).toBe('');

        // Editing a field marks the modal dirty.
        const sqliteInput = document.getElementById('audit-zotero-sqlite');
        sqliteInput.value = '/Users/alice/unsaved.sqlite';
        sqliteInput.dispatchEvent(new Event('input', { bubbles: true }));
        expect(statusEl.textContent).toMatch(/Unsaved changes/i);

        // Closing without saving and reopening resets inputs to _auditCfg and clears dirty banner.
        const modal = document.getElementById('audit-settings-modal');
        modal.classList.add('open');
        await vi.waitFor(() => {
            expect(sqliteInput.value).toBe('~/Zotero/zotero.sqlite');
            expect(statusEl.textContent).toBe('');
        });
        modal.classList.remove('open');

        document.getElementById('audit-zotero-sqlite-browse').click();
        await vi.waitFor(() => {
            expect(document.getElementById('audit-zotero-sqlite').value).toBe('/Users/alice/Zotero/zotero.sqlite');
        });
        expect(statusEl.textContent).toMatch(/Unsaved changes/i);

        document.getElementById('audit-zotero-storage-browse').click();
        await vi.waitFor(() => {
            expect(document.getElementById('audit-zotero-storage').value).toBe('/Users/alice/Zotero/storage');
        });

        await audit.saveAuditSettings();
        expect(auditPosts).toHaveLength(1);
        expect(auditPosts[0].audit_zotero_sqlite).toBe('/Users/alice/Zotero/zotero.sqlite');
        expect(auditPosts[0].audit_zotero_storage).toBe('/Users/alice/Zotero/storage');
        expect(statusEl.textContent).toBe('Saved.');
    });

    it('handles cancel ({path: null}) and headless (503) states cleanly without mutating inputs', async () => {
        document.body.innerHTML = `
            <div id="sr-status-announcer" role="status"></div>
            <div id="sr-error-announcer" role="alert"></div>
            <div id="settings-modal">
                <input type="text" id="set-history-archive-dir" value="/existing/archive" />
                <button type="button" id="set-history-archive-dir-browse">Browse…</button>
                <div id="settings-status" role="status"></div>
            </div>
        `;
        let mode = 'cancel';
        vi.stubGlobal('fetch', vi.fn(async (url) => {
            const u = String(url);
            if (u.includes('/api/config/native-pick-folder')) {
                if (mode === 'cancel') {
                    return okJson({ ok: true, cancelled: true, path: null });
                }
                return { ok: false, status: 503, json: async () => ({ error: 'Native folder picker is only available inside the desktop app.' }) };
            }
            return okJson({});
        }));

        const settings = await import('../../static/js/settings.js');
        settings.initSettings({ refactor_archive_dir: '/existing/archive' });

        await settings.pickSettingsFolder('set-history-archive-dir');
        expect(document.getElementById('set-history-archive-dir').value).toBe('/existing/archive');

        mode = 'headless';
        await settings.pickSettingsFolder('set-history-archive-dir');
        expect(document.getElementById('set-history-archive-dir').value).toBe('/existing/archive');
        expect(document.getElementById('settings-status').textContent).toMatch(/desktop app/i);
    });
});

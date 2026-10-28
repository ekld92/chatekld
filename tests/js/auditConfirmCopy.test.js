// Phase 0 of the 2026-09-10 Library Audit UX plan — "stabilize the map".
//
// Defect class pinned: write verbs whose confirm did not say WHAT they write
// or WHERE the undo lives (the deck's `Link "x" to key?` named neither), a
// review workspace that mounted silently over the Not in Zotero queue, and
// copy that outlived the code — the Inventory board carried a "Read-only —
// no write verbs here." comment for two days after [unmap] / [never
// auto-map] / the pdf: link landed, and the Settings notesync hint still
// described the retired one-directional "drifted refresh" path.
//
// These are SOURCE ratchets (the confirmCeremony.test.js pattern): they pin
// strings, not behaviour, because the defect IS the string. Editing one of
// the pinned confirms is fine — keep the two halves ("writes X" and
// "undo from Y") or update this file in the same commit, deliberately.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const audit = readFileSync(resolve(root, 'static/js/audit.js'), 'utf8');
const pending = readFileSync(resolve(root, 'static/js/pendingReview.js'), 'utf8');
const indexHtml = readFileSync(resolve(root, 'templates/index.html'), 'utf8');
const guide = readFileSync(resolve(root, 'static/help/guide.md'), 'utf8');

describe('stale copy stays dead (Phase 0)', () => {
    it('audit.js no longer claims the Inventory board is read-only', () => {
        expect(audit).not.toContain('Read-only — no write verbs here');
        expect(audit).not.toContain('read-only posture are unchanged');
    });

    it('guide.md no longer claims nothing on the Inventory board writes', () => {
        expect(guide).not.toContain('Read-only — nothing here writes');
        // and the replacement names the verbs + their undo homes
        expect(guide).toContain('[unmap]');
        expect(guide).toContain('[never auto-map]');
    });

    it('the Settings notesync hint describes the two-way verbs, not the retired refresh', () => {
        expect(indexHtml).not.toContain('drifted</strong> note');
        expect(indexHtml).toContain('When Zotero is authoritative is off');
        expect(indexHtml).toContain('[take Zotero\'s]');
        expect(indexHtml).toContain('[keep mine]');
        // the undo split — pull vs push land in different Restore panels
        expect(indexHtml).toContain('Settings → History and Restore');
        expect(indexHtml).toContain('Tagging → Restore');
    });

    it('guide.md Notes Sync names both restore panels for the two directions', () => {
        expect(guide).not.toContain('Needs review');
        expect(guide).toContain('[take Zotero\'s]');
        expect(guide).toContain('[keep mine]');
    });
});

describe('the review workspace announces its mode (Phase 0)', () => {
    it('mounting pending review sets an explicit mode title on the meta line', () => {
        expect(audit).toContain('PDF identification review');
        expect(audit).toContain('mapping, curation and Zotero writes each ask separately');
    });
});

describe('confirms name destination + undo (Phase 0)', () => {
    // audit.js — the gap sites the Phase 0 survey found. Each pinned pair is
    // ("writes X", "undo from Y") for one verb.
    it('accession deck link / no-match / pending name mapping.json and the deck undo', () => {
        expect(audit).toContain('mapping.json only — nothing touches the vault or Zotero. Undo: ⌫ on this deck');
        expect(audit).toMatch(/mapping\.json curated queue; it is listed under Not in Zotero\.[\s\S]{0,40}Undo: ⌫ on this deck, or \[clear\] there/);
    });

    it('ambiguous-table assign names mapping.json and both undo homes', () => {
        expect(audit).toContain('Writes the audit’s mapping.json only. ');
        expect(audit).toContain('Undo: ⌫ in the deck view, or [unmap] on the Inventory pair');
    });

    it('Not in Zotero single mark names mapping.json and [clear] (the bulk form already did)', () => {
        expect(audit).toContain('Mark this PDF as ${what}? Writes the audit’s mapping.json; undo: [clear] on this tab.');
        expect(audit).toContain('One atomic write to the audit’s mapping.json; undo: [clear] on this tab.');
    });

    it('the journalled bulk verbs name Tagging → Restore', () => {
        // zotlink attach, harmonize, zotlink trash, retire-tag
        expect(audit).toContain('undo from Tagging → Restore (each also becomes removable from this tab)');
        const named = audit.match(/\(also Tagging → Restore\)/g) || [];
        expect(named.length).toBeGreaterThanOrEqual(3);
    });

    // pendingReview.js — the review workspace's own writes.
    it('curation confirm names mapping.json and keeps identification approval separate', () => {
        expect(pending).toContain('Writes the audit’s mapping.json only — identification approval is untouched');
        expect(pending).toContain('Undo: “Clear mapping curation” here, or [clear] on the Not in Zotero tab');
    });

    it('map confirm names mapping.json and the [unmap] undo', () => {
        expect(pending).toContain('Writes the audit’s mapping.json only — nothing touches the vault or Zotero. Undo: [unmap] on the Inventory tab');
    });

    it('attach confirm discloses the Desktop undo limitation', () => {
        expect(pending).toContain('Automatic Desktop undo is unavailable. Review corrections in Zotero.');
    });

    it('the curation section says what curation writes, not only what it is not', () => {
        expect(pending).toContain('Curation queues the file in the audit’s mapping.json');
        expect(pending).toContain('writes only the local review store');
    });
});

describe('the Help legend exists (Phase 0)', () => {
    it('guide.md carries the verb → writes → undo table', () => {
        expect(guide).toContain('| The verb | Writes | Undo |');
        expect(guide).toContain('### What writes where (and how to undo it)');
    });

    it('guide.md disambiguates the four overloaded words', () => {
        for (const word of ['**pending**', '**confirm**', '**map / link**', '**attach**']) {
            expect(guide).toContain(word);
        }
    });

    it('guide.md covers the PDF workspace queue and the review workspace', () => {
        // Renamed 2026-09-19: the tab has been "PDF workspace" since the
        // Library IA pass; the guide still called it "Not in Zotero".
        expect(guide).toContain('PDF workspace');
        expect(guide).toContain('PDF identification review');
    });
});

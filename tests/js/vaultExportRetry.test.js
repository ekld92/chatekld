// P8.1 (2026-07-30) — the vault-chat export must not duplicate the user turn
// when a Retry follows a PARTIAL failure.
//
// Shape of the defect: a mid-stream failure that had already streamed text
// records {user, assistant+"[interrupted]"} so the export keeps what the user
// watched arrive. Retry then re-asks the SAME question, and the success path
// pushed the user turn a SECOND time — the exported transcript showed the
// question twice with one answer between the copies.
//
// `chatWithVault` is DOM- and SSE-heavy (input element, streaming renderer,
// abort wiring), so this is a source ratchet on the invariant rather than a
// full drive of the function: both history-recording paths must go through the
// dedupe helper, never a bare user push. Ratchets are the documented pattern
// for exactly this in tests/js/deckPreview.test.js + taggingPdf.test.js.
import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { buildConversationExport } from '../../static/js/ui.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const VAULT_JS = fs.readFileSync(path.join(HERE, '../../static/js/vault.js'), 'utf8');

describe('vault export: no duplicate user turn after retry-after-partial', () => {
    it('source ratchet: exactly ONE place pushes a user turn (the helper)', () => {
        // A second literal `_pushVaultTurn({ role: 'user' ...})` — i.e. one
        // outside _pushUserTurnOnce — reintroduces the defect.
        const bareUserPush = /_pushVaultTurn\(\s*\{\s*role:\s*'user'/g;
        expect(VAULT_JS.match(bareUserPush) || []).toHaveLength(1);
    });

    it('source ratchet: the dedupe helper exists and is used on both paths', () => {
        expect(VAULT_JS).toContain('function _pushUserTurnOnce(');
        // Success path AND partial-failure path both call it. The trailing `;`
        // keeps the function DEFINITION out of the count.
        const calls = VAULT_JS.match(/_pushUserTurnOnce\(question, fromRetry\);/g) || [];
        expect(calls).toHaveLength(2);
    });

    it('source ratchet: the partial path marks the question as recorded', () => {
        // Without this the retry has no way to know a user turn already exists.
        expect(VAULT_JS).toContain('_recordedRetryQuestion = question;');
        // ...and a completed answer clears it, so a LATER identical question
        // is recorded normally instead of being silently swallowed.
        expect(VAULT_JS).toContain('_recordedRetryQuestion = null;');
    });

    it('the resulting history exports one question with both outcomes', () => {
        // What the fixed recording produces: ONE user turn, the interrupted
        // partial, then the successful retry answer.
        const history = [
            { role: 'user', content: 'Quelle est la dose ?' },
            { role: 'assistant', content: 'La dose est…\n[interrupted]' },
            { role: 'assistant', content: 'La dose est 20 mg.', sources: ['notes/a.md'] },
        ];
        const { content } = buildConversationExport(history, 'md', 'Vault Conversation');
        expect(content.match(/^## You$/gm)).toHaveLength(1);          // asked once
        expect(content.match(/^## Assistant$/gm)).toHaveLength(2);    // both outcomes
        expect(content).toContain('[interrupted]');
        expect(content).toContain('La dose est 20 mg.');
    });
});

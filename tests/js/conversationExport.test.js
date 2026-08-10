// Review lot 4.3 pins for ui.js::buildConversationExport — the pure transcript
// builder behind the vault-chat / plain-chat Export MD/TXT buttons. Invariants:
// one title only (the server's raw mode adds nothing), per-turn role headers,
// and vault turns' retrieval sources emitted under their answer.
import { describe, it, expect } from 'vitest';
import { buildConversationExport } from '../../static/js/ui.js';

const HISTORY = [
    { role: 'user', content: 'Quelle est la dose ?' },
    { role: 'assistant', content: 'La dose est 20 mg.', sources: ['notes/a.md', 'papers/b.pdf'] },
    { role: 'user', content: 'Et ensuite ?' },
    { role: 'assistant', content: 'Titrer.' },   // no sources (plain-chat shape)
];

describe('buildConversationExport (review lot 4.3)', () => {
    it('md: single H1 title, role sections, sources line when present', () => {
        const { format, content } = buildConversationExport(HISTORY, 'md', 'Vault Conversation');
        expect(format).toBe('md');
        expect(content.match(/^# /gm)).toHaveLength(1);           // ONE title
        expect(content).toContain('# Vault Conversation');
        expect(content.match(/^## You$/gm)).toHaveLength(2);
        expect(content.match(/^## Assistant$/gm)).toHaveLength(2);
        expect(content).toContain('*Sources: notes/a.md, papers/b.pdf*');
        // The sourceless turn gets no dangling Sources line.
        expect(content.match(/Sources:/g)).toHaveLength(1);
    });

    it('txt: plain headers, unknown format falls back to md', () => {
        const { content } = buildConversationExport(HISTORY, 'txt', 'T');
        expect(content).toContain('### You');
        expect(content).toContain('Sources: notes/a.md, papers/b.pdf');
        expect(content).not.toMatch(/^## You$/m);   // no md-style section headers
        expect(buildConversationExport([], 'weird', 'T').format).toBe('md');
    });
});

// Export provenance (2026-07-22): conversation-level header block, per-turn
// meta/usage "Model:" lines, {source, score} object sources, and the agent
// tool-call trace. Legacy string sources and headerless calls stay byte-safe
// (the lot 4.3 suite above runs against the same function unchanged).
describe('buildConversationExport provenance (2026-07-22)', () => {
    const RICH = [
        { role: 'user', content: 'Q' },
        {
            role: 'assistant', content: 'A',
            meta: { provider: 'anthropic', model: 'claude-sonnet-5', agent_enabled: true },
            usage: { input_tokens: 1200, output_tokens: 340, cost_usd: 0.0042, iteration_count: 3 },
            sources: [{ source: 'notes/a.md', score: 0.8123 }, { source: 'b.pdf' }],
            trace: [
                { id: 't1', name: 'vault_search', arguments: { query: 'dose' } },
                { id: 't2', name: 'vault_read_note', arguments: { rel_path: 'a.md' }, is_error: true },
            ],
        },
    ];

    it('renders the header block under the title', () => {
        const { content } = buildConversationExport(RICH, 'md', 'T', {
            header: { Exported: '2026-07-22T10:00:00Z', Model: 'anthropic / claude-sonnet-5', Empty: '' },
        });
        expect(content).toContain('*Exported: 2026-07-22T10:00:00Z*');
        expect(content).toContain('*Model: anthropic / claude-sonnet-5*');
        expect(content).not.toContain('Empty:');   // empty values skipped
    });

    it('renders per-turn model/usage line, scored sources, and the tool trace', () => {
        const { content } = buildConversationExport(RICH, 'md', 'T');
        expect(content).toContain('Model: anthropic / claude-sonnet-5');
        expect(content).toContain('agent mode');
        expect(content).toContain('1200 in / 340 out tokens');
        expect(content).toContain('$0.0042');
        expect(content).toContain('3 agent iterations');
        expect(content).toContain('Sources: notes/a.md (0.812), b.pdf');
        expect(content).toContain('- vault_search({"query":"dose"})');
        expect(content).toContain('- vault_read_note({"rel_path":"a.md"}) — error');
    });

    it('legacy turns render without any provenance lines', () => {
        const { content } = buildConversationExport(
            [{ role: 'assistant', content: 'A' }], 'md', 'T');
        expect(content).not.toContain('Model:');
        expect(content).not.toContain('Tool calls:');
    });
});

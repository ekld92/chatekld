// Paper labels (2026-09-23) — the vault chat's Retrieval Context panel shows a
// chunk's paper label with its path in the tooltip, and the MD/TXT export
// carries the label too. The label is bibliography text: it must reach the DOM
// through textContent only.
import { describe, it, expect, beforeEach } from 'vitest';
import { _renderRetrievalTrace } from '../../static/js/vault.js';
import { buildConversationExport } from '../../static/js/ui.js';

function mount() {
    document.body.innerHTML = '<div id="chat"><div id="bot"></div></div>';
    return { chat: document.getElementById('chat'), bot: document.getElementById('bot') };
}

describe('Retrieval Context panel: paper labels', () => {
    beforeEach(() => { document.body.innerHTML = ''; });

    it('shows the label as the link text and the path as VISIBLE text', () => {
        const { chat, bot } = mount();
        _renderRetrievalTrace([
            { source: 'Z/biblio/smith_2020.pdf', score: 0.8, is_image: false,
              paper: { key: 'smithStudy2020', label: 'Smith et al., 2020, A Study' } },
            { source: 'notes/perso.md', score: 0.4, is_image: false },
        ], bot, chat);
        const links = chat.querySelectorAll('.retrieval-src a');
        expect(links[0].textContent).toBe('Smith et al., 2020, A Study');
        expect(links[0].getAttribute('href')).toBe('Z/biblio/smith_2020.pdf');
        // The path is a second visible line, not only a tooltip (VoiceOver
        // does not reliably read `title`; touch has no tooltip).
        const paths = chat.querySelectorAll('.retrieval-src .retrieval-path');
        expect(paths.length).toBe(1);
        expect(paths[0].textContent).toBe('Z/biblio/smith_2020.pdf');
        expect(links[0].title).toBe('Reveal in Finder');
        // Unlabelled rows are unchanged: the path IS the link text.
        expect(links[1].textContent).toBe('notes/perso.md');
        expect(links[1].title).toBe('Reveal in Finder');
        // The kind still derives from the path.
        const kinds = chat.querySelectorAll('.retrieval-kind');
        expect(kinds[0].textContent).toBe('pdf');
        expect(kinds[1].textContent).toBe('note');
    });

    it('never parses the label as HTML', () => {
        const { chat, bot } = mount();
        _renderRetrievalTrace([
            { source: 'a.pdf', score: 0.5,
              paper: { key: 'k', label: '<img src=x onerror=alert(1)>Evil' } },
        ], bot, chat);
        const link = chat.querySelector('.retrieval-src a');
        expect(link.querySelector('img')).toBeNull();
        expect(link.textContent).toBe('<img src=x onerror=alert(1)>Evil');
        expect(chat.querySelector('.retrieval-path img')).toBeNull();
    });

    it('the export includes the label beside the path', () => {
        const history = [
            { role: 'user', content: 'q' },
            { role: 'assistant', content: 'a', sources: [
                { source: 'Z/smith_2020.pdf', score: 0.8,
                  paper: { key: 'k', label: 'Smith, 2020, T' } },
                { source: 'notes/b.md', score: 0.5 },
            ] },
        ];
        const { content } = buildConversationExport(history, 'txt', 'Vault');
        expect(content).toContain('Z/smith_2020.pdf — Smith, 2020, T (0.800)');
        expect(content).toContain('notes/b.md (0.500)');
    });
});

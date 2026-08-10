// Review lot 6 pins for ui.js::installDraftPersistence (axis v — remember the
// last value typed into key inputs across app quits). Invariants: restore
// fills ONLY an empty field (a config-sourced value must never be clobbered),
// saves are debounced on input, and clearing a field to '' persists the ''
// (a deliberate clear must not resurrect the old draft on next launch).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { installDraftPersistence } from '../../static/js/ui.js';

function makeInput(id, value = '') {
    const el = document.createElement('input');
    el.id = id;
    el.value = value;
    document.body.appendChild(el);
    return el;
}

describe('installDraftPersistence (axis v)', () => {
    beforeEach(() => {
        vi.useFakeTimers();
        // This vitest environment does not ship a working localStorage —
        // stub a minimal in-memory one (the code under test only uses
        // getItem/setItem, both inside try/catch for private-mode safety).
        const store = new Map();
        vi.stubGlobal('localStorage', {
            getItem: (k) => (store.has(k) ? store.get(k) : null),
            setItem: (k, v) => store.set(k, String(v)),
        });
        document.body.innerHTML = '';
    });
    afterEach(() => {
        vi.useRealTimers();
        vi.unstubAllGlobals();
    });

    it('restores a saved draft into an EMPTY field only', () => {
        localStorage.setItem('chatekld-draft:f1', 'saved draft');
        localStorage.setItem('chatekld-draft:f2', 'saved draft 2');
        const empty = makeInput('f1');
        const filled = makeInput('f2', 'from config');
        installDraftPersistence([{ id: 'f1' }, { id: 'f2' }]);
        expect(empty.value).toBe('saved draft');
        expect(filled.value).toBe('from config');   // never clobbered
    });

    it('saves on input (debounced) and persists a deliberate clear', () => {
        const el = makeInput('f3');
        installDraftPersistence([{ id: 'f3' }]);
        el.value = 'typing…';
        el.dispatchEvent(new Event('input'));
        expect(localStorage.getItem('chatekld-draft:f3')).toBeNull(); // not yet
        vi.advanceTimersByTime(300);
        expect(localStorage.getItem('chatekld-draft:f3')).toBe('typing…');
        // Clearing the field saves '' — the old draft must not come back.
        el.value = '';
        el.dispatchEvent(new Event('input'));
        vi.advanceTimersByTime(300);
        expect(localStorage.getItem('chatekld-draft:f3')).toBe('');
    });

    it('skips missing elements silently', () => {
        expect(() => installDraftPersistence([{ id: 'nope' }])).not.toThrow();
    });
});

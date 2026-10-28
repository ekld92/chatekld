/** Workspace composition. Existing feature nodes retain their values and handlers. */
import { showTab, confirmInline, taskBegin, taskEnd, taskNote } from './ui.js';
import { consumeSSE, secureFetch } from './api.js';

export function initStudio() {
    const root = document.getElementById('deck-tab');
    const report = document.getElementById('docgen-tab');
    if (!root || !report || root.dataset.studioReady) return;
    root.dataset.studioReady = '1';
    const deckCards = [...root.querySelectorAll('.pane-view > .card')];
    const reportCards = [...report.querySelectorAll('.pane-view > .card')];
    const pane = root.querySelector('.pane-view');
    reportCards.forEach(card => pane.append(card));
    const kind = document.getElementById('studio-kind');
    const action = document.getElementById('studio-action');
    const format = document.getElementById('studio-format');
    const cards = [...deckCards, ...reportCards];
    // Put the writing brief first; retain full template editing in a disclosure.
    for (const prefix of ['deck', 'docgen']) {
        const body = document.getElementById(`${prefix}-topic`)?.closest('.deck-body');
        if (!body) continue;
        const brief = document.createElement('section'); brief.className = 'studio-brief';
        for (const suffix of ['topic', 'instructions', 'audience', 'title', 'subtitle']) {
            const group = document.getElementById(`${prefix}-${suffix}`)?.closest('.control-group');
            if (group) brief.append(group);
        }
        const fresh = document.getElementById(`${prefix}-force-fresh`)?.closest('label');
        if (fresh) brief.append(fresh);
        body.prepend(brief);
        const template = document.createElement('details'); template.className = 'studio-template';
        const summary = document.createElement('summary'); summary.textContent = 'Template & house style';
        template.append(summary);
        for (const suffix of ['template-path', 'template-editor']) {
            const group = document.getElementById(`${prefix}-${suffix}`)?.closest('.control-group');
            if (group) template.append(group);
        }
        brief.after(template);
    }
    const update = () => {
        cards.forEach(card => { card.hidden = true; });
        const markdown = format.value === 'md';
        const generating = action.value === 'generate';
        root.dataset.studioKind = kind.value;
        root.dataset.studioFormat = format.value;
        root.dataset.studioAction = action.value;
        if (markdown && generating) {
            reportCards[0].hidden = false;
            reportCards[1].hidden = false;
        } else if (!markdown) {
            (kind.value === 'deck' ? deckCards[generating ? 0 : 1] : reportCards[generating ? 0 : 2]).hidden = false;
        }
        document.getElementById('studio-md-augment').hidden = !(markdown && !generating);
        const mdTitle = reportCards[1]?.querySelector('h2');
        const subtitle = document.getElementById('docgen-subtitle')?.closest('.control-group');
        if (subtitle) subtitle.hidden = markdown;
        if (mdTitle) mdTitle.textContent = kind.value === 'deck' ? 'Save a Markdown slide deck' : 'Save a Markdown report';
        document.dispatchEvent(new CustomEvent('studio:selection', {detail: {kind: kind.value, action: action.value, format: format.value}}));
    };
    const augment = document.createElement('section');
    augment.id = 'studio-md-augment'; augment.className = 'card'; augment.hidden = true;
    // Static trusted markup only; document content is rendered by the controller.
    augment.innerHTML = `<div class="header"><h2>Augment a Markdown document</h2></div>
      <div class="deck-body"><p>Review a proposed change before applying it. Your original is retained in History and Restore.</p>
      <label for="studio-md-path">Existing document · path relative to your vault</label><input type="text" id="studio-md-path" placeholder="writing/report.md">
      <label for="studio-md-instruction">What should be added?</label><textarea id="studio-md-instruction" rows="4"></textarea>
      <p class="control-hint">New sections or slides are appended after your review. Existing content stays intact. Maximum source size: 120 KB.</p>
      <div class="actions"><button type="button" id="studio-md-preview" class="btn btn-primary">Preview change</button><button type="button" id="studio-md-cancel" class="btn btn-outline" hidden>Cancel</button></div>
      <p id="studio-md-status" role="status" aria-live="polite"></p><div id="studio-md-result"></div></div>`;
    pane.append(augment);
    [kind, action, format].forEach(control => control.addEventListener('change', update));
    document.addEventListener('chatekld:navigate', event => {
        if (event.detail.tabId !== 'docgen') return;
        event.preventDefault(); kind.value = 'report'; format.value = 'tex'; update(); showTab('deck');
    });
    initMarkdownAugment();
    update();
}

export function initIndexWorkspace() {
    const modal = document.querySelector('#index-settings-modal .modal');
    const extraction = document.getElementById('online-ocr-panel');
    if (!modal || !extraction || modal.dataset.workspaceReady) return;
    modal.dataset.workspaceReady = '1'; modal.classList.add('index-workspace');
    const tabs = document.createElement('div'); tabs.className = 'workspace-switcher'; tabs.setAttribute('role', 'group'); tabs.setAttribute('aria-label', 'Index tools views');
    const panels = [];
    const create = (label) => {
        const panel = document.createElement('section'); panel.className = 'index-workspace-view';
        const button = document.createElement('button'); button.type = 'button'; button.className = 'btn btn-outline'; button.textContent = label;
        button.addEventListener('click', () => select(panel)); tabs.append(button); panels.push({panel, button}); return panel;
    };
    const update = create('Update options'), documents = create('Documents'), history = create('History');
    const select = selected => panels.forEach(({panel, button}) => { panel.hidden = panel !== selected; button.setAttribute('aria-pressed', String(panel === selected)); });
    const heading = modal.querySelector('h2'); heading.after(tabs);
    const actions = modal.querySelector('.modal-actions');
    for (const {panel} of panels) modal.insertBefore(panel, actions);
    for (const child of [...modal.children]) {
        if ([heading, tabs, actions, ...panels.map(p => p.panel)].includes(child) || child === extraction) continue;
        update.append(child);
    }
    extraction.open = true; extraction.querySelector('summary').hidden = true; documents.append(extraction);
    const body = extraction.querySelector('.control-group');
    const historyTitle = document.createElement('h3'); historyTitle.textContent = 'Previous operations';
    const historyNote = document.createElement('p'); historyNote.textContent = 'Review an operation to see affected documents and available recovery actions.';
    history.append(historyTitle, historyNote);
    const historyHost = document.getElementById('online-run-history');
    const refresh = [...body.querySelectorAll('button')].find(b => b.getAttribute('onclick') === 'refreshOnlineHistory()');
    if (refresh) { refresh.className = 'btn btn-outline'; history.append(refresh); }
    history.append(historyHost);
    const target = document.createElement('details'); target.className = 'targeted-extraction';
    const title = document.createElement('summary'); title.textContent = 'Process a specific document or page range'; target.append(title);
    const search = document.getElementById('online-source-search');
    const searchLabel = body.querySelector('label[for="online-source-search"]');
    const browse = [...body.querySelectorAll('button')].find(b => b.getAttribute('onclick') === 'chooseOnlineSources()');
    const picker = document.getElementById('online-source-picker');
    for (const child of [...body.children]) if (![search, searchLabel, browse, picker].includes(child)) target.append(child);
    if (browse) browse.className = 'btn btn-primary';
    body.append(target);
    for (const id of ['online-ocr-status', 'online-ocr-announcement']) {
        const status = document.getElementById(id);
        if (status) modal.insertBefore(status, actions);
    }
    const rebuild = document.getElementById('vault-online-full-rebuild');
    const operation = document.getElementById('advanced-index-btn');
    rebuild?.addEventListener('change', () => { if (operation) operation.textContent = rebuild.checked ? 'Review full rebuild' : 'Review online update'; });
    document.addEventListener('library-extraction-history', () => select(history));
    select(update);
}

function initMarkdownAugment() {
    const get = id => document.getElementById(`studio-md-${id}`);
    const preview = get('preview'), cancel = get('cancel'), status = get('status'), result = get('result');
    let controller = null;
    const say = (message, failed = false) => {
        status.textContent = message;
        taskNote('studio-markdown', message, failed ? 'failed' : null);
    };
    cancel.addEventListener('click', () => controller?.abort());
    preview.addEventListener('click', async () => {
        if (controller) return;
        const path = get('path').value.trim(), instruction = get('instruction').value.trim();
        if (!path || !instruction) { say('Enter the document path and requested additions.'); return; }
        controller = new AbortController(); preview.disabled = true; cancel.hidden = false; result.replaceChildren();
        taskBegin('studio-markdown', 'Preparing Markdown additions');
        let received = false;
        let outcome = 'unknown';
        try {
            await consumeSSE('/api/docgen/markdown-preview', {
                method: 'POST', signal: controller.signal,
                body: JSON.stringify({path, instruction, output_kind: document.getElementById('studio-kind').value}),
            }, {
                onInfo: message => say(message),
                onError: message => { throw new Error(message); },
                onOther: event => {
                    if (!event.markdown_preview) return;
                    received = true;
                    outcome = 'ended';
                    const proposal = event.markdown_preview;
                    const heading = document.createElement('h3'); heading.textContent = `Proposed additions · ${proposal.path}`;
                    const text = document.createElement('pre'); text.textContent = proposal.addition;
                    const apply = document.createElement('button'); apply.className = 'btn btn-primary'; apply.textContent = 'Apply additions';
                    apply.addEventListener('click', () => confirmInline(apply, {
                        message: `Append these additions to ${proposal.path}? The original is retained in History and Restore.`,
                        confirmLabel: 'Apply additions',
                        onConfirm: async () => {
                            apply.disabled = true;
                            taskBegin('studio-markdown-apply', 'Saving Markdown additions');
                            // Dispatch is not proof of either publication or rejection.
                            let outcome = 'unknown';
                            try {
                                const response = await secureFetch('/api/docgen/markdown-apply', {method: 'POST', body: JSON.stringify({token: proposal.token, confirm: true})});
                                const data = await response.json();
                                if (!response.ok) {
                                    if (response.status < 500 && data.publication_outcome === 'not_written') {
                                        outcome = 'failed';
                                        apply.disabled = data.retry_allowed !== true;
                                    }
                                    throw new Error(data.error || 'Apply was not confirmed.');
                                }
                                if (data.ok !== true || data.path !== proposal.path) throw new Error('No matching save confirmation was received.');
                                outcome = 'completed';
                                status.textContent = `Saved ${data.path}. Original available in History and Restore.`;
                            } catch (error) {
                                status.textContent = outcome === 'unknown'
                                    ? `Save outcome unknown. Inspect ${proposal.path} and History and Restore before trying again. ${error.message}`
                                    : outcome === 'failed' && apply.disabled
                                        ? `${error.message} Generate a fresh preview before applying again.`
                                        : error.message;
                            } finally {
                                taskNote('studio-markdown-apply', status.textContent);
                                taskEnd('studio-markdown-apply', outcome);
                            }
                        },
                    }));
                    result.append(heading, text, apply); say('Ready to review. The document has not changed.');
                },
            });
            if (!received) say('No preview received. The document has not changed.', true);
        } catch (error) {
            const cancelled = error.name === 'AbortError';
            outcome = cancelled ? 'cancelled' : 'failed';
            say(cancelled ? 'Preview cancelled.' : error.message, !cancelled);
        }
        finally { controller = null; preview.disabled = false; cancel.hidden = true; taskEnd('studio-markdown', outcome); }
    });
}

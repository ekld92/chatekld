/**
 * Deck Generator tab: turn the vault into a Beamer .tex deck from a
 * user-supplied (and in-app editable) template.
 *
 * Imports only ui.js + api.js (per the JS module hierarchy). User-controlled
 * strings are rendered with textContent / createElement, never innerHTML.
 */
import { secureFetch, readSSE, logError } from './api.js';
import { setStatusA11y, taskBegin, taskEnd } from './ui.js';

let _generating = false;

function $(id) { return document.getElementById(id); }

function _status(msg, isError = false) {
    const el = $('deck-status');
    if (!el) return;
    el.style.display = msg ? 'block' : 'none';
    setStatusA11y(el, msg, isError);
}

function _clear(el) { while (el && el.firstChild) el.removeChild(el.firstChild); }

function _addActivity(text, muted = false) {
    const log = $('deck-activity');
    if (!log) return;
    const line = document.createElement('div');
    line.className = muted ? 'deck-activity-line muted' : 'deck-activity-line';
    line.textContent = text;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
}

// --- Template loading -------------------------------------------------------

export async function pickTemplateFile() {
    try {
        const r = await secureFetch('/api/deck/native-pick-file', { method: 'POST' });
        const d = await r.json();
        if (d && d.path) {
            $('deck-template-path').value = d.path;
            await loadTemplate();
        }
    } catch (e) { logError('Template pick failed', e); }
}

export async function pickOutDir() {
    try {
        const r = await secureFetch('/api/deck/native-pick-folder', { method: 'POST' });
        const d = await r.json();
        if (d && d.path) $('deck-out-dir').value = d.path;
    } catch (e) { logError('Output folder pick failed', e); }
}

export async function loadTemplate() {
    const path = ($('deck-template-path').value || '').trim();
    if (!path) { _status('Enter or browse to a template .tex/.sty first.', true); return; }
    _status('Loading template…');
    try {
        const r = await secureFetch('/api/deck/load-template', {
            method: 'POST',
            body: JSON.stringify({ path }),
        });
        const d = await r.json();
        if (!r.ok || d.error) { _status(d.error || 'Could not load template.', true); return; }

        $('deck-template-editor').value = d.tex || '';
        if (d.suite_root && !($('deck-out-dir').value || '').trim()) {
            $('deck-out-dir').value = d.suite_root;
        }
        _renderTemplateHint(d);
        _status('Template loaded. Edit the preamble below if you like, then Generate.');
    } catch (e) {
        logError('Load template failed', e);
        _status('Could not load template (see console).', true);
    }
}

function _renderTemplateHint(d) {
    const hint = $('deck-template-hint');
    if (!hint) return;
    _clear(hint);

    const macros = Array.isArray(d.macros) ? d.macros : [];
    const macroLine = document.createElement('div');
    if (macros.length) {
        macroLine.appendChild(document.createTextNode('Custom macros the model will use: '));
        macros.forEach((m, i) => {
            const code = document.createElement('code');
            code.textContent = m.signature;
            if (m.description) code.title = m.description;
            macroLine.appendChild(code);
            if (i < macros.length - 1) macroLine.appendChild(document.createTextNode(', '));
        });
    } else {
        macroLine.textContent = 'No custom macros detected in this template.';
    }
    hint.appendChild(macroLine);

    const bibLine = document.createElement('div');
    bibLine.className = 'muted';
    const n = d.bib_keys_count || 0;
    bibLine.textContent = n
        ? `${n} bibliography key(s) available — \\citefoot{key} citations enabled.`
        : 'No bibliography resolved (no active \\addbibresource) — citations will be plain-prose.';
    hint.appendChild(bibLine);
}

// --- Generation -------------------------------------------------------------

export async function generate() {
    if (_generating) return;
    const topic = ($('deck-topic').value || '').trim();
    const templateTex = $('deck-template-editor').value || '';
    if (!topic) { _status('A topic is required.', true); return; }
    if (!templateTex.trim()) { _status('Load (and optionally edit) a template first.', true); return; }

    const payload = {
        topic,
        template_tex: templateTex,
        template_path: ($('deck-template-path').value || '').trim(),
        out_dir: ($('deck-out-dir').value || '').trim(),
        deck_name: ($('deck-name').value || '').trim(),
        instructions: $('deck-instructions').value || '',
        audience: ($('deck-audience').value || '').trim(),
        citations_enabled: $('deck-citations').checked,
        overwrite: $('deck-overwrite').checked,
        max_sections: parseInt($('deck-max-sections').value, 10) || 8,
        agent_max_iterations: parseInt($('deck-agent-iters').value, 10) || 6,
        temperature: parseFloat($('deck-temp').value),
    };

    _generating = true;
    taskBegin('deck-generate', 'Generating deck');
    $('deck-generate-btn').disabled = true;
    _clear($('deck-activity'));
    _clear($('deck-result'));
    _status('Generating — this can take a few minutes for a multi-section deck…');

    try {
        const resp = await secureFetch('/api/deck/generate', {
            method: 'POST',
            body: JSON.stringify(payload),
        });
        // The streaming route returns 200; any non-200 is a pre-stream reject
        // (validation 400, origin 403) or an unexpected framework error. Surface
        // it and stop — otherwise readSSE() would consume an empty/HTML body and
        // leave the UI stuck on "Generating…".
        if (!resp.ok) {
            let msg = `Generation failed (HTTP ${resp.status}).`;
            try {
                const d = await resp.json();
                if (d && d.error) msg = d.error;
            } catch (_) { /* non-JSON error body */ }
            _status(msg, true);
            return;
        }
        for await (const evt of readSSE(resp)) {
            if (evt.info) _addActivity(evt.info);
            else if (evt.error) { _status(evt.error, true); _addActivity('ERROR: ' + evt.error); }
            else if (evt.iteration) _addActivity(`· iteration ${evt.iteration}`, true);
            else if (evt.tool_call) _addActivity(`· ${evt.tool_call.name}()`, true);
            else if (evt.tool_result) _addActivity(`· tool result${evt.tool_result.is_error ? ' [error]' : ''}`, true);
            else if (evt.deck) _renderDeck(evt.deck);
            // `thought` events are intentionally not surfaced (noisy).
        }
    } catch (e) {
        logError('Deck generation failed', e);
        _status('Deck generation failed (see console).', true);
    } finally {
        _generating = false;
        $('deck-generate-btn').disabled = false;
        taskEnd('deck-generate');
    }
}

function _renderDeck(deck) {
    const root = $('deck-result');
    _clear(root);

    if (deck.scaffold_error) {
        const err = document.createElement('div');
        err.className = 'warning-banner';
        err.style.display = 'block';
        err.textContent = 'Not written to disk: ' + deck.scaffold_error +
            ' — the generated .tex is still shown below; copy it manually.';
        root.appendChild(err);
        _status('Generated, but not written: ' + deck.scaffold_error, true);
    } else if (deck.tex_path) {
        const ok = document.createElement('div');
        ok.className = 'deck-summary';
        const n = deck.section_count || 0;
        const ph = deck.placeholder_count || 0;
        ok.textContent = `Wrote ${deck.tex_path}` +
            ` — ${n} section(s)${ph ? `, ${ph} placeholder` : ''}.`;
        root.appendChild(ok);
        if (deck.make_hint) {
            const hint = document.createElement('div');
            hint.className = 'muted';
            hint.appendChild(document.createTextNode('Compile it: '));
            const code = document.createElement('code');
            code.textContent = deck.make_hint;
            hint.appendChild(code);
            root.appendChild(hint);
        }
        _status(`Deck written (${n} section(s)). ` +
            (ph ? `${ph} section(s) had no content — review those.` : 'Done.'),
            ph > 0);
    }

    const warnings = Array.isArray(deck.warnings) ? deck.warnings : [];
    if (warnings.length) {
        const wrap = document.createElement('div');
        wrap.className = 'deck-warnings';
        const h = document.createElement('strong');
        h.textContent = 'Review before compiling:';
        wrap.appendChild(h);
        const ul = document.createElement('ul');
        warnings.forEach((w) => {
            const li = document.createElement('li');
            li.textContent = w;
            ul.appendChild(li);
        });
        wrap.appendChild(ul);
        root.appendChild(wrap);
    }

    const label = document.createElement('label');
    label.textContent = 'Generated .tex';
    label.setAttribute('for', 'deck-tex');
    root.appendChild(label);
    const ta = document.createElement('textarea');
    ta.id = 'deck-tex';
    ta.readOnly = true;
    ta.rows = 18;
    ta.className = 'deck-tex-output';
    ta.value = deck.tex || '';
    root.appendChild(ta);
}

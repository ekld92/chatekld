/** Explicit per-entry PDF cleanup. Reads cannot dispatch or replay mutations. */
import { secureFetch } from './api.js';
import { confirmInline } from './ui.js';
import { PreviewResource } from './pdfPreview.js';

const decorate = root => root.querySelectorAll('button').forEach(button => { if (!button.className) button.className = 'btn btn-outline btn-sm'; });
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function request(url, data, signal) {
    const response = await secureFetch(url, data === undefined ? {signal} : {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data), signal,
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
    return result;
}

export function mountPdfManagement(host, key) {
    let disposed = false, generation = 0, survivor = '', busy = false;
    let controller = null;
    const previews = [new PreviewResource(), new PreviewResource()];
    host.innerHTML = `<section class="pdf-management" aria-label="Files and attachments for ${esc(key)}">
      <h3>Files &amp; attachments — ${esc(key)}</h3><button type="button" data-pm-close>Close</button>
      <p data-pm-status role="status">Loading current attachments…</p>
      <div data-pm-content></div><div data-pm-plan></div>
      <div class="pdf-management-previews"><div data-pm-preview="0"></div><div data-pm-preview="1"></div></div>
    </section>`;
    const status = host.querySelector('[data-pm-status]');
    const content = host.querySelector('[data-pm-content]');
    const panel = host.querySelector('[data-pm-plan]');
    const say = text => { if (!disposed) status.textContent = text; };
    const invalidate = () => { generation++; panel.replaceChildren(); };
    function setBusy(value) {
        busy = value;
        content.querySelectorAll('button,input,select').forEach(el => { el.disabled = value || el.dataset.unavailable === 'true'; });
    }
    async function showPdf(rel, side) {
        const target = host.querySelector(`[data-pm-preview="${side}"]`);
        target.textContent = 'Loading PDF…';
        try {
            const url = await previews[side].load(`/api/audit/pdf?rel=${encodeURIComponent(rel)}`);
            if (!url || disposed) return;
            target.innerHTML = `<p>${esc(rel)}</p><iframe class="pending-full-pdf" title="${esc(rel)}" src="${esc(url)}"></iframe>`;
        } catch (error) { if (!disposed && error.name !== 'AbortError') target.textContent = error.message; }
    }
    async function load() {
        controller?.abort(); controller = new AbortController();
        const current = ++generation;
        panel.replaceChildren();
        try {
            const data = await request(`/api/accession/pdfs/entry?key=${encodeURIComponent(key)}`, undefined, controller.signal);
            if (disposed || current !== generation) return;
            if (!data.rows.some(r => r.rel === survivor && r.physical && r.attachments.length)) survivor = '';
            content.innerHTML = `<p><strong>${esc(data.parent.fields.title || key)}</strong> · Zotero ${esc(data.parent.key)}</p>
              <p>Choose the linked PDF to keep. Remove unwanted Zotero attachments explicitly, then preview deletion of unused files. Deletion moves files into recoverable storage; it does not erase them permanently.</p>
              <p>${esc(data.removal_rule || 'A copy that carries annotations or dependencies is kept unless you acknowledge its removal explicitly.')}</p>
              <div class="audit-table-scroll"><table class="audit-table"><thead><tr><th>Keep</th><th>PDF / content</th><th>Annotations &amp; dependencies</th><th>Zotero attachments</th><th>File action</th><th>Preview</th></tr></thead><tbody>
              ${data.rows.map(row => {
                  const cmp = row.comparison || {};
                  const same = cmp.identical_to || (row.physical ? data.rows.filter(r => r.rel !== row.rel && r.physical?.sha256 === row.physical.sha256).map(r => r.rel) : []);
                  const different = cmp.different_from || [];
                  const unavailable = !row.physical || !row.attachments.length;
                  const variant = cmp.variant ? `Collision clue: ${esc(cmp.variant)} suffix${cmp.counter != null ? ' ' + esc(cmp.counter) : ''} of ${esc(cmp.base_stem)} — a clue, not proof of identical content.` : '';
                  const facts = [];
                  if (row.native_annotations) facts.push(`${esc(row.native_annotations)} native Zotero annotation(s)`);
                  if (row.embedded_annotations) facts.push(`${esc(row.embedded_annotations)} annotation(s) embedded in the PDF`);
                  if (row.embedded_annotations === null) facts.push('embedded annotations unknown (PDF not readable)');
                  if (row.note_dependencies?.length) facts.push(`referenced by ${esc(row.note_dependencies.length)} note(s): ${row.note_dependencies.map(esc).join(', ')}`);
                  if (row.other_keys?.length) facts.push(`also mapped to ${row.other_keys.map(esc).join(', ')}`);
                  return `<tr data-pm-rel="${esc(row.rel)}" data-pm-annotated="${(row.native_annotations || row.embedded_annotations) ? '1' : '0'}"><td><input type="radio" name="pdf-survivor" value="${esc(row.rel)}" aria-label="Keep ${esc(row.rel)}" ${survivor === row.rel ? 'checked' : ''} ${unavailable ? 'disabled data-unavailable="true"' : ''}></td>
                    <td>${esc(row.rel)}<br>${row.physical ? `${esc(row.physical.size)} bytes` : esc(row.error)}${same.length ? `<br>Identical bytes: ${same.map(esc).join(', ')}` : ''}${different.length ? `<br>Different bytes from: ${different.map(esc).join(', ')} — a different document version; kept by default.` : ''}${variant ? `<br>${variant}` : ''}</td>
                    <td>${facts.length ? facts.join('<br>') : 'No annotations or dependencies found'}${(row.native_annotations || row.embedded_annotations) ? '<br><strong>Blocks removal until acknowledged.</strong>' : ''}</td>
                    <td>${row.attachments.map(a => `<p>${esc(a.title || a.key)} · ${esc(a.mode)} · ${esc(a.child_counts?.annotations ?? 'Unknown')} Zotero annotations · ${esc(a.child_counts?.notes ?? 'Unknown')} child notes <button type="button" data-pm-trash="${esc(a.key)}">Remove attachment…</button></p>`).join('') || 'No live attachment'}</td>
                    <td><select data-pm-action aria-label="Action for ${esc(row.rel)}"><option value="keep">Keep file</option><option value="unmap">Unmap only</option><option value="delete">Delete file (recoverable)</option></select></td>
                    <td><button type="button" data-pm-view="0">Preview left</button><button type="button" data-pm-view="1">Preview right</button></td></tr>`;
              }).join('')}</tbody></table></div>
              <label><input type="checkbox" data-pm-ack-annotations> I acknowledge that a copy marked for deletion carries annotations; they leave with the file into recoverable storage (native Zotero annotations must be removed with their attachment first).</label>
              <button type="button" data-pm-cleanup>Preview selected cleanup</button>
              ${data.attachments?.filter(a => !data.rows.some(r => r.rel === a.rel)).length ? `<details><summary>Other attachments on this entry</summary>${data.attachments.filter(a => !data.rows.some(r => r.rel === a.rel)).map(a => `<p>${esc(a.title || a.key)} · ${esc(a.mode)} · ${esc(a.path)} <button type="button" data-pm-trash="${esc(a.key)}">Remove attachment…</button></p>`).join('')}</details>` : ''}
              <details><summary>Link another PDF</summary><p>Choose a PDF in the bibliography folder. Existing PDFs remain until separately removed.</p>
                <label>Find PDF <input type="search" data-pm-query></label><button type="button" data-pm-search>Search files</button>
                <select data-pm-candidate aria-label="PDF to link"></select><button type="button" data-pm-link>Preview link</button></details>
              <details><summary>Operations and recovery (${data.operations.length})</summary>${data.operations.map(op => `<p>${esc(op.kind || 'cleanup')} · ${esc(op.status)} · ${esc(op.error || '')} <code>${esc(op.id)}</code><br>${(op.steps || []).map(step => `${esc(step.action)}: ${esc(step.rel)} (${esc(step.status)})`).join('<br>')}${op.kind === 'link' ? esc(op.plan.rel) : ''}${op.kind === 'trash' ? esc(op.plan.before.title || op.plan.attachment) : ''}
                ${(!op.kind || (op.kind === 'link' && op.status === 'failed')) && op.status !== 'restored' ? `<button type="button" data-pm-restore="${esc(op.id)}">${op.kind === 'link' ? 'Revert the local mapping…' : 'Restore files and mappings…'}</button>` : ''}
                ${(op.kind || op.status === 'needs_inspection') && !['complete', 'restored', 'failed'].includes(op.status) ? `<button type="button" data-pm-inspect="${esc(op.id)}">Inspect retained outcome…</button>` : ''}</p>`).join('') || '<p>No operations yet.</p>'}</details>`;
            decorate(content);
            content.querySelectorAll('input[name="pdf-survivor"]').forEach(input => { input.onchange = () => {
                survivor = input.value; invalidate();
                content.querySelectorAll('[data-pm-rel]').forEach(row => { if (row.dataset.pmRel === survivor) row.querySelector('[data-pm-action]').value = 'keep'; });
            }; });
            content.querySelectorAll('[data-pm-action]').forEach(select => { select.onchange = invalidate; });
            content.querySelectorAll('[data-pm-view]').forEach(button => { button.onclick = () => showPdf(button.closest('[data-pm-rel]').dataset.pmRel, Number(button.dataset.pmView)); });
            content.querySelectorAll('[data-pm-trash]').forEach(button => { button.onclick = () => propose({action:'trash', attachment:button.dataset.pmTrash, survivor}); });
            content.querySelector('[data-pm-cleanup]').onclick = () => {
                const actions = {};
                let annotated = false;
                content.querySelectorAll('[data-pm-rel]').forEach(row => { const action = row.querySelector('select').value; if (action !== 'keep') { actions[row.dataset.pmRel] = action; if (action === 'delete' && row.dataset.pmAnnotated === '1') annotated = true; } });
                const acknowledge = annotated && content.querySelector('[data-pm-ack-annotations]')?.checked === true;
                propose({action:'cleanup', survivor, actions, acknowledge_annotations:acknowledge});
            };
            content.querySelector('[data-pm-search]').onclick = async () => {
                const current = ++generation;
                const q = content.querySelector('[data-pm-query]').value;
                try {
                    const found = await request(`/api/accession/pdfs/candidates?q=${encodeURIComponent(q)}`);
                    if (disposed || current !== generation) return;
                    content.querySelector('[data-pm-candidate]').innerHTML = found.paths.map(path => `<option value="${esc(path)}">${esc(path)}</option>`).join('');
                    panel.replaceChildren(); say(`${found.paths.length} of ${found.total} files shown.`);
                } catch (error) { say(error.message); }
            };
            content.querySelector('[data-pm-candidate]').onchange = invalidate;
            content.querySelector('[data-pm-link]').onclick = () => propose({action:'link', pdf:content.querySelector('[data-pm-candidate]').value});
            content.querySelectorAll('[data-pm-restore]').forEach(button => { button.onclick = () => confirmInline(button, {
                message:'Restore this operation’s physical files and mappings? New files and subsequent mapping edits will not be overwritten.', confirmLabel:'Restore',
                onConfirm:() => execute('/api/accession/pdfs/restore', {operation:button.dataset.pmRestore, confirm:true}),
            }); });
            content.querySelectorAll('[data-pm-inspect]').forEach(button => { button.onclick = async () => {
                const current = ++generation;
                try {
                    const result = await request('/api/accession/pdfs/inspect', {operation:button.dataset.pmInspect});
                    if (disposed || current !== generation) return;
                    panel.innerHTML = `<h4>Observed outcome</h4><p>${esc(result.reason)}</p>${result.completable ? '<button type="button" data-pm-complete>Record this completion…</button>' : ''}`;
                    const complete = panel.querySelector('[data-pm-complete]');
                    if (complete) complete.onclick = () => confirmInline(complete, {message:'Record the observed completion? This performs no Zotero write.', confirmLabel:'Record completion', onConfirm:() => {
                        if (current !== generation || disposed) return;
                        return execute('/api/accession/pdfs/complete', {operation:result.operation, token:result.token, confirm:true});
                    }});
                } catch (error) { say(error.message); }
            }; });
            say('Current entry loaded. No changes made by this refresh.');
        } catch (error) { if (!disposed && error.name !== 'AbortError') say(error.message); }
    }
    async function propose(action) {
        if (busy) return;
        const current = ++generation;
        panel.replaceChildren(); setBusy(true); say('Checking current evidence…');
        try {
            const result = await request('/api/accession/pdfs/preview', {key, ...action});
            if (disposed || current !== generation) return;
            const plan = result.plan;
            panel.innerHTML = `<h4>Review changes</h4><p>${esc(result.message)}</p>
              <p>Entry: ${esc(plan.parent.fields.title || key)} (${esc(plan.parent.key)})</p>
              ${plan.survivor ? `<p>Keep: ${esc(typeof plan.survivor === 'string' ? plan.survivor : plan.survivor.rel)}</p>` : ''}
              ${plan.rel ? `<p>Link: ${esc(plan.rel)}</p>` : ''}
              ${plan.attachment ? `<p>Remove attachment: ${esc(plan.before.title || plan.attachment)} (${esc(plan.attachment)}) · ${esc(plan.before.path || '')}</p>` : ''}
              ${(plan.steps || []).map(step => `<p>${esc(step.action)}: ${esc(step.rel)}${step.notes.length ? `<br>Referenced by: ${step.notes.map(esc).join(', ')}` : ''}</p>`).join('')}
              ${(result.blockers || []).map(msg => `<p>${esc(msg)}</p>`).join('')}
              ${action.action === 'trash' ? '<label><input type="checkbox" data-pm-loss> Include all annotations and child notes belonging to this attachment, including changes made before removal.</label>' : ''}
              ${action.action === 'link' ? '<label><input type="checkbox" data-pm-plugins> I reviewed Zotero attachment-renaming plugin settings.</label>' : ''}
              ${result.executable ? '<button type="button" data-pm-confirm>Confirm this plan…</button>' : '<p>No changes can be applied until the listed conflicts are resolved.</p>'}`;
            decorate(panel);
            const button = panel.querySelector('[data-pm-confirm]');
            if (button) button.onclick = () => {
                if (action.action === 'trash' && !panel.querySelector('[data-pm-loss]').checked) { say('Confirm the annotation and child-note scope first.'); return; }
                const accept = panel.querySelector('[data-pm-loss]')?.checked === true;
                const plugins = panel.querySelector('[data-pm-plugins]')?.checked === true;
                confirmInline(button, {message:result.message, confirmLabel:'Apply reviewed changes', onConfirm:() => {
                    if (current !== generation || disposed) return;
                    return execute('/api/accession/pdfs/apply', {key, ...action, token:result.token, confirm:true, accept_annotation_loss:accept, acknowledge_plugins:plugins});
                    // `action` already carries acknowledge_annotations for a cleanup, so the apply repeats exactly what was previewed.
                }});
            }
            say('Preview ready. Nothing has been changed.');
        } catch (error) { if (current === generation) say(error.message); }
        finally { if (!disposed) setBusy(false); }
    }
    async function execute(url, data) {
        if (busy || disposed) return;
        setBusy(true); panel.replaceChildren(); say('Applying reviewed changes…');
        try {
            const result = await request(url, data);
            if (disposed) return;
            await load();
            say(`Operation ${result.operation.status}. ${result.operation.error || ''} ${result.operation.status === 'needs_inspection' ? 'Inspect the retained operation before continuing.' : ''}`);
        } catch (error) { say(`Outcome needs checking: ${error.message}. Refresh this entry to inspect retained operations.`); }
        finally { if (!disposed) setBusy(false); }
    }
    function teardown() {
        disposed = true; generation++; controller?.abort(); previews.forEach(p => p.clear()); host.remove();
    }
    host.querySelector('[data-pm-close]').onclick = teardown;
    decorate(host);
    load();
    return teardown;
}

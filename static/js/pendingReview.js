/** Pending PDFs: selection, proposals and per-file approval are separate state. */
import { secureFetch, consumeSSE } from './api.js';
import { confirmInline, foldText, announceError } from './ui.js';
import { createPagePreview } from './pdfPreview.js';

const selections = new Map();
const registrationDrafts = new Map();
/** Match styles.css pending-layout collapse — JS used to auto-hide at 1200px. */
const QUEUE_AUTO_HIDE_MQ = '(max-width: 900px)';
const FILTER_DEBOUNCE_MS = 200;
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
async function post(path, data, signal) {
    const response = await secureFetch(`/api/accession/review/${path}`, { method: 'POST', body: JSON.stringify(data), signal });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
    return result;
}

function isTypingTarget(el) {
    if (!el || !el.closest) return false;
    if (el.closest('.inline-confirm-strip')) return true;
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
    return !!el.closest('[contenteditable="true"]');
}

function rowHaystack(r) {
    return foldText([
        r.pdf.name,
        (r.review?.chosen || {}).title || '',
        ...(r.resolution?.candidates || []).map(c => c.title || ''),
    ].join(' '));
}

export function mountPendingReview(host, data, {initialPdf = null} = {}) {
    let disposed = false, generation = 0, closePreview = () => {}, active = null, opened = null, candidate = null;
    let controller = null, busy = false, filter = '', mappingOffers = null, filterTimer = null;
    const rows = new Map(data.rows.map(r => [r.pdf.rel, r]));
    const selection = selections.get(data.review_scope) || new Set();
    selections.set(data.review_scope, selection);
    for (const rel of selection) if (!rows.has(rel)) selection.delete(rel);
    host.innerHTML = `<div class="pending-review" tabindex="-1">
      <div class="pending-batch" role="toolbar" aria-label="Review batch actions">
        <button type="button" class="btn btn-outline btn-sm" data-pending="visible">Select visible</button>
        <button type="button" class="btn btn-outline btn-sm" data-pending="confirmed">Select visible confirmed</button>
        <button type="button" class="btn btn-outline btn-sm" data-pending="clear">Clear selection</button>
        <button type="button" class="btn btn-outline btn-sm" data-pending="export">Copy confirmed DOIs</button>
        <button type="button" class="btn btn-outline btn-sm" data-pending="reconcile">Check bibliography</button>
        <button type="button" class="btn btn-outline btn-sm" data-pending="authorize">Authorize Zotero Desktop…</button>
        <span class="pending-count"></span>
        <p class="pending-kbd-hint">With a PDF open: <kbd>C</kbd> confirm · <kbd>W</kbd> wrong · <kbd>S</kbd> skip · <kbd>⌫</kbd> undo · ←/→ navigate</p>
      </div>
      <p class="pending-status" role="status" aria-live="polite"></p><div class="pending-export"></div>
      <div class="pending-layout"><section class="pending-queue" aria-label="PDF queue"><label>Status <select class="pending-state"><option value="">All PDFs</option>${['Needs identification','Needs review','Ready to register','Awaiting bibliography','Linked','Discrepancies','Recovery','Excluded','Retired','Accepted differences'].map(s=>`<option value="${esc(s)}">${esc(data.labels?.[s] || s)}</option>`).join('')}</select></label><button type="button" class="btn btn-outline btn-sm pending-refresh">Refresh Zotero changes</button><p class="pending-freshness"></p><details class="pending-changes"><summary>Recent Zotero changes</summary><div></div></details><label>Filter PDFs <input type="search" class="pending-filter"></label><table class="audit-table"><thead><tr><th scope="col">Select</th><th scope="col">PDF</th><th scope="col">Review</th></tr></thead><tbody></tbody></table></section>
      <section class="pending-reader" aria-label="PDF review"><p>Select a PDF to review it.</p></section></div></div>`;
    const root = host.querySelector('.pending-review'), list = root.querySelector('tbody');
    const reader = root.querySelector('.pending-reader'), status = root.querySelector('.pending-status');
    const results = root.querySelector('.pending-export');
    // One static live region for the panel — never mint role="status" inside
    // reader on open (WebKit can miss a region created the same tick it fills).
    const say = (text, isError = false) => {
        if (disposed) return;
        status.textContent = text || '';
        if (isError && text) announceError(text);
    };
    const visible = () => {
        const tokens = filter.split(/\s+/).filter(Boolean);
        return [...rows.values()].filter(r => {
            const selectedState = root.querySelector('.pending-state').value;
            if (selectedState && r.state !== selectedState) return false;
            const hay = rowHaystack(r);
            return tokens.every(t => hay.includes(t));
        });
    };
    const items = () => [...selection].map(pdf => ({ pdf, token: rows.get(pdf)?.review?.token || null }));
    function count() {
        const shown = new Set(visible().map(r => r.pdf.rel));
        const picked = [...selection].map(rel => rows.get(rel));
        const eligible = picked.filter(r => r?.review?.export_eligible);
        root.querySelector('.pending-count').textContent = `${picked.length} PDFs selected · ${new Set(eligible.map(r => r.review.chosen.doi)).size} unique confirmed DOIs · ${picked.length - eligible.length} need review · ${[...selection].filter(rel => !shown.has(rel)).length} hidden selected`;
        root.querySelectorAll('[data-pending="export"], [data-pending="reconcile"]').forEach(b => { b.disabled = !selection.size || busy || !!data.review_error; });
    }
    function table() {
        list.innerHTML = visible().map(r => `<tr data-pdf="${esc(r.pdf.rel)}"${r.pdf.rel === active ? ' class="pending-active"' : ''}><td><input type="checkbox" aria-label="Select ${esc(r.pdf.name)}" ${selection.has(r.pdf.rel) ? 'checked' : ''} ${r.exists ? '' : 'disabled'}></td><td><button type="button" class="audit-link-btn pending-open" ${r.exists ? '' : 'disabled'}>${esc(r.pdf.name)}</button><small>${esc(r.state_label || r.state)}</small></td><td>${esc(r.review?.identity_state === 'changed' ? 'file changed' : r.review?.status || 'unreviewed')}${r.review?.conflicting_evidence ? ' · new evidence conflicts' : ''}</td></tr>`).join('');
        count();
    }
    function details() {
        const proposal = opened.proposal || {}, candidates = proposal.candidates || [];
        const rejected = new Set(opened.rejected || []);
        if (!candidate || !opened.candidate_ids.includes(candidate)) {
            const confirmedIndex = opened.chosen ? candidates.findIndex(c => JSON.stringify(c) === JSON.stringify(opened.chosen)) : -1;
            candidate = confirmedIndex >= 0 ? opened.candidate_ids[confirmedIndex] : opened.candidate_ids.find(id => !rejected.has(id)) || null;
        }
        const chosen = candidates[opened.candidate_ids.indexOf(candidate)];
        const part = reader.querySelector('.pending-details');
        const focusedAction = part.contains(document.activeElement) ? document.activeElement.dataset.review : null;
        const focusedCandidate = document.activeElement === part.querySelector('select');
        part.innerHTML = `<h3 tabindex="-1">${esc(rows.get(active).pdf.name)}</h3><p>${esc(opened.status)}${opened.conflicting_evidence ? ' — new evidence conflicts with the confirmed DOI; review before continuing.' : ''}</p>
          ${opened.status === 'confirmed' && opened.chosen ? `<p class="pending-confirmed">Confirmed identification: ${esc(opened.chosen.title || 'Title unavailable')} — ${esc(opened.chosen.doi || 'No DOI')}. Inspecting another candidate does not replace this confirmation.</p>` : ''}
          <p>${proposal.legacy_evidence ? 'Legacy evidence · ' : ''}${esc(proposal.tier || 'No identification')} · ${esc(proposal.evidence || 'Evidence unavailable')}</p>
          <p>Lookup outcome: ${esc(proposal.outcome || 'Unavailable')}</p><p>${esc((proposal.notes || []).join(' · '))}</p>
          <label>Candidate <select class="pending-candidates"><option value="">Choose a candidate</option>${candidates.map((c, i) => `<option value="${esc(opened.candidate_ids[i])}" ${candidate === opened.candidate_ids[i] ? 'selected' : ''}>${rejected.has(opened.candidate_ids[i]) ? 'Rejected — ' : ''}${esc(c.title || c.doi || 'Untitled candidate')}</option>`).join('')}</select></label>
          ${chosen ? `<dl><dt>Title</dt><dd>${esc(chosen.title || 'Unavailable')}</dd><dt>Authors</dt><dd>${esc((chosen.authors || []).join('; ') || 'Unavailable')}</dd><dt>Year</dt><dd>${esc(chosen.year || 'Unavailable')}</dd><dt>Journal / venue</dt><dd>${esc(chosen.container || 'Unavailable')}</dd><dt>DOI</dt><dd>${esc(chosen.doi || 'Unavailable — cannot export a DOI')}</dd><dt>Source</dt><dd>${esc(chosen.source || 'Unavailable')}</dd></dl>` : '<p>No candidate selected.</p>'}
          ${chosen ? `<p>Title agreement: ${chosen.title_agreement == null ? 'Unavailable' : esc(Math.round(chosen.title_agreement * 100)) + '% similarity (not a probability)'} · Author agreement: ${chosen.author_agreement == null ? 'Unavailable' : chosen.author_agreement ? 'Observed name overlap' : 'No observed name overlap'}</p>` : ''}
          ${proposal.evidence_snapshot ? `<details><summary>Observed PDF evidence and sources</summary><p>Extracted title: ${esc(proposal.evidence_snapshot.title || 'Unavailable')}</p><ul>${(proposal.evidence_snapshot.identifiers || []).map(o => `<li>${esc(o.value)} — ${esc(o.origin)}</li>`).join('')}</ul><pre>${esc(JSON.stringify({field_sources:chosen?.field_sources || {}, field_conflicts:chosen?.field_conflicts || {}, lookups:proposal.source_attempts || []}, null, 2))}</pre></details>` : ''}
          <div class="pending-actions">
            <button type="button" class="btn btn-primary btn-sm" data-review="confirm" ${chosen ? '' : 'disabled'}>[confirm &amp; next]</button>
            <button type="button" class="audit-link-btn" data-review="reject" ${chosen ? '' : 'disabled'}>[wrong match]</button>
            <button type="button" class="audit-link-btn" data-review="defer">[skip]</button>
            <button type="button" class="audit-link-btn" data-review="undo">[undo]</button>
            <button type="button" class="audit-link-btn" data-review="identify">[identify again]</button>
            <button type="button" class="audit-link-btn" data-review="search">[edit search]</button>
          </div>
          <div class="pending-search"></div>
          <p>Zotero reference: ${esc(rows.get(active).parent_state || 'unverified')} · Attachment: ${esc(rows.get(active).attachment_state || 'unverified')} · Bibliography: ${esc(rows.get(active).bibliography_state || 'unknown')} · ${esc(rows.get(active).state_label || rows.get(active).state)} ${esc(rows.get(active).reason || '')} ${esc(rows.get(active).reopened || '')}</p>
          ${(rows.get(active).associations || []).length ? `<details open><summary>Current Zotero and CHERQAiN associations</summary><pre>${esc(JSON.stringify({zotero:rows.get(active).associations, paperMind:rows.get(active).mapping, previous:rows.get(active).accepted?.relationship},null,2))}</pre></details>` : ''}
          ${(['Discrepancies','Accepted differences','Awaiting bibliography'].includes(rows.get(active).state) || (rows.get(active).state === 'Needs review' && rows.get(active).registration_state === 'registered')) ? `<div>${[['use','Use the Zotero association'],['keep','Keep both as they are'],['later','Review later'],['revoke','Reconsider / revoke'],['repair','Preview repair of Zotero']].map(([action,label])=>`<button type="button" class="btn btn-outline btn-sm" data-discrepancy="${action}">${label}</button>`).join('')}</div>` : ''}
          ${rows.get(active).can_create ? '<button type="button" class="btn btn-outline btn-sm" data-review="register">Register/link…</button>' : ''}
          ${rows.get(active).state === 'Recovery' ? `<p>Retained operation evidence. An absent search result does not authorize another POST.</p><pre>${esc(JSON.stringify(rows.get(active).attempt,null,2))}</pre><button type="button" class="btn btn-outline btn-sm" data-review="recover">Inspect / recover acknowledged attachment</button>` : ''}
          ${(rows.get(active).bookkeeping || []).some(i=>i.status==='prepared') ? `<p>Local mapping completion needs verification. Refresh Zotero changes to inspect the saved mapping; use the Zotero association again only after reviewing it.</p><pre>${esc(JSON.stringify(rows.get(active).bookkeeping,null,2))}</pre>` : ''}
          <div class="pending-repair"></div><div class="pending-registration"></div>
          ${(rows.get(active).external_repairs || []).length ? `<details><summary>Retained parent-repair operations</summary><pre>${esc(JSON.stringify(rows.get(active).external_repairs,null,2))}</pre>${rows.get(active).external_repairs.map(op=>`<button type="button" data-repair-inspect="${esc(op.id)}">Inspect retained repair</button>`).join('')}</details>` : ''}
          <button type="button" class="audit-link-btn" data-review="rename-preview">[preview rename]</button>
          ${rows.get(active).mapped_key ? '<button type="button" class="btn btn-outline btn-sm" data-review="attach">Attach this PDF in Zotero…</button>' : ''}
          <details class="pending-curation"><summary>PDF curation</summary><p>Curation queues the file in the audit’s mapping.json (“pending” = add to Zotero later). It is separate from identification approval, which writes only the local review store.</p>
            <button type="button" class="audit-link-btn" data-curation="pending">[add later]</button>
            <button type="button" class="audit-link-btn" data-curation="no_match">[exclude from registration]</button>
            <button type="button" class="audit-link-btn" data-curation="clear">[clear curation]</button>
          </details>`;
        part.querySelector('select').onchange = event => { candidate = event.target.value || null; details(); };
        if (focusedCandidate) part.querySelector('select').focus();
        else if (focusedAction) part.querySelector(`[data-review="${focusedAction}"]`)?.focus();
    }
    async function open(pdf) {
        // generation + AbortController: a slow open must not paint after the
        // user already moved to another row (or left the tab).
        controller?.abort(); controller = new AbortController();
        const current = ++generation;
        closePreview(); active = pdf; opened = null; candidate = null;
        say('Loading review…');
        reader.innerHTML = '<p>Loading review…</p>'; table();
        try {
            const result = await post('open', {pdf}, controller.signal);
            if (disposed || current !== generation) return;
            opened = result; rows.get(pdf).review = result;
            // Auto-hide only where CSS already stacks the queue (900px), so a
            // mid-width window keeps both panes unless the user toggles.
            if (window.matchMedia?.(QUEUE_AUTO_HIDE_MQ).matches) root.classList.add('pending-queue-hidden');
            reader.innerHTML = `<div class="pending-navigation">
              <button type="button" class="audit-link-btn" data-nav="-1">[previous]</button>
              <button type="button" class="audit-link-btn" data-nav="1">[next]</button>
              <button type="button" class="audit-link-btn" data-toggle-queue>[hide / show queue]</button>
            </div><div class="pending-document"></div><div class="pending-details"></div>`;
            closePreview = createPagePreview(reader.querySelector('.pending-document'), pdf, result);
            details(); table();
            say('');
            reader.querySelector('h3')?.focus();
        } catch (error) {
            if (!disposed && current === generation && error.name !== 'AbortError') {
                reader.innerHTML = '<p>Select a PDF to review it.</p>';
                say(error.message, true);
            }
        }
    }
    function next(offset) {
        const queue = visible().map(r => r.pdf.rel).filter(rel => rows.get(rel).exists);
        const index = queue.indexOf(active) + offset;
        if (index >= 0 && index < queue.length) open(queue[index]);
        else say('End of the filtered queue.');
    }
    async function decision(action) {
        // busy gates overlapping writes; generation refuses a late response
        // after the user opened another PDF mid-request.
        if (!opened || busy) return;
        const pdf = active, current = generation;
        busy = true; count();
        try {
            const result = await post('decision', {pdf, token: opened.token, candidate_id: candidate, action, confirm: true});
            if (disposed || current !== generation) return;
            if (result.workspace_row) rows.set(pdf, result.workspace_row);
            else rows.get(pdf).review = result;
            opened = {...opened, ...result};
            if (action === 'reject') candidate = null;
            table(); details(); say(action === 'confirm' ? 'Confirmation saved for this file.' : 'Review saved.');
            if (action === 'confirm' || action === 'defer') next(1);
        } catch (error) { say(error.message, true); }
        finally { busy = false; if (!disposed) count(); }
    }
    async function identify(search) {
        if (!opened || busy) return;
        const pdf = active, current = generation, token = opened.token;
        busy = true; count();
        try {
            let result = null;
            await consumeSSE('/api/accession/review/search', {method:'POST', body:JSON.stringify({pdf, token, search, confirm:true})}, {
                onInfo:say, onOther: frame => { if (frame.review) result = frame.review; },
                onError: error => { throw new Error(error); },
            });
            if (!result) throw new Error('Identification ended without a saved result.');
            rows.get(pdf).review = result;
            if (!disposed && current === generation) { opened = {...opened, ...result}; candidate = null; details(); table(); say('New proposals saved. Previous decisions are preserved.'); }
        } catch (error) { say(error.message, true); }
        finally { busy = false; if (!disposed) count(); }
    }
    function clickReview(action, btn) {
        if (busy || !action) return;
        if (action === 'register' || action === 'recover') { registration(action === 'recover'); return; }
        if (action === 'rename-preview') {
            const pdf = active, current = generation;
            busy = true; count();
            post('rename-preview', {pdf}).then(result => {
                if (disposed || current !== generation) return;
                const panel = document.createElement('div');
                const heading = document.createElement('h4'); heading.textContent = 'Rename preview — no changes made';
                const text = document.createElement('div');
                text.innerHTML = `<dl><dt>Physical PDF filename</dt><dd>${esc(result.physical_filename.current)} → ${esc(result.physical_filename.proposed || 'No rename proposed')}</dd><dt>Zotero attachment title</dt><dd>Preserved</dd><dt>Zotero linked-file paths</dt><dd>${result.linked_paths.length ? result.linked_paths.map(p => `${esc(p.current)} → ${esc(p.proposed || 'No change proposed')}`).join('<br>') : 'No links found in the database snapshot'}</dd><dt>Reference-note filenames</dt><dd>Preserved</dd><dt>Notes referring to this PDF</dt><dd>${result.reference_notes.references.map(esc).join('<br>') || 'None found'}</dd></dl><p>${esc(result.verification_error || '')}</p><p>${result.limitations.map(esc).join('<br>')}</p>`;
                panel.append(heading, text); results.replaceChildren(panel);
                say(result.verification_error || result.proposal.reason);
            }).catch(error => say(error.message, true)).finally(() => {busy=false;if(!disposed)count();});
            return;
        }
        if (action === 'attach') { attach(btn,active,rows.get(active).mapped_key); return; }
        if (action === 'identify') { confirmInline(btn, {message:'Look up this PDF online? Identifiers and search terms go to the configured sources. Results are saved to the local review store only — nothing is mapped or attached.', confirmLabel:'Identify online', onConfirm:()=>identify(null)}); return; }
        if (action === 'search') {
            const form = reader.querySelector('.pending-search');
            form.innerHTML = `<form><h4>Correct the search</h4>${['title','author','year','doi'].map(k=>`<label>${esc(k)} <input name="${k}" maxlength="${k === 'title' ? 500 : 200}"></label>`).join('')}<button type="submit" class="btn btn-outline btn-sm">Search online</button></form>`;
            form.querySelector('form').onsubmit = event => { event.preventDefault(); const fields = Object.fromEntries(new FormData(event.target)); const button = event.target.querySelector('button'); confirmInline(button, {message:'Send these corrected search terms to the configured bibliographic sources?', confirmLabel:'Search', onConfirm:()=>identify(fields)}); };
            return;
        }
        // confirm / reject / undo rewrite the durable review store — same
        // confirmInline posture as ambiguous-deck assign/undo. defer (skip)
        // is navigation-only and stays one-click like sibling decks.
        if (action === 'confirm') {
            confirmInline(btn, {
                message: 'Confirm this identification for the open PDF? Writes the local review store only — nothing is mapped or attached yet.',
                confirmLabel: 'Confirm',
                onConfirm: () => decision('confirm'),
            });
            return;
        }
        if (action === 'reject') {
            confirmInline(btn, {
                message: 'Mark this candidate as a wrong match? Writes the local review store; other candidates stay available.',
                confirmLabel: 'Wrong match',
                onConfirm: () => decision('reject'),
            });
            return;
        }
        if (action === 'undo') {
            confirmInline(btn, {
                message: 'Undo the last review decision for this PDF? Restores the prior local review state for this file.',
                confirmLabel: 'Undo',
                onConfirm: () => decision('undo'),
            });
            return;
        }
        decision(action);
    }
    reader.addEventListener('click', event => {
        const btn = event.target.closest('button'); if (!btn) return;
        if (btn.hasAttribute('data-nav')) { next(Number(btn.dataset.nav)); return; }
        if (btn.hasAttribute('data-toggle-queue')) {
            root.classList.toggle('pending-queue-hidden');
            if (!root.classList.contains('pending-queue-hidden')) root.querySelector('.pending-filter').focus();
            return;
        }
        if (btn.dataset.repairInspect && !busy) {
            const operation=btn.dataset.repairInspect, current=generation, pdf=active;
            busy=true; count();
            post('repair-inspect',{operation}).then(inspected=>{
                if(disposed || current!==generation || pdf!==active) return;
                const panel=reader.querySelector('.pending-repair');
                panel.innerHTML=`<h4>Repair inspection</h4><p>${esc(inspected.message)}</p><pre>${esc(JSON.stringify(inspected.evidence.current,null,2))}</pre>${inspected.eligible ? '<button type="button" data-repair-complete>Record inspected completion</button>' : '<p>Completion is unavailable for this evidence. The retained history is preserved.</p>'}`;
                const complete=panel.querySelector('[data-repair-complete]');
                if(complete) complete.onclick=()=>confirmInline(complete,{message:'Record only the inspected current relationship in CHERQAiN? The original outcome remains in history; no Zotero request or undo is authorized.',confirmLabel:'Confirm bookkeeping completion',onConfirm:async()=>{
                    if(busy || disposed || current!==generation || pdf!==active) return;
                    busy=true; count();
                    try {await post('repair-complete',{operation,token:inspected.token,confirm:true}); replaceRows(await post('refresh-zotero',{})); say('Inspected completion recorded; original outcome retained.');}
                    catch(error){say(error.message,true);}
                    finally{busy=false;if(!disposed)count();}
                }});
            }).catch(error=>say(error.message,true)).finally(()=>{busy=false;if(!disposed)count();});
            return;
        }
        if (btn.dataset.discrepancy && !busy) {
            const row = rows.get(active), action = btn.dataset.discrepancy;
            confirmInline(btn, {message:action === 'repair' ? 'Preview the external repair requirements? This does not write to Zotero.' : 'Save this local reconciliation choice for the displayed relationship?', confirmLabel:'Continue', onConfirm:async()=>{
                try {
                    const result = await post('discrepancy',{pdf:row.pdf.rel,evidence:row.evidence,action,confirm:true});
                    if (result.preview) {
                        if (disposed || active !== row.pdf.rel) return;
                        const panel=reader.querySelector('.pending-repair'), preview=result.preview;
                        const parents=preview.command.parents || [];
                        const parentDescription=(parent={})=>`${esc(parent.title || 'Untitled')}<br>DOI: ${esc(parent.DOI || 'Unavailable')}<br>${esc((parent.creators || []).map(c=>c.name || [c.firstName,c.lastName].filter(Boolean).join(' ')).join('; '))}`;
                        panel.innerHTML=`<h4>Review parent repair</h4><p>${esc(preview.message)}</p><p>PDF: ${esc(row.pdf.name)}</p><dl><dt>Current parent (${esc(preview.command.old_parent)})</dt><dd>${parentDescription(parents[0])}</dd><dt>Proposed parent (${esc(preview.command.desired_parent)})</dt><dd>${parentDescription(parents[1])}</dd></dl><button type="button" data-repair-apply>Confirm displayed parent change</button>`;
                        panel.querySelector('button').onclick=async()=>{
                            if(busy)return;busy=true;
                            try {const applied=await post('repair',{pdf:row.pdf.rel,token:preview.token,confirm:true});say(applied.message);replaceRows(await post('refresh-zotero',{}));}
                            catch(error){say(error.message,true);}finally{busy=false;count();}
                        };
                    } else replaceRows(result);
                }
                catch(error) {say(error.message,true);}
            }}); return;
        }
        if (btn.dataset.curation && !busy) {
            const pdf=active, state=btn.dataset.curation;
            const what = state === 'no_match' ? '“not a paper for Zotero”'
                : state === 'pending' ? '“add to Zotero later” (pending)'
                : 'cleared (back to unmapped)';
            const undoBit = state === 'clear' ? '' : ' Undo: “Clear mapping curation” here, or [clear] on the Not in Zotero tab.';
            confirmInline(btn,{message:`Change mapping curation for ${pdf} to ${what}? Writes the audit’s mapping.json only — identification approval is untouched.${undoBit}`,confirmLabel:'Change curation',onConfirm:async()=>{
                try {const response=await secureFetch('/api/audit/mapping',{method:'POST',body:JSON.stringify({pdf,[state]:true,confirm:true})});const result=await response.json();if(!response.ok)throw new Error(result.error);replaceRows(await post('refresh-zotero',{}));say('Curation saved and PDF workspace refreshed.');}catch(error){say(error.message, true);}
            }});return;
        }
        clickReview(btn.dataset.review, btn);
    });
    // Deck-like single letters — ignore while typing in filter/search/select or
    // while a confirm strip owns the keys (accession/ambiguous deck posture).
    root.addEventListener('keydown', event => {
        if (busy || !opened || isTypingTarget(event.target)) return;
        if (event.target.tagName === 'BUTTON' && (event.key === 'Enter' || event.key === ' ')) return;
        const reviewBtn = (action) => reader.querySelector(`[data-review="${action}"]`);
        switch (event.key) {
            case 'c': case 'C': {
                const b = reviewBtn('confirm');
                if (b && !b.disabled) { clickReview('confirm', b); event.preventDefault(); }
                break;
            }
            case 'w': case 'W': {
                const b = reviewBtn('reject');
                if (b && !b.disabled) { clickReview('reject', b); event.preventDefault(); }
                break;
            }
            case 's': case 'S': {
                const b = reviewBtn('defer');
                if (b) { clickReview('defer', b); event.preventDefault(); }
                break;
            }
            case 'Backspace': {
                const b = reviewBtn('undo');
                if (b) { clickReview('undo', b); event.preventDefault(); }
                break;
            }
            case 'ArrowLeft': next(-1); event.preventDefault(); break;
            case 'ArrowRight': next(1); event.preventDefault(); break;
            default: break;
        }
    });
    list.addEventListener('change', event => {
        if (!event.target.matches('input[type="checkbox"]')) return;
        const pdf = event.target.closest('tr').dataset.pdf;
        if (event.target.checked) selection.add(pdf); else selection.delete(pdf);
        count();
    });
    list.addEventListener('click', event => {
        if (event.target.closest('input, label')) return;
        const row = event.target.closest('tr'); if (row) open(row.dataset.pdf);
    });
    root.querySelector('.pending-filter').oninput = event => {
        const raw = event.target.value;
        clearTimeout(filterTimer);
        filterTimer = setTimeout(() => {
            if (disposed) return;
            filter = foldText(raw);
            table();
        }, FILTER_DEBOUNCE_MS);
    };
    root.querySelector('.pending-batch').addEventListener('click', async event => {
        const action = event.target.closest('button')?.dataset.pending;
        if (!action || busy) return;
        if (action === 'clear') { selection.clear(); table(); return; }
        if (action === 'visible' || action === 'confirmed') {
            visible().filter(r => r.exists && (action === 'visible' || r.review?.export_eligible)).forEach(r=>selection.add(r.pdf.rel)); table(); return;
        }
        if (action === 'authorize') {
            const button = event.target.closest('button');
            busy = true; count();
            try {
                const response = await secureFetch('/api/zotlink/local-status');
                const state = await response.json();
                if (!response.ok || !state.supports_writes) throw new Error(state.error || 'Zotero 10 or later is required.');
                if (disposed) return;
                confirmInline(button, {
                    message:'Ask Zotero to authorize CHERQAiN? In automatic transport mode, this selects Desktop for this session. Allow permits one write request; Always Allow lets CHERQAiN reuse permission until it closes. Each library action still requires confirmation.',
                    confirmLabel:'Authorize',
                    onConfirm:async()=>{
                        if (disposed || busy) return;
                        busy = true; count();
                        try {
                            const response = await secureFetch('/api/zotlink/authorize-local', {method:'POST',body:JSON.stringify({confirm:true,server_id:state.server_id})});
                            const result = await response.json();
                            if (!response.ok) throw new Error(result.error);
                            say('Zotero Desktop authorized. Review and confirm each library action separately. Explicit Web transport settings remain unchanged.');
                        } catch(error) {say(error.message, true);}
                        finally {busy=false;if(!disposed)count();}
                    },
                });
            } catch(error) {say(error.message, true);}
            finally {busy=false;if(!disposed)count();}
            return;
        }
        busy = true; count();
        try {
            if (action === 'export') {
                const result = await post('export', {items:items()});
                if (disposed) return;
                results.innerHTML = `<p>${result.included.length} PDFs included; ${result.dois.length} unique DOIs.</p><ul>${result.included.map(r=>`<li>${esc(r.title || r.pdf)} — ${esc(r.doi)}</li>`).join('')}</ul><p>${result.excluded.map(r=>`${esc(r.pdf)}: ${esc(r.reason)}`).join('<br>')}</p><button type="button" class="btn btn-outline btn-sm pending-copy" ${result.dois.length ? '' : 'disabled'}>Copy these DOIs</button>`;
                results.querySelector('button').onclick = async () => {
                    try {
                        // Recheck at the actual copy action, not only when showing its preview.
                        const fresh = await post('export', {items:items()});
                        if (JSON.stringify(fresh.included) !== JSON.stringify(result.included)) throw new Error('Selection or reviews changed. Prepare the DOI list again.');
                        try { await navigator.clipboard.writeText(fresh.dois.join('\n')); say('DOIs copied. PDFs remain pending until mapped.'); }
                        catch (_) { const text = document.createElement('textarea'); text.readOnly = true; text.value = fresh.dois.join('\n'); text.setAttribute('aria-label','Copy DOIs manually'); results.append(text); text.focus(); text.select(); say('Clipboard unavailable. Copy the selected text manually; selection is preserved.'); }
                    } catch (error) { say(error.message, true); }
                };
            } else if (action === 'reconcile') {
                mappingOffers = await post('reconcile', {items:items()});
                if (disposed) return;
                results.innerHTML = `<p>Bibliography checked ${esc(new Date(mappingOffers.checked_at * 1000).toLocaleString())}. This does not verify Zotero synchronization.</p>` + mappingOffers.rows.map((r,i)=>`<div data-offer="${i}"><p>${esc(r.pdf)} ${esc(r.error || '')}</p>${r.matches?.length ? `<select aria-label="Bibliography entry"><option value="">Choose an entry</option>${r.matches.map(m=>`<option value="${esc(m.citation_key)}">${esc(m.title || m.citation_key)} — ${esc(m.citation_key)}</option>`).join('')}</select><button type="button" class="btn btn-outline btn-sm pending-map">Map this PDF</button>` : r.error ? '' : '<p>No matching bibliography entry. PDF remains pending.</p>'}</div>`).join('');
                results.querySelectorAll('.pending-map').forEach(btn=>{ btn.onclick=()=>{ const offer = btn.closest('[data-offer]'), row = mappingOffers.rows[Number(offer.dataset.offer)], key = offer.querySelector('select').value; if (!key) {say('Choose the target entry.');return;} confirmInline(btn,{message:`Map ${row.pdf} to ${key}? Writes the audit’s mapping.json only — nothing touches the vault or Zotero. Undo: [unmap] on the Inventory tab.`,confirmLabel:'Map PDF',onConfirm: async()=>{try {await post('link',{...row,citation_key:key,bibliography_revision:mappingOffers.bibliography_revision,confirm:true}); if(disposed)return; rows.get(row.pdf).state='mapped'; rows.get(row.pdf).mapped_key=key; table(); offer.innerHTML=`<p>Mapped to ${esc(key)}. Run an audit scan, then use Zotero Attachments → Link vault PDFs (or Attach below).</p><button type="button" class="btn btn-outline btn-sm">Attach this PDF in Zotero…</button> <button type="button" class="audit-link-btn pending-goto-attach">[Zotero Attachments]</button>`; const attachBtn=offer.querySelector('button'); attachBtn.onclick=()=>attach(attachBtn,row.pdf,key); const jump=offer.querySelector('.pending-goto-attach'); if(jump)jump.onclick=()=>{ if(typeof window!=='undefined'&&typeof window.selectAuditReport==='function') window.selectAuditReport('zotero_attachments'); };}catch(error){say(error.message, true);}}});};});
            }
        } catch (error) { say(error.message, true); }
        finally { busy = false; if (!disposed) count(); }
    });
    function attach(btn,pdf,key) {
        confirmInline(btn,{message:`Create a linked-file attachment for ${pdf} under ${key} in Zotero? Requires a refreshed audit inventory and writable Zotero transport. Automatic Desktop undo is unavailable. Review corrections in Zotero.`,confirmLabel:'Attach in Zotero',onConfirm:async()=>{
            if (disposed || busy) return;
            busy = true; count();
            try {
                await consumeSSE('/api/zotlink/attach', {method:'POST',body:JSON.stringify({citation_keys:[key],pdfs:[pdf],review_items:{[pdf]:rows.get(pdf)?.review?.token},confirm:true})}, {
                    onInfo:say,
                    onOther:frame=>{
                        if (!frame.zotlink_attach || disposed) return;
                        const outcome = frame.zotlink_attach;
                        say(`Zotero attachment result: ${outcome.attached || 0} created; ${outcome.recovered || 0} recovered. ${outcome.cancelled ? 'Cancelled; completed mappings remain saved.' : ''} ${outcome.note || ''}`);
                        if (outcome.journal_warning) {
                            const warning = document.createElement('p'); warning.setAttribute('role','alert');
                            warning.textContent = `Recovery record incomplete: ${outcome.journal_warning}. Do not assume Restore is available.`;
                            results.append(warning);
                        }
                        const detail=document.createElement('pre');
                        detail.textContent=JSON.stringify({results:outcome.results || {},failures:outcome.failures || []},null,2);
                        results.append(detail);
                    },
                    onError:error=>{throw new Error(error);},
                });
                if (!disposed) replaceRows(await post('refresh-zotero',{}));
            } catch(error) {say(error.message, true);}
            finally {busy=false;if(!disposed)count();}
        }});
    }
    function replaceRows(result) {
        rows.clear(); result.rows.forEach(r=>rows.set(r.pdf.rel,r));
        data.freshness = result.freshness;
        data.changes = result.changes || [];
        for (const rel of selection) if (!rows.has(rel)) selection.delete(rel);
        table(); freshness();
        if (opened && rows.has(active)) details();
    }
    function freshness() {
        const f = data.freshness || {};
        root.querySelector('.pending-changes div').textContent = (data.changes || []).slice(0,100).map(c=>`${c.attribution || 'Changed in Zotero'} · ${c.identity || ''} · ${c.kind} ${(c.fields || []).join(', ')}`).join('\n') || 'No changes in the latest successful comparison.';
        root.querySelector('.pending-freshness').textContent = f.available === false
            ? `Zotero unavailable: ${f.error || 'Refresh failed'}. Last successful observation retained.`
            : `Zotero observed ${f.checked_at ? new Date(f.checked_at * 1000).toLocaleString() : 'not yet'}.`;
    }
    root.querySelector('.pending-state').onchange = table;
    root.querySelector('.pending-refresh').onclick = async () => {
        if (busy) return;
        busy = true; count(); say('Refreshing Zotero metadata and relationships…');
        try { replaceRows(await post('refresh-zotero',{})); say('Zotero relationships refreshed. No online identification or PDF extraction.'); }
        catch(error) {say(error.message,true);}
        finally {busy=false;count();}
    };
    async function registration(recover=false) {
        const pdf = active, current = generation;
        const host = reader.querySelector('.pending-registration');
        const call = async (url, body) => {
            const response = await secureFetch(url,{method:'POST',body:JSON.stringify(body)});
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || result.message || 'Registration failed');
            return result;
        };
        try {
            const preview = await call('/api/zotcreate/preview',{pdf});
            if (disposed || current !== generation) return;
            if (recover) {
                const result = await call('/api/zotcreate/recover',{pdf,physical:preview.physical,confirm:true});
                say(result.message || result.note); replaceRows(await post('refresh-zotero',{})); return;
            }
            if (preview.can_write === false) { host.textContent = preview.registration_block_reason || 'Authorize Zotero Desktop before registration.'; return; }
            const response = await secureFetch('/api/accession/review/metadata');
            const suggestions = response.ok ? await response.json() : {journals:[],creators:[]};
            if (disposed || current !== generation) return;
            const key = `${data.review_scope}:${pdf}:${preview.pdf_sha256}:${JSON.stringify(preview.review_token)}`;
            const draft = registrationDrafts.get(key) || structuredClone(preview.draft);
            const save = () => registrationDrafts.set(key,draft);
            const allowed = {journalArticle:['title','date','publicationTitle','DOI','url'],book:['title','date','publisher','ISBN','url'],report:['title','date','institution','reportNumber','url'],webpage:['title','date','websiteTitle','url'],preprint:['title','date','repository','DOI','url'],bookSection:['title','bookTitle','date','publisher','pages','ISBN','url'],thesis:['title','date','university','thesisType','url'],conferencePaper:['title','date','proceedingsTitle','conferenceName','pages','DOI','url'],blogPost:['title','date','blogTitle','url'],magazineArticle:['title','date','publicationTitle','volume','issue','pages','url'],newspaperArticle:['title','date','publicationTitle','section','pages','url'],document:['title','date','publisher','url'],manuscript:['title','date','manuscriptType','url']};
            const render = () => {
                host.innerHTML = `<h4>Register ${esc(pdf)}</h4><p>Automatic Desktop undo is unavailable. Review all edits before confirming.</p><label>Item type <select data-type>${preview.item_types.map(t=>`<option ${t===draft.item_type?'selected':''}>${esc(t)}</option>`).join('')}</select></label><div class="registration-fields"></div><datalist id="pending-journals">${suggestions.journals.map(j=>`<option value="${esc(j)}">`).join('')}</datalist><div class="registration-creators"></div><button type="button" data-add-creator>Add creator</button><button type="button" data-create>Register in Zotero</button>`;
                const fieldHost=host.querySelector('.registration-fields');
                (allowed[draft.item_type] || ['title','date','url']).forEach(name=>{
                    const label=document.createElement('label'); label.textContent=name+' ';
                    const input=document.createElement('input'); input.value=draft.fields[name] || ''; input.dataset.field=name;
                    if(name==='publicationTitle') input.setAttribute('list','pending-journals');
                    input.oninput=()=>{draft.fields[name]=input.value;save();}; label.append(input);fieldHost.append(label);
                });
                const creators=host.querySelector('.registration-creators');
                (draft.creators || []).forEach((creator,i)=>{
                    const row=document.createElement('div');
                    row.innerHTML=`<label>Creator suggestion <select data-suggest><option value="">Free entry</option>${suggestions.creators.map((c,n)=>`<option value="${n}">${esc(c.name || [c.lastName,c.firstName].filter(Boolean).join(', '))} (${esc(c.creatorType)})</option>`).join('')}</select></label><label>Role <input data-role value="${esc(creator.creatorType)}"></label><label>Kind <select data-kind><option value="person">Person</option><option value="organisation" ${creator.name !== undefined?'selected':''}>Organisation</option></select></label>${creator.name !== undefined ? `<label>Name <input data-name="name" value="${esc(creator.name)}"></label>` : `<label>Family name <input data-name="lastName" value="${esc(creator.lastName)}"></label><label>Given name <input data-name="firstName" value="${esc(creator.firstName)}"></label>`}<button type="button" data-remove>Remove creator</button>`;
                    row.querySelector('[data-suggest]').onchange=e=>{if(e.target.value==='')return;draft.creators[i]=structuredClone(suggestions.creators[Number(e.target.value)]);save();render();};
                    row.querySelector('[data-role]').oninput=e=>{creator.creatorType=e.target.value;save();};
                    row.querySelector('[data-kind]').onchange=e=>{draft.creators[i]=e.target.value==='organisation'?{creatorType:creator.creatorType,name:creator.lastName || ''}:{creatorType:creator.creatorType,lastName:creator.name || '',firstName:''};save();render();};
                    row.querySelectorAll('[data-name]').forEach(input=>input.oninput=()=>{creator[input.dataset.name]=input.value;save();});
                    row.querySelector('[data-remove]').onclick=()=>{draft.creators.splice(i,1);save();render();};creators.append(row);
                });
                host.querySelector('[data-add-creator]').onclick=()=>{draft.creators.push({creatorType:'author',lastName:'',firstName:''});save();render();};
                host.querySelector('[data-type]').onchange=e=>{draft.item_type=e.target.value;save();render();};
                host.querySelector('[data-create]').onclick=e=>{
                    const fields=Object.fromEntries((allowed[draft.item_type] || ['title','date','url']).filter(k=>draft.fields[k]).map(k=>[k,draft.fields[k]]));
                    const body={pdf,physical:preview.physical,review_token:preview.review_token,target:preview.target,item_type:draft.item_type,fields,creators:structuredClone(draft.creators),confirm:true};
                    confirmInline(e.target,{message:`Create “${fields.title || ''}” in Zotero and attach ${pdf}? Automatic Desktop undo is unavailable.`,confirmLabel:'Register',onConfirm:async()=>{
                        try {const result=await call('/api/zotcreate/create',body);registrationDrafts.delete(key);say(result.note);replaceRows(await post('refresh-zotero',{}));}
                        catch(error){say(error.message,true);}
                    }});
                };
            };
            render();
        } catch(error) {say(error.message,true);}
    }
    freshness();
    table(); if (data.review_error) say(data.review_error, true);
    if (initialPdf && rows.has(initialPdf)) open(initialPdf);
    return () => {
        disposed = true;
        generation += 1;
        clearTimeout(filterTimer);
        controller?.abort();
        closePreview();
    };
}

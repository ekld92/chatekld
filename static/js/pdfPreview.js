/** Shared authenticated preview transport. A superseded fetch cannot own a URL. */
import { secureFetch } from './api.js';

export class PreviewResource {
    constructor() { this.controller = null; this.url = null; this.generation = 0; }
    clear() {
        this.generation += 1;
        this.controller?.abort();
        this.controller = null;
        if (this.url) URL.revokeObjectURL(this.url);
        this.url = null;
    }
    async load(url) {
        this.clear();
        const generation = this.generation;
        const controller = new AbortController();
        this.controller = controller;
        const response = await secureFetch(url, { signal: controller.signal });
        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.error || `Preview failed (${response.status}).`);
        }
        const blob = await response.blob();
        if (generation !== this.generation) return null;
        this.url = URL.createObjectURL(blob);
        return this.url;
    }
}

export function createPagePreview(host, pdf, review) {
    const resource = new PreviewResource();
    let page = 1, scale = 1, disposed = false, generation = 0;
    const bar = document.createElement('div');
    bar.className = 'pending-preview-controls';
    const mkBtn = (label) => {
        const b = document.createElement('button');
        b.type = 'button'; b.className = 'btn btn-outline btn-sm'; b.textContent = label;
        return b;
    };
    const previous = mkBtn('Previous page');
    const next = mkBtn('Next page');
    const number = document.createElement('input');
    number.type = 'number'; number.min = '1'; number.max = String(review.pages);
    number.value = '1'; number.setAttribute('aria-label', 'PDF page');
    const total = document.createElement('span'); total.textContent = `of ${review.pages}`;
    const zoom = document.createElement('select'); zoom.setAttribute('aria-label', 'PDF zoom');
    for (const value of [0.5, 0.75, 1, 1.5, 2, 3]) {
        const option = document.createElement('option'); option.value = String(value);
        option.textContent = `${value * 100}%`; option.selected = value === 1; zoom.append(option);
    }
    const full = mkBtn('Full document');
    bar.append(previous, number, total, next, zoom, full);
    const status = document.createElement('p'); status.setAttribute('role', 'status');
    const canvas = document.createElement('div'); canvas.className = 'pending-page-scroll';
    host.replaceChildren(bar, status, canvas);
    async function render(native = false) {
        const request = ++generation;
        canvas.replaceChildren(); status.textContent = 'Loading PDF…';
        previous.disabled = page === 1; next.disabled = page === review.pages;
        number.value = String(page);
        try {
            const url = await resource.load(native
                ? `/api/audit/pdf?rel=${encodeURIComponent(pdf)}`
                : `/api/accession/review/page?${new URLSearchParams({pdf, sha256: review.sha256, page: String(page), scale: String(scale)})}`);
            if (disposed || request !== generation || !url) return;
            const element = document.createElement(native ? 'iframe' : 'img');
            element.src = url;
            if (native) { element.title = `Full document: ${pdf}`; element.className = 'pending-full-pdf'; }
            else { element.alt = `${pdf}, page ${page}`; element.style.width = `${scale * 100}%`; element.style.maxWidth = 'none'; }
            canvas.replaceChildren(element); status.textContent = native ? 'Full document' : `Page ${page} of ${review.pages}`;
        } catch (error) {
            if (!disposed && request === generation && error.name !== 'AbortError') status.textContent = error.message;
        }
    }
    previous.onclick = () => { page = Math.max(1, page - 1); render(); };
    next.onclick = () => { page = Math.min(review.pages, page + 1); render(); };
    number.onchange = () => { page = Math.min(review.pages, Math.max(1, Number(number.value) || 1)); render(); };
    zoom.onchange = () => { scale = Number(zoom.value); render(); };
    full.onclick = () => render(true);
    render();
    return () => { disposed = true; generation += 1; resource.clear(); host.replaceChildren(); };
}

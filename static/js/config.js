/**
 * Provider / model configuration controller. Owns the provider, chat-model,
 * embedding-model, and OCR/Vision provider+model selectors, and the Ollama
 * model-pull flow. Imports only ui.js + api.js (per the JS module hierarchy);
 * it must NEVER import app.js — that is what the cycle-free `updateProviderBadge`
 * placement in ui.js exists to permit.
 *
 * Online providers (openai/anthropic/google) are chat-only, so when one is
 * active the embedding list is repopulated from a LOCAL provider via
 * /api/vision-models (see _populateEmbeddingsFromLocal). Each online provider
 * remembers its own chat model in a distinct config field
 * (openai_model/anthropic_model/google_model), resolved by _resolveSavedChatModel.
 */
import { secureFetch, consumeSSE } from './api.js';
import { updateProviderBadge, taskBegin, taskEnd, isOnlineProvider, announceStatus, announceError } from './ui.js';

let _activeProvider = 'ollama';
let _configModel = '';
let _configEmbed = '';
let _configOcrProvider = 'ollama';
let _configOcrModel = '';
let _configVisionProvider = 'ollama';
let _configVisionModel = '';
let _configEmbedProvider = 'ollama';
// Item 3.2 (improvement plan 2026-07-04): generation tokens serialising the
// two multi-await flows that used to race. A rapid double provider-switch
// interleaved onProviderChange's 4 async steps, letting switch A's late
// loadModels/save land AFTER B became active — persisting A's model into
// B's config key (the test_03b clobber family, this time client-side). Each
// run captures ++gen and abandons itself after any await if a newer run
// started. Invariant (pinned by tests/js/configPersistence.test.js): only
// the LATEST switch's model list survives into the DOM.
let _providerSwitchGen = 0;
let _loadModelsGen = 0;

const _ONLINE_MODEL_KEYS = {
    openai: 'openai_model',
    anthropic: 'anthropic_model',
    google: 'google_model',
};

/**
 * Fetch /api/config and prime every config-owned control + the module's cached
 * provider/model state. Returns the raw config object so app.js init can pass it
 * on to the other modules' initialisers without a second GET.
 */
export async function loadConfig() {
    const resp = await secureFetch('/api/config');
    const data = await resp.json();
    if (data.provider) {
        const provSelect = document.getElementById('provider-select');
        if (provSelect) provSelect.value = data.provider;
        _activeProvider = data.provider;
        _configModel = _resolveSavedChatModel(data, data.provider);
        _configEmbed = data.embed || '';
        _configEmbedProvider = data.embed_provider || 'ollama';
        _configOcrProvider = data.ocr_provider || 'ollama';
        _configOcrModel = data.ocr_model || '';
        _configVisionProvider = data.vision_provider || 'ollama';
        _configVisionModel = data.vision_model || '';
        const embedProviderSelect = document.getElementById('embed-provider-select');
        if (embedProviderSelect) {
            embedProviderSelect.value = _configEmbedProvider;
            embedProviderSelect.onchange = onEmbedProviderChange;
        }
        const ocrProviderSelect = document.getElementById('ocr-provider-select');
        if (ocrProviderSelect) ocrProviderSelect.value = _configOcrProvider;
        const visionProviderSelect = document.getElementById('vision-provider-select');
        if (visionProviderSelect) visionProviderSelect.value = _configVisionProvider;

        const pullBtn = document.getElementById('pull-model-btn');
        if (pullBtn) pullBtn.style.display = data.provider === 'ollama' ? '' : 'none';

        const vaultDisplay = document.getElementById('vault-path-display');
        if (vaultDisplay && data.obsidian_vault_path) vaultDisplay.textContent = data.obsidian_vault_path;

        return data;
    }
    return data;
}

function _resolveSavedChatModel(config, provider) {
    const key = _ONLINE_MODEL_KEYS[provider];
    if (key && config[key]) return config[key];
    return config.llm || '';
}

/**
 * Persist a provider switch and refresh everything that depends on it: re-reads
 * the saved chat model (each online provider stores its own), updates the badge,
 * toggles the Ollama-only Pull button, broadcasts a `providerChanged` window
 * event for other tabs to react to, then reloads the model lists.
 */
export async function onProviderChange() {
    const providerSelect = document.getElementById('provider-select');
    const newProvider = providerSelect.value;
    // Item 3.2: capture this switch's generation; abandon after any await if
    // a newer switch started (see the token declaration for the clobber this
    // prevents). The trailing saveSelectedModels() is GONE: a provider switch
    // is a load, and loads never save — the new provider's saved model is
    // re-read from config and restored by loadModels; only an explicit user
    // change event persists a model.
    const gen = ++_providerSwitchGen;
    try {
        const saveResp = await secureFetch('/api/config', {
            method: 'POST',
            body: JSON.stringify({ provider: newProvider })
        });
        if (gen !== _providerSwitchGen) return;
        if (saveResp.ok) {
            _activeProvider = newProvider;
            // When switching providers, refresh the saved chat model
            // from /api/config because each online provider remembers
            // its own selection in a distinct field.
            try {
                const cfgResp = await secureFetch('/api/config');
                const cfgData = await cfgResp.json();
                if (gen !== _providerSwitchGen) return;
                _configModel = _resolveSavedChatModel(cfgData, newProvider);
            } catch (_) { /* best-effort */ }
            if (gen !== _providerSwitchGen) return;
            updateProviderBadge(newProvider);
            const pullBtn = document.getElementById('pull-model-btn');
            if (pullBtn) pullBtn.style.display = newProvider === 'ollama' ? '' : 'none';
            window.dispatchEvent(new CustomEvent('providerChanged', { detail: { provider: newProvider } }));
            await loadModels();
        }
    } catch (e) {
        console.error('Provider change failed:', e);
    }
}

async function onEmbedProviderChange() {
    const select = document.getElementById('embed-provider-select');
    if (!select) return;
    _configEmbedProvider = select.value === 'lm_studio' ? 'lm_studio' : 'ollama';
    try {
        await secureFetch('/api/config', {
            method: 'POST',
            body: JSON.stringify({ embed_provider: _configEmbedProvider }),
        });
    } catch (e) {
        console.error('Embed provider save failed:', e);
    }
    // The selection only changes the embedding source while the chat
    // provider is online (local chat providers embed with their own models).
    // Repopulate the embedding-model list from the chosen local provider.
    if (isOnlineProvider(_activeProvider)) {
        const embedSelect = document.getElementById('embed-select');
        if (embedSelect) {
            // Item 3.2: repopulate WITHOUT clearing the saved embed or
            // saving. The old code blanked _configEmbed then persisted the
            // new provider's first listed model — an automatic adoption the
            // user never chose. Restore the saved embed when the new
            // provider still offers it; otherwise the user picks (the
            // select's change handler is the persist path).
            await _populateEmbeddingsFromLocal(embedSelect);
            if (_configEmbed && [...embedSelect.options].some(opt => opt.value === _configEmbed)) {
                embedSelect.value = _configEmbed;
            }
        }
    }
}

/**
 * Populate the chat- and embedding-model selectors from /api/models. For local
 * providers the single returned list is split heuristically into generative vs
 * embedding models (by name); for online providers the list is chat-only and the
 * embedding selector is filled separately from a local provider. Restores the
 * persisted selection when it is still a valid option, then saves.
 */
export async function loadModels() {
    // Item 3.2: latest-wins — a stale concurrent load (rapid provider
    // switches, pull-completion refresh racing boot) must not rebuild the
    // selectors with an outdated list.
    const gen = ++_loadModelsGen;
    const resp = await secureFetch('/api/models');
    const data = await resp.json();
    if (gen !== _loadModelsGen) return;
    const modelSelect = document.getElementById('model-select');
    const embedSelect = document.getElementById('embed-select');
    if (!modelSelect || !embedSelect) return;

    modelSelect.innerHTML = '';
    embedSelect.innerHTML = '';
    const allModels = Array.isArray(data.models) ? data.models : [];
    const onlineProvider = isOnlineProvider(_activeProvider);

    let generativeModels;
    if (onlineProvider) {
        generativeModels = allModels;
    } else {
        generativeModels = allModels.filter(m => typeof m === 'string' && !m.toLowerCase().includes('embed'));
        if (generativeModels.length === 0 && allModels.length > 0) generativeModels = allModels;
    }

    const populate = (select, models, fallback) => {
        if (models.length === 0) {
            const opt = document.createElement('option');
            opt.textContent = fallback;
            opt.disabled = true;
            select.appendChild(opt);
            select.disabled = true;
        } else {
            select.disabled = false;
            models.forEach(m => {
                const opt = document.createElement('option');
                opt.value = m; opt.textContent = m;
                select.appendChild(opt);
            });
        }
    };

    populate(modelSelect, generativeModels, 'No chat models available');

    if (onlineProvider) {
        // Embedding still has to come from a local provider — fetch the
        // embed-provider's models separately rather than displaying the
        // online provider's chat-only list.
        await _populateEmbeddingsFromLocal(embedSelect);
    } else {
        const embeddingModels = allModels.filter(m =>
            typeof m === 'string' &&
            (m.toLowerCase().includes('embed') || m.toLowerCase().includes('nomic') || m.toLowerCase().includes('mxbai'))
        );
        // Item 3.2: the old fallback dumped the WHOLE model list (i.e. chat
        // models) into the embed selector when nothing embed-shaped existed —
        // and the load-time save then PERSISTED a chat model as `embed`,
        // which the indexer would faithfully embed the whole vault with.
        // An honest empty state ("No embedding models available", selector
        // disabled) beats a silently wrong persisted default.
        populate(embedSelect, embeddingModels, 'No embedding models available');
    }

    // Item 3.2 — LOADS NEVER SAVE. This function used to (a) silently ADOPT
    // the first listed option into the in-memory selection whenever the saved
    // model was missing from the list, and (b) unconditionally end with
    // saveSelectedModels() — so booting against a degraded/offline backend
    // (empty or partial model list) REWROTE the user's persisted model with
    // whatever happened to be first. Now: restore the saved selection when
    // present; otherwise the <select> merely DISPLAYS its first option
    // without adopting or persisting anything. The ONLY persist paths are
    // the explicit user change handlers below (and deliberate actions like
    // indexVault that read the live selects). Invariant (pinned by
    // tests/js/configPersistence.test.js): loadModels never POSTs /api/config.
    if (_configModel && [...modelSelect.options].some(opt => opt.value === _configModel)) {
        modelSelect.value = _configModel;
    }
    if (_configEmbed && [...embedSelect.options].some(opt => opt.value === _configEmbed)) {
        embedSelect.value = _configEmbed;
    }

    modelSelect.onchange = saveSelectedModels;
    embedSelect.onchange = saveSelectedModels;
}

async function _populateEmbeddingsFromLocal(embedSelect) {
    // When the chat provider is online, embeddings still have to come
    // from a local provider. /api/vision-models exposes a
    // provider-scoped model list that works for any local provider.
    const provider = _configEmbedProvider === 'lm_studio' ? 'lm_studio' : 'ollama';
    try {
        const resp = await secureFetch(`/api/vision-models?provider=${encodeURIComponent(provider)}&kind=ocr`);
        const data = await resp.json();
        const allModels = Array.isArray(data.models) ? data.models : [];
        const embeddingModels = allModels.filter(m =>
            typeof m === 'string' &&
            (m.toLowerCase().includes('embed') || m.toLowerCase().includes('nomic') || m.toLowerCase().includes('mxbai'))
        );
        // Same fallback kill as loadModels (item 3.2): never offer chat
        // models as embedding candidates — an honest empty state instead.
        embedSelect.innerHTML = '';
        if (embeddingModels.length === 0) {
            const opt = document.createElement('option');
            opt.textContent = `No embedding models on ${provider}`;
            opt.disabled = true;
            embedSelect.appendChild(opt);
            embedSelect.disabled = true;
            return;
        }
        embedSelect.disabled = false;
        embeddingModels.forEach(m => {
            const opt = document.createElement('option');
            opt.value = m; opt.textContent = m;
            embedSelect.appendChild(opt);
        });
    } catch (e) {
        console.error('Failed to load local embedding models:', e);
    }
}

/**
 * Populate both the OCR and Vision provider+model selectors and persist the
 * resolved selections. OCR/Vision settings are independent of the chat provider
 * (see Provider Rules) — they always target a local provider (Ollama/LM Studio).
 */
export async function loadVisionModels() {
    await loadVisionModelSelect('ocr');
    await loadVisionModelSelect('vision');
    await saveVisionModels();
}

async function loadVisionModelSelect(kind) {
    const providerSelect = document.getElementById(`${kind}-provider-select`);
    const modelSelect = document.getElementById(`${kind}-model-select`);
    if (!providerSelect || !modelSelect) return;

    const provider = providerSelect.value || (kind === 'ocr' ? _configOcrProvider : _configVisionProvider);
    const resp = await secureFetch(`/api/vision-models?provider=${encodeURIComponent(provider)}&kind=${encodeURIComponent(kind)}`);
    const data = await resp.json();
    const allModels = Array.isArray(data.models) ? data.models : [];
    let models = allModels;
    if (kind === 'ocr') {
        _configOcrProvider = provider;
        _configOcrModel = data.selected_model || data.ocr_model || _configOcrModel;
    } else {
        _configVisionProvider = provider;
        _configVisionModel = data.selected_model || data.vision_model || _configVisionModel;
    }
    const selected = kind === 'ocr' ? _configOcrModel : _configVisionModel;
    const fallback = kind === 'ocr' ? 'No OCR-capable models listed' : 'No vision-capable models listed';
    populateModelSelect(modelSelect, models, selected, fallback);
    if (!modelSelect.disabled && modelSelect.value) {
        if (kind === 'ocr') _configOcrModel = modelSelect.value;
        else _configVisionModel = modelSelect.value;
    }

    providerSelect.onchange = async () => {
        if (kind === 'ocr') _configOcrProvider = providerSelect.value;
        else _configVisionProvider = providerSelect.value;
        await loadVisionModelSelect(kind);
        await saveVisionModels();
    };
    modelSelect.onchange = saveVisionModels;
}

function populateModelSelect(select, models, selected, fallback) {
    select.innerHTML = '';
    if (!models.length) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = fallback;
        opt.disabled = true;
        select.appendChild(opt);
        select.disabled = true;
        return;
    }
    select.disabled = false;
    for (const model of models) {
        const opt = document.createElement('option');
        opt.value = model;
        opt.textContent = model;
        select.appendChild(opt);
    }
    if (selected && [...select.options].some(opt => opt.value === selected)) {
        select.value = selected;
    }
}

/** The currently-active chat provider (cached; read by vault.js / summarizer.js). */
export function getActiveProvider() { return _activeProvider; }
/** The selected chat model — the live <select> value, falling back to the cache. */
export function getSelectedModel() {
    return document.getElementById('model-select')?.value || _configModel;
}
/** The selected embedding model — the live <select> value, falling back to the cache. */
export function getSelectedEmbed() {
    return document.getElementById('embed-select')?.value || _configEmbed;
}

/**
 * Persist the current chat + embedding model selections to /api/config. Only
 * non-empty fields are sent; /api/config routes `llm` per the Provider Rules
 * (into the active online provider's field, or the local `llm`). No-ops when
 * nothing is selected.
 */
export async function saveSelectedModels() {
    const llm = getSelectedModel();
    const embed = getSelectedEmbed();
    _configModel = llm;
    _configEmbed = embed;
    const payload = {};
    if (llm) payload.llm = llm;
    if (embed) payload.embed = embed;
    if (Object.keys(payload).length === 0) return;
    await secureFetch('/api/config', {
        method: 'POST',
        body: JSON.stringify(payload)
    });
}

/** Persist the OCR + Vision provider/model selections to /api/config. */
export async function saveVisionModels() {
    const ocrProvider = document.getElementById('ocr-provider-select')?.value || _configOcrProvider;
    const ocr = document.getElementById('ocr-model-select')?.value || _configOcrModel;
    const visionProvider = document.getElementById('vision-provider-select')?.value || _configVisionProvider;
    const vision = document.getElementById('vision-model-select')?.value || _configVisionModel;
    const payload = {};
    if (ocrProvider) {
        payload.ocr_provider = ocrProvider;
        _configOcrProvider = ocrProvider;
    }
    if (ocr) {
        payload.ocr_model = ocr;
        _configOcrModel = ocr;
    }
    if (visionProvider) {
        payload.vision_provider = visionProvider;
        _configVisionProvider = visionProvider;
    }
    if (vision) {
        payload.vision_model = vision;
        _configVisionModel = vision;
    }
    if (Object.keys(payload).length === 0) return;
    await secureFetch('/api/config', {
        method: 'POST',
        body: JSON.stringify(payload)
    });
}

/**
 * Pull an Ollama model (Ollama-only feature). Streams /api/pull progress via SSE,
 * driving a determinate progress bar from the `completed`/`total` byte counts,
 * then reloads the model list so the new model is selectable.
 */
export async function pullModel() {
    const model = document.getElementById('pull-model-input').value.trim();
    if (!model) return;
    
    const btn = document.getElementById('pull-btn');
    btn.disabled = true;
    const statusEl = document.getElementById('pull-status');
    const progressFill = document.getElementById('pull-progress-fill');
    const progressWrap = document.getElementById('pull-progress-wrap');
    progressFill.style.width = '0%';
    if (progressWrap) {
        progressWrap.style.display = 'block';
        progressWrap.setAttribute('aria-valuenow', '0');
    }

    taskBegin('pull-model', `Pulling ${model}\u2026`);

    try {
        await consumeSSE('/api/pull', {
            method: 'POST',
            body: JSON.stringify({model})
        }, {
            onInfo: () => {},
            onToken: () => {},
            onOther: (d) => {
                if (d.status) statusEl.textContent = d.status;
                if (d.total && d.completed) {
                    const pct = Math.round((d.completed / d.total) * 100);
                    progressFill.style.width = pct + '%';
                    if (progressWrap) progressWrap.setAttribute('aria-valuenow', String(pct));
                }
            },
            onError: (err) => { throw new Error(err); }
        });

        statusEl.textContent = 'Done!';
        // Track 6b: TERMINAL-only announcements. #pull-status is deliberately
        // NOT a live region — the per-chunk progress writes above would spam
        // a screen reader on every download chunk.
        announceStatus('Model download complete.');
        await loadModels();
    } catch (e) {
        statusEl.textContent = 'Error: ' + e.message;
        announceError('Model download failed: ' + e.message);
    } finally {
        btn.disabled = false;
        taskEnd('pull-model');
        // Hide the bar so a stale full/partial fill isn't shown next open.
        if (progressWrap) progressWrap.style.display = 'none';
    }
}

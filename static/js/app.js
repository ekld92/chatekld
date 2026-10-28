/**
 * Application root — the ROOT of the JS module hierarchy: it imports from every
 * other module (which in turn import only ui.js + api.js), so this is the single
 * place the dependency graph converges and nothing imports from here. Wires the
 * DOMContentLoaded bootstrap: loads config once and fans it out to each feature
 * module's initialiser, then publishes the inline-handler entry points the HTML
 * references onto `window` (e.g. window.chatPlain, window.historyOpenRestore).
 */
import * as UI from './ui.js';
import { initStudio, initIndexWorkspace } from './studio.js';
import * as Config from './config.js';
import * as Vault from './vault.js';
import * as Summarizer from './summarizer.js';
import * as Audit from './audit.js';
import * as Deck from './deck.js';
import * as Docgen from './docgen.js';
import * as History from './history.js';
import * as Settings from './settings.js';
import * as PlainChat from './plainchat.js';
import * as Prompts from './prompts.js';
import * as Help from './help.js';
import * as Tagging from './tagging.js';
import { secureFetch } from './api.js';
import { updateProviderBadge } from './ui.js';

// --- Initialization ---

/**
 * One-shot bootstrap on DOMContentLoaded. Loads /api/config ONCE and threads the
 * result into each module's initialiser (so they don't each re-fetch it), binds
 * the `window.*` inline-handler entry points the templates call, wires tablist
 * keyboard nav, and starts the 15 s runtime-status poll. A failure in the init
 * block is logged but non-fatal — event binding below still runs.
 */
async function init() {
    console.log('[CHERQAiN] Initializing...');
    
    // Item 3.8 (improvement plan 2026-07-04): each initialiser is isolated.
    // The old single try/catch meant ONE rejection (say, /api/models timing
    // out) silently skipped every later step — no settings wiring, no audit
    // tab, no deck init — for a failure unrelated to them. Order preserved;
    // a failed step logs its own name and the rest still run.
    const step = async (name, fn) => {
        try { await fn(); } catch (e) { console.error(`Init step failed: ${name}`, e); }
    };
    let config = {};
    await step('loadConfig', async () => {
        config = await Config.loadConfig() || {};
        updateProviderBadge(config.provider || 'ollama');
    });
    await step('loadModels', () => Config.loadModels());
    await step('loadVisionModels', () => Config.loadVisionModels());
    await step('renderExclusions', () => Vault.renderExclusions());
    await step('onlineVisualSettings', () => Vault.applyOnlineVisualSettings(config));
    await step('vaultChatParams', () => {
        Vault.applyVaultChatParams(config);
        Vault.wireVaultChatParamControls();
    });
    await step('initSettings', () => Settings.initSettings(config));
    await step('refreshIndexState', () => Vault.refreshIndexState());
    await step('loadReportTypes', () => Summarizer.loadReportTypes());
    await step('initAuditTab', () => Audit.initAuditTab());
    await step('initDeck', () => Deck.initDeck(config));
    await step('initDocgen', () => Docgen.initDocgen());
    await step('initMaintenance', async () => {
        // Private-only subsystem probe (Maintenance, STRIP STEP 4): on the
        // public mirror the blueprint and static/js/maintenance.js are both
        // stripped, this GET 404s, no import is attempted and the hidden tab
        // stays hidden. The import MUST stay dynamic — a static import of
        // maintenance.js would break the whole module graph where the file
        // is absent (moduleHierarchy.test.js pins exactly this edge).
        let maintenanceModulePromise = null;
        const importMaintenance = () => {
            if (!maintenanceModulePromise) {
                maintenanceModulePromise = import('./maintenance.js');
            }
            return maintenanceModulePromise;
        };
        const loadMaintenance = async () => {
            let res;
            try {
                res = await secureFetch('/api/maintenance/steps');
            } catch (error) {
                // On a private build the dynamic module exists and can expose
                // a retryable visible boundary. On a public artifact it is
                // intentionally absent; swallowing only that optional UI path
                // keeps the capability hidden without masking app bootstrap.
                try {
                    const mod = await importMaintenance();
                    mod.initMaintenanceError(
                        'Maintenance registry request failed.', loadMaintenance);
                } catch (_) { /* public artifact: capability intentionally absent */ }
                return;
            }
            if (!res || res.status === 404) return;
            const mod = await importMaintenance();
            if (!res.ok) {
                mod.initMaintenanceError(
                    `Maintenance registry failed (HTTP ${res.status}).`,
                    loadMaintenance);
                return;
            }
            const data = await res.json().catch(() => null);
            if (!data || !Array.isArray(data.steps)) {
                mod.initMaintenanceError(
                    'Maintenance registry returned an invalid response.',
                    loadMaintenance);
                return;
            }
            await mod.initMaintenance(data);
        };
        await loadMaintenance();
    });

    document.addEventListener('chatekld:navigate', event => {
        if (event.detail.tabId !== 'maintenance') return;
        event.preventDefault(); UI.showTab('audit'); Audit.selectAuditReport('library_maintenance');
    });
    // Bind events
    window.onProviderChange = Config.onProviderChange;
    window.pullModel = Config.pullModel;
    window.cancelPull = Config.cancelPull;
    window.historyOpenRestore = History.openRestore;
    window.historyRevertAll = History.revertAll;
    window.showTab = UI.showTab;
    // Single help source (2026-08-05): the sidebar "How to Use" button routes
    // to the Help tab (guide.md) — the old modal duplicate guide is gone.
    // showTab alone would not fire the guide's lazy loader (click-bound to
    // #tab-help), hence the explicit pair. tests/js/helpButton.test.js pins.
    window.openHowTo = () => { UI.showTab('help'); Help.loadHelpTab(); };
    window.openModal = UI.openModal;
    window.closeModal = UI.closeModal;
    window.pickVaultFolder = Vault.pickVaultFolder;
    window.previewOnlineOcr = Vault.previewOnlineOcr;
    window.chooseOnlineSources = Vault.chooseOnlineSources;
    window.refreshOnlineHistory = Vault.refreshOnlineHistory;
    window.onlineOcrProviderChanged = Vault.onlineOcrProviderChanged;
    window.onlineVisionProviderChanged = Vault.onlineVisionProviderChanged;
    window.startOnlineOcr = Vault.startOnlineOcr;
    window.cancelOnlineOcr = Vault.cancelOnlineOcr;
    window.promoteOnlineOcr = Vault.promoteOnlineOcr;
    window.indexVault = Vault.previewLocalIndex;
    window.checkExtractionMemory = Vault.checkExtractionMemory;
    window.advancedLocalIndex = Vault.advancedLocalIndex;
    document.addEventListener('library-extraction-history', () => Vault.refreshOnlineHistory());
    document.addEventListener('library-extraction-refresh', () => Vault.pollOnlineOcr());
    document.addEventListener('library-index-preview', event => {
        const pending = Vault.previewLocalIndex(event.detail?.anchor, event.detail?.prepareOnly === true, event.detail?.onPreview, event.detail?.onlineChoice ?? null, event.detail?.initiatingAction);
        event.detail?.waitUntil?.(pending);
    });
    window.advancedOnlineIndex = Vault.advancedOnlineIndex;
    window.pauseVaultIndex = Vault.pauseVaultIndex;
    window.resumeVaultIndex = Vault.resumeVaultIndex;
    window.cancelVaultIndex = Vault.cancelVaultIndex;
    window.chatWithVault = Vault.chatWithVault;
    window.clearVaultChat = Vault.clearVaultChat;
    window.stopVaultChat = Vault.stopChat;
    window.exportVaultConversation = Vault.exportVaultConversation;
    window.addExclusion = Vault.addExclusion;
    window.removeExclusion = Vault.removeExclusion;
    window.toggleVaultMaterials = Vault.toggleVaultMaterials;
    window.refreshVaultMaterials = Vault.refreshVaultMaterials;
    window.openImageExtsModal = Vault.openImageExtsModal;
    window.saveImageExts = Vault.saveImageExts;
    window.uploadPDF = Summarizer.uploadPDF;
    Summarizer.wireUploadDropzone();
    window.summarisePDF = Summarizer.summarisePDF;
    window.exportSummary = Summarizer.exportSummary;
    window.copySummary = Summarizer.copySummary;
    window.resetUpload = Summarizer.resetUpload;
    window.resetAppData = resetAppData;
    window.runAuditScan = Audit.runAuditScan;
    window.cancelAuditScan = Audit.cancelAuditScan;
    window.refreshWidgetSnapshot = Audit.refreshWidgetSnapshot;
    window.selectAuditReport = Audit.selectAuditReport;
    window.saveAuditSettings = Audit.saveAuditSettings;
    // audit.js delegates the Tagging sub-view body render to this global
    // (a call, not an import — keeps audit.js -> tagging.js out of the graph).
    window.taggingRenderInto = Tagging.taggingRenderInto;
    window.deckLoadTemplate = Deck.loadTemplate;
    window.deckPickTemplateFile = Deck.pickTemplateFile;
    window.deckPickOutDir = Deck.pickOutDir;
    window.deckGenerate = Deck.generate;
    window.deckPickAugmentDeck = Deck.pickAugmentDeck;
    window.deckLoadDeckSections = Deck.loadDeckSections;
    window.deckAugScopeChanged = Deck.augScopeChanged;
    window.deckAugmentPreview = Deck.augmentPreview;
    window.deckAugPreviewPdf = Deck.augPreviewPdf;
    window.docgenLoadTemplate = Docgen.loadTemplate;
    window.docgenPickTemplateFile = Docgen.pickTemplateFile;
    window.docgenPickOutDir = Docgen.pickOutDir;
    window.docgenGenerate = Docgen.generate;
    window.docgenGenerateMarkdown = Docgen.generateMarkdown;
    window.docgenPickAugmentDoc = Docgen.pickAugmentDoc;
    window.docgenLoadDocSections = Docgen.loadDocSections;
    window.docgenAugScopeChanged = Docgen.augScopeChanged;
    window.docgenAugmentPreview = Docgen.augmentPreview;
    window.chatPlain = PlainChat.chatPlain;
    window.stopPlainChat = PlainChat.stopChat;
    window.plainchatNew = PlainChat.newChat;
    window.exportPlainConversation = PlainChat.exportPlainConversation;
    // Plain Chat attachments: Attach button, hidden file input, drag-and-drop.
    // Wired here with addEventListener rather than as inline onclick attributes
    // — the CSP already carries 111 hashes under 'unsafe-hashes', and each one
    // authorises that exact handler string on ANY element.
    PlainChat.initAttachments();
    window.promptsRefresh = Prompts.loadPrompts;
    window.setTheme = UI.setTheme;

    // Lazily load the Prompt Hub when its tab is first shown (and refresh on
    // every re-visit) so it always reflects the latest captures without polling.
    // The inline onclick already calls showTab('prompts'); this adds the fetch.
    const promptsTabBtn = document.getElementById('tab-prompts');
    if (promptsTabBtn) promptsTabBtn.addEventListener('click', () => Prompts.loadPrompts());

    // Live theme machinery (OS-change tracking + radiogroup keys). The resolved
    // theme is already painted by the index.html bootstrap; this syncs the
    // control and keeps System mode following the OS.
    UI.initTheme();

    // No in-app navigation, ever: every anchor click is intercepted and routed
    // through POST /api/open (http/https → system browser, vault path → Finder
    // reveal, everything else swallowed). A raw click on a relative link the
    // LLM emitted used to navigate the whole PyWebView window to a 404.
    UI.installLinkInterceptor();
    // Installed app-wide, not per panel: an unhandled file drop navigates the
    // PyWebView window to the file and replaces the app with a PDF viewer. The
    // real drop zones preventDefault their own events first, so this only ever
    // sees a drop that missed them.
    UI.installGlobalDropGuard();

    // Arrow-key navigation for the ARIA tablists.
    UI.wireTablistKeys(
        document.querySelector('.tabs'),
        (tab) => {
            const id = tab.id.replace('tab-', '');
            UI.showTab(id);
            // The Help guide's lazy loader is click-bound below — mirror it
            // here so arrow-key activation doesn't land on an empty guide.
            if (id === 'help') Help.loadHelpTab();
        },
    );
    UI.wireTablistKeys(
        document.getElementById('audit-report-tabs'),
        (tab) => Audit.selectAuditReport(tab.dataset.report),
    );

    initStudio();
    initIndexWorkspace();

    // Click-to-insert example prompts (French, tuned to the maintainer's
    // psychiatry / clinical-research vault).
    wireExamplePrompts();

    // Contextual help: "?" badges on settings (hover tooltip) and actions
    // (click popover), keyed by existing control/button ids. Idempotent and
    // tolerant of ids not in the current DOM.
    Help.installHelp();

    // Every `.control-hint` becomes the aria-describedby of the control above
    // it (2026-07-26 UX audit): the template had 71 hints and 2
    // aria-describedby, so a screen-reader user got the control's NAME and none
    // of its operative instructions. One central pass — see
    // ui.js::wireControlHints for the association rule and why it is not 71
    // hand-edits. Runs after installHelp so a "?" badge inserted next to a
    // label is never mistaken for the described control.
    UI.wireControlHints();

    // Help tab (Batch H): the guide loads lazily on first activation; the
    // guided tour starts from its button. ⌘K opens the command palette.
    const helpTabBtn = document.getElementById('tab-help');
    if (helpTabBtn) helpTabBtn.addEventListener('click', () => Help.loadHelpTab());
    const tourBtn = document.getElementById('help-tour-btn');
    if (tourBtn) tourBtn.addEventListener('click', () => Help.startTour());
    UI.installCommandPalette(_paletteActions());

    // Remember the last value typed into the main free-text inputs across a
    // full app quit (axis v — "keep my last prompt"). Client-side localStorage,
    // per field; only fills an empty field so config-sourced values still win.
    UI.installDraftPersistence([
        { id: 'deck-topic' },
        { id: 'deck-title' },
        { id: 'deck-subtitle' },
        { id: 'deck-instructions' },
        { id: 'deck-audience' },
        { id: 'docgen-topic' },
        { id: 'docgen-title' },
        { id: 'docgen-subtitle' },
        { id: 'docgen-instructions' },
        { id: 'docgen-audience' },
        { id: 'doc-focus-question' },
        { id: 'plainchat-input' },
    ]);

    // System status loop
    fetchRuntimeStatus();
    setInterval(fetchRuntimeStatus, 15000);
}

// Static example-prompt chips. Each entry's `text` fills the field on click;
// `label` is the short chip caption.
function wireExamplePrompts() {
    const deckEl = document.getElementById('deck-instructions');
    if (deckEl) {
        UI.renderExampleChips(deckEl, [
            { label: 'Graduate seminar',
              text: 'Audience: graduate seminar. Emphasize research methodology, study design, and key limitations. Maximum 4 bullet points per slide.' },
            { label: '15-min overview',
              text: '15-minute research briefing. Structure: background, methodology, key findings, and practical implications. Stay strictly grounded in the vault notes.' },
            { label: 'Synthesis + citations',
              text: 'Pedagogical tone. Add a synthesis slide at the end of each section and cite key sources (author, year).' },
            { label: 'Multilingual sources',
              text: 'Draw on vault notes across all available languages, synthesize the evidence in English, and cite key primary sources.' },
        ], { title: 'Examples:' });
    }

    // Deck AUGMENT instruction. Distilled from the maintainer's hand-written
    // "Comprehensive LaTeX Beamer Audit" prompt: the workflow scaffolding it
    // carried ("act as an expert", "ask for confirmation", "run make", "report
    // in a table") is all provided by the app already — augment previews, the
    // confirm gate, Compile & Auto-Fix — so only the task itself is kept.
    const deckAugEl = document.getElementById('deck-aug-instruction');
    if (deckAugEl) {
        UI.renderExampleChips(deckAugEl, [
            { label: 'Split dense slides',
              text: 'Split overly dense slides across multiple frames with continued titles (1/2, 2/2). Preserve all content, ordering, and citations.' },
            { label: 'Clean up artifacts',
              text: 'Remove unfilled placeholders, empty sections without frames, and blank bullet points. Leave commented-out code and template comments intact.' },
            { label: 'LaTeX typography',
              text: 'Fix typography: escape special characters (% & _ # $), replace \\footnotesize{...} with {\\footnotesize ...}, and remove bare URLs.' },
        ], { title: 'Examples:' });
    }

    const vaultSysEl = document.getElementById('vault-system-prompt');
    if (vaultSysEl) {
        UI.renderExampleChips(vaultSysEl, [
            { label: 'Concise & analytical',
              text: 'Respond concisely with an analytical academic tone. Cite retrieved notes and papers directly and preserve domain terminology.' },
            { label: 'Evidence vs interpretation',
              text: 'Clearly distinguish between findings directly supported by retrieved sources and broader methodological interpretation or commentary.' },
            { label: 'Structured synthesis',
              text: 'Structure responses into: background/definitions, methodology and mechanisms, and key findings or limitations.' },
            { label: 'Cross-lingual sources',
              text: 'Synthesize evidence across all languages present in the vault (e.g., English, French, German) and cite non-English passages on equal footing.' },
        ], { title: 'Examples:' });
    }

    const plainEl = document.getElementById('plainchat-input');
    const plainMount = document.getElementById('plainchat-examples');
    if (plainEl && plainMount) {
        UI.renderExampleChips(plainEl, [
            { label: 'Compare methodologies',
              text: 'Compare randomized controlled trials and observational cohort studies for causal inference, using a structured comparison table.' },
            { label: 'Discussion paragraph',
              text: 'Draft a discussion paragraph examining study limitations, generalizability, and future research directions for a systematic review.' },
            { label: 'Academic translation',
              text: 'Translate the following passage into clear academic English while preserving technical terminology: ' },
        ], { mountEl: plainMount, title: 'Examples:' });
    }
}

// Item 3.3: 15 s interval + no reentrancy guard meant a slow /api/status or
// /api/health response could land AFTER a newer tick's, repainting the dots
// and warning banner with stale state. Latest-wins: only the newest
// invocation may write the DOM.
const _runtimeStatusGate = UI.makeLatestGate();

/** Flip every status dot to `state` with an "unreachable" label (E2 helper). */
function _setAllStatusDots(state, label) {
    for (const id of ['rs-provider-dot', 'rs-vector-dot', 'rs-local-dot']) {
        const el = document.getElementById(id);
        if (el) {
            el.style.background = '';
            el.dataset.state = state;
            el.setAttribute('aria-label', label);
        }
    }
}

async function fetchRuntimeStatus() {
    const isCurrent = _runtimeStatusGate.enter();
    let providerOk = false;
    let providerError = null;
    let providerWarnings = [];
    // E2 (2026-07-22 audit): when the Flask process is GONE both fetches throw
    // — the old catches only console.error'd, so the dots froze at their last
    // (likely green) state and the empty-warnings fall-through below actively
    // HID the banner. The UI asserted health exactly when everything was dead.
    let serverUnreachable = false;
    let serverBusy = false;

    try {
        const res = await secureFetch('/api/status');
        const d = await res.json();
        if (!isCurrent()) return;
        providerOk = !!d.ok;
        providerError = d.error;
        providerWarnings = Array.isArray(d.warnings) ? d.warnings : [];

        const dotEl = document.getElementById('rs-provider-dot');
        if (dotEl) {
            // Track 6a: the state rides data-state so CSS gives each state a
            // distinct SHAPE as well as its theme-token color (color-only
            // signalling excluded color-blind users). An inline background
            // would override the [data-state] rules, so clear any legacy one.
            dotEl.style.background = '';
            dotEl.dataset.state = providerOk ? 'ok' : 'error';
            dotEl.setAttribute('aria-label', providerOk ? 'Provider online' : 'Provider offline');
        }
    } catch (e) {
        console.error('Status check failed:', e);
        // Only a failed fetch (TypeError) means the server is gone. A reply
        // that is slow, non-JSON or a 5xx means it is up but busy — a scan
        // or an index run — and reporting "unreachable" then was false
        // (2026-09-19 UI review).
        if (e instanceof TypeError) serverUnreachable = true; else serverBusy = true;
    }

    let healthDetails = null;
    try {
        const res = await secureFetch('/api/health');
        const d = await res.json();
        if (!isCurrent()) return;
        healthDetails = d.details;

        const vecDot = document.getElementById('rs-vector-dot');
        if (vecDot && healthDetails && healthDetails.vector_store) {
            const vs = healthDetails.vector_store;
            vecDot.style.background = '';
            vecDot.dataset.state = vs.status === 'ok' ? 'ok' : (vs.status === 'degraded' ? 'degraded' : 'error');
            vecDot.setAttribute('aria-label', `Vector store ${vs.status}: ${vs.error || 'healthy'}`);
        }

        const locDot = document.getElementById('rs-local-dot');
        if (locDot && healthDetails && healthDetails.local_model) {
            const lm = healthDetails.local_model;
            locDot.style.background = '';
            locDot.dataset.state = lm.status === 'ok' ? 'ok' : (lm.status === 'degraded' ? 'degraded' : 'error');
            locDot.setAttribute('aria-label', `Local models ${lm.status}: ${lm.error || 'healthy'}`);
        }
    } catch (e) {
        console.error('Health check failed:', e);
        if (e instanceof TypeError) serverUnreachable = true; else serverBusy = true;
    }

    if (!isCurrent()) return;
    if (serverUnreachable) {
        _setAllStatusDots('error', 'App server unreachable');
    }
    const warningEl = document.getElementById('runtime-warning');
    if (warningEl) {
        const warnings = [];
        if (serverUnreachable) {
            warnings.push('App server unreachable — the backend process may '
                + 'have stopped. Actions will fail until it is back; try '
                + 'relaunching the app.');
        } else if (serverBusy) {
            warnings.push('App server busy — it answered, but not normally '
                + '(a scan or an index run may be in progress). Status will '
                + 'refresh on its own.');
        }
        if (providerError) warnings.push(providerError);
        warnings.push(...providerWarnings);

        if (healthDetails) {
            if (healthDetails.vector_store && healthDetails.vector_store.error) {
                warnings.push(`Vector Store: ${healthDetails.vector_store.error}`);
            }
            if (healthDetails.local_model && healthDetails.local_model.error) {
                warnings.push(`Local Models: ${healthDetails.local_model.error}`);
            }
        }

        warningEl.textContent = warnings.filter(Boolean).join(' | ');
        warningEl.style.display = warnings.length ? 'block' : 'none';
    }
}

/** G3 (2026-07-22): arm the tab-strip's "more tabs off-screen" fade from the
 * MEASURED overflow, not a width guess — a large system font overflows the 8
 * tabs above the old 1024px media query with no cue. */
function _updateTabsOverflow() {
    const tabs = document.querySelector('.tabs');
    if (!tabs) return;
    tabs.classList.toggle('tabs-overflowing', tabs.scrollWidth > tabs.clientWidth + 1);
}

document.addEventListener('DOMContentLoaded', () => {
    _updateTabsOverflow();
    window.addEventListener('resize', _updateTabsOverflow);
});

document.addEventListener('DOMContentLoaded', init);

async function resetAppData() {
    // C1/#19 (2026-07-13): disable the button for the flight (no double-fire),
    // and surface a failed reset instead of leaving the modal silently open.
    const btn = document.getElementById('reset-confirm-btn');
    const status = document.getElementById('reset-status');
    if (btn) btn.disabled = true;
    if (status) { status.style.display = 'none'; status.textContent = ''; }
    try {
        const resp = await secureFetch('/api/reset', {
            method: 'POST',
            body: JSON.stringify({ confirm: 'reset' })
        });
        if (resp.ok) {
            UI.closeModal('reset-modal');
            window.location.reload();
            return;
        }
        let msg = `Reset failed (HTTP ${resp.status}).`;
        try { const d = await resp.json(); if (d && d.error) msg = d.error; } catch (_) { /* ignore */ }
        if (status) { status.textContent = msg; status.style.display = 'block'; }
        if (btn) btn.disabled = false;
    } catch (e) {
        console.error('Reset failed:', e);
        if (status) { status.textContent = 'Reset failed (see console).'; status.style.display = 'block'; }
        if (btn) btn.disabled = false;
    }
}

// Global error handlers
window.addEventListener('unhandledrejection', (event) => {
    console.error('[CHERQAiN] Unhandled rejection:', event.reason);
});

/**
 * Command-palette action registry (Batch H, 2026-07-22). Navigation + safe,
 * non-destructive actions only — anything that writes keeps its own confirmed
 * button. app.js owns the registry (it may import every feature module);
 * ui.js just renders and runs it.
 */
function _paletteActions() {
    const tab = (id, label) => ({
        label: `Go to ${label}`, hint: 'view',
        run: () => UI.showTab(id),
    });
    return [
        tab('obsidian', 'Ask · vault questions'),
        tab('document', 'Read · single paper'),
        tab('audit', 'Library · update search'),
        tab('deck', 'Create · Studio'),
        tab('docgen', 'Studio · Report / Article'),
        tab('chat', 'Plain Chat'),
        tab('prompts', 'Prompt Hub'),
        {
            label: 'Go to Help', hint: 'view',
            run: () => { UI.showTab('help'); Help.loadHelpTab(); },
        },
        {
            label: 'Open Settings', hint: 'modal',
            run: () => UI.openModal('settings-modal'),
        },
        {
            label: 'Open History and Restore', hint: 'modal',
            run: () => History.openRestore(),
        },
        {
            label: 'Start the guided tour', hint: 'help',
            run: () => Help.startTour(),
        },
        {
            label: 'New Plain Chat conversation', hint: 'chat',
            run: () => { UI.showTab('chat'); window.plainchatNew && window.plainchatNew(); },
        },
    ];
}

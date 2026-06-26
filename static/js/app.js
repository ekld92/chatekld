/**
 * Application root — the ROOT of the JS module hierarchy: it imports from every
 * other module (which in turn import only ui.js + api.js), so this is the single
 * place the dependency graph converges and nothing imports from here. Wires the
 * DOMContentLoaded bootstrap: loads config once and fans it out to each feature
 * module's initialiser, then publishes the inline-handler entry points the HTML
 * references onto `window` (e.g. window.chatPlain, window.refactorRunPlan).
 */
import * as UI from './ui.js';
import * as Config from './config.js';
import * as Vault from './vault.js';
import * as Summarizer from './summarizer.js';
import * as Audit from './audit.js';
import * as Deck from './deck.js';
import * as Refactor from './refactor.js';
import * as Settings from './settings.js';
import * as PlainChat from './plainchat.js';
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
    console.log('[ChatEKLD] Initializing...');
    
    // Load initial data
    try {
        const config = await Config.loadConfig();
        updateProviderBadge(config.provider || 'ollama');
        await Config.loadModels();
        await Config.loadVisionModels();
        await Vault.renderExclusions();
        Vault.applyVaultChatParams(config);
        Vault.wireVaultChatParamControls();
        Settings.initSettings(config);
        await Vault.refreshIndexState();
        await Summarizer.loadReportTypes();
        await Audit.initAuditTab();
        Refactor.initRefactorTab(config);
    } catch (e) {
        console.error('Init config failed:', e);
    }

    // Bind events
    window.onProviderChange = Config.onProviderChange;
    window.pullModel = Config.pullModel;
    window.showTab = UI.showTab;
    window.openModal = UI.openModal;
    window.closeModal = UI.closeModal;
    window.pickVaultFolder = Vault.pickVaultFolder;
    window.indexVault = Vault.indexVault;
    window.pauseVaultIndex = Vault.pauseVaultIndex;
    window.resumeVaultIndex = Vault.resumeVaultIndex;
    window.cancelVaultIndex = Vault.cancelVaultIndex;
    window.chatWithVault = Vault.chatWithVault;
    window.clearVaultChat = Vault.clearVaultChat;
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
    window.resetUpload = Summarizer.resetUpload;
    window.resetAppData = resetAppData;
    window.runAuditScan = Audit.runAuditScan;
    window.cancelAuditScan = Audit.cancelAuditScan;
    window.selectAuditReport = Audit.selectAuditReport;
    window.saveAuditSettings = Audit.saveAuditSettings;
    window.deckLoadTemplate = Deck.loadTemplate;
    window.deckPickTemplateFile = Deck.pickTemplateFile;
    window.deckPickOutDir = Deck.pickOutDir;
    window.deckGenerate = Deck.generate;
    window.refactorRunPlan = Refactor.runPlan;
    window.refactorPickScopeFolder = Refactor.pickScopeFolder;
    window.refactorOpenApply = Refactor.openApply;
    window.refactorOpenRestore = Refactor.openRestore;
    window.refactorConfirmApply = Refactor.confirmApply;
    window.refactorRevertAll = Refactor.revertAll;
    window.chatPlain = PlainChat.chatPlain;
    window.plainchatNew = PlainChat.newChat;

    // Arrow-key navigation for the ARIA tablists.
    UI.wireTablistKeys(
        document.querySelector('.tabs'),
        (tab) => UI.showTab(tab.id.replace('tab-', '')),
    );
    UI.wireTablistKeys(
        document.getElementById('audit-report-tabs'),
        (tab) => Audit.selectAuditReport(tab.dataset.report),
    );

    // System status loop
    fetchRuntimeStatus();
    setInterval(fetchRuntimeStatus, 15000);
}

async function fetchRuntimeStatus() {
    try {
        const res = await secureFetch('/api/status');
        const d = await res.json();
        
        const dotEl = document.getElementById('rs-provider-dot');
        if (dotEl) {
            dotEl.style.background = d.ok ? '#34c759' : 'var(--danger)';
            // Non-color signal: state isn't conveyed by hue alone.
            dotEl.setAttribute('aria-label', d.ok ? 'Provider online' : 'Provider offline');
        }
        
        const labelEl = document.getElementById('provider-badge-label');
        if (labelEl && d.error) {
            labelEl.title = d.error; // Show error on hover
        }
        const warningEl = document.getElementById('runtime-warning');
        if (warningEl) {
            const warnings = [];
            if (d.error) warnings.push(d.error);
            if (Array.isArray(d.warnings)) warnings.push(...d.warnings);
            warningEl.textContent = warnings.filter(Boolean).join(' ');
            warningEl.style.display = warnings.length ? 'block' : 'none';
        }
    } catch (e) {
        console.error('Status check failed:', e);
    }
}

document.addEventListener('DOMContentLoaded', init);

async function resetAppData() {
    try {
        const resp = await secureFetch('/api/reset', {
            method: 'POST',
            body: JSON.stringify({ confirm: 'reset' })
        });
        if (resp.ok) {
            UI.closeModal('reset-modal');
            window.location.reload();
        }
    } catch (e) {
        console.error('Reset failed:', e);
    }
}

// Global error handlers
window.addEventListener('unhandledrejection', (event) => {
    console.error('[ChatEKLD] Unhandled rejection:', event.reason);
});

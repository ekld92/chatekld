import base from './playwright.config.mjs';
export default {
    ...base,
    testMatch: 'pdfBackend.spec.js',
    testIgnore: [],
    webServer: {...base.webServer, env: {...base.webServer.env, PAPERMIND_PDF_FIXTURE: '1'}},
};

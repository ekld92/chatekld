import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Hermetic by the same contract as the root conftest.py: the app is booted
// against a throwaway BASE_DIR so no real config, vault, index or journal is
// read or written by the browser matrix. serve.py installs an outbound network
// denial hook before importing the app, including local model discovery.
const baseDir = mkdtempSync(join(tmpdir(), 'papermind-browser-'));
const python = process.env.CHERQAIN_PYTHON || process.env.PAPERMIND_PYTHON || 'python3';
const PORT = 5177;

export default {
    testDir: './tests/browser',
    testIgnore: 'pdfBackend.spec.js', // requires the isolated PDF backend configuration
    // The suite asserts response headers and layout, both of which are
    // deterministic; a retry would only mask a real flake.
    retries: 0,
    reporter: process.env.CI ? 'github' : 'list',
    use: {
        baseURL: `http://127.0.0.1:${PORT}`,
    },
    webServer: {
        command: `${python} tests/browser/serve.py ${PORT}`,
        url: `http://127.0.0.1:${PORT}/api/health`,
        reuseExistingServer: false,
        timeout: 120_000,
        env: {
            CHERQAIN_BASE_DIR: baseDir,
            CHATEKLD_BASE_DIR: baseDir,
            PYTHONPATH: '.',
            // The health probe and every page load must reach the app without a
            // configured vault; nothing here may point at the developer's data.
            CHERQAIN_API_URL: '',
            CHATEKLD_API_URL: '',
        },
    },
};

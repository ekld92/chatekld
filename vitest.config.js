// Minimal Vitest harness (improvement plan 2026-07-04, Track 7.3): jsdom gives
// the browser modules under static/js/ a real DOM (DOMParser, document,
// TextDecoder) so ui.js::sanitiseHtml and api.js::readSSE can be exercised as
// the app runs them — not re-implemented in the test. Test-only tooling: the
// app's runtime never touches node_modules (vendored-JS rule in CLAUDE.md).
import { defineConfig } from 'vitest/config';

export default defineConfig({
    test: {
        environment: 'jsdom',
        include: ['tests/js/**/*.test.js'],
    },
});

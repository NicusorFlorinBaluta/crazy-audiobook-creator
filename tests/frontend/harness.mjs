/**
 * Behavioural DOM harness for the dashboard frontend.
 *
 * Why this exists: `tests/test_dashboard_frontend_ux.py` asserts that specific
 * substrings appear in the frontend source. That style breaks when an
 * attribute is reordered and passes when the surrounding logic is broken, so
 * it could not catch either of the two bugs these tests now pin -- both of
 * which were wrong *branch selection*, with the markup entirely intact.
 *
 * The harness loads the real `index.html`, evaluates the real scripts against
 * it, and calls the real render functions. Nothing is stubbed except the
 * network and the socket.
 */

import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));

/**
 * Frontend directory under test. Overridable so a test can point the harness
 * at a modified copy of the sources -- used to prove that a regression test
 * actually fails against the pre-fix code rather than passing vacuously.
 */
export const FRONTEND_DIR =
    process.env.CAC_FRONTEND_DIR ||
    resolve(HERE, '..', '..', 'brain', 'dashboard', 'frontend');

/** Scripts in load order, matching the order `index.html` declares them. */
const SCRIPTS = [
    'js/dom-utils.js',
    'js/app.js',
];

/**
 * Boot a dashboard page and return its window plus the script scope.
 *
 * `initApp` is deliberately never run: the harness waits for the document to
 * finish loading *before* injecting the scripts, so the `DOMContentLoaded`
 * listener app.js registers is attached after the event has already fired.
 * That keeps every test in control of exactly what gets rendered, and stops
 * the page's polling timers from ever starting.
 */
export async function loadDashboard({ frontendDir = FRONTEND_DIR } = {}) {
    const dom = new JSDOM(readFileSync(join(frontendDir, 'index.html'), 'utf8'), {
        url: 'http://localhost:8000/',
    });
    const { window } = dom;

    await new Promise((done) => {
        if (window.document.readyState === 'complete') done();
        else window.addEventListener('load', done);
    });

    // The page must never reach the network from a test.
    window.fetch = () =>
        Promise.reject(new Error('network access is not available in tests'));
    window.WebSocket = class {
        constructor() {}
        close() {}
        send() {}
    };

    const scope = vm.createContext(window);
    for (const script of SCRIPTS) {
        vm.runInContext(readFileSync(join(frontendDir, script), 'utf8'), scope, {
            filename: script,
        });
    }

    const text = (id) => window.document.getElementById(id)?.textContent?.trim();

    return {
        window,
        scope,
        text,
        close: () => window.close(),
    };
}

/** A project payload shaped like `/api/projects` returns. */
export function projectFixture(overrides = {}) {
    return {
        project_id: 'fixture-book',
        title: 'Fixture Book',
        status: 'created',
        active_stage: null,
        total_chapters: 10,
        total_lines: 100,
        scripted_chapters: [],
        generated_chapters: [],
        mastered_chapters: [],
        chapter_details: [],
        generation_chapter_selection: null,
        active_generation_chapter_selection: null,
        pause_reason: null,
        running: false,
        progress: null,
        work_progress: {},
        ...overrides,
    };
}

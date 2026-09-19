/**
 * The attention panel is the first thing on the project page. It must never
 * contradict itself, because an operator who learns to ignore a red banner
 * will ignore the real one too.
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import { loadDashboard, projectFixture } from './harness.mjs';

/** Put the inbox in the state a render needs, without touching the network. */
function seedInbox(scope, items) {
    scope.attentionState.data = { items, total_count: items.length };
    scope.attentionState.projectId = 'fixture-book';
}

test('a review gate with no blocking items does not claim "0 Action Required items"', async () => {
    // Regression: the warning branch fires for `waiting_for_review` as well as
    // for blocking items, but the summary always named the blocking count. With
    // zero blocking items that rendered "⚠️ Action required ... ⚠️ 0 Action
    // Required items" -- a red alarm asserting nothing is wrong.
    const dash = await loadDashboard();
    try {
        const project = projectFixture({
            status: 'waiting_for_review',
            pause_reason: 'Review and approve the speaking cast before audio generation.',
        });
        seedInbox(dash.scope, [
            { category: 'pronunciation', blocking: false },
            { category: 'character', blocking: false },
        ]);
        dash.scope.renderAttentionInbox(project);

        const summary = dash.text('attention-summary');
        assert.doesNotMatch(summary, /0 Action Required items?/);
        assert.match(summary, /Review and approve the speaking cast/);
        assert.match(summary, /1 Pronunciation Terms/);
        assert.equal(dash.text('attention-kicker'), '⚠️ Action required');
    } finally {
        dash.close();
    }
});

test('a review gate without a pause reason still names an action', async () => {
    // `/api/projects` returns `pause_reason`, but the detail payload can carry
    // null, so the fallback is a real code path rather than a defensive tail.
    const dash = await loadDashboard();
    try {
        const project = projectFixture({ status: 'waiting_for_review', pause_reason: null });
        seedInbox(dash.scope, []);
        dash.scope.renderAttentionInbox(project);

        const summary = dash.text('attention-summary');
        assert.doesNotMatch(summary, /0 Action Required items?/);
        assert.match(summary, /Approval required before the pipeline can continue/);
    } finally {
        dash.close();
    }
});

test('real blocking items are still counted', async () => {
    const dash = await loadDashboard();
    try {
        const project = projectFixture({ status: 'scripting' });
        seedInbox(dash.scope, [
            { category: 'attribution', blocking: true },
            { category: 'attribution', blocking: true },
            { category: 'pronunciation', blocking: false },
        ]);
        dash.scope.renderAttentionInbox(project);

        assert.match(dash.text('attention-summary'), /2 Action Required items/);
        assert.equal(dash.text('attention-kicker'), '⚠️ Attention required');
    } finally {
        dash.close();
    }
});

test('one blocking item is singular', async () => {
    const dash = await loadDashboard();
    try {
        const project = projectFixture({ status: 'scripting' });
        seedInbox(dash.scope, [{ category: 'attribution', blocking: true }]);
        dash.scope.renderAttentionInbox(project);
        assert.match(dash.text('attention-summary'), /1 Action Required item\b/);
    } finally {
        dash.close();
    }
});

test('a clear project reports itself clear', async () => {
    const dash = await loadDashboard();
    try {
        const project = projectFixture({ status: 'scripting' });
        seedInbox(dash.scope, [{ category: 'pronunciation', blocking: false }]);
        dash.scope.renderAttentionInbox(project);

        assert.match(dash.text('attention-summary'), /0 Blocking Issues/);
        assert.equal(dash.text('attention-kicker'), 'Project Clear / Optional Tools');
    } finally {
        dash.close();
    }
});

test('a pause reason is escaped rather than interpolated as markup', async () => {
    // `pause_reason` is server-supplied text reaching the DOM through innerHTML.
    const dash = await loadDashboard();
    try {
        const project = projectFixture({
            status: 'waiting_for_review',
            pause_reason: '<img src=x onerror="window.__pwned=1">',
        });
        seedInbox(dash.scope, []);
        dash.scope.renderAttentionInbox(project);

        const panel = dash.window.document.getElementById('attention-summary');
        assert.equal(panel.querySelectorAll('img').length, 0);
        assert.equal(dash.window.__pwned, undefined);
    } finally {
        dash.close();
    }
});

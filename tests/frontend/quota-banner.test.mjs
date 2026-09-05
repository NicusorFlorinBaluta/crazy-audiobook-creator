/**
 * The review-inbox banner has to answer one question: wait, or go and review by
 * hand? It said "Quota Paused" with no number, so a thirty-second circuit
 * cooldown and a twenty-hour quota wait looked identical.
 *
 * It also decided the daily budget was exhausted by searching an error string
 * for the literal "50/50", which never matches the 450-request flash-lite
 * limit and breaks whenever a limit is reconfigured. The server now reports
 * usage and the exact reset instant, and these pin that it is used.
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import { readFileSync } from 'node:fs';
import { loadDashboard } from './harness.mjs';

const QUOTA = {
    day: '2026-09-05',
    timezone: 'America/Los_Angeles',
    resets_at: '2026-09-06T00:00:00-07:00',
    resets_in_seconds: 71809,
    models: {
        'gemini-3.5-flash-lite': { used: 31, limit: 450, remaining: 419, exhausted: false },
        'gemini-3.5-flash': { used: 50, limit: 50, remaining: 0, exhausted: true },
    },
    any_exhausted: true,
};

test('a wait is expressed in units a person can act on', async () => {
    const dash = await loadDashboard();
    try {
        const { formatDuration } = dash.scope;
        assert.equal(formatDuration(0), '', 'no wait should render as nothing, not "0s"');
        assert.equal(formatDuration(45), '45s');
        assert.equal(formatDuration(1830), '30m 30s');
        assert.equal(formatDuration(71809), '19h 56m', 'a 20-hour wait must not read as seconds');
    } finally {
        dash.close();
    }
});

test('quota usage names the model, so a cheap tier is not mistaken for the expensive one', async () => {
    const dash = await loadDashboard();
    try {
        const { describeQuotaUsage } = dash.scope;
        assert.equal(describeQuotaUsage(QUOTA), '31/450 flash-lite, 50/50 flash');
        assert.equal(describeQuotaUsage({}), 'unknown', 'missing data must not render "undefined"');
    } finally {
        dash.close();
    }
});

test('the reset is given as both a countdown and a wall-clock time', async () => {
    const dash = await loadDashboard();
    try {
        const { describeQuotaReset } = dash.scope;
        const text = describeQuotaReset(QUOTA);
        assert.match(text, /in 19h 56m/, 'must say how long');
        assert.match(text, /your time/, 'and must localise the instant, since the budget rolls over in Los Angeles');
        assert.equal(describeQuotaReset({}), 'shortly', 'missing data must degrade, not throw');
    } finally {
        dash.close();
    }
});

test('exhaustion comes from the reported flag, not from string-matching an error', async () => {
    // The old check looked for "50/50" in last_error. With the flash-lite limit
    // at 450 that never fires, so the banner would sit on "Ready" while every
    // request was being refused.
    const source = readFileSync(new URL('../../brain/dashboard/frontend/js/app.js', import.meta.url), 'utf8');
    {
        assert.ok(
            !source.includes("includes('50/50')"),
            'the banner must not infer budget exhaustion from a hardcoded "50/50"',
        );
        assert.ok(
            source.includes('quota.any_exhausted'),
            'exhaustion should come from the server-reported quota block',
        );
    }
});

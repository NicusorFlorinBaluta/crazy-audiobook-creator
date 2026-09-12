/**
 * The work-status panel is the operator's single answer to "what is the
 * pipeline doing and what do I do next". These tests pin the branch selection,
 * which is where it went wrong.
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import { loadDashboard, projectFixture } from './harness.mjs';

test('a project awaiting voice-cast approval is told to approve voices', async () => {
    // Regression: `waiting_for_review` was classified as a terminal status, so
    // it shadowed `active_stage` and the `voice_review` branch became
    // unreachable. The panel fell through to the generic default and told the
    // operator to "Choose chapters and start the pipeline" -- an instruction
    // that cannot clear a review gate.
    const dash = await loadDashboard();
    try {
        const project = projectFixture({
            status: 'waiting_for_review',
            active_stage: 'voice_review',
            total_chapters: 32,
            scripted_chapters: Array.from({ length: 19 }, (_, i) => i + 1),
        });
        dash.scope.state.currentProject = project;
        dash.scope.renderWorkStatus(project);

        assert.equal(dash.text('work-status-title'), 'Waiting for voice-cast approval');
        assert.match(dash.text('work-status-detail'), /Voice casting tab/);
        assert.equal(dash.text('work-overall-label'), 'Voice preparation');
        assert.equal(dash.text('work-chapter-label'), 'Speaking cast');
        assert.equal(dash.text('work-line-label'), 'Next action');

        assert.notEqual(dash.text('work-status-title'), 'Waiting to start');
        assert.doesNotMatch(
            dash.text('work-status-detail'),
            /Choose chapters and start the pipeline/,
        );
    } finally {
        dash.close();
    }
});

test('an unknown review gate still renders review copy, not "Waiting to start"', async () => {
    // A future gate whose `active_stage` this function does not recognise must
    // degrade to an honest review message rather than the generic default.
    const dash = await loadDashboard();
    try {
        const project = projectFixture({
            status: 'waiting_for_review',
            active_stage: 'some_future_review',
            pause_reason: 'Approve the pronunciation list before continuing.',
        });
        dash.scope.state.currentProject = project;
        dash.scope.renderWorkStatus(project);

        assert.equal(dash.text('work-status-title'), 'Waiting for review');
        assert.equal(
            dash.text('work-status-detail'),
            'Approve the pronunciation list before continuing.',
        );
    } finally {
        dash.close();
    }
});

test('a genuinely terminal status still wins over the stage it stopped in', async () => {
    // The terminal override exists for a reason and must survive the fix: a
    // paused project must not be described as though it were still generating.
    const dash = await loadDashboard();
    try {
        const project = projectFixture({
            status: 'paused',
            active_stage: 'generating',
            pause_reason: 'Paused by the operator.',
        });
        dash.scope.state.currentProject = project;
        dash.scope.renderWorkStatus(project);

        assert.equal(dash.text('work-status-title'), 'Pipeline paused');
        assert.equal(dash.text('work-status-detail'), 'Paused by the operator.');
    } finally {
        dash.close();
    }
});

test('resolvePipelineStage prefers the specific stage over the coarse status', async () => {
    const dash = await loadDashboard();
    try {
        const { resolvePipelineStage } = dash.scope;
        assert.equal(
            resolvePipelineStage({ status: 'waiting_for_review', active_stage: 'voice_review' }),
            'voice_review',
        );
        assert.equal(resolvePipelineStage({ status: 'Generating' }), 'generating');
        assert.equal(resolvePipelineStage({}), '');
        assert.equal(resolvePipelineStage(null), '');
    } finally {
        dash.close();
    }
});

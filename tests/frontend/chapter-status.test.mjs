import assert from 'node:assert/strict';
import test from 'node:test';
import { loadDashboard, projectFixture } from './harness.mjs';

test('actively mastering chapter displays "Mastering..." badge rather than static "Mastered"', async (t) => {
    const dashboard = await loadDashboard();
    t.after(() => dashboard.close());

    const project = projectFixture({
        project_id: 'test-book',
        status: 'mastering',
        active_stage: 'mastering',
        running: true,
        mastered_chapters: [1, 2],
        generated_chapters: [1, 2, 3],
        total_chapters: 3,
        current_master_chapter: 2,
        chapter_details: [
            { number: 1, title: 'Chapter 1', total_lines: 50, lines_generated: 50, progress_percent: 100 },
            { number: 2, title: 'Chapter 2', total_lines: 60, lines_generated: 60, progress_percent: 100 },
            { number: 3, title: 'Chapter 3', total_lines: 70, lines_generated: 70, progress_percent: 100 },
        ],
    });

    dashboard.window.currentProject = project;
    dashboard.window.renderChapterList(project);

    const rows = dashboard.window.document.querySelectorAll('#chapter-grid .chapter-cell');
    assert.equal(rows.length, 3);

    // Chapter 1 is static mastered
    const ch1Badge = rows[0].querySelector('.chapter-status-pill');
    assert.equal(ch1Badge.textContent.trim(), 'Mastered');

    // Chapter 2 is currently mastering
    const ch2Badge = rows[1].querySelector('.chapter-status-pill');
    assert.equal(ch2Badge.textContent.trim(), 'Mastering...');
    assert.ok(rows[1].classList.contains('chapter-active'), 'Mastering chapter row has pulsing chapter-active class');
});

test('chapter pending voice revision displays "Needs audio update" and 0% progress', async (t) => {
    const dashboard = await loadDashboard();
    t.after(() => dashboard.close());

    const project = projectFixture({
        project_id: 'test-book',
        status: 'generating',
        active_stage: 'generation',
        running: false,
        mastered_chapters: [],
        generated_chapters: [1],
        voice_revision_pending_chapters: [2],
        total_chapters: 2,
        chapter_details: [
            { number: 1, title: 'Chapter 1', total_lines: 50, lines_generated: 50, progress_percent: 100 },
            { number: 2, title: 'Chapter 2', total_lines: 60, lines_generated: 0, progress_percent: 0 },
        ],
    });

    dashboard.window.currentProject = project;
    dashboard.window.renderChapterList(project);

    const rows = dashboard.window.document.querySelectorAll('#chapter-grid .chapter-cell');
    assert.equal(rows.length, 2);

    // Chapter 2 is pending revision
    const ch2Badge = rows[1].querySelector('.chapter-status-pill');
    assert.equal(ch2Badge.textContent.trim(), 'Needs audio update');

    const ch2Progress = rows[1].querySelector('.chapter-progress-value > span');
    assert.equal(ch2Progress.textContent.trim(), '0%');
});

test('actively generating chapter displays live line count and active pill', async (t) => {
    const dashboard = await loadDashboard();
    t.after(() => dashboard.close());

    const project = projectFixture({
        project_id: 'test-book',
        status: 'generating',
        active_stage: 'generation',
        running: true,
        current_gen_chapter: 1,
        lines_generated: 25,
        total_chapters: 1,
        chapter_details: [
            { number: 1, title: 'Chapter 1', total_lines: 50, lines_generated: 25, progress_percent: 50 },
        ],
    });

    dashboard.window.currentProject = project;
    dashboard.window.renderChapterList(project);

    const rows = dashboard.window.document.querySelectorAll('#chapter-grid .chapter-cell');
    assert.equal(rows.length, 1);

    const ch1Badge = rows[0].querySelector('.chapter-status-pill');
    assert.equal(ch1Badge.textContent.trim(), 'Generating 25/50');
    assert.ok(rows[0].classList.contains('chapter-active'), 'Actively generating chapter has chapter-active class');

    const ch1Progress = rows[0].querySelector('.chapter-progress-value > span');
    assert.equal(ch1Progress.textContent.trim(), '50%');
});

test('purely scripted chapter that never had audio displays "Scripted · X lines" and not "Needs audio update"', async (t) => {
    const dashboard = await loadDashboard();
    t.after(() => dashboard.close());

    const project = projectFixture({
        project_id: 'test-book',
        status: 'generating',
        active_stage: 'generation',
        running: false,
        mastered_chapters: [1],
        generated_chapters: [1],
        scripted_chapters: [1, 2, 3],
        voice_revision_pending_chapters: [],
        total_chapters: 3,
        chapter_details: [
            { number: 1, title: 'Chapter 1', total_lines: 50, lines_generated: 50, progress_percent: 100 },
            { number: 2, title: 'Chapter 2', total_lines: 60, lines_generated: 0, progress_percent: 0 },
            { number: 3, title: 'Chapter 3', total_lines: 70, lines_generated: 0, progress_percent: 0 },
        ],
    });

    dashboard.window.currentProject = project;
    dashboard.window.renderChapterList(project);

    const rows = dashboard.window.document.querySelectorAll('#chapter-grid .chapter-cell');
    assert.equal(rows.length, 3);

    // Chapter 1 is mastered
    assert.equal(rows[0].querySelector('.chapter-status-pill').textContent.trim(), 'Mastered');

    // Chapters 2 & 3 were never generated, only scripted
    const ch2Badge = rows[1].querySelector('.chapter-status-pill');
    assert.equal(ch2Badge.textContent.trim(), 'Scripted · 60 lines');
    assert.notEqual(ch2Badge.textContent.trim(), 'Needs audio update');

    const ch3Badge = rows[2].querySelector('.chapter-status-pill');
    assert.equal(ch3Badge.textContent.trim(), 'Scripted · 70 lines');
    assert.notEqual(ch3Badge.textContent.trim(), 'Needs audio update');
});


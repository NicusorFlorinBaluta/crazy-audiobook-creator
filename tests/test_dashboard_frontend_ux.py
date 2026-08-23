from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "brain" / "dashboard" / "frontend"


def _read(relative: str) -> str:
    return (FRONTEND / relative).read_text(encoding="utf-8")


def test_voice_assignment_uses_actual_cast_assignment() -> None:
    script = _read("js/script-viewer.js")

    assert "assignedVoiceByCharacter.get(character.character_id)" in script
    assert "candidate.voice_id === character.voice_id" not in script


def test_project_navigation_and_tabs_are_keyboard_semantic() -> None:
    page = _read("index.html")
    app = _read("js/app.js")

    assert 'class="nav-brand" id="nav-home-btn" aria-label="Back to projects"' in page
    assert 'role="tablist"' in page
    assert page.count('role="tab"') == 4
    assert page.count('role="tabpanel"') == 4
    assert "document.createElement('button')" in app
    assert "handleTabKeydown" in app


def test_upload_dialog_has_focus_and_close_support() -> None:
    page = _read("index.html")
    app = _read("js/app.js")

    assert 'role="dialog" aria-modal="true"' in page
    assert 'aria-label="Close new project dialog"' in page
    assert "event.key === 'Escape'" in app
    assert "state.lastModalTrigger" in app


def test_mobile_chapter_layout_and_compact_quality_grid_are_defined() -> None:
    styles = _read("css/styles.css")

    assert ".chapter-section-heading { align-items: stretch; flex-direction: column" in styles
    assert ".chapter-selection-summary { align-self: flex-start; max-width: 100%; }" in styles
    assert ".quality-overview { grid-template-columns: repeat(2, minmax(0, 1fr));" in styles


def test_review_and_log_filters_are_present() -> None:
    page = _read("index.html")
    script = _read("js/script-viewer.js")
    logs = _read("js/log-console.js")

    assert 'id="casting-filter"' in page
    assert 'id="script-speaker-filter"' in page
    assert "join-filter-disposition" in script
    assert "join-bulk-acceptable" in script
    assert "ROUTINE_LINE" in logs
    assert 'id="log-level-filter"' in page


def test_pipeline_disclosure_survives_status_polling() -> None:
    pipeline = _read("js/pipeline.js")

    assert "pipelineDisclosureInitialized" in pipeline
    assert "projectId !== pipelineDisclosureProject" in pipeline
    assert "pipelineDetails.open = !isDone || isRunning" in pipeline


def test_chapters_use_the_shared_native_disclosure_pattern() -> None:
    page = _read("index.html")
    app = _read("js/app.js")

    assert '<details class="chapter-progress-section schedule-section"' in page
    assert '<summary class="chapter-section-heading" role="button">' in page
    assert 'id="btn-toggle-chapters"' not in page
    assert "btnToggleChapters" not in app
    assert "chapterDetails.open" in app


def test_manual_metadata_search_and_selection_controls_are_present() -> None:
    page = _read("index.html")
    app = _read("js/app.js")

    assert 'id="metadata-search-form"' in page
    assert 'id="metadata-search-results"' in page
    assert "/search-metadata`" in app
    assert "provider_id: providerId" in app


def test_voice_casting_exposes_the_bulk_sample_download() -> None:
    page = _read("index.html")
    script = _read("js/script-viewer.js")

    assert 'id="btn-download-all-voices"' in page
    assert 'data-server-download href="#"' in page
    assert "/voices/download-all`" in script
    assert "readyVoiceCount" in script


def test_schedule_time_controls_have_stable_responsive_grid_areas() -> None:
    app = _read("js/app.js")
    styles = _read("css/styles.css")

    assert 'class="schedule-separator">to</span>' in app
    assert 'grid-template-areas: "days start separator end remove";' in styles
    assert "grid-template-columns: minmax(250px, 1fr) 142px auto 142px 34px;" in styles
    assert "@media (max-width: 480px)" in styles


def test_dashboard_uses_canonical_chapter_numbers_instead_of_source_headings() -> None:
    app = _read("js/app.js")
    script_viewer = _read("js/script-viewer.js")

    assert "const title = `Chapter ${chapter}`;" in app
    assert "const chapterTitle = currentChapter ? `Chapter ${currentChapter}` : '';" in app
    assert "progress.current = `Scripting · Chapter ${current} of ${total}`;" in app
    assert "const title = detail.title || `Chapter ${chapter}`;" not in app
    assert "opt.textContent = `Chapter ${chNum}`;" in script_viewer
    assert "opt.textContent = `Chapter ${chNum}: ${title}`;" not in script_viewer
    assert "ch.chapter_title ?" not in script_viewer


def test_empty_next_run_selection_is_not_replaced_by_stale_active_selection() -> None:
    app = _read("js/app.js")

    assert "state.chapterSelection" in app
    assert "const savedSelection = project.running === true" in app
    assert "project.active_generation_chapter_selection\n        || project.generation_chapter_selection" not in app


def test_manual_resume_requests_a_one_run_schedule_override() -> None:
    app = _read("js/app.js")
    pipeline = _read("js/pipeline.js")

    assert "/start?override_schedule=true`" in app
    assert "/stop?resume_on_schedule=true`" in app
    assert "result.schedule_overridden" in app
    assert "result.will_resume_on_schedule" in app
    assert "outside configured working hours for this run only" in pipeline

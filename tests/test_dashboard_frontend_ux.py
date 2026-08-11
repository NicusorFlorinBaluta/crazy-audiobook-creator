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

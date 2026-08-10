import unittest
import re
from pathlib import Path


FRONTEND = Path("brain/dashboard/frontend")


class DashboardBasePathTests(unittest.TestCase):
    def test_dashboard_frontend_has_no_root_absolute_app_urls(self):
        files = [
            FRONTEND / "index.html",
            *(FRONTEND / "js").glob("*.js"),
        ]
        forbidden = (
            '"/static/',
            "'/static/",
            "`/static/",
            '"/api/',
            "'/api/",
            "`/api/",
            '"/ws/',
            "'/ws/",
            "`/ws/",
        )
        violations: list[str] = []
        for path in files:
            text = path.read_text(encoding="utf-8")
            for marker in forbidden:
                if marker in text:
                    violations.append(f"{path}: {marker}")
        self.assertFalse(
            violations,
            "Root-absolute URLs break /audiobook/: " + ", ".join(violations),
        )

    def test_websocket_url_is_resolved_relative_to_the_document(self):
        app_js = (FRONTEND / "js/app.js").read_text(encoding="utf-8")
        self.assertIn("new URL('ws/updates', window.location.href)", app_js)

    def test_frontend_assets_share_one_cache_revision(self):
        index = (FRONTEND / "index.html").read_text(encoding="utf-8")
        revisions = re.findall(
            r'(?:styles\.css|(?:app|pipeline|script-viewer|log-console)\.js)'
            r'\?v=([^"\']+)',
            index,
        )
        self.assertEqual(len(revisions), 5)
        self.assertEqual(len(set(revisions)), 1)

    def test_embedded_frontend_disables_stale_browser_caching(self):
        api_source = Path("brain/dashboard/api/main.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '"no-store, no-cache, max-age=0, must-revalidate"',
            api_source,
        )
        self.assertIn('"X-Crazy-Audiobook-UI-Version"', api_source)

    def test_voice_candidate_controls_use_supported_assignment_route(self):
        viewer = (FRONTEND / "js/script-viewer.js").read_text(encoding="utf-8")
        self.assertNotIn("/voices/${character.character_id}/assign", viewer)
        self.assertIn("/characters/${encodeURIComponent(character.character_id)}/voice", viewer)
        self.assertIn("if (!response.ok)", viewer)
        self.assertIn("Voice option applied", viewer)

    def test_voice_regeneration_targets_the_selected_option(self):
        viewer = (FRONTEND / "js/script-viewer.js").read_text(encoding="utf-8")
        self.assertIn("selected?.value || mainVoice.voice_id", viewer)
        self.assertIn("Regenerate option ${optionLabel} (replaces option ${optionLabel})", viewer)
        self.assertIn("Only the selected comparison option will be replaced", viewer)

    def test_voice_assignment_button_has_busy_feedback(self):
        viewer = (FRONTEND / "js/script-viewer.js").read_text(encoding="utf-8")
        self.assertIn("button.textContent = 'Assigning...'", viewer)
        self.assertIn("button.disabled = true", viewer)


if __name__ == "__main__":
    unittest.main()

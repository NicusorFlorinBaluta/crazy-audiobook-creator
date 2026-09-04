"""Opt-in live voice-upload checks.

These tests require a running dashboard, a prepared project, and real audio.
They are excluded from ordinary discovery unless ``RUN_LIVE_UPLOAD_E2E=1``.
"""

from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

RUN_LIVE = os.environ.get("RUN_LIVE_UPLOAD_E2E") == "1"
BASE_URL = os.environ.get("UPLOAD_E2E_BASE_URL", "http://127.0.0.1:8000")
PROJECT_ID = os.environ.get("UPLOAD_E2E_PROJECT_ID", "sample_book-1")
VOICE_ID = os.environ.get("UPLOAD_E2E_VOICE_ID", "fake_cand")
FILE_PATH = Path(
    os.environ.get(
        "UPLOAD_E2E_AUDIO_FILE",
        "voice_library/sample_book-1/child_female.wav",
    )
)
TRANSCRIPT = (
    "She walked through the moonlit garden, listening as fallen leaves "
    "whispered beneath each careful step."
)


@unittest.skipUnless(RUN_LIVE, "set RUN_LIVE_UPLOAD_E2E=1 for live API tests")
class VoiceUploadLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not FILE_PATH.is_file():
            raise unittest.SkipTest(f"live audio fixture not found: {FILE_PATH}")
        cls.fixture_dir = TemporaryDirectory()
        cls.fixture_path = Path(cls.fixture_dir.name) / FILE_PATH.name
        shutil.copy2(FILE_PATH, cls.fixture_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture_dir.cleanup()

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.audio_path = Path(self.temp_dir.name) / self.fixture_path.name
        shutil.copy2(self.fixture_path, self.audio_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _upload(self, transcript: str) -> httpx.Response:
        with self.audio_path.open("rb") as audio:
            return httpx.post(
                f"{BASE_URL}/api/projects/{PROJECT_ID}/voices/{VOICE_ID}/upload",
                files={"file": (self.audio_path.name, audio, "audio/wav")},
                data={"transcript": transcript},
                timeout=300.0,
            )

    def test_matching_transcript_is_accepted(self) -> None:
        response = self._upload(TRANSCRIPT)
        self.assertEqual(response.status_code, 200, response.text)

    def test_mismatched_transcript_is_rejected(self) -> None:
        response = self._upload(
            "This is completely different text that should fail."
        )
        self.assertNotEqual(response.status_code, 200, response.text)


if __name__ == "__main__":
    unittest.main()

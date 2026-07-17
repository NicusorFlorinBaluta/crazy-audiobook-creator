"""Voice Designer — Bootstrap character voices from text descriptions.

Orchestrates the Voice Design → Save → Clone workflow:
1. Takes character voice descriptions from the LLM
2. Generates reference clips using Qwen3-TTS Voice Design mode
3. Saves them to the Voice Library for reuse
"""

from __future__ import annotations

import logging
from pathlib import Path

from voice.tts_server.qwen3_engine import Qwen3TTSEngine
from voice.tts_server.voice_library import VoiceLibraryManager
from shared.constants import Gender, VOICE_DESIGN_TEST_SENTENCES
from shared.models import (
    BootstrapVoiceResult,
    BootstrapVoicesRequest,
    BootstrapVoicesResponse,
    Character,
)

logger = logging.getLogger(__name__)


class VoiceDesigner:
    """Generate unique voice reference clips for characters."""

    def __init__(
        self,
        engine: Qwen3TTSEngine,
        library: VoiceLibraryManager,
    ):
        self.engine = engine
        self.library = library

    def bootstrap_voices(
        self,
        request: BootstrapVoicesRequest,
    ) -> BootstrapVoicesResponse:
        """Generate voice reference clips for all characters in a project.

        For each character:
        1. Check if a voice already exists (skip if idempotent)
        2. Select a test sentence based on gender
        3. Use Qwen3-TTS Voice Design to generate a reference clip
        4. Save to the voice library

        Args:
            request: Bootstrap request with project ID and characters.

        Returns:
            Response with generated voice file paths.
        """
        project_id = request.project_id
        voices_generated: dict[str, BootstrapVoiceResult] = {}

        import subprocess
        import time
        import requests
        
        logger.info(
            "Bootstrapping %d voices for project '%s'",
            len(request.characters),
            project_id,
        )

        # Boot up Parler Microservice
        logger.info("Booting Parler-TTS Microservice on port 8101...")
        parler_proc = subprocess.Popen(
            ["/home/crazywiz/crazy-audiobook-creator/venv_parler/bin/python", "/home/crazywiz/crazy-audiobook-creator/parler_server.py"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        
        # Wait for microservice to be healthy
        for _ in range(30):
            try:
                resp = requests.get("http://127.0.0.1:8101/health")
                if resp.status_code == 200:
                    logger.info("Parler-TTS Microservice is ready!")
                    break
            except Exception:
                time.sleep(2)
        else:
            parler_proc.kill()
            raise RuntimeError("Parler-TTS Microservice failed to start")

        try:
            for char_id, character in request.characters.items():
                # Check if voice already exists and skip if not forcing regeneration
                if not request.force_regenerate and self.library.voice_exists(
                    project_id, char_id
                ):
                    existing = self.library.get_voice_info(project_id, char_id)
                    if existing:
                        logger.info("Voice for '%s' already exists, skipping", char_id)
                        voices_generated[char_id] = BootstrapVoiceResult(
                            file=existing.get("file", ""),
                            duration_seconds=existing.get("duration_seconds", 0.0),
                            sample_rate=existing.get("sample_rate", 24000),
                        )
                        continue

                # Generate voice reference clip
                result = self._generate_voice(project_id, char_id, character)
                voices_generated[char_id] = result
        
        finally:
            # Shut down Parler Microservice to free up VRAM for Qwen
            logger.info("Shutting down Parler-TTS Microservice...")
            parler_proc.terminate()
            parler_proc.wait(timeout=10)

        return BootstrapVoicesResponse(
            status="success",
            project_id=project_id,
            voices_generated=voices_generated,
        )

    def regenerate_voice(
        self,
        project_id: str,
        character_id: str,
        character: Character,
    ) -> BootstrapVoiceResult:
        """Force-regenerate a single character's voice."""
        logger.info("Regenerating voice for '%s' in project '%s'", character_id, project_id)
        return self._generate_voice(project_id, character_id, character)

    def _generate_voice(
        self,
        project_id: str,
        char_id: str,
        character: Character,
    ) -> BootstrapVoiceResult:
        """Generate a single voice reference clip."""
        # Select test sentence based on gender
        gender_key = character.gender.value if isinstance(character.gender, Gender) else str(character.gender)
        test_sentence = VOICE_DESIGN_TEST_SENTENCES.get(
            gender_key,
            VOICE_DESIGN_TEST_SENTENCES["other"],
        )

        # Generate voice using Voice Design mode
        output_path = self.library.get_voice_path(project_id, char_id)

        logger.info(
            "Generating voice for '%s' (%s): %s",
            character.name,
            char_id,
            character.voice_description[:60],
        )

        import requests
        resp = requests.post("http://127.0.0.1:8101/voices/design", json={
            "prompt": character.voice_description,
            "text": test_sentence,
            "output_path": str(output_path)
        })
        
        if resp.status_code != 200:
            raise RuntimeError(f"Parler microservice failed: {resp.text}")

        # Try to read the file to get duration and sample rate
        import soundfile as sf
        audio, sr = sf.read(str(output_path))
        duration_seconds = len(audio) / sr

        # Save to voice library registry
        self.library.register_voice(
            project_id=project_id,
            character_id=char_id,
            name=character.name,
            description=character.voice_description,
            gender=gender_key,
            file_path=str(output_path),
            duration_seconds=duration_seconds,
            sample_rate=sr,
        )

        return BootstrapVoiceResult(
            file=str(output_path),
            duration_seconds=duration_seconds,
            sample_rate=sr,
        )

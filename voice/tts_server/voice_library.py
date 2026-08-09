"""Voice Library Manager — Manage saved voice reference clips.

Handles:
  - Per-project voice storage (voice_library/{project_id}/)
  - Voice registry (voices.json per project)
  - Path management for voice reference clips
  - Listing and querying available voices
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.artifacts import atomic_write_json

logger = logging.getLogger(__name__)


class VoiceLibraryManager:
    """Manage the voice library (saved reference clips per project)."""

    def __init__(self, library_dir: str | Path = "voice_library"):
        self.library_dir = Path(library_dir).resolve()
        self.library_dir.mkdir(parents=True, exist_ok=True)

    def _project_dir(self, project_id: str) -> Path:
        """Resolve a project directory without permitting path traversal."""
        if (
            not project_id
            or project_id in {".", ".."}
            or "/" in project_id
            or "\\" in project_id
            or ":" in project_id
        ):
            raise ValueError("Invalid project ID")
        project_dir = (self.library_dir / project_id).resolve()
        if (
            not project_dir.is_relative_to(self.library_dir)
            or project_dir == self.library_dir
        ):
            raise ValueError("Invalid project ID")
        return project_dir

    @staticmethod
    def _safe_character_id(character_id: str) -> str:
        if (
            not character_id
            or character_id in {".", ".."}
            or "/" in character_id
            or "\\" in character_id
            or ":" in character_id
        ):
            raise ValueError("Invalid character ID")
        return character_id

    def get_voice_path(self, project_id: str, character_id: str) -> Path:
        """Get the file path for a character's voice reference clip.

        Checks the voices.json registry first so that hashed filenames
        (e.g. narrator_male_7f8dfaa9.wav) are resolved correctly.  Falls
        back to the legacy <character_id>.wav convention when the character
        has no registry entry yet.
        """
        project_dir = self._project_dir(project_id)
        project_dir.mkdir(parents=True, exist_ok=True)
        info = self.get_voice_info(project_id, self._safe_character_id(character_id))
        if info and info.get("file"):
            registered = Path(info["file"])
            if registered.is_absolute():
                return registered
            return project_dir / registered
        return project_dir / f"{self._safe_character_id(character_id)}.wav"

    def voice_exists(self, project_id: str, character_id: str) -> bool:
        """Check if a voice reference clip exists for a character."""
        return self.get_voice_path(project_id, character_id).exists()

    def register_voice(
        self,
        project_id: str,
        character_id: str,
        name: str,
        description: str,
        gender: str,
        file_path: str,
        duration_seconds: float,
        sample_rate: int,
        ref_text: str = "",
        design_fingerprint: str = "",
        source_type: str = "generated",
        source_filename: str = "",
    ) -> None:
        """Register a voice in the project's voice registry (voices.json)."""
        project_dir = self._project_dir(project_id)
        character_id = self._safe_character_id(character_id)
        resolved_file = Path(file_path).resolve()
        if not resolved_file.is_relative_to(project_dir):
            raise ValueError("Voice reference is outside the project voice library")
        registry = self._load_registry(project_id)

        registry["project_id"] = project_id
        registry.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        registry.setdefault("voices", {})

        registry["voices"][character_id] = {
            "name": name,
            "file": str(resolved_file),
            "description": description,
            "gender": gender,
            "duration_seconds": duration_seconds,
            "sample_rate": sample_rate,
            "ref_text": ref_text,
            "design_fingerprint": design_fingerprint,
            "source_type": source_type,
            "source_filename": source_filename,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

        self._save_registry(project_id, registry)

    def get_voice_info(self, project_id: str, character_id: str) -> dict[str, Any] | None:
        """Get info about a specific voice."""
        registry = self._load_registry(project_id)
        return registry.get("voices", {}).get(character_id)

    def get_voice_ref_text(self, project_id: str, character_id: str) -> str:
        """Get the reference transcript for a character's voice clip."""
        info = self.get_voice_info(project_id, character_id)
        if info:
            return info.get("ref_text", "")
        return ""

    def list_voices(self, project_id: str) -> dict[str, Any]:
        """List all voices for a project."""
        registry = self._load_registry(project_id)
        return registry

    def delete_voice(self, project_id: str, character_id: str) -> None:
        """Delete a voice reference clip and its registry entry."""
        # Delete the audio file
        voice_path = self.get_voice_path(project_id, character_id)
        if voice_path.exists():
            voice_path.unlink()

        # Remove from registry
        registry = self._load_registry(project_id)
        if character_id in registry.get("voices", {}):
            del registry["voices"][character_id]
            self._save_registry(project_id, registry)

    # ------------------------------------------------------------------
    # Registry file management
    # ------------------------------------------------------------------

    def _load_registry(self, project_id: str) -> dict[str, Any]:
        """Load the voice registry for a project (cached in memory)."""
        if not hasattr(self, "_registry_cache"):
            self._registry_cache = {}

        if project_id in self._registry_cache:
            return self._registry_cache[project_id]

        registry_path = self._project_dir(project_id) / "voices.json"
        if registry_path.exists():
            with open(registry_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self._registry_cache[project_id] = data
                return data
        data = {"project_id": project_id, "voices": {}}
        self._registry_cache[project_id] = data
        return data

    def _save_registry(self, project_id: str, registry: dict[str, Any]) -> None:
        """Save the voice registry for a project and update cache."""
        if not hasattr(self, "_registry_cache"):
            self._registry_cache = {}

        project_dir = self._project_dir(project_id)
        self._registry_cache[project_id] = registry
        project_dir.mkdir(parents=True, exist_ok=True)
        registry_path = project_dir / "voices.json"
        atomic_write_json(registry_path, registry)

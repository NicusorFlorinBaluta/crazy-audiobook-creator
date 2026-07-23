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

logger = logging.getLogger(__name__)


class VoiceLibraryManager:
    """Manage the voice library (saved reference clips per project)."""

    def __init__(self, library_dir: str | Path = "voice_library"):
        self.library_dir = Path(library_dir)
        self.library_dir.mkdir(parents=True, exist_ok=True)

    def get_voice_path(self, project_id: str, character_id: str) -> Path:
        """Get the file path for a character's voice reference clip."""
        project_dir = self.library_dir / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        return project_dir / f"{character_id}.wav"

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
    ) -> None:
        """Register a voice in the project's voice registry (voices.json)."""
        registry = self._load_registry(project_id)

        registry["project_id"] = project_id
        registry.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        registry.setdefault("voices", {})

        registry["voices"][character_id] = {
            "name": name,
            "file": file_path,
            "description": description,
            "gender": gender,
            "duration_seconds": duration_seconds,
            "sample_rate": sample_rate,
            "ref_text": ref_text,
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

        registry_path = self.library_dir / project_id / "voices.json"
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

        self._registry_cache[project_id] = registry
        project_dir = self.library_dir / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        registry_path = project_dir / "voices.json"

        with open(registry_path, "w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2, default=str)

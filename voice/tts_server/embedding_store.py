"""Embedding & Voice Cache Store — SQLite-backed persistence for speaker embeddings and FX cache.

Handles:
  - Persistent caching of pre-computed speaker reference embedding tensors
  - Fast SHA-256 audio hash verification
  - Caching of pitch/tonal voice FX pre-processed audio reference clips
  - Line-level generation fingerprints for smart incremental chapter generation
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import torch

logger = logging.getLogger(__name__)


class EmbeddingStore:
    """SQLite database store for PyTorch voice embeddings and audio fingerprints."""

    def __init__(self, db_path: str | Path = "voice_cache.db"):
        self.db_path = str(db_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Create a database connection with WAL mode."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        """Initialize database schema."""
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        with self._connect() as conn:
            # Speaker reference embedding tensors
            conn.execute("""
                CREATE TABLE IF NOT EXISTS speaker_embeddings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    character_id TEXT NOT NULL,
                    embedding_blob BLOB NOT NULL,
                    ref_audio_hash TEXT NOT NULL,
                    ref_text TEXT DEFAULT '',
                    voice_description TEXT DEFAULT '',
                    embedding_shape TEXT,
                    sample_rate INTEGER DEFAULT 24000,
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id, character_id, ref_audio_hash)
                )
            """)

            # Voice FX pre-processed reference clip cache
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fx_prompt_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_audio_hash TEXT NOT NULL,
                    fx_settings_hash TEXT NOT NULL,
                    processed_audio_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(source_audio_hash, fx_settings_hash)
                )
            """)

            # Line generation fingerprints for incremental skipping
            conn.execute("""
                CREATE TABLE IF NOT EXISTS generation_fingerprints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    line_id TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    speaker_id TEXT NOT NULL,
                    emotion TEXT DEFAULT '',
                    speed REAL DEFAULT 1.0,
                    fx_hash TEXT DEFAULT '',
                    output_path TEXT NOT NULL,
                    duration_seconds REAL,
                    wer REAL,
                    quality_score REAL,
                    validation_status TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id, line_id)
                )
            """)
            conn.commit()

    @staticmethod
    def hash_file(file_path: str | Path) -> str:
        """Calculate SHA-256 hash of a file."""
        p = Path(file_path)
        if not p.exists():
            return ""
        hasher = hashlib.sha256()
        with open(p, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()

    @staticmethod
    def hash_text(text: str) -> str:
        """Calculate SHA-256 hash of string content."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Speaker Embedding CRUD
    # ------------------------------------------------------------------

    def get_embedding(
        self,
        project_id: str,
        character_id: str,
        ref_audio_path: str | Path,
    ) -> torch.Tensor | None:
        """Retrieve pre-computed PyTorch embedding tensor if valid."""
        audio_hash = self.hash_file(ref_audio_path)
        if not audio_hash:
            return None

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT embedding_blob FROM speaker_embeddings
                WHERE project_id = ? AND character_id = ? AND ref_audio_hash = ?
                """,
                (project_id, character_id, audio_hash),
            ).fetchone()

        if not row:
            return None

        try:
            buffer = io.BytesIO(row[0])
            tensor = torch.load(buffer, map_location="cpu")
            logger.info("Loaded cached embedding for character '%s' (%s)", character_id, project_id)
            return tensor
        except Exception as e:
            logger.warning("Failed to deserialize embedding for '%s': %s", character_id, e)
            return None

    def save_embedding(
        self,
        project_id: str,
        character_id: str,
        embedding: torch.Tensor,
        ref_audio_path: str | Path,
        ref_text: str = "",
        voice_description: str = "",
        sample_rate: int = 24000,
    ) -> None:
        """Save PyTorch embedding tensor to SQLite BLOB."""
        audio_hash = self.hash_file(ref_audio_path)
        if not audio_hash:
            return

        buffer = io.BytesIO()
        torch.save(embedding, buffer)
        blob = buffer.getvalue()
        shape_str = str(list(embedding.shape))
        now = datetime.now(timezone.utc).isoformat()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO speaker_embeddings
                    (project_id, character_id, embedding_blob, ref_audio_hash, ref_text, voice_description, embedding_shape, sample_rate, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    character_id,
                    blob,
                    audio_hash,
                    ref_text,
                    voice_description,
                    shape_str,
                    sample_rate,
                    now,
                ),
            )
            conn.commit()

        logger.info("Saved embedding for character '%s' (%s, shape=%s)", character_id, project_id, shape_str)

    # ------------------------------------------------------------------
    # Voice FX Prompt Audio Cache
    # ------------------------------------------------------------------

    def get_fx_prompt(
        self,
        source_audio_path: str | Path,
        fx_settings_dict: dict[str, Any],
    ) -> Path | None:
        """Get path to cached VoiceFX pre-processed reference clip."""
        source_hash = self.hash_file(source_audio_path)
        if not source_hash:
            return None

        fx_hash = self.hash_text(json.dumps(fx_settings_dict, sort_keys=True))

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT processed_audio_path FROM fx_prompt_cache
                WHERE source_audio_hash = ? AND fx_settings_hash = ?
                """,
                (source_hash, fx_hash),
            ).fetchone()

        if row and Path(row[0]).exists():
            return Path(row[0])
        return None

    def save_fx_prompt(
        self,
        source_audio_path: str | Path,
        fx_settings_dict: dict[str, Any],
        processed_audio_path: str | Path,
    ) -> None:
        """Save VoiceFX pre-processed audio reference clip mapping."""
        source_hash = self.hash_file(source_audio_path)
        if not source_hash:
            return

        fx_hash = self.hash_text(json.dumps(fx_settings_dict, sort_keys=True))
        now = datetime.now(timezone.utc).isoformat()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO fx_prompt_cache
                    (source_audio_hash, fx_settings_hash, processed_audio_path, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (source_hash, fx_hash, str(processed_audio_path), now),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Generation Fingerprints for Incremental Generation
    # ------------------------------------------------------------------

    def line_needs_regeneration(
        self,
        project_id: str,
        line_id: str,
        text: str,
        speaker: str,
        emotion: str = "",
        speed: float = 1.0,
        fx_dict: dict[str, Any] | None = None,
        output_path: str | Path | None = None,
    ) -> bool:
        """Check if a line needs to be re-synthesized based on fingerprint match."""
        if output_path and (not Path(output_path).exists() or Path(output_path).stat().st_size < 1000):
            return True

        text_hash = self.hash_text(text)
        fx_hash = self.hash_text(json.dumps(fx_dict or {}, sort_keys=True))

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT text_hash, speaker_id, emotion, speed, fx_hash
                FROM generation_fingerprints
                WHERE project_id = ? AND line_id = ?
                """,
                (project_id, line_id),
            ).fetchone()

        if not row:
            return True

        cached_text_hash, cached_speaker, cached_emotion, cached_speed, cached_fx_hash = row
        if (
            cached_text_hash == text_hash
            and cached_speaker == speaker
            and (cached_emotion or "").strip().lower() == (emotion or "").strip().lower()
            and abs(cached_speed - speed) < 1e-3
            and cached_fx_hash == fx_hash
        ):
            return False

        return True

    def save_generation_fingerprint(
        self,
        project_id: str,
        line_id: str,
        text: str,
        speaker: str,
        emotion: str,
        speed: float,
        fx_dict: dict[str, Any] | None,
        output_path: str | Path,
        duration_seconds: float = 0.0,
        wer: float = -1.0,
        quality_score: float = -1.0,
        validation_status: str = "pass",
    ) -> None:
        """Save line generation fingerprint after validation."""
        text_hash = self.hash_text(text)
        fx_hash = self.hash_text(json.dumps(fx_dict or {}, sort_keys=True))
        now = datetime.now(timezone.utc).isoformat()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO generation_fingerprints
                    (project_id, line_id, text_hash, speaker_id, emotion, speed, fx_hash, output_path, duration_seconds, wer, quality_score, validation_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    line_id,
                    text_hash,
                    speaker,
                    emotion,
                    speed,
                    fx_hash,
                    str(output_path),
                    duration_seconds,
                    wer,
                    quality_score,
                    validation_status,
                    now,
                ),
            )
            conn.commit()

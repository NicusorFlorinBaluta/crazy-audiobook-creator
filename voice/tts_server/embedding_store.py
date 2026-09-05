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
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import torch

logger = logging.getLogger(__name__)


class EmbeddingStore:
    """SQLite database store for PyTorch voice embeddings and audio fingerprints."""

    def __init__(self, db_path: str | Path = "voice_cache.db"):
        self.db_path = str(db_path)
        self._init_db()

    @contextmanager
    def _connect(self):
        """Create a database connection with WAL mode."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS voice_clone_prompts (
                    ref_audio_hash TEXT NOT NULL,
                    ref_text_hash TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    prompt_blob BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (ref_audio_hash, ref_text_hash, model_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS validation_results (
                    project_id TEXT NOT NULL,
                    line_id TEXT NOT NULL,
                    validation_fingerprint TEXT NOT NULL,
                    output_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, line_id)
                )
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(generation_fingerprints)").fetchall()}
            if "fingerprint_hash" not in columns:
                conn.execute("ALTER TABLE generation_fingerprints ADD COLUMN fingerprint_hash TEXT DEFAULT ''")
            if "output_hash" not in columns:
                conn.execute("ALTER TABLE generation_fingerprints ADD COLUMN output_hash TEXT DEFAULT ''")
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
        now = datetime.now(UTC).isoformat()

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
        now = datetime.now(UTC).isoformat()

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
    # Qwen voice-clone prompt cache
    # ------------------------------------------------------------------

    def get_voice_clone_prompt(
        self,
        ref_audio_path: str | Path,
        ref_text: str,
        model_id: str,
    ) -> list[dict[str, Any]] | None:
        """Load cached prompt tensors as plain dictionaries."""
        audio_hash = self.hash_file(ref_audio_path)
        if not audio_hash:
            return None
        ref_text_hash = self.hash_text(ref_text or "")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT prompt_blob FROM voice_clone_prompts
                WHERE ref_audio_hash = ? AND ref_text_hash = ? AND model_id = ?
                """,
                (audio_hash, ref_text_hash, model_id),
            ).fetchone()
        if not row:
            return None
        try:
            return torch.load(io.BytesIO(row[0]), map_location="cpu", weights_only=True)
        except TypeError:
            return torch.load(io.BytesIO(row[0]), map_location="cpu")
        except Exception as exc:
            logger.warning("Failed to load cached voice-clone prompt: %s", exc)
            return None

    def save_voice_clone_prompt(
        self,
        ref_audio_path: str | Path,
        ref_text: str,
        model_id: str,
        items: list[dict[str, Any]],
    ) -> None:
        audio_hash = self.hash_file(ref_audio_path)
        if not audio_hash:
            return
        ref_text_hash = self.hash_text(ref_text or "")
        buffer = io.BytesIO()
        torch.save(items, buffer)
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO voice_clone_prompts
                    (ref_audio_hash, ref_text_hash, model_id, prompt_blob, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    audio_hash,
                    ref_text_hash,
                    model_id,
                    buffer.getvalue(),
                    now,
                ),
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
        generation_context: dict[str, Any] | None = None,
    ) -> bool:
        """Check if a line needs to be re-synthesized based on fingerprint match."""
        if output_path and (not Path(output_path).exists() or Path(output_path).stat().st_size < 1000):
            return True

        expected_fingerprint = self._generation_fingerprint(
            text=text,
            speaker=speaker,
            emotion=emotion,
            speed=speed,
            fx_dict=fx_dict,
            generation_context=generation_context,
        )
        current_output_hash = self.hash_file(output_path) if output_path else ""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT fingerprint_hash, output_hash, validation_status
                FROM generation_fingerprints
                WHERE project_id = ? AND line_id = ?
                """,
                (project_id, line_id),
            ).fetchone()

        if not row:
            return True

        cached_fingerprint, cached_output_hash, validation_status = row
        return not (
            cached_fingerprint == expected_fingerprint
            and cached_output_hash
            and cached_output_hash == current_output_hash
            and validation_status in {"pass", "accepted_with_warning"}
        )

    def line_needs_synthesis(
        self,
        project_id: str,
        line_id: str,
        text: str,
        speaker: str,
        emotion: str = "",
        speed: float = 1.0,
        fx_dict: dict[str, Any] | None = None,
        output_path: str | Path | None = None,
        generation_context: dict[str, Any] | None = None,
    ) -> bool:
        """Return whether audio must be synthesized, independent of validation.

        A crash may occur after a valid WAV is written but before validation or
        the chapter manifest is committed. Such a WAV is safe to revalidate
        when its generation fingerprint and content hash still match; it is
        never treated as accepted until validation has its own matching record.
        """
        if output_path and (not Path(output_path).exists() or Path(output_path).stat().st_size < 1000):
            return True
        expected_fingerprint = self._generation_fingerprint(
            text=text,
            speaker=speaker,
            emotion=emotion,
            speed=speed,
            fx_dict=fx_dict,
            generation_context=generation_context,
        )
        current_output_hash = self.hash_file(output_path) if output_path else ""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT fingerprint_hash, output_hash
                FROM generation_fingerprints
                WHERE project_id = ? AND line_id = ?
                """,
                (project_id, line_id),
            ).fetchone()
        return not (row and row[0] == expected_fingerprint and row[1] and row[1] == current_output_hash)

    def save_synthesis_fingerprint(
        self,
        *,
        project_id: str,
        line_id: str,
        text: str,
        speaker: str,
        emotion: str,
        speed: float,
        fx_dict: dict[str, Any] | None,
        output_path: str | Path,
        duration_seconds: float,
        generation_context: dict[str, Any] | None = None,
    ) -> None:
        """Checkpoint a valid but not-yet-accepted synthesis artifact."""
        self.save_generation_fingerprint(
            project_id=project_id,
            line_id=line_id,
            text=text,
            speaker=speaker,
            emotion=emotion,
            speed=speed,
            fx_dict=fx_dict,
            output_path=output_path,
            duration_seconds=duration_seconds,
            wer=-1.0,
            quality_score=-1.0,
            validation_status="synthesized",
            generation_context=generation_context,
        )

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
        generation_context: dict[str, Any] | None = None,
    ) -> None:
        """Save line generation fingerprint after validation."""
        text_hash = self.hash_text(text)
        fx_hash = self.hash_text(json.dumps(fx_dict or {}, sort_keys=True))
        fingerprint_hash = self._generation_fingerprint(
            text=text,
            speaker=speaker,
            emotion=emotion,
            speed=speed,
            fx_dict=fx_dict,
            generation_context=generation_context,
        )
        output_hash = self.hash_file(output_path)
        now = datetime.now(UTC).isoformat()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO generation_fingerprints
                    (project_id, line_id, text_hash, speaker_id, emotion, speed,
                     fx_hash, output_path, duration_seconds, wer, quality_score,
                     validation_status, created_at, fingerprint_hash, output_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    fingerprint_hash,
                    output_hash,
                ),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Accepted validation cache (independent from synthesis identity)
    # ------------------------------------------------------------------

    def get_validation_result(
        self,
        *,
        project_id: str,
        line_id: str,
        output_path: str | Path,
        expected_text: str,
        validation_context: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return a cached accepted result when audio and validation match."""
        output_hash = self.hash_file(output_path)
        if not output_hash:
            return None
        fingerprint = self._validation_fingerprint(
            expected_text=expected_text,
            validation_context=validation_context,
        )
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT result_json, output_hash, validation_fingerprint
                FROM validation_results
                WHERE project_id = ? AND line_id = ?
                """,
                (project_id, line_id),
            ).fetchone()
        if not row or row[1] != output_hash or row[2] != fingerprint:
            return None
        try:
            result = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        if result.get("status") not in {"pass", "accepted_with_warning"}:
            return None
        return result

    def save_validation_result(
        self,
        *,
        project_id: str,
        line_id: str,
        output_path: str | Path,
        expected_text: str,
        validation_context: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Persist an accepted validation result for an unchanged WAV."""
        if result.get("status") not in {"pass", "accepted_with_warning"}:
            return
        output_hash = self.hash_file(output_path)
        if not output_hash:
            return
        fingerprint = self._validation_fingerprint(
            expected_text=expected_text,
            validation_context=validation_context,
        )
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO validation_results
                    (project_id, line_id, validation_fingerprint, output_hash,
                     result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    line_id,
                    fingerprint,
                    output_hash,
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            conn.commit()

    def _validation_fingerprint(
        self,
        *,
        expected_text: str,
        validation_context: dict[str, Any],
    ) -> str:
        payload = {
            "expected_text": expected_text,
            "context": validation_context,
        }
        return self.hash_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def _generation_fingerprint(
        self,
        *,
        text: str,
        speaker: str,
        emotion: str,
        speed: float,
        fx_dict: dict[str, Any] | None,
        generation_context: dict[str, Any] | None,
    ) -> str:
        from voice.tts_server.qwen3_engine import MOOD_TIER_VERSION

        payload = {
            "text": text,
            "speaker": speaker,
            "emotion": (emotion or "").strip().lower(),
            "speed": round(float(speed), 6),
            "fx": fx_dict or {},
            "context": generation_context or {},
            "mood_tier_version": MOOD_TIER_VERSION,
        }
        return self.hash_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

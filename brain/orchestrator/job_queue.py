"""Job Queue — SQLite-backed state persistence for the pipeline.

Stores project state, tracks pipeline progress, and enables
resume-after-crash functionality.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from shared.models import ProgressSnapshot

logger = logging.getLogger(__name__)


class JobState(StrEnum):
    """Job states (maps to PipelineStage)."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    PAUSED = "paused"


class JobQueue:
    """SQLite-backed job queue and state store."""

    def __init__(self, db_path: str = "pipeline_state.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Create the jobs table if it doesn't exist."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    project_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS quality_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    line_id TEXT NOT NULL,
                    chapter_number INTEGER,
                    attempt INTEGER DEFAULT 1,
                    wer REAL,
                    quality_score REAL,
                    status TEXT,
                    details TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (project_id) REFERENCES jobs(project_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_quality_project
                ON quality_logs(project_id, chapter_number)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS review_items (
                    project_id TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    disposition TEXT NOT NULL DEFAULT 'unreviewed',
                    note TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, item_type, item_id),
                    FOREIGN KEY (project_id) REFERENCES jobs(project_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_review_project
                ON review_items(project_id, item_type, disposition)
            """)
            conn.commit()

    @contextmanager
    def _connect(self):
        """Create a database connection."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Job management
    # ------------------------------------------------------------------

    def create_job(self, project_id: str, state: dict[str, Any]) -> None:
        """Create a new job entry."""
        state = dict(state)
        # All jobs created after the casting-review feature was introduced are
        # fail-closed. Legacy rows already in SQLite remain untouched.
        state.setdefault("voice_review_policy", "required_once")
        state.setdefault("voice_review_status", "pending")
        state.setdefault("voice_review_approved", False)
        state.setdefault("voice_review_approved_at", None)
        state.setdefault("voice_review_approved_revision", None)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (project_id, state, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (project_id, json.dumps(state, default=str), now, now),
            )
            conn.commit()

    def get_job(self, project_id: str) -> dict[str, Any]:
        """Get the current state of a job."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state, created_at, updated_at FROM jobs WHERE project_id = ?",
                (project_id,),
            ).fetchone()

        if row is None:
            raise KeyError(f"Job not found: {project_id}")

        state = json.loads(row[0])
        state["created_at"] = row[1]
        state["updated_at"] = row[2]
        return state

    def update_job(self, project_id: str, updates: dict[str, Any]) -> None:
        """Update job state with a serialized read-modify-write transaction."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state FROM jobs WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Job not found: {project_id}")
            current = json.loads(row[0])
            current.update(updates)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE project_id = ?",
                (json.dumps(current, default=str), now, project_id),
            )
            conn.commit()

    def update_progress(
        self,
        project_id: str,
        progress: ProgressSnapshot | dict[str, Any],
    ) -> None:
        """Persist one canonical progress snapshot and compatibility fields."""
        snapshot = (
            progress
            if isinstance(progress, ProgressSnapshot)
            else ProgressSnapshot.model_validate(progress)
        )
        payload = snapshot.model_dump(mode="json")
        updates: dict[str, Any] = {
            "progress": payload,
            "current_work_phase": snapshot.phase,
            "current_line_id": snapshot.line_id or None,
            "current_line": snapshot.line_position,
            "current_attempt": snapshot.attempt,
            "current_cache_hit": snapshot.cache_hit,
            "eta_seconds": snapshot.eta_seconds,
            "last_activity_at": payload["updated_at"],
        }
        if snapshot.chapter is not None:
            updates["current_chapter"] = snapshot.chapter
        self.update_job(project_id, updates)

    def list_jobs(self) -> list[dict[str, Any]]:
        """List all jobs with their states."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT project_id, state, created_at, updated_at FROM jobs ORDER BY updated_at DESC"
            ).fetchall()

        return [
            {
                "project_id": row[0],
                **json.loads(row[1]),
                "created_at": row[2],
                "updated_at": row[3],
            }
            for row in rows
        ]

    def delete_job(self, project_id: str) -> None:
        """Delete a job and its quality logs."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute(
                "SELECT 1 FROM jobs WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(f"Job not found: {project_id}")
            conn.execute("DELETE FROM quality_logs WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM review_items WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM jobs WHERE project_id = ?", (project_id,))
            conn.commit()

    # ------------------------------------------------------------------
    # Human quality review
    # ------------------------------------------------------------------

    def set_review_item(
        self,
        project_id: str,
        item_type: str,
        item_id: str,
        disposition: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Create or replace a non-destructive human review disposition."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO review_items
                    (project_id, item_type, item_id, disposition, note, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, item_type, item_id) DO UPDATE SET
                    disposition=excluded.disposition,
                    note=excluded.note,
                    updated_at=excluded.updated_at
                """,
                (project_id, item_type, item_id, disposition, note, now),
            )
            conn.commit()
        return {
            "project_id": project_id,
            "item_type": item_type,
            "item_id": item_id,
            "disposition": disposition,
            "note": note,
            "updated_at": now,
        }

    def get_review_items(
        self,
        project_id: str,
        item_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return persisted human review dispositions for a project."""
        query = (
            "SELECT item_type, item_id, disposition, note, updated_at "
            "FROM review_items WHERE project_id = ?"
        )
        params: list[Any] = [project_id]
        if item_type:
            query += " AND item_type = ?"
            params.append(item_type)
        query += " ORDER BY item_type, item_id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "item_type": row[0],
                "item_id": row[1],
                "disposition": row[2],
                "note": row[3],
                "updated_at": row[4],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Quality logging
    # ------------------------------------------------------------------

    def log_quality(
        self,
        project_id: str,
        line_id: str,
        chapter_number: int,
        attempt: int,
        wer: float,
        quality_score: float,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Log a quality validation result."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO quality_logs
                    (project_id, line_id, chapter_number, attempt, wer, quality_score, status, details, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    line_id,
                    chapter_number,
                    attempt,
                    wer,
                    quality_score,
                    status,
                    json.dumps(details or {}),
                    now,
                ),
            )
            conn.commit()

    def clear_quality_logs(
        self,
        project_id: str,
        chapter_number: int | None = None,
    ) -> None:
        """Clear stale validation attempts before persisting a fresh run."""
        with self._connect() as conn:
            if chapter_number is None:
                conn.execute(
                    "DELETE FROM quality_logs WHERE project_id = ?",
                    (project_id,),
                )
            else:
                conn.execute(
                    "DELETE FROM quality_logs "
                    "WHERE project_id = ? AND chapter_number = ?",
                    (project_id, chapter_number),
                )
            conn.commit()

    def get_quality_report(self, project_id: str) -> list[dict[str, Any]]:
        """Get all quality logs for a project."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT line_id, chapter_number, attempt, wer, quality_score, status, details
                FROM quality_logs
                WHERE project_id = ?
                ORDER BY chapter_number, line_id, attempt
                """,
                (project_id,),
            ).fetchall()

        return [
            {
                "line_id": row[0],
                "chapter_number": row[1],
                "attempt": row[2],
                "wer": row[3],
                "quality_score": row[4],
                "status": row[5],
                "details": json.loads(row[6]) if row[6] else {},
            }
            for row in rows
        ]

    @staticmethod
    def _selected_quality_logs(
        logs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Choose the winning artifact attempt, with legacy max-attempt fallback."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in logs:
            grouped.setdefault(item["line_id"], []).append(item)
        selected: list[dict[str, Any]] = []
        for attempts in grouped.values():
            explicit = [
                item for item in attempts if item.get("details", {}).get("selected")
            ]
            selected.append(
                max(explicit or attempts, key=lambda item: int(item.get("attempt") or 1))
            )
        return selected

    @staticmethod
    def _quality_retry_count(logs: list[dict[str, Any]]) -> int:
        """Count attempted retries independently of which artifact won."""
        highest_attempt: dict[str, int] = {}
        for item in logs:
            line_id = str(item["line_id"])
            highest_attempt[line_id] = max(
                highest_attempt.get(line_id, 1),
                int(item.get("attempt") or 1),
            )
        return sum(max(0, attempt - 1) for attempt in highest_attempt.values())

    def get_chapter_quality_summary(self, project_id: str, chapter_number: int) -> dict[str, Any]:
        """Get aggregated quality metrics for a chapter."""
        logs = [
            item for item in self.get_quality_report(project_id)
            if item.get("chapter_number") == chapter_number
        ]
        final = self._selected_quality_logs(logs)
        if not final:
            return {}
        wers = [float(item.get("wer") or 0.0) for item in final]
        scores = [float(item.get("quality_score") or 0.0) for item in final]
        return {
            "total_segments": len(final),
            "passed": sum(item.get("status") == "pass" for item in final),
            "accepted_with_warning": sum(item.get("status") == "accepted_with_warning" for item in final),
            "failed": sum(item.get("status") == "fail" for item in final),
            "flagged": sum(item.get("status") == "flagged" for item in final),
            "average_wer": sum(wers) / len(wers),
            "worst_wer": max(wers, default=0.0),
            "average_quality_score": sum(scores) / len(scores),
            "total_retries": self._quality_retry_count(logs),
        }

    def get_project_quality_summary(self, project_id: str) -> dict[str, Any]:
        """Aggregate only each line's final validation attempt for a project."""
        logs = self.get_quality_report(project_id)
        final = self._selected_quality_logs(logs)
        if not final:
            return {}
        wers = [float(item.get("wer") or 0.0) for item in final]
        return {
            "total_segments": len(final),
            "passed": sum(item.get("status") == "pass" for item in final),
            "accepted_with_warning": sum(item.get("status") == "accepted_with_warning" for item in final),
            "failed": sum(item.get("status") == "fail" for item in final),
            "flagged": sum(item.get("status") == "flagged" for item in final),
            "average_wer": sum(wers) / len(wers),
            "worst_wer": max(wers, default=0.0),
            "total_retries": self._quality_retry_count(logs),
        }

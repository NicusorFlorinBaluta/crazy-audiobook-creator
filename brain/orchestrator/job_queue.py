"""Job Queue — SQLite-backed state persistence for the pipeline.

Stores project state, tracks pipeline progress, and enables
resume-after-crash functionality.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

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
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        """Create a database connection."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ------------------------------------------------------------------
    # Job management
    # ------------------------------------------------------------------

    def create_job(self, project_id: str, state: dict[str, Any]) -> None:
        """Create a new job entry."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO jobs (project_id, state, created_at, updated_at)
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
        """Update job state with partial updates (merge)."""
        current = self.get_job(project_id)
        current.update(updates)
        now = datetime.now(timezone.utc).isoformat()

        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE project_id = ?",
                (json.dumps(current, default=str), now, project_id),
            )
            conn.commit()

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
            conn.execute("DELETE FROM quality_logs WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM jobs WHERE project_id = ?", (project_id,))
            conn.commit()

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

    def get_chapter_quality_summary(self, project_id: str, chapter_number: int) -> dict[str, Any]:
        """Get aggregated quality metrics for a chapter."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status = 'pass' THEN 1 ELSE 0 END) as passed,
                    SUM(CASE WHEN status = 'fail' THEN 1 ELSE 0 END) as failed,
                    SUM(CASE WHEN status = 'flagged' THEN 1 ELSE 0 END) as flagged,
                    AVG(wer) as avg_wer,
                    MAX(wer) as worst_wer,
                    AVG(quality_score) as avg_score,
                    SUM(CASE WHEN attempt > 1 THEN 1 ELSE 0 END) as retries
                FROM quality_logs
                WHERE project_id = ? AND chapter_number = ?
                  AND attempt = (
                    SELECT MAX(attempt) FROM quality_logs q2
                    WHERE q2.project_id = quality_logs.project_id
                      AND q2.line_id = quality_logs.line_id
                  )
                """,
                (project_id, chapter_number),
            ).fetchone()

        if row is None:
            return {}

        return {
            "total_segments": row[0],
            "passed": row[1],
            "failed": row[2],
            "flagged": row[3],
            "average_wer": row[4] or 0.0,
            "worst_wer": row[5] or 0.0,
            "average_quality_score": row[6] or 0.0,
            "total_retries": row[7] or 0,
        }

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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS external_validation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    decision TEXT NOT NULL,
                    confidence REAL,
                    reason TEXT NOT NULL DEFAULT '',
                    latency_ms INTEGER,
                    details TEXT NOT NULL DEFAULT '{}',
                    human_disposition TEXT,
                    human_value TEXT,
                    human_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (project_id) REFERENCES jobs(project_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_external_validation_project
                ON external_validation_events(project_id, item_type, item_id)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS playback_progress (
                    project_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL DEFAULT '',
                    chapter_number INTEGER NOT NULL DEFAULT 1,
                    position_ms INTEGER NOT NULL DEFAULT 0,
                    playback_speed REAL NOT NULL DEFAULT 1.0,
                    is_completed INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (project_id) REFERENCES jobs(project_id)
                )
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
            conn.execute("DELETE FROM external_validation_events WHERE project_id = ?", (project_id,))
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

    def log_external_validation(
        self, project_id: str, item_type: str, item_id: str, provider: str,
        model: str, decision: str, confidence: float | None, reason: str,
        latency_ms: int | None = None, details: dict[str, Any] | None = None,
    ) -> int:
        """Append one immutable machine-decision event."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO external_validation_events
                (project_id,item_type,item_id,provider,model,decision,confidence,reason,latency_ms,details,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (project_id, item_type, item_id, provider, model, decision,
                 confidence, reason, latency_ms, json.dumps(details or {}), now),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def reconcile_external_validation(
        self, project_id: str, item_type: str, item_id: str,
        human_disposition: str, human_value: str = "",
    ) -> None:
        """Attach a human outcome to all prior machine decisions for an item."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """UPDATE external_validation_events
                SET human_disposition=?, human_value=?, human_at=?
                WHERE project_id=? AND item_type=? AND item_id=? AND human_at IS NULL""",
                (human_disposition, human_value, now, project_id, item_type, item_id),
            )
            conn.commit()

    def get_external_validation_events(self, project_id: str) -> list[dict[str, Any]]:
        """Return the external decision ledger newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id,item_type,item_id,provider,model,decision,confidence,reason,
                latency_ms,details,human_disposition,human_value,human_at,created_at
                FROM external_validation_events WHERE project_id=? ORDER BY id DESC""",
                (project_id,),
            ).fetchall()
        keys = ("id","item_type","item_id","provider","model","decision","confidence",
                "reason","latency_ms","details","human_disposition","human_value","human_at","created_at")
        result = []
        for row in rows:
            item = dict(zip(keys, row))
            item["details"] = json.loads(item["details"] or "{}")
            result.append(item)
        return result

    @staticmethod
    def _calibration_summary(
        outcomes: list[tuple[float, bool]], minimum_samples: int,
    ) -> dict[str, Any]:
        """Summarize confidence-versus-human agreement without tuning policy."""
        sample_count = len(outcomes)
        bins = []
        for low in (0.0, 0.5, 0.7, 0.85):
            high = {0.0: 0.5, 0.5: 0.7, 0.7: 0.85, 0.85: 1.01}[low]
            rows = [agree for confidence, agree in outcomes if low <= confidence < high]
            bins.append({"low": low, "high": min(high, 1.0), "samples": len(rows),
                         "agreement": sum(rows) / len(rows) if rows else None})
        recommended = None
        if sample_count >= minimum_samples:
            lowest_observed = min(confidence for confidence, _ in outcomes)
            for threshold in (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95):
                if threshold < lowest_observed:
                    continue
                rows = [agree for confidence, agree in outcomes if confidence >= threshold]
                if len(rows) >= 10 and sum(rows) / len(rows) >= 0.95:
                    recommended = threshold
                    break
        brier_score = (
            sum((confidence - float(agrees)) ** 2 for confidence, agrees in outcomes)
            / sample_count
            if sample_count else None
        )
        populated_bins = [row for row in bins if row["samples"]]
        expected_calibration_error = (
            sum(
                row["samples"]
                / sample_count
                * abs(
                    sum(
                        confidence
                        for confidence, _ in outcomes
                        if row["low"] <= confidence < (row["high"] if row["high"] < 1 else 1.01)
                    )
                    / row["samples"]
                    - float(row["agreement"])
                )
                for row in populated_bins
            )
            if sample_count else None
        )
        return {"sample_count": sample_count, "minimum_samples": minimum_samples,
                "samples_needed": max(0, minimum_samples - sample_count),
                "ready": sample_count >= minimum_samples, "bins": bins,
                "brier_score": brier_score,
                "expected_calibration_error": expected_calibration_error,
                "recommended_auto_accept_threshold": recommended,
                "applied_automatically": False}

    def external_validation_calibration(self, project_id: str, minimum_samples: int = 25) -> dict[str, Any]:
        """Compute project and purpose-matched pooled calibration metrics.

        Pooling is deliberately segmented by provider, model, item purpose, and
        schema/prompt revision so an accurate audio validator cannot lend false
        confidence to a different attribution workflow. Recommendations remain
        advisory and never alter configured thresholds.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT project_id,item_type,item_id,provider,model,decision,
                confidence,details,human_disposition,id
                FROM external_validation_events
                WHERE human_disposition IS NOT NULL
                ORDER BY id DESC"""
            ).fetchall()

        groups: dict[tuple[str, str, str, str], list[tuple[float, bool]]] = {}
        project_outcomes: list[tuple[float, bool]] = []
        seen: set[tuple[str, str, str]] = set()
        for row in rows:
            (
                event_project, item_type, item_id, provider, model, decision,
                confidence, details_json, human, _event_id,
            ) = row
            item_key = (str(event_project), str(item_type), str(item_id))
            if item_key in seen or confidence is None:
                continue
            seen.add(item_key)
            decision_norm = str(decision or "").lower()
            human_norm = str(human or "").lower()
            agrees = (
                decision_norm in {"accept", "accepted", "resolved"}
                and human_norm in {"acceptable", "resolved"}
            ) or (
                decision_norm in {"reject", "regenerate"}
                and human_norm in {"regenerate", "source_tts_issue"}
            )
            outcome = (max(0.0, min(1.0, float(confidence))), agrees)
            if event_project == project_id:
                project_outcomes.append(outcome)
            try:
                details = json.loads(details_json or "{}")
            except (TypeError, json.JSONDecodeError):
                details = {}
            revision = str(
                details.get("purpose_version")
                or details.get("schema_revision")
                or details.get("schema")
                or "legacy"
            )
            key = (
                str(provider or "unknown"),
                str(model or "unknown"),
                str(item_type or "unknown"),
                revision,
            )
            groups.setdefault(key, []).append(outcome)

        result = self._calibration_summary(project_outcomes, minimum_samples)
        result["pooled_groups"] = [
            {
                "provider": key[0],
                "model": key[1],
                "purpose": key[2],
                "revision": key[3],
                **self._calibration_summary(outcomes, minimum_samples),
            }
            for key, outcomes in sorted(groups.items())
        ]
        result["pooling_policy"] = "provider_model_purpose_revision"
        return result

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

    def delete_review_item(self, project_id: str, item_type: str, item_id: str) -> None:
        """Remove a review marker after its requested replacement validates."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM review_items WHERE project_id = ? AND item_type = ? AND item_id = ?",
                (project_id, item_type, item_id),
            )
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

    def set_playback_progress(
        self,
        project_id: str,
        client_id: str = "",
        chapter_number: int = 1,
        position_ms: int = 0,
        playback_speed: float = 1.0,
        is_completed: bool = False,
    ) -> dict[str, Any]:
        """Save playback progress for a project."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO playback_progress (project_id, client_id, chapter_number, position_ms, playback_speed, is_completed, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    client_id = excluded.client_id,
                    chapter_number = excluded.chapter_number,
                    position_ms = excluded.position_ms,
                    playback_speed = excluded.playback_speed,
                    is_completed = excluded.is_completed,
                    updated_at = excluded.updated_at
                """,
                (project_id, client_id, int(chapter_number), int(position_ms), float(playback_speed), 1 if is_completed else 0, now),
            )
            conn.commit()
        return {
            "project_id": project_id,
            "client_id": client_id,
            "chapter_number": int(chapter_number),
            "position_ms": int(position_ms),
            "playback_speed": float(playback_speed),
            "is_completed": bool(is_completed),
            "updated_at": now,
        }

    def get_playback_progress(self, project_id: str) -> dict[str, Any] | None:
        """Get the latest saved playback progress for a project."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT client_id, chapter_number, position_ms, playback_speed, is_completed, updated_at
                FROM playback_progress WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "project_id": project_id,
            "client_id": row[0],
            "chapter_number": int(row[1]),
            "position_ms": int(row[2]),
            "playback_speed": float(row[3]),
            "is_completed": bool(row[4]),
            "updated_at": row[5],
        }

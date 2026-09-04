"""External-validation status, events and retry.

Split out of `main.py`. Shared state and helpers come from `..runtime`;
nothing here imports `main`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from brain.dashboard.api import runtime
from shared import paths as shared_paths
from shared.artifacts import atomic_write_json, atomic_write_text
from shared.cache import cache_service
from shared.models import ScriptChapter

logger = logging.getLogger(__name__)

router = APIRouter()


async def get_external_validation_events(project_id: str):
    """Expose the audit ledger and passive confidence calibration."""
    runtime.require_job(project_id)
    return {
        "events": runtime.job_queue.get_external_validation_events(project_id),
        "calibration": runtime.job_queue.external_validation_calibration(project_id),
    }


async def get_external_validation_status(project_id: str):
    """Return readiness without exposing API keys or Gemini conversation URLs."""
    runtime.require_job(project_id)
    config = runtime.load_config().get("external_validation", {})
    api = config.get("api", {}) if isinstance(config, dict) else {}
    browser = config.get("browser", {}) if isinstance(config, dict) else {}
    key_name = str(api.get("api_key_env", "GEMINI_API_KEY"))
    profile = Path(str(browser.get("profile_dir", "brain/projects/.gemini-browser-profile")))
    state_path = runtime.project_dir(project_id) / "external_validation" / "browser_state.json"
    purposes: list[str] = []
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            purposes = sorted(state.get("conversations", {}))
        except (OSError, json.JSONDecodeError, TypeError):
            purposes = []
    return {
        "enabled": bool(config.get("enabled", False)),
        "auto_accept_confidence": config.get("auto_accept_confidence", 0.9),
        "manual_review_confidence": config.get("manual_review_confidence", 0.75),
        "api": {
            "enabled": bool(api.get("enabled", True)),
            "configured": bool(os.getenv(key_name, "").strip()),
            "triage_model": api.get("triage_model"),
            "adjudication_model": api.get("adjudication_model"),
        },
        "browser": {
            "enabled": bool(browser.get("enabled", False)),
            "profile_initialized": profile.is_dir() and any(profile.iterdir()),
            "persistent_conversations": purposes,
        },
        "provider_health": (runtime.pipeline.external_validator.health_snapshot() if runtime.pipeline else {}),
        "calibration": runtime.job_queue.external_validation_calibration(project_id),
    }


_retry_locks: dict[str, asyncio.Lock] = {}


async def retry_external_validation(
    project_id: str,
    reset_circuit: bool = False,
):
    """Retry external validation with Gemini for unresolved attribution items."""
    runtime.require_job(project_id)
    if not runtime.pipeline:
        raise HTTPException(status_code=503, detail="Server not initialized")

    project_dir = runtime.project_dir(project_id)
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

    lock = _retry_locks.setdefault(project_id, asyncio.Lock())
    if lock.locked():
        raise HTTPException(
            status_code=409,
            detail="A validation retry is already in progress for this project",
        )

    async with lock:
        # Load character registry
        chars_path = project_dir / "characters.json"
        character_context = {}
        character_ids: set[str] = set()
        if chars_path.is_file():
            try:
                chars_data = json.loads(chars_path.read_text(encoding="utf-8"))
                char_map = chars_data.get("characters", {})
                character_ids = set(char_map.keys())
                character_context = {
                    cid: {
                        "id": cid,
                        "name": c.get("name", cid),
                        "aliases": c.get("aliases", []),
                        "gender": c.get("gender", "neutral"),
                        "age_range": c.get("age_range", "adult"),
                    }
                    for cid, c in char_map.items()
                }
            except Exception as exc:
                logger.warning("Could not read characters for retry: %s", exc)

        # Load chapter scripts
        scripts_dir = project_dir / "script"
        if not scripts_dir.is_dir():
            raise HTTPException(status_code=404, detail="No chapter scripts found")

        chapter_scripts: list[ScriptChapter] = []
        for path in sorted(scripts_dir.glob("chapter_*.json")):
            if path.name.endswith(".meta.json"):
                continue
            try:
                chapter_scripts.append(ScriptChapter.model_validate_json(path.read_text(encoding="utf-8")))
            except Exception as exc:
                logger.warning("Could not read script %s for validation retry: %s", path.name, exc)

        if not chapter_scripts:
            raise HTTPException(status_code=404, detail="No valid chapter scripts found to retry")

        # If requested, reset circuit breaker health
        if reset_circuit:
            health_path = project_dir / ".external_validation_health.json"
            global_health_path = shared_paths.PROJECTS_DIR / ".external_validation_health.json"
            for hp in (health_path, global_health_path):
                try:
                    hp.unlink(missing_ok=True)
                except Exception:
                    pass

        # Run resolve_attributions in a background thread to keep event loop responsive
        escalation = await asyncio.to_thread(
            runtime.pipeline.external_validator.resolve_attributions,
            project_dir=project_dir,
            chapters=chapter_scripts,
            character_ids=character_ids,
            character_context=character_context,
        )

        # Persist modified chapter scripts
        book_script_chapters = []
        total_lines = 0
        unresolved_count = 0
        for script in chapter_scripts:
            script_path = scripts_dir / f"chapter_{script.chapter_number:03d}.json"
            atomic_write_text(script_path, script.model_dump_json(indent=2))
            book_script_chapters.append(script.model_dump(mode="json"))
            total_lines += len(script.lines)
            unresolved_count += sum(1 for line in script.lines if line.attribution_review_required)

        # Update book_script.json (merge to preserve top-level metadata)
        book_script_path = project_dir / "book_script.json"
        existing_book_script: dict[str, Any] = {}
        if book_script_path.is_file():
            try:
                existing_book_script = json.loads(book_script_path.read_text(encoding="utf-8"))
            except Exception:
                existing_book_script = {}
        existing_book_script["chapters"] = book_script_chapters
        existing_book_script["total_lines"] = total_lines
        atomic_write_json(book_script_path, existing_book_script)

        # Invalidate project caches
        cache_service.delete(
            f"script_chapters:{project_id}",
            f"script_line_index:{project_id}",
            f"script_summary:{project_id}",
            f"script_review:{project_dir.resolve()}",
            f"pronunciation_inv:{project_dir.resolve()}",
        )

        return {
            "status": "success",
            "attempted": escalation.get("attempted", 0),
            "resolved": escalation.get("resolved", 0),
            "manual_review": escalation.get("manual_review", unresolved_count),
            "unresolved_remaining": unresolved_count,
            "provider_health": runtime.pipeline.external_validator.health_snapshot(),
        }

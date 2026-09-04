"""Process-wide runtime objects and the helpers every router needs.

`pipeline` and `job_queue` are constructed in the dashboard's `lifespan`, not at
import time, so they cannot be imported by value -- a router doing
`from .main import pipeline` would capture `None` forever. They live here as
module attributes instead, and callers read them through the module
(`runtime.pipeline`) so they always see the value `lifespan` installed.

`app.state` carries the same objects for anything holding a `Request`. This
module exists for the large amount of route code that does not.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from shared import paths as shared_paths

# Installed by `main.lifespan` at startup. `None` before then, and in any test
# that imports a router without booting the application.
pipeline: Any = None
job_queue: Any = None

# project_id -> the asyncio Task running its pipeline, when one is running.
running_tasks: dict[str, asyncio.Task] = {}


def bind(*, pipeline_obj: Any, job_queue_obj: Any) -> None:
    """Install the runtime objects. Called once, from `lifespan`."""
    global pipeline, job_queue
    pipeline = pipeline_obj
    job_queue = job_queue_obj


def project_dir(project_id: str) -> Path:
    """Resolve a project directory, refusing anything outside the root.

    The containment check is the reason this is a helper rather than a join:
    `project_id` arrives from the URL, so `../` must not escape.
    """
    root = shared_paths.PROJECTS_DIR.resolve()
    candidate = (root / project_id).resolve()
    if not candidate.is_relative_to(root) or candidate == root:
        raise HTTPException(status_code=400, detail="Invalid project ID")
    return candidate


def workspace_project_dir(project_id: str) -> Path:
    """As `project_dir`, for the audio workspace root."""
    root = shared_paths.WORKSPACE_DIR.resolve()
    candidate = (root / project_id).resolve()
    if not candidate.is_relative_to(root) or candidate == root:
        raise HTTPException(status_code=400, detail="Invalid project ID")
    return candidate


def require_job(project_id: str) -> dict[str, Any]:
    """Return a project's job state, or raise the right HTTP error."""
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    try:
        return job_queue.get_job(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc


def require_project_stopped(project_id: str) -> dict[str, Any]:
    """Reject mutations that could race a live pipeline worker."""
    state = require_job(project_id)
    task = running_tasks.get(project_id)
    if bool(state.get("running")) or (task is not None and not task.done()):
        raise HTTPException(
            status_code=409,
            detail="Stop the pipeline before changing this project",
        )
    return state


def pronunciation_llm():
    """Return the pipeline's Ollama client, if one is available.

    Routing recommendation lookups through it gives them the retry budget,
    repetition-loop detection and cooperative cancellation that a bare HTTP
    call to Ollama does not have.
    """
    return getattr(pipeline, "ollama", None) if pipeline else None

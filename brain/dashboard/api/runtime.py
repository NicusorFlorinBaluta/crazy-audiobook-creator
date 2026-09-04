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
import logging
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException

from shared import paths as shared_paths

logger = logging.getLogger(__name__)

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


def load_config(config_path: str = "brain/config.yaml") -> dict[str, Any]:
    """Load a YAML config, resolving a relative path from the repository root.

    The default was working-directory relative, so a dashboard started from
    anywhere but the repository root silently loaded an empty config -- every
    setting falling back to its in-code default with no error.
    """
    path = Path(config_path)
    if not path.is_absolute():
        path = shared_paths.REPO_ROOT / path
    if path.exists():
        with open(path, encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    return {}


# ---------------------------------------------------------------------------
# Pipeline start, inverted
# ---------------------------------------------------------------------------
#
# `start_pipeline` is a route handler in `main`, but several things that are
# not routes need to start a run -- the scheduler, the deploy-resume path, and
# `schedule_resume_after_reviews` below. Importing it from `main` would make
# every such module depend on the whole 4,000-line application, which is the
# coupling the router split exists to remove.
#
# `main` registers its implementation here at import instead, and callers ask
# `runtime` to start a pipeline without learning where that lives.

_pipeline_starter: Any = None


def register_pipeline_starter(starter: Any) -> None:
    """Install the coroutine that starts a pipeline run. Called once, by `main`."""
    global _pipeline_starter
    _pipeline_starter = starter


async def start_pipeline(project_id: str, **kwargs: Any) -> Any:
    """Start a pipeline run through whatever `main` registered."""
    if _pipeline_starter is None:
        raise HTTPException(status_code=503, detail="Server not initialized")
    return await _pipeline_starter(project_id, **kwargs)


def schedule_resume_after_reviews(project_id: str) -> bool:
    """Start a waiting project once its last blocking review is resolved.

    Lives here rather than in `main` because both the monolith and the quality
    router need it, and it is the only thing either of them needed
    `start_pipeline` for.
    """
    from brain.orchestrator.review_gate import collect_review_gate
    from shared.constants import PipelineStage

    if not job_queue:
        return False
    state = job_queue.get_job(project_id)
    if state.get("status") != PipelineStage.WAITING_FOR_REVIEW.value:
        return False
    gate = collect_review_gate(project_id, project_dir(project_id), job_queue)
    if gate.blocking_items:
        return False

    async def resume() -> None:
        try:
            await start_pipeline(project_id, override_schedule=True)
        except HTTPException as exc:
            logger.warning("Automatic review resume skipped for %s: %s", project_id, exc.detail)

    asyncio.create_task(resume())
    return True

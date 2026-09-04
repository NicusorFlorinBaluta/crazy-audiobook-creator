"""Repository-root-relative path resolution and a process-cached config reader.

Why this module exists
----------------------

Roughly thirty call sites across ``brain/`` and ``voice/`` resolved paths as
bare relative literals -- ``Path("brain/projects")``, ``Path("workspace")``,
``Path("voice/config.yaml")``. Those depend on the process working directory.
Every supported launcher happens to start from the repository root, so it
worked, but nothing enforced or documented it and the failure mode was silent:
``shared/pronunciation.py`` wrapped its config read in ``except Exception:
pass`` and would quietly fall back to a hardcoded model tag, so a dashboard
started from another directory could resolve pronunciations on a different
model than the one scripting the book.

``voice/config.yaml`` was additionally re-read from disk at seven separate call
sites. Beyond redundant I/O, an edit mid-run meant different subsystems
observed different values within a single chapter -- very hard to diagnose from
artifacts alone.

Resolving from ``__file__`` removes the working-directory dependency entirely,
and the cached readers give each config file one value per process.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml

# shared/paths.py -> shared/ -> <repo root>
REPO_ROOT = Path(__file__).resolve().parents[1]

BRAIN_DIR = REPO_ROOT / "brain"
VOICE_DIR = REPO_ROOT / "voice"
BRAIN_CONFIG_PATH = BRAIN_DIR / "config.yaml"
VOICE_CONFIG_PATH = VOICE_DIR / "config.yaml"
PROJECTS_DIR = BRAIN_DIR / "projects"
WORKSPACE_DIR = REPO_ROOT / "workspace"
VOICE_LIBRARY_DIR = REPO_ROOT / "voice_library"


def repo_path(*parts: str) -> Path:
    """Return a path beneath the repository root, independent of the CWD."""
    return REPO_ROOT.joinpath(*parts)


def project_dir(project_id: str) -> Path:
    """Return the brain-side project directory for `project_id`."""
    return PROJECTS_DIR / project_id


def workspace_project_dir(project_id: str) -> Path:
    """Return the workspace (audio intermediates) directory for `project_id`."""
    return WORKSPACE_DIR / project_id


# --- Cached configuration -------------------------------------------------

_config_cache: dict[str, dict[str, Any]] = {}
_config_cache_lock = threading.Lock()


def load_yaml_config(path: str | Path, *, refresh: bool = False) -> dict[str, Any]:
    """Read a YAML config once per process.

    Passing ``refresh=True`` re-reads from disk; use it only where an operator
    has explicitly edited configuration through the dashboard and the new value
    is meant to take effect immediately. Ordinary readers should take the cached
    value so a mid-run edit cannot make two subsystems disagree within one
    chapter.
    """
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = REPO_ROOT / resolved
    key = str(resolved)
    if not refresh:
        with _config_cache_lock:
            cached = _config_cache.get(key)
        if cached is not None:
            return cached
    if resolved.is_file():
        value = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    else:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{resolved} must contain a YAML mapping")
    with _config_cache_lock:
        _config_cache[key] = value
    return value


def voice_config(*, refresh: bool = False) -> dict[str, Any]:
    """Return the Voice service configuration."""
    return load_yaml_config(VOICE_CONFIG_PATH, refresh=refresh)


def brain_config(*, refresh: bool = False) -> dict[str, Any]:
    """Return the Brain/dashboard configuration."""
    return load_yaml_config(BRAIN_CONFIG_PATH, refresh=refresh)


def clear_config_cache() -> None:
    """Drop every cached config. Intended for tests."""
    with _config_cache_lock:
        _config_cache.clear()

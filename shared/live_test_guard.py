"""Explicit opt-in guard for scripts that may load large models."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def require_model_opt_in(argv: Sequence[str]) -> list[str]:
    """Remove --allow-models or abort before a benchmark loads a model."""
    values = list(argv)
    if "--allow-models" not in values:
        raise SystemExit(
            "Refusing to load models without --allow-models. Run this only "
            "during an approved live-test window."
        )
    values.remove("--allow-models")
    return values


def add_model_opt_in(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-models",
        action="store_true",
        help="Explicitly permit loading GPU/LLM models for this run",
    )

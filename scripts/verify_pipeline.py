"""Tiered, non-destructive verification entry point.

Static and artifact tiers never start models. GPU/chapter/full tiers are
deliberately refused unless --allow-models is passed, making expensive tests an
explicit operator decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.runtime_preflight import collect_runtime_report

MODEL_TIERS = {"gpu-smoke", "chapter", "full"}


def _run(command: list[str], cwd: Path) -> dict[str, Any]:
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-4000:],
        "stderr_tail": result.stderr[-4000:],
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_control_state(root: Path) -> dict[str, Any]:
    """Capture the exact candidate identity without mutating the repository."""
    commands = {
        "commit": ["git", "rev-parse", "HEAD"],
        "branch": ["git", "branch", "--show-current"],
        "status": ["git", "status", "--porcelain", "--untracked-files=normal"],
    }
    values: dict[str, str] = {}
    errors: dict[str, str] = {}
    for field, command in commands.items():
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            values[field] = result.stdout.strip()
        else:
            errors[field] = (result.stderr or result.stdout).strip()
    status = values.get("status", "")
    return {
        "commit": values.get("commit"),
        "branch": values.get("branch"),
        "dirty": bool(status),
        "status": status.splitlines(),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tier",
        choices=("static", "artifact", "gpu-smoke", "chapter", "full"),
        default="static",
    )
    parser.add_argument("--project-dir", type=Path)
    parser.add_argument("--allow-models", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.tier in MODEL_TIERS and not args.allow_models:
        parser.error(
            f"tier {args.tier!r} may load models; pass --allow-models only during an approved live-test window"
        )

    report: dict[str, Any] = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "tier": args.tier,
        "source_control": _source_control_state(root),
        "runtime": collect_runtime_report(),
        "checks": [],
        "artifacts": [],
    }
    if args.tier == "static":
        report["checks"].append(
            _run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                root,
            )
        )
        report["checks"].append(
            _run(
                [sys.executable, "-m", "compileall", "-q", "brain", "voice", "shared", "scripts"],
                root,
            )
        )
        node_files = (
            "brain/dashboard/frontend/js/app.js",
            "brain/dashboard/frontend/js/pipeline.js",
            "brain/dashboard/frontend/js/script-viewer.js",
            "desktop/main.js",
        )
        for relative in node_files:
            report["checks"].append(_run(["node", "--check", relative], root))
        report["checks"].append(_run([sys.executable, "scripts/check_markdown_links.py"], root))
    elif args.tier == "artifact":
        if not args.project_dir:
            parser.error("--project-dir is required for artifact verification")
        project_dir = args.project_dir.resolve()
        candidates = sorted(project_dir.glob("*.m4b"))
        for artifact in candidates:
            report["artifacts"].append(
                {
                    "path": str(artifact),
                    "size_bytes": artifact.stat().st_size,
                    "sha256": _sha256(artifact),
                }
            )
        if not candidates:
            report["checks"].append({"returncode": 1, "error": "No M4B artifact found"})
    else:
        report["checks"].append(
            {
                "returncode": 3,
                "error": (
                    "Model tier is intentionally a tomorrow/live-test hook; "
                    "invoke the project-specific runner during that window"
                ),
            }
        )

    report["ok"] = all(check.get("returncode", 1) == 0 for check in report["checks"])
    output = args.output or root / "verification-manifest.json"
    output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(output)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Sync reading assets (lyrics & reader JSONs) to 24/7 NAS storage.

Generates synchronized lyrics and ebook reader JSON files for each chapter of a book
and uploads them atomically to /mnt/nas/media/crazybooks/{project_id}/lyrics/ and /reader/.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from brain.orchestrator.nas_syncer import NASSyncer
from shared import paths as shared_paths

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sync_reading_assets")


def sync_project_reading_assets(project_id: str) -> None:
    project_dir = shared_paths.PROJECTS_DIR / project_id
    if not project_dir.is_dir():
        logger.error("Project directory does not exist: %s", project_dir)
        sys.exit(1)

    syncer = NASSyncer()
    if not syncer.is_configured:
        logger.error("NAS is not configured. Check environment credentials.")
        sys.exit(1)

    logger.info("Connecting to NAS %s...", syncer.host)
    with syncer.sftp_session() as sftp:
        nas_root = syncer.resolve_nas_root(sftp)
        proj_remote_dir = f"{nas_root}/{project_id}"
        logger.info("Syncing reading assets for '%s' to %s...", project_id, proj_remote_dir)
        syncer.sync_reading_assets(project_id, project_dir, sftp, proj_remote_dir)
        logger.info("Reading assets sync completed successfully!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync chapter lyrics and reader JSONs to NAS.")
    parser.add_argument(
        "--project-id",
        default="the-finest-edge-of-twilight-book",
        help="Project ID to sync reading assets for (default: the-finest-edge-of-twilight-book)",
    )
    args = parser.parse_args()
    sync_project_reading_assets(args.project_id)

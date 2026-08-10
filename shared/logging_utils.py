"""Small file-log lifecycle helpers used before managed services start."""

from __future__ import annotations

from pathlib import Path


def rotate_file(
    path: str | Path,
    *,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 3,
) -> bool:
    """Rotate a closed log file when it exceeds the configured size.

    Rotation is deliberately performed only before a managed subprocess opens
    the log, avoiding races with active writers. Returns whether rotation ran.
    """
    log_path = Path(path)
    if not log_path.is_file() or log_path.stat().st_size <= max_bytes:
        return False
    backup_count = max(1, int(backup_count))
    oldest = log_path.with_name(f"{log_path.name}.{backup_count}")
    if oldest.exists():
        oldest.unlink()
    for index in range(backup_count - 1, 0, -1):
        source = log_path.with_name(f"{log_path.name}.{index}")
        if source.exists():
            source.replace(log_path.with_name(f"{log_path.name}.{index + 1}"))
    log_path.replace(log_path.with_name(f"{log_path.name}.1"))
    return True

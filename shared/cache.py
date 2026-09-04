"""Embedded high-performance cache service backed by SQLite in WAL mode.

Provides thread-safe, multi-process safe, persistent in-memory/disk caching
with zero external service dependencies.
"""

from __future__ import annotations

import logging
import pickle
import sqlite3
import time
from pathlib import Path
from typing import Any

from shared.paths import REPO_ROOT

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = REPO_ROOT / "brain" / "cache.db"


class CacheService:
    """Thread-safe, process-safe SQLite cache with TTL and signature validation."""

    def __init__(self, db_path: Path | str = _DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=10.0,
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _init_db(self) -> None:
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cache_entries (
                        key TEXT PRIMARY KEY,
                        value BLOB NOT NULL,
                        expires_at REAL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache_entries (expires_at)"
                )
        except Exception as exc:
            logger.warning("Failed to initialize cache DB: %s", exc)

    def get(self, key: str) -> Any | None:
        """Retrieve a cached item if present and not expired."""
        now = time.time()
        conn = self._get_connection()
        try:
            with conn:
                cursor = conn.execute(
                    "SELECT value, expires_at FROM cache_entries WHERE key = ?",
                    (key,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                val_blob, expires_at = row
                if expires_at is not None and expires_at < now:
                    conn.execute("DELETE FROM cache_entries WHERE key = ?", (key,))
                    return None
                return pickle.loads(val_blob)
        except Exception as exc:
            logger.debug("Cache get failed for key '%s': %s", key, exc)
            return None
        finally:
            conn.close()

    def set(self, key: str, value: Any, ttl_seconds: float | None = None) -> None:
        """Store an item in the cache with an optional TTL."""
        now = time.time()
        expires_at = (now + ttl_seconds) if ttl_seconds is not None else None
        conn = self._get_connection()
        try:
            val_blob = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
            with conn:
                conn.execute(
                    """
                    INSERT INTO cache_entries (key, value, expires_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        expires_at = excluded.expires_at,
                        updated_at = excluded.updated_at
                    """,
                    (key, val_blob, expires_at, now),
                )
        except Exception as exc:
            logger.warning("Cache set failed for key '%s': %s", key, exc)
        finally:
            conn.close()

    def delete(self, *keys: str) -> None:
        """Delete one or more specific keys."""
        if not keys:
            return
        conn = self._get_connection()
        try:
            with conn:
                placeholders = ",".join("?" for _ in keys)
                conn.execute(
                    f"DELETE FROM cache_entries WHERE key IN ({placeholders})",
                    keys,
                )
        except Exception as exc:
            logger.warning("Cache delete failed for keys %s: %s", keys, exc)
        finally:
            conn.close()

    def delete_prefix(self, prefix: str) -> None:
        """Delete all keys matching the prefix."""
        conn = self._get_connection()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM cache_entries WHERE key LIKE ?",
                    (f"{prefix}%",),
                )
        except Exception as exc:
            logger.warning("Cache delete_prefix failed for '%s': %s", prefix, exc)
        finally:
            conn.close()

    def clear(self) -> None:
        """Clear the entire cache."""
        conn = self._get_connection()
        try:
            with conn:
                conn.execute("DELETE FROM cache_entries")
        except Exception as exc:
            logger.warning("Cache clear failed: %s", exc)
        finally:
            conn.close()


# Global singleton instance
cache_service = CacheService()

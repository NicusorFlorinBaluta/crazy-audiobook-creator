"""Safe planning, publication, and indexing for incremental audiobook parts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from pydantic import BaseModel, Field

from shared.single_instance import SingleInstanceLock
from shared.artifacts import atomic_write_text

logger = logging.getLogger(__name__)

DELIVERY_INDEX_SCHEMA_VERSION = 2
DELIVERY_EXPORT_REVISION = "incremental-m4b-v1"
DEFAULT_SUPERSEDED_REVISION_RETENTION = 2
DEFAULT_DELIVERY_HISTORY_RETENTION = 2


class DeliveryError(RuntimeError):
    """Base error for incremental-delivery state and publication failures."""


class DeliveryIndexCorruptError(DeliveryError):
    """Raised when neither the current nor previous delivery index is valid."""


class DeliveryIndexVersionError(DeliveryError):
    """Raised when an index comes from an unsupported future schema."""


class DeliveryPlanLockedError(DeliveryError):
    """Raised when published part boundaries would be changed."""


class DeliveryPackagingBusyError(DeliveryError):
    """Raised when another export/remux operation owns the project lock."""


class DeliveryPart(BaseModel):
    """The current authoritative revision of one published audiobook part."""

    delivery_id: str = Field(pattern=r"^part-\d{3,}$")
    ordinal: int = Field(ge=1)
    revision: int = Field(default=1, ge=1)
    chapter_numbers: list[int]
    artifact: str
    status: Literal["published", "stale"] = "published"
    stale_reason: str = ""
    published_at: str
    sha256: str
    bytes: int = Field(ge=1)
    duration_seconds: float = Field(gt=0.0)
    plan_fingerprint: str = ""
    master_manifest_hashes: dict[str, str] = Field(default_factory=dict)
    metadata_fingerprint: str = ""
    quality: dict[str, Any] = Field(default_factory=dict)
    superseded_artifacts: list[str] = Field(default_factory=list)


class DeliveryIndex(BaseModel):
    """Authoritative inventory and locked batch layout for one project."""

    schema_version: int = DELIVERY_INDEX_SCHEMA_VERSION
    plan_fingerprint: str = ""
    batch_size: int = Field(default=5, ge=1, le=20)
    chapter_numbers: list[int] = Field(default_factory=list)
    export_revision: str = DELIVERY_EXPORT_REVISION
    superseded_revision_retention: int = Field(
        default=DEFAULT_SUPERSEDED_REVISION_RETENTION,
        ge=0,
        le=20,
    )
    deliveries: list[DeliveryPart] = Field(default_factory=list)


class DeliveryBatch(BaseModel):
    """A deterministic, ordered batch of chapters to publish as one part."""

    delivery_id: str = Field(pattern=r"^part-\d{3,}$")
    ordinal: int = Field(ge=1)
    chapter_numbers: list[int]


class DeliveryManager:
    """Manage delivery plans, immutable revisions, and safe artifact access."""

    def __init__(self, project_dir: Path):
        self.project_dir = Path(project_dir)
        self.deliveries_dir = self.project_dir / "deliveries"

    @property
    def index_path(self) -> Path:
        return self.deliveries_dir / "index.json"

    @property
    def previous_index_path(self) -> Path:
        return self.deliveries_dir / "index.json.previous"

    def init_storage(self) -> None:
        self.deliveries_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _read_index(path: Path) -> DeliveryIndex:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Delivery index root must be an object")
        version = int(raw.get("schema_version", 1) or 1)
        if version > DELIVERY_INDEX_SCHEMA_VERSION:
            raise DeliveryIndexVersionError(
                f"Delivery index schema {version} is newer than supported "
                f"schema {DELIVERY_INDEX_SCHEMA_VERSION}"
            )
        if version < 1:
            raise ValueError(f"Invalid delivery index schema version: {version}")
        while version < DELIVERY_INDEX_SCHEMA_VERSION:
            if version == 1:
                raw.setdefault(
                    "superseded_revision_retention",
                    DEFAULT_SUPERSEDED_REVISION_RETENTION,
                )
                version = 2
                raw["schema_version"] = version
                continue
            raise DeliveryIndexVersionError(
                f"No delivery index migration is available from schema {version}"
            )
        return DeliveryIndex.model_validate(raw)

    def load_index(self) -> DeliveryIndex:
        """Load the index, recovering only from the last atomic backup."""
        if not self.index_path.exists():
            return DeliveryIndex()
        try:
            return self._read_index(self.index_path)
        except DeliveryIndexVersionError:
            raise
        except Exception as exc:
            logger.error("Failed to load delivery index %s: %s", self.index_path, exc)
            corrupt_path = self.index_path.with_name(
                f"index.json.corrupt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            )
            try:
                shutil.copy2(self.index_path, corrupt_path)
            except OSError:
                logger.exception("Could not preserve corrupt delivery index")
            if self.previous_index_path.is_file():
                try:
                    recovered = self._read_index(self.previous_index_path)
                    logger.warning("Recovered delivery index from %s", self.previous_index_path)
                    return recovered
                except Exception:
                    logger.exception("Previous delivery index is also invalid")
            raise DeliveryIndexCorruptError(
                f"Delivery index is corrupt; preserved copy: {corrupt_path.name}"
            ) from exc

    def save_index(self, index: DeliveryIndex) -> None:
        """Flush and atomically replace the index while retaining one backup."""
        self.init_storage()
        index.schema_version = DELIVERY_INDEX_SCHEMA_VERSION
        if self.index_path.is_file():
            try:
                shutil.copy2(self.index_path, self.previous_index_path)
            except OSError:
                pass
        atomic_write_text(self.index_path, index.model_dump_json(indent=2))

    @contextmanager
    def packaging_lock(
        self,
        *,
        wait: bool = False,
        timeout_seconds: float = 300.0,
    ) -> Iterator[None]:
        """Serialize publication, export, metadata remux, reset, and cleanup."""
        lock_key = hashlib.sha256(
            str(self.project_dir.resolve()).casefold().encode("utf-8")
        ).hexdigest()[:20]
        lock = SingleInstanceLock(f"crazy-audiobook-package-{lock_key}.lock")
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while not lock.acquire():
            if not wait or time.monotonic() >= deadline:
                raise DeliveryPackagingBusyError(
                    "Another packaging, metadata, reset, or cleanup operation is active"
                )
            time.sleep(0.1)
        try:
            yield
        finally:
            lock.release()

    def get_plan_fingerprint(
        self,
        chapter_numbers: list[int],
        batch_size: int,
        *,
        script_dependency_fingerprint: str = "",
        export_revision: str = DELIVERY_EXPORT_REVISION,
    ) -> str:
        payload = (
            f"chapters={','.join(map(str, chapter_numbers))}\n"
            f"batch_size={batch_size}\n"
            f"scripts={script_dependency_fingerprint}\n"
            f"export={export_revision}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def plan_deliveries(
        self,
        chapter_numbers: list[int],
        batch_size: int,
    ) -> list[DeliveryBatch]:
        if batch_size < 1 or batch_size > 20:
            raise ValueError("Delivery batch size must be between 1 and 20")
        if len(set(chapter_numbers)) != len(chapter_numbers):
            raise ValueError("Delivery chapter order contains duplicates")
        return [
            DeliveryBatch(
                delivery_id=f"part-{ordinal:03d}",
                ordinal=ordinal,
                chapter_numbers=chapter_numbers[offset : offset + batch_size],
            )
            for ordinal, offset in enumerate(
                range(0, len(chapter_numbers), batch_size),
                start=1,
            )
        ]

    def ensure_plan(
        self,
        chapter_numbers: list[int],
        batch_size: int,
        *,
        script_dependency_fingerprint: str = "",
    ) -> tuple[DeliveryIndex, list[DeliveryBatch]]:
        """Create/reconcile a plan without ever changing published boundaries."""
        batches = self.plan_deliveries(chapter_numbers, batch_size)
        plan_fingerprint = self.get_plan_fingerprint(
            chapter_numbers,
            batch_size,
            script_dependency_fingerprint=script_dependency_fingerprint,
        )
        index = self.load_index()
        changed = (
            index.plan_fingerprint != plan_fingerprint
            or index.batch_size != batch_size
            or index.chapter_numbers != chapter_numbers
        )
        if changed:
            index.plan_fingerprint = plan_fingerprint
            index.batch_size = batch_size
            index.chapter_numbers = list(chapter_numbers)
            index.export_revision = DELIVERY_EXPORT_REVISION
            # A dependency or batch size change keeps files recoverable but marks old
            # revisions as stale until republished.
            for part in index.deliveries:
                if part.plan_fingerprint != plan_fingerprint:
                    part.status = "stale"
                    part.stale_reason = "Script, batch size, or export dependencies changed"
            self.save_index(index)
        return index, batches

    def get_published_part(
        self,
        delivery_id: str,
        *,
        include_stale: bool = False,
    ) -> DeliveryPart | None:
        for part in self.load_index().deliveries:
            if part.delivery_id == delivery_id and (
                include_stale or part.status == "published"
            ):
                return part
        return None

    def resolve_artifact(self, artifact: str, *, require_file: bool = True) -> Path:
        """Resolve an indexed basename without allowing path traversal."""
        if Path(artifact).name != artifact or not artifact.lower().endswith(".m4b"):
            raise DeliveryError("Delivery index contains an unsafe artifact name")
        root = self.deliveries_dir.resolve()
        resolved = (root / artifact).resolve()
        if not resolved.is_relative_to(root):
            raise DeliveryError("Delivery artifact escapes the project directory")
        if require_file and (not resolved.is_file() or resolved.is_symlink()):
            raise DeliveryError("Delivery artifact is missing or is not a regular file")
        return resolved

    @staticmethod
    def _hash_file(file_path: Path) -> str:
        digest = hashlib.sha256()
        with open(file_path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def is_published_and_valid(
        self,
        batch: DeliveryBatch,
        *,
        plan_fingerprint: str = "",
        master_manifest_hashes: dict[str, str] | None = None,
        metadata_fingerprint: str | None = None,
        verify_hash: bool = True,
    ) -> bool:
        part = self.get_published_part(batch.delivery_id)
        if part is None or part.chapter_numbers != batch.chapter_numbers:
            return False
        if plan_fingerprint and part.plan_fingerprint != plan_fingerprint:
            return False
        if (
            master_manifest_hashes is not None
            and part.master_manifest_hashes != master_manifest_hashes
        ):
            return False
        if (
            metadata_fingerprint is not None
            and part.metadata_fingerprint != metadata_fingerprint
        ):
            return False
        try:
            artifact_path = self.resolve_artifact(part.artifact)
            if artifact_path.stat().st_size != part.bytes:
                return False
            return not verify_hash or self._hash_file(artifact_path) == part.sha256
        except (OSError, DeliveryError):
            return False

    def resolve_published_artifact(self, delivery_id: str) -> tuple[DeliveryPart, Path]:
        """Return a current, hash-verified delivery artifact for download."""
        part = self.get_published_part(delivery_id)
        if part is None:
            raise DeliveryError("Delivery part is missing or stale")
        artifact_path = self.resolve_artifact(part.artifact)
        if (
            artifact_path.stat().st_size != part.bytes
            or self._hash_file(artifact_path) != part.sha256
        ):
            raise DeliveryError("Delivery artifact failed integrity validation")
        return part, artifact_path

    def publish_delivery(
        self,
        batch: DeliveryBatch,
        temp_artifact_path: Path,
        duration_seconds: float,
        master_manifest_hashes: dict[str, str],
        metadata_fingerprint: str,
        book_title: str,
        *,
        plan_fingerprint: str = "",
        quality: dict[str, Any] | None = None,
    ) -> DeliveryPart:
        """Publish a validated immutable revision, then atomically switch index."""
        temp_artifact_path = Path(temp_artifact_path)
        if not batch.chapter_numbers:
            raise ValueError("Cannot publish an empty delivery batch")
        if float(duration_seconds) <= 0:
            raise ValueError("Delivery duration must be greater than zero")
        if (
            not temp_artifact_path.is_file()
            or temp_artifact_path.is_symlink()
            or temp_artifact_path.stat().st_size == 0
        ):
            raise ValueError(f"Temporary artifact is missing or empty: {temp_artifact_path}")

        index = self.load_index()
        if plan_fingerprint:
            if not index.plan_fingerprint or plan_fingerprint != index.plan_fingerprint:
                raise DeliveryPlanLockedError(
                    "Publication fingerprint does not match the active delivery plan"
                )
            expected_batch = next(
                (
                    planned
                    for planned in self.plan_deliveries(
                        index.chapter_numbers,
                        index.batch_size,
                    )
                    if planned.delivery_id == batch.delivery_id
                ),
                None,
            )
            if expected_batch is None or expected_batch != batch:
                raise DeliveryPlanLockedError(
                    "Publication chapters do not match the locked delivery plan"
                )
            expected_manifest_keys = {str(number) for number in batch.chapter_numbers}
            if set(master_manifest_hashes) != expected_manifest_keys:
                raise ValueError("Delivery mastering dependencies are incomplete")
            if not metadata_fingerprint:
                raise ValueError("Delivery metadata fingerprint is required")
        existing = next(
            (part for part in index.deliveries if part.delivery_id == batch.delivery_id),
            None,
        )
        revision = (existing.revision + 1) if existing else 1
        safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", book_title).strip(" .")[:120]
        safe_title = safe_title or "Audiobook"
        chapter_label = (
            f"{batch.chapter_numbers[0]}-{batch.chapter_numbers[-1]}"
            if len(batch.chapter_numbers) > 1
            else str(batch.chapter_numbers[0])
        )
        while True:
            filename = (
                f"{safe_title} - Part {batch.ordinal:02d} - "
                f"Chapters {chapter_label}-r{revision}.m4b"
            )
            final_path = self.resolve_artifact(filename, require_file=False)
            if not final_path.exists():
                break
            revision += 1

        file_bytes = temp_artifact_path.stat().st_size
        sha256 = self._hash_file(temp_artifact_path)
        self.init_storage()
        os.replace(temp_artifact_path, final_path)

        superseded = list(existing.superseded_artifacts) if existing else []
        if existing and existing.artifact not in superseded:
            superseded.append(existing.artifact)
        new_part = DeliveryPart(
            delivery_id=batch.delivery_id,
            ordinal=batch.ordinal,
            revision=revision,
            chapter_numbers=list(batch.chapter_numbers),
            artifact=filename,
            status="published",
            published_at=datetime.now(timezone.utc).isoformat(),
            sha256=sha256,
            bytes=file_bytes,
            duration_seconds=max(0.0, float(duration_seconds)),
            plan_fingerprint=plan_fingerprint or index.plan_fingerprint,
            master_manifest_hashes=dict(master_manifest_hashes),
            metadata_fingerprint=metadata_fingerprint,
            quality=quality or {},
            superseded_artifacts=superseded,
        )
        index.deliveries = [
            part for part in index.deliveries if part.delivery_id != batch.delivery_id
        ]
        index.deliveries.append(new_part)
        index.deliveries.sort(key=lambda part: part.ordinal)
        if not index.deliveries[:-1] and not index.chapter_numbers:
            index.batch_size = min(20, max(1, len(batch.chapter_numbers)))
        if plan_fingerprint:
            index.plan_fingerprint = plan_fingerprint
        try:
            self.save_index(index)
        except Exception:
            final_path.unlink(missing_ok=True)
            raise
        self._prune_superseded_revisions(index, batch.delivery_id)
        return new_part

    def _prune_superseded_revisions(
        self,
        index: DeliveryIndex,
        delivery_id: str,
    ) -> None:
        """Keep only the configured number of recoverable older part files."""
        part = next(
            (item for item in index.deliveries if item.delivery_id == delivery_id),
            None,
        )
        if part is None:
            return
        retain = index.superseded_revision_retention
        pruned = part.superseded_artifacts[:-retain] if retain else list(part.superseded_artifacts)
        if not pruned:
            return
        part.superseded_artifacts = part.superseded_artifacts[-retain:] if retain else []
        try:
            # Commit the reference removal before deleting files. A crash can
            # therefore leave only harmless orphan files, never dangling index
            # references to intentionally pruned revisions.
            self.save_index(index)
        except Exception:
            logger.exception("Could not persist superseded delivery retention")
            return
        for artifact in pruned:
            try:
                self.resolve_artifact(artifact, require_file=False).unlink(missing_ok=True)
            except (OSError, DeliveryError):
                logger.warning("Could not prune superseded delivery artifact %s", artifact)

    def prune_delivery_history(
        self,
        *,
        retain: int = DEFAULT_DELIVERY_HISTORY_RETENTION,
    ) -> list[Path]:
        """Remove the oldest timestamped delivery archives, retaining the newest."""
        if retain < 0:
            raise ValueError("Delivery-history retention cannot be negative")
        history_root = self.project_dir / "delivery_history"
        if not history_root.is_dir():
            return []
        project_root = self.project_dir.resolve()
        archives = sorted(
            (
                path for path in history_root.iterdir()
                if path.is_dir() and not path.is_symlink()
            ),
            key=lambda path: path.name,
            reverse=True,
        )
        removed: list[Path] = []
        for archive in archives[retain:]:
            resolved = archive.resolve()
            if not resolved.is_relative_to(project_root):
                raise DeliveryError("Delivery history archive escapes the project directory")
            shutil.rmtree(resolved)
            removed.append(resolved)
        return removed

    def mark_stale_for_chapters(
        self,
        chapter_numbers: set[int] | list[int],
        reason: str,
    ) -> int:
        """Invalidate current parts containing affected chapters, preserving files."""
        affected = set(chapter_numbers)
        if not affected or not self.index_path.exists():
            return 0
        index = self.load_index()
        changed = 0
        for part in index.deliveries:
            if affected.intersection(part.chapter_numbers) and part.status != "stale":
                part.status = "stale"
                part.stale_reason = reason
                changed += 1
        if changed:
            self.save_index(index)
        return changed

    def mark_all_stale(self, reason: str) -> int:
        if not self.index_path.exists():
            return 0
        index = self.load_index()
        changed = 0
        for part in index.deliveries:
            if part.status != "stale":
                part.status = "stale"
                part.stale_reason = reason
                changed += 1
        if changed:
            self.save_index(index)
        return changed

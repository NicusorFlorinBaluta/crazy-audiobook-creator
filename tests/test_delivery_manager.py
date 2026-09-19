from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brain.orchestrator.delivery_manager import (
    DeliveryBatch,
    DeliveryIndex,
    DeliveryIndexCorruptError,
    DeliveryIndexVersionError,
    DeliveryManager,
    DeliveryPart,
)


class DeliveryManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp_dir.name)
        self.dm = DeliveryManager(self.project_dir)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_plan_deliveries_splits_chapters_deterministically(self) -> None:
        chapters = list(range(1, 13))  # 12 chapters
        batches = self.dm.plan_deliveries(chapters, batch_size=5)

        self.assertEqual(len(batches), 3)
        self.assertEqual(batches[0].delivery_id, "part-001")
        self.assertEqual(batches[0].ordinal, 1)
        self.assertEqual(batches[0].chapter_numbers, [1, 2, 3, 4, 5])

        self.assertEqual(batches[1].delivery_id, "part-002")
        self.assertEqual(batches[1].ordinal, 2)
        self.assertEqual(batches[1].chapter_numbers, [6, 7, 8, 9, 10])

        self.assertEqual(batches[2].delivery_id, "part-003")
        self.assertEqual(batches[2].ordinal, 3)
        self.assertEqual(batches[2].chapter_numbers, [11, 12])

    def test_plan_deliveries_rejects_invalid_batch_size(self) -> None:
        chapters = [1, 2, 3, 4, 5, 6]
        with self.assertRaises(ValueError):
            self.dm.plan_deliveries(chapters, batch_size=0)

    def test_plan_fingerprint_changes_with_chapters_or_size(self) -> None:
        fp1 = self.dm.get_plan_fingerprint([1, 2, 3], batch_size=5)
        fp2 = self.dm.get_plan_fingerprint([1, 2, 3], batch_size=5)
        fp3 = self.dm.get_plan_fingerprint([1, 2, 3, 4], batch_size=5)
        fp4 = self.dm.get_plan_fingerprint([1, 2, 3], batch_size=2)

        self.assertEqual(fp1, fp2)
        self.assertNotEqual(fp1, fp3)
        self.assertNotEqual(fp1, fp4)

    def test_load_index_returns_default_when_missing(self) -> None:
        index = self.dm.load_index()
        self.assertIsInstance(index, DeliveryIndex)
        self.assertEqual(index.schema_version, 2)
        self.assertEqual(index.deliveries, [])

    def test_save_and_load_index_roundtrip(self) -> None:
        part = DeliveryPart(
            delivery_id="part-001",
            ordinal=1,
            revision=1,
            chapter_numbers=[1, 2, 3],
            artifact="Book - Part 01 - Chapters 1-3-r1.m4b",
            status="published",
            published_at="2026-08-14T00:00:00Z",
            sha256="abcdef123456",
            bytes=1024,
            duration_seconds=120.5,
        )
        index = DeliveryIndex(batch_size=3, deliveries=[part])
        self.dm.save_index(index)

        reloaded = self.dm.load_index()
        self.assertEqual(reloaded.batch_size, 3)
        self.assertEqual(len(reloaded.deliveries), 1)
        self.assertEqual(reloaded.deliveries[0].delivery_id, "part-001")
        self.assertEqual(reloaded.deliveries[0].sha256, "abcdef123456")

    def test_load_index_recovers_from_corrupt_json(self) -> None:
        self.dm.init_storage()
        self.dm.index_path.write_text("{ corrupt json ...", encoding="utf-8")

        with self.assertRaises(DeliveryIndexCorruptError):
            self.dm.load_index()
        self.assertTrue(list(self.dm.deliveries_dir.glob("index.json.corrupt-*")))

    def test_v1_index_is_migrated_and_future_schema_fails_closed(self) -> None:
        self.dm.init_storage()
        self.dm.index_path.write_text(
            json.dumps({"schema_version": 1, "batch_size": 5, "deliveries": []}),
            encoding="utf-8",
        )
        migrated = self.dm.load_index()
        self.assertEqual(migrated.schema_version, 2)
        self.assertEqual(migrated.superseded_revision_retention, 2)

        self.dm.index_path.write_text(
            json.dumps({"schema_version": 999, "deliveries": []}),
            encoding="utf-8",
        )
        with self.assertRaises(DeliveryIndexVersionError):
            self.dm.load_index()

    def test_publish_delivery_moves_artifact_and_updates_index(self) -> None:
        temp_artifact = self.project_dir / "temp_part.m4b"
        temp_artifact.write_bytes(b"dummy m4b content for testing")

        batch = DeliveryBatch(
            delivery_id="part-001",
            ordinal=1,
            chapter_numbers=[1, 2, 3, 4, 5],
        )

        part = self.dm.publish_delivery(
            batch=batch,
            temp_artifact_path=temp_artifact,
            duration_seconds=300.0,
            master_manifest_hashes={"1": "h1", "2": "h2"},
            metadata_fingerprint="meta_fp_123",
            book_title="The Great Adventure",
        )

        self.assertFalse(temp_artifact.exists())
        self.assertEqual(part.delivery_id, "part-001")
        self.assertEqual(part.ordinal, 1)
        self.assertEqual(part.revision, 1)
        self.assertEqual(part.chapter_numbers, [1, 2, 3, 4, 5])
        self.assertEqual(part.artifact, "The Great Adventure - Part 01 - Chapters 1-5-r1.m4b")
        self.assertTrue((self.dm.deliveries_dir / part.artifact).exists())
        self.assertEqual(part.bytes, len(b"dummy m4b content for testing"))

        # Check index
        index = self.dm.load_index()
        self.assertEqual(len(index.deliveries), 1)
        self.assertEqual(index.deliveries[0].artifact, part.artifact)

    def test_publish_delivery_increments_revision_and_preserves_old_file(self) -> None:
        temp_artifact1 = self.project_dir / "temp1.m4b"
        temp_artifact1.write_bytes(b"rev1 content")

        batch = DeliveryBatch(
            delivery_id="part-001",
            ordinal=1,
            chapter_numbers=[1, 2],
        )

        part1 = self.dm.publish_delivery(
            batch=batch,
            temp_artifact_path=temp_artifact1,
            duration_seconds=100.0,
            master_manifest_hashes={},
            metadata_fingerprint="",
            book_title="Test Book",
        )
        self.assertEqual(part1.revision, 1)
        old_file = self.dm.deliveries_dir / part1.artifact
        self.assertTrue(old_file.exists())

        # Publish revision 2
        temp_artifact2 = self.project_dir / "temp2.m4b"
        temp_artifact2.write_bytes(b"rev2 updated content")

        part2 = self.dm.publish_delivery(
            batch=batch,
            temp_artifact_path=temp_artifact2,
            duration_seconds=110.0,
            master_manifest_hashes={},
            metadata_fingerprint="",
            book_title="Test Book",
        )
        self.assertEqual(part2.revision, 2)
        self.assertEqual(part2.artifact, "Test Book - Part 01 - Chapters 1-2-r2.m4b")
        self.assertTrue((self.dm.deliveries_dir / part2.artifact).exists())
        self.assertTrue(old_file.exists())
        self.assertIn(part1.artifact, part2.superseded_artifacts)

        index = self.dm.load_index()
        self.assertEqual(len(index.deliveries), 1)
        self.assertEqual(index.deliveries[0].revision, 2)

    def test_publish_retains_only_two_superseded_revisions(self) -> None:
        batch = DeliveryBatch(delivery_id="part-001", ordinal=1, chapter_numbers=[1])
        parts = []
        for revision in range(1, 5):
            temp = self.project_dir / f"temp-{revision}.m4b"
            temp.write_bytes(f"revision {revision}".encode())
            parts.append(self.dm.publish_delivery(batch, temp, 10.0, {}, "", "Book"))

        current = self.dm.load_index().deliveries[0]
        self.assertEqual(
            current.superseded_artifacts,
            [parts[1].artifact, parts[2].artifact],
        )
        self.assertFalse((self.dm.deliveries_dir / parts[0].artifact).exists())
        self.assertTrue((self.dm.deliveries_dir / parts[1].artifact).exists())
        self.assertTrue((self.dm.deliveries_dir / parts[2].artifact).exists())
        self.assertTrue((self.dm.deliveries_dir / parts[3].artifact).exists())

    def test_save_index_replace_failure_preserves_current_index(self) -> None:
        self.dm.save_index(DeliveryIndex(batch_size=2))
        original = self.dm.index_path.read_bytes()
        real_replace = __import__("os").replace

        def fail_final_replace(source, destination):
            if Path(destination) == self.dm.index_path:
                raise OSError("injected index replace failure")
            return real_replace(source, destination)

        with patch("brain.orchestrator.delivery_manager.os.replace", side_effect=fail_final_replace):
            with self.assertRaises(OSError):
                self.dm.save_index(DeliveryIndex(batch_size=3))

        self.assertEqual(self.dm.index_path.read_bytes(), original)
        self.assertEqual(self.dm.load_index().batch_size, 2)

    def test_publication_index_failure_removes_only_new_orphan(self) -> None:
        batch = DeliveryBatch(delivery_id="part-001", ordinal=1, chapter_numbers=[1])
        first_temp = self.project_dir / "first.m4b"
        first_temp.write_bytes(b"first")
        first = self.dm.publish_delivery(batch, first_temp, 10.0, {}, "", "Book")
        second_temp = self.project_dir / "second.m4b"
        second_temp.write_bytes(b"second")

        with patch.object(self.dm, "save_index", side_effect=OSError("injected crash")):
            with self.assertRaises(OSError):
                self.dm.publish_delivery(batch, second_temp, 11.0, {}, "", "Book")

        self.assertTrue((self.dm.deliveries_dir / first.artifact).is_file())
        self.assertFalse((self.dm.deliveries_dir / "Book - Part 01 - Chapters 1-r2.m4b").exists())
        self.assertEqual(self.dm.load_index().deliveries[0].revision, 1)

    def test_delivery_history_retains_newest_two_archives(self) -> None:
        history = self.project_dir / "delivery_history"
        history.mkdir()
        for name in ["20260101", "20260102", "20260103", "20260104"]:
            archive = history / name
            archive.mkdir()
            (archive / "index.json").write_text("{}", encoding="utf-8")

        removed = self.dm.prune_delivery_history(retain=2)

        self.assertEqual({path.name for path in removed}, {"20260101", "20260102"})
        self.assertEqual(
            {path.name for path in history.iterdir()},
            {"20260103", "20260104"},
        )

    def test_publish_delivery_rejects_missing_or_empty_temp_file(self) -> None:
        batch = DeliveryBatch(delivery_id="part-001", ordinal=1, chapter_numbers=[1])
        missing_file = self.project_dir / "does_not_exist.m4b"
        with self.assertRaises(ValueError):
            self.dm.publish_delivery(
                batch=batch,
                temp_artifact_path=missing_file,
                duration_seconds=0.0,
                master_manifest_hashes={},
                metadata_fingerprint="",
                book_title="Book",
            )

        empty_file = self.project_dir / "empty.m4b"
        empty_file.write_bytes(b"")
        with self.assertRaises(ValueError):
            self.dm.publish_delivery(
                batch=batch,
                temp_artifact_path=empty_file,
                duration_seconds=0.0,
                master_manifest_hashes={},
                metadata_fingerprint="",
                book_title="Book",
            )

    def test_is_published_and_valid(self) -> None:
        batch = DeliveryBatch(delivery_id="part-001", ordinal=1, chapter_numbers=[1])
        self.assertFalse(self.dm.is_published_and_valid(batch))

        temp_artifact = self.project_dir / "temp.m4b"
        temp_artifact.write_bytes(b"valid content")
        part = self.dm.publish_delivery(
            batch=batch,
            temp_artifact_path=temp_artifact,
            duration_seconds=50.0,
            master_manifest_hashes={},
            metadata_fingerprint="",
            book_title="Book",
        )
        self.assertTrue(self.dm.is_published_and_valid(batch))

        # If file on disk is removed, becomes invalid
        (self.dm.deliveries_dir / part.artifact).unlink()
        self.assertFalse(self.dm.is_published_and_valid(batch))

    def test_resolve_artifact_rejects_path_traversal(self) -> None:
        with self.assertRaises(Exception):
            self.dm.resolve_artifact("../outside.m4b", require_file=False)

    def test_plan_dependency_change_marks_publication_stale(self) -> None:
        index, batches = self.dm.ensure_plan([1, 2], 2, script_dependency_fingerprint="scripts-v1")
        temp = self.project_dir / "part.m4b"
        temp.write_bytes(b"valid audio")
        self.dm.publish_delivery(
            batches[0],
            temp,
            10.0,
            {"1": "h1", "2": "h2"},
            "metadata",
            "Book",
            plan_fingerprint=index.plan_fingerprint,
        )
        changed, _ = self.dm.ensure_plan([1, 2], 2, script_dependency_fingerprint="scripts-v2")
        self.assertEqual(changed.deliveries[0].status, "stale")

    def test_locked_plan_rejects_incomplete_master_dependencies(self) -> None:
        index, batches = self.dm.ensure_plan([1, 2], 2)
        temp = self.project_dir / "part.m4b"
        temp.write_bytes(b"valid audio")
        with self.assertRaises(ValueError):
            self.dm.publish_delivery(
                batches[0],
                temp,
                10.0,
                {"1": "only-one-manifest"},
                "metadata",
                "Book",
                plan_fingerprint=index.plan_fingerprint,
            )

    def test_mark_all_stale_handles_legacy_index_without_plan(self) -> None:
        batch = DeliveryBatch(delivery_id="part-001", ordinal=1, chapter_numbers=[1])
        temp = self.project_dir / "part.m4b"
        temp.write_bytes(b"valid audio")
        self.dm.publish_delivery(batch, temp, 10.0, {}, "", "Book")
        self.assertEqual(self.dm.mark_all_stale("metadata changed"), 1)
        self.assertEqual(self.dm.load_index().deliveries[0].status, "stale")


if __name__ == "__main__":
    unittest.main()

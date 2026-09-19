"""Clean up duplicate ghost playback flags in pipeline_state.db and project JSON.

Spec F2:
For each (chapter_number, position_ms) pair with more than one row,
keep the row whose enriched_data contains active_line and delete the rest.
Supports --dry-run (default) and --apply.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import paths as shared_paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id", nargs="?", default="the-finest-edge-of-twilight-book")
    parser.add_argument("--apply", action="store_true", help="Perform deletion (default is dry-run)")
    args = parser.parse_args()

    db_path = shared_paths.PROJECTS_DIR / "pipeline_state.db"
    if not db_path.exists():
        print(f"Database not found at {db_path}")
        return 1

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id, flag_id, chapter_number, position_ms, line_id, status, enriched_data, created_at
        FROM playback_flags
        WHERE project_id = ?
        ORDER BY chapter_number, position_ms, id
        """,
        (args.project_id,),
    )
    rows = cur.fetchall()
    print(f"Loaded {len(rows)} total flags for project {args.project_id!r}")

    grouped = defaultdict(list)
    for r in rows:
        row_id, flag_id, ch, pos, line_id, status, enriched_str, created_at = r
        try:
            enriched = json.loads(enriched_str) if enriched_str else {}
        except Exception:
            enriched = {}
        has_active_line = bool(enriched.get("active_line"))
        grouped[(ch, pos)].append(
            {
                "row_id": row_id,
                "flag_id": flag_id,
                "chapter": ch,
                "position_ms": pos,
                "line_id": line_id,
                "status": status,
                "has_active_line": has_active_line,
                "created_at": created_at,
            }
        )

    to_delete_ids: list[int] = []
    to_delete_flag_ids: list[str] = []

    for (ch, pos), items in grouped.items():
        if len(items) <= 1:
            continue
        print(f"\nDuplicate group at Chapter {ch}, Position {pos} ms ({len(items)} items):")
        # Find which items have active_line
        with_active = [item for item in items if item["has_active_line"]]
        without_active = [item for item in items if not item["has_active_line"]]

        if with_active:
            keep = with_active[0]
            # Delete any others
            for item in items:
                if item["row_id"] != keep["row_id"]:
                    to_delete_ids.append(item["row_id"])
                    to_delete_flag_ids.append(item["flag_id"])
                    print(
                        f"  [DELETE] row_id={item['row_id']} flag_id={item['flag_id']} has_active_line={item['has_active_line']} status={item['status']}"
                    )
            print(
                f"  [KEEP]   row_id={keep['row_id']} flag_id={keep['flag_id']} has_active_line=True status={keep['status']}"
            )
        else:
            # None have active_line, keep the first one
            keep = items[0]
            for item in items[1:]:
                to_delete_ids.append(item["row_id"])
                to_delete_flag_ids.append(item["flag_id"])
                print(
                    f"  [DELETE] row_id={item['row_id']} flag_id={item['flag_id']} has_active_line={item['has_active_line']} status={item['status']}"
                )
            print(f"  [KEEP]   row_id={keep['row_id']} flag_id={keep['flag_id']} status={keep['status']}")

    print(f"\nFound {len(to_delete_ids)} duplicates to delete.")

    if not to_delete_ids:
        print("Nothing to clean up.")
        return 0

    if not args.apply:
        print("\nDry-run complete. Run with --apply to delete from database and update playback_flags.json.")
        return 0

    # Delete from DB
    placeholders = ",".join("?" for _ in to_delete_ids)
    cur.execute(f"DELETE FROM playback_flags WHERE id IN ({placeholders})", to_delete_ids)
    conn.commit()
    conn.close()
    print(f"Successfully deleted {len(to_delete_ids)} rows from pipeline_state.db.")

    # Also update project's playback_flags.json if present
    flags_json_path = shared_paths.PROJECTS_DIR / args.project_id / "playback_flags.json"
    if flags_json_path.is_file():
        try:
            with open(flags_json_path, encoding="utf-8") as f:
                disk_data = json.load(f)
            orig_flags = disk_data.get("flags", [])
            del_set = set(to_delete_flag_ids)
            filtered_flags = [f for f in orig_flags if f.get("flag_id") not in del_set]
            disk_data["flags"] = filtered_flags
            disk_data["total_flags"] = len(filtered_flags)
            with open(flags_json_path, "w", encoding="utf-8") as f:
                json.dump(disk_data, f, indent=2, ensure_ascii=False)
            print(f"Updated {flags_json_path}: reduced flags from {len(orig_flags)} to {len(filtered_flags)}.")
        except Exception as exc:
            print(f"Warning: Failed updating {flags_json_path}: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

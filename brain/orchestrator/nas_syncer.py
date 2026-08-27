"""NAS Syncer for Crazy Audiobook Creator.

Handles atomic synchronization of incremental delivery parts, full audiobooks,
artwork, metadata manifests (catalog.json and book.json), and cleanup to the NAS
shared storage over SFTP/SSH.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import re
import socket
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import paramiko
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load .env variables if present
load_dotenv()

DEFAULT_NAS_PORT = 22
DEFAULT_SHARED_FOLDER = "crazybooks"
DEFAULT_CONNECT_TIMEOUT = 10.0


class NASError(RuntimeError):
    """Base exception for NAS synchronization operations."""


class NASConnectionError(NASError):
    """Raised when connecting or authenticating with NAS fails."""


class NASSyncer:
    """Manages SFTP connection, volume discovery, atomic file uploads, and manifests on NAS."""

    def __init__(
        self,
        host: str | None = None,
        username: str | None = None,
        password: str | None = None,
        port: int | None = None,
        shared_folder: str | None = None,
        prune_parts_on_full: bool = True,
        auto_sync: bool = True,
    ):
        if host is None:
            self.host = (
                os.getenv("NAS_SSH_HOST")
                or os.getenv("HA_SERVER_SSH_HOST")
                or "192.168.50.26"
            ).strip()
        else:
            self.host = host.strip()

        if username is None:
            self.username = (
                os.getenv("NAS_SSH_USER")
                or os.getenv("HA_SERVER_SSH_USER")
                or "crazywiz"
            ).strip()
        else:
            self.username = username.strip()

        if password is None:
            self.password = (
                os.getenv("NAS_SSH_PASSWORD")
                or os.getenv("HA_SERVER_SSH_PASSWORD")
                or ""
            )
        else:
            self.password = password

        self.port = int(port or os.getenv("NAS_SSH_PORT") or DEFAULT_NAS_PORT)
        if shared_folder is None:
            self.shared_folder = (
                os.getenv("NAS_SHARED_FOLDER")
                or DEFAULT_SHARED_FOLDER
            ).strip().strip("/")
        else:
            self.shared_folder = shared_folder.strip().strip("/")
        self.prune_parts_on_full = prune_parts_on_full
        self.auto_sync = auto_sync
        self._resolved_nas_root: str | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.host and self.username and self.password)

    @contextmanager
    def sftp_session(self) -> Iterator[paramiko.SFTPClient]:
        """Establish an SFTP session with connection pooling / auto-close."""
        if not self.is_configured:
            raise NASConnectionError("NAS credentials are not configured in environment or settings.")

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=DEFAULT_CONNECT_TIMEOUT,
                banner_timeout=DEFAULT_CONNECT_TIMEOUT,
                auth_timeout=DEFAULT_CONNECT_TIMEOUT,
            )
            sftp = ssh.open_sftp()
            try:
                yield sftp
            finally:
                sftp.close()
        except (paramiko.SSHException, socket.error, OSError) as exc:
            logger.error("Failed to connect to NAS %s@%s:%d - %s", self.username, self.host, self.port, exc)
            raise NASConnectionError(f"NAS SSH/SFTP connection failed: {exc}") from exc
        finally:
            ssh.close()

    def resolve_nas_root(self, sftp: paramiko.SFTPClient) -> str:
        """Auto-discover the absolute path to the shared folder on NAS."""
        if self._resolved_nas_root:
            return self._resolved_nas_root

        candidates = [
            f"/mnt/nas/media/{self.shared_folder}",
            f"/mnt/nas/{self.shared_folder}",
            f"/volume1/{self.shared_folder}",
            f"/volume2/{self.shared_folder}",
            f"/volume3/{self.shared_folder}",
            f"/{self.shared_folder}",
            f"/mnt/{self.shared_folder}",
            f"/var/services/homes/{self.username}/{self.shared_folder}",
            f"/home/{self.username}/{self.shared_folder}",
            f"{self.shared_folder}",
        ]

        for candidate in candidates:
            try:
                stat = sftp.stat(candidate)
                # Found existing directory
                self._resolved_nas_root = candidate
                return candidate
            except (IOError, OSError):
                continue

        # If not found, try to create /volume1/{shared_folder} or ~/{shared_folder}
        for fallback in [f"/volume1/{self.shared_folder}", f"{self.shared_folder}"]:
            try:
                self._mkdir_p(sftp, fallback)
                self._resolved_nas_root = fallback
                logger.info("Created NAS shared folder at %s", fallback)
                return fallback
            except Exception:
                continue

        raise NASError(f"Could not locate or create '{self.shared_folder}' on NAS.")

    def test_connection(self) -> dict[str, Any]:
        """Test reachability, authentication, and write access on the NAS."""
        with self.sftp_session() as sftp:
            nas_root = self.resolve_nas_root(sftp)
            test_file = posixpath.join(nas_root, ".test_connection")
            try:
                with sftp.open(test_file, "w") as f:
                    f.write("crazy-audiobook-creator-test\n")
                sftp.remove(test_file)
                return {
                    "success": True,
                    "host": self.host,
                    "nas_root": nas_root,
                    "message": "NAS SSH/SFTP connected and write-verified successfully.",
                }
            except Exception as e:
                raise NASError(f"Write permission check failed on {nas_root}: {e}") from e

    def _mkdir_p(self, sftp: paramiko.SFTPClient, remote_directory: str) -> None:
        """Create remote directory tree recursively."""
        parts = remote_directory.strip("/").split("/")
        current = "/" if remote_directory.startswith("/") else ""
        for part in parts:
            if not part:
                continue
            current = posixpath.join(current, part)
            try:
                sftp.stat(current)
            except (IOError, OSError):
                try:
                    sftp.mkdir(current)
                except (IOError, OSError):
                    # May exist or parent created
                    pass

    def _atomic_upload(
        self,
        sftp: paramiko.SFTPClient,
        local_path: Path,
        remote_path: str,
    ) -> None:
        """Upload file to staging .tmp_{filename} and atomically rename."""
        local_path = Path(local_path)
        if not local_path.is_file():
            raise FileNotFoundError(f"Local file does not exist: {local_path}")

        parent_dir = posixpath.dirname(remote_path)
        self._mkdir_p(sftp, parent_dir)

        filename = posixpath.basename(remote_path)
        tmp_remote = posixpath.join(parent_dir, f".tmp_{filename}")

        logger.info("Uploading %s -> %s", local_path.name, tmp_remote)
        sftp.put(str(local_path), tmp_remote)

        # Verify size
        remote_size = sftp.stat(tmp_remote).st_size
        local_size = local_path.stat().st_size
        if remote_size != local_size:
            try:
                sftp.remove(tmp_remote)
            except Exception:
                pass
            raise NASError(
                f"SFTP upload corrupted: local {local_size} bytes != remote {remote_size} bytes"
            )

        # Atomic rename
        try:
            # POSIX rename overwrites target atomically
            sftp.posix_rename(tmp_remote, remote_path)
        except Exception:
            try:
                sftp.remove(remote_path)
            except Exception:
                pass
            sftp.rename(tmp_remote, remote_path)

        logger.info("Atomically published %s", remote_path)

    def _atomic_write_json(
        self,
        sftp: paramiko.SFTPClient,
        data: dict[str, Any],
        remote_path: str,
    ) -> None:
        """Write JSON atomically to remote path."""
        parent_dir = posixpath.dirname(remote_path)
        self._mkdir_p(sftp, parent_dir)

        filename = posixpath.basename(remote_path)
        tmp_remote = posixpath.join(parent_dir, f".tmp_{filename}")

        content = json.dumps(data, indent=2, ensure_ascii=False)
        with sftp.open(tmp_remote, "w") as handle:
            handle.write(content)

        try:
            sftp.posix_rename(tmp_remote, remote_path)
        except Exception:
            try:
                sftp.remove(remote_path)
            except Exception:
                pass
            sftp.rename(tmp_remote, remote_path)

    def sync_delivery_part(
        self,
        project_id: str,
        project_dir: Path,
        part_artifact_path: Path,
        part_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Sync a published incremental M4B part, cover, book.json, and catalog.json to NAS."""
        if not self.is_configured:
            logger.warning("NAS is not configured; skipping delivery part sync.")
            return {"status": "skipped", "reason": "not_configured"}

        project_dir = Path(project_dir)
        part_artifact_path = Path(part_artifact_path)

        with self.sftp_session() as sftp:
            nas_root = self.resolve_nas_root(sftp)
            proj_remote_dir = posixpath.join(nas_root, project_id)
            parts_remote_dir = posixpath.join(proj_remote_dir, "parts")

            # 1. Upload part artifact
            remote_part_path = posixpath.join(parts_remote_dir, part_artifact_path.name)
            self._atomic_upload(sftp, part_artifact_path, remote_part_path)

            # 2. Upload cover artwork if present
            self._upload_cover_if_needed(sftp, project_dir, proj_remote_dir)

            # 3. Clean up superseded revisions in parts/ on NAS if applicable
            if part_info and part_info.get("superseded_artifacts"):
                for superseded in part_info["superseded_artifacts"]:
                    try:
                        sftp.remove(posixpath.join(parts_remote_dir, superseded))
                        logger.info("Pruned superseded part on NAS: %s", superseded)
                    except Exception:
                        pass

            # 4. Generate & upload book.json and api/mobile/v1 alias
            book_manifest = self._generate_book_manifest(project_id, project_dir, sftp, proj_remote_dir)
            self._atomic_write_json(sftp, book_manifest, posixpath.join(proj_remote_dir, "book.json"))
            self._atomic_write_json(sftp, book_manifest, posixpath.join(nas_root, "api/mobile/v1/books", project_id))

            # 5. Rebuild global catalog.json
            self.rebuild_global_catalog(sftp=sftp, nas_root=nas_root)

            return {
                "status": "synced",
                "part_artifact": part_artifact_path.name,
                "remote_path": remote_part_path,
            }

    def sync_full_export(
        self,
        project_id: str,
        project_dir: Path,
        full_m4b_path: Path,
        prune_parts: bool | None = None,
    ) -> dict[str, Any]:
        """Sync full M4B export, prune intermediate parts, update book.json & catalog.json."""
        if not self.is_configured:
            logger.warning("NAS is not configured; skipping full export sync.")
            return {"status": "skipped", "reason": "not_configured"}

        project_dir = Path(project_dir)
        full_m4b_path = Path(full_m4b_path)
        should_prune = self.prune_parts_on_full if prune_parts is None else prune_parts

        with self.sftp_session() as sftp:
            nas_root = self.resolve_nas_root(sftp)
            proj_remote_dir = posixpath.join(nas_root, project_id)
            full_remote_dir = posixpath.join(proj_remote_dir, "full")

            # 1. Upload full M4B
            remote_full_path = posixpath.join(full_remote_dir, full_m4b_path.name)
            self._atomic_upload(sftp, full_m4b_path, remote_full_path)

            # 2. Upload cover artwork
            self._upload_cover_if_needed(sftp, project_dir, proj_remote_dir)

            # 3. Prune intermediate parts if requested
            if should_prune:
                parts_remote_dir = posixpath.join(proj_remote_dir, "parts")
                try:
                    for item in sftp.listdir(parts_remote_dir):
                        if item.lower().endswith(".m4b") or item.endswith(".json"):
                            try:
                                sftp.remove(posixpath.join(parts_remote_dir, item))
                                logger.info("Pruned partial delivery file on NAS: %s", item)
                            except Exception:
                                pass
                except (IOError, OSError):
                    pass

            # 4. Generate & upload book.json and api/mobile/v1 alias
            book_manifest = self._generate_book_manifest(project_id, project_dir, sftp, proj_remote_dir)
            self._atomic_write_json(sftp, book_manifest, posixpath.join(proj_remote_dir, "book.json"))
            self._atomic_write_json(sftp, book_manifest, posixpath.join(nas_root, "api/mobile/v1/books", project_id))

            # 5. Rebuild global catalog.json
            self.rebuild_global_catalog(sftp=sftp, nas_root=nas_root)

            return {
                "status": "synced",
                "full_artifact": full_m4b_path.name,
                "remote_path": remote_full_path,
                "parts_pruned": should_prune,
            }

    def delete_project(
        self,
        project_id: str,
        delete_from_nas: bool = False,
    ) -> dict[str, Any]:
        """Optionally remove project directory from NAS and rebuild catalog."""
        if not delete_from_nas:
            logger.info("delete_from_nas is False; preserving NAS copy of %s", project_id)
            return {"status": "preserved", "project_id": project_id}

        if not self.is_configured:
            return {"status": "skipped", "reason": "not_configured"}

        with self.sftp_session() as sftp:
            nas_root = self.resolve_nas_root(sftp)
            proj_remote_dir = posixpath.join(nas_root, project_id)

            self._rmtree(sftp, proj_remote_dir)
            logger.info("Deleted %s from NAS", proj_remote_dir)

            self.rebuild_global_catalog(sftp=sftp, nas_root=nas_root)

            return {"status": "deleted", "project_id": project_id}

    def _rmtree(self, sftp: paramiko.SFTPClient, remote_path: str) -> None:
        """Recursively delete a remote directory."""
        try:
            stat = sftp.stat(remote_path)
        except (IOError, OSError):
            return

        try:
            for item in sftp.listdir(remote_path):
                child = posixpath.join(remote_path, item)
                try:
                    sftp.remove(child)
                except IOError:
                    self._rmtree(sftp, child)
            sftp.rmdir(remote_path)
        except Exception as e:
            logger.warning("Error deleting %s on NAS: %s", remote_path, e)

    def _upload_cover_if_needed(
        self,
        sftp: paramiko.SFTPClient,
        project_dir: Path,
        proj_remote_dir: str,
    ) -> None:
        """Upload cover artwork to NAS if present."""
        cover_candidates = [
            project_dir / "cover.jpg",
            project_dir / "cover.png",
        ]
        book_json = project_dir / "book.json"
        if book_json.is_file():
            try:
                bdata = json.loads(book_json.read_text(encoding="utf-8"))
                raw_cov = bdata.get("metadata", {}).get("cover_image_path")
                if raw_cov:
                    p = Path(raw_cov)
                    cover_candidates.insert(0, p if p.is_absolute() else (project_dir / p))
            except Exception:
                pass

        for candidate in cover_candidates:
            if candidate.is_file() and candidate.stat().st_size > 0:
                remote_cover = posixpath.join(proj_remote_dir, "cover.jpg")
                try:
                    self._atomic_upload(sftp, candidate, remote_cover)
                    return
                except Exception as e:
                    logger.warning("Failed to upload cover %s to NAS: %s", candidate, e)

    def _generate_book_manifest(
        self,
        project_id: str,
        project_dir: Path,
        sftp: paramiko.SFTPClient,
        proj_remote_dir: str,
    ) -> dict[str, Any]:
        """Generate a complete book.json manifest referencing relative NAS stream URLs."""
        book_json_path = project_dir / "book.json"
        metadata: dict[str, Any] = {}
        book_chapters: list[dict[str, Any]] = []

        if book_json_path.is_file():
            try:
                bdata = json.loads(book_json_path.read_text(encoding="utf-8"))
                metadata = bdata.get("metadata", {})
                book_chapters = bdata.get("chapters", [])
            except Exception:
                pass

        title = str(metadata.get("title") or project_id)
        author = str(metadata.get("author") or "Unknown Author")
        genre = str(metadata.get("genre") or "")
        year = str(metadata.get("year") or "")
        description = str(metadata.get("description") or "")
        isbn = str(metadata.get("isbn") or "")
        series = str(metadata.get("series") or "")
        part = str(metadata.get("series_index") or metadata.get("part") or "")

        # Find cover URL relative to project
        cover_url = None
        try:
            sftp.stat(posixpath.join(proj_remote_dir, "cover.jpg"))
            cover_url = f"{project_id}/cover.jpg"
        except (IOError, OSError):
            pass

        # Check full M4B
        full_remote_dir = posixpath.join(proj_remote_dir, "full")
        full_m4b_name = None
        full_m4b_size = 0
        try:
            for item in sftp.listdir(full_remote_dir):
                if item.lower().endswith(".m4b"):
                    full_m4b_name = item
                    full_m4b_size = sftp.stat(posixpath.join(full_remote_dir, item)).st_size
                    break
        except (IOError, OSError):
            pass

        # Check delivery parts mapping from deliveries/index.json if present
        delivery_chapter_map: dict[int, str] = {}
        delivery_info_map: dict[str, dict[str, Any]] = {}
        deliveries_index = project_dir / "deliveries" / "index.json"
        if deliveries_index.is_file():
            try:
                d_data = json.loads(deliveries_index.read_text(encoding="utf-8"))
                for deliv in d_data.get("deliveries", []):
                    if deliv.get("status") == "published" and deliv.get("artifact"):
                        art = deliv["artifact"]
                        url = f"{project_id}/parts/{quote(art)}"
                        delivery_info_map[art] = deliv
                        delivery_info_map[url] = deliv
                        for c_num in deliv.get("chapter_numbers", []):
                            delivery_chapter_map[c_num] = url
            except Exception:
                pass

        # Helper to compute duration of a chapter
        def get_chapter_duration(ch_num: int) -> float:
            m_file = project_dir / "manifests" / f"chapter_{ch_num:03d}.master.json"
            if m_file.is_file():
                try:
                    m_d = json.loads(m_file.read_text(encoding="utf-8"))
                    d = m_d.get("mastering_quality", {}).get("duration_seconds") or m_d.get("duration_seconds")
                    if d:
                        return float(d)
                except Exception:
                    pass
            # Try workspace wav
            for cand in [
                Path("workspace") / project_id / "chapters" / f"chapter_{ch_num:03d}.wav",
                project_dir.parent.parent / "workspace" / project_id / "chapters" / f"chapter_{ch_num:03d}.wav",
                project_dir / "chapters" / f"chapter_{ch_num:03d}.wav",
            ]:
                if cand.is_file():
                    try:
                        import wave
                        with wave.open(str(cand), "rb") as handle:
                            frames = handle.getnframes()
                            rate = handle.getframerate()
                            if rate > 0:
                                return round(frames / float(rate), 2)
                    except Exception:
                        pass
                    try:
                        import soundfile as sf
                        info = sf.info(str(cand))
                        return round(float(info.duration), 2)
                    except Exception:
                        pass
            return 0.0

        # Check delivery parts directory on NAS
        parts_remote_dir = posixpath.join(proj_remote_dir, "parts")
        parts_list: list[dict[str, Any]] = []
        chapter_delivery_offsets: dict[int, tuple[int, int]] = {}
        try:
            for item in sorted(sftp.listdir(parts_remote_dir)):
                if item.lower().endswith(".m4b"):
                    # Ignore and prune superseded part revisions if delivery_info_map has active artifacts
                    if delivery_info_map and item not in delivery_info_map:
                        try:
                            sftp.remove(posixpath.join(parts_remote_dir, item))
                            logger.info("Pruned superseded part artifact on NAS: %s", item)
                        except Exception:
                            pass
                        continue

                    m_part = re.search(r"Part\s+(\d+)", item, re.IGNORECASE)
                    ord_val = int(m_part.group(1)) if m_part else 1
                    download_url = f"{project_id}/parts/{quote(item)}"
                    m_ch = re.search(r"Chapters\s+(\d+)-(\d+)", item, re.IGNORECASE)
                    ch_nums = [c for c, u in delivery_chapter_map.items() if u == download_url]
                    if not ch_nums and m_ch:
                        ch_nums = list(range(int(m_ch.group(1)), int(m_ch.group(2)) + 1))
                        for c in ch_nums:
                            delivery_chapter_map[c] = download_url

                    # Build accurate chapter details with start_ms and duration
                    part_ch_details: list[dict[str, Any]] = []
                    part_cum_offset = 0.0
                    for c_num in ch_nums:
                        c_dur = get_chapter_duration(c_num)
                        ch_entry = book_chapters[c_num - 1] if 0 <= c_num - 1 < len(book_chapters) else {}
                        c_title = str(ch_entry.get("source_heading") or ch_entry.get("title") or f"Chapter {c_num}").strip()
                        c_start = int(part_cum_offset * 1000)
                        c_end = int((part_cum_offset + c_dur) * 1000)
                        part_cum_offset += c_dur
                        chapter_delivery_offsets[c_num] = (c_start, c_end)
                        part_ch_details.append({
                            "number": c_num,
                            "title": c_title,
                            "start_ms": c_start,
                            "end_ms": c_end,
                            "duration_seconds": c_dur,
                            "stream_url": download_url,
                            "status": "mastered",
                        })

                    deliv_info = delivery_info_map.get(item) or delivery_info_map.get(download_url) or {}
                    part_total_dur = float(deliv_info.get("duration_seconds") or part_cum_offset)

                    parts_list.append({
                        "delivery_id": f"part-{ord_val:03d}",
                        "title": f"Part {ord_val:02d}",
                        "filename": item,
                        "chapters": ch_nums,
                        "chapter_details": part_ch_details,
                        "duration_seconds": part_total_dur,
                        "status": "published",
                        "download_url": download_url,
                    })
        except (IOError, OSError):
            pass

        # Build chapter manifests
        chapters_manifest: list[dict[str, Any]] = []
        cumulative_offset = 0.0
        total_chapters = len(book_chapters) or int(metadata.get("total_chapters") or 0)

        # Try to load master durations from local project if present
        for idx, ch in enumerate(book_chapters, 1):
            dur = get_chapter_duration(idx)
            if idx in chapter_delivery_offsets:
                start_ms, end_ms = chapter_delivery_offsets[idx]
            else:
                start_ms = int(cumulative_offset * 1000)
                end_ms = int((cumulative_offset + (dur or 60.0)) * 1000)
            if dur:
                cumulative_offset += dur

            source_heading = ch.get("source_heading") or ch.get("title") or f"Chapter {idx}"
            raw_title = str(source_heading).strip()
            formatted_title = raw_title if raw_title else f"Chapter {idx}"

            # Stream URL: full M4B if full export exists, else chapter-specific delivery part
            if full_m4b_name:
                stream_url = f"{project_id}/full/{quote(full_m4b_name)}#chapter={idx}"
                ch_status = "mastered" if dur else "ready"
            elif idx in delivery_chapter_map:
                stream_url = f"{delivery_chapter_map[idx]}#chapter={idx}"
                ch_status = "mastered"
            else:
                stream_url = None
                ch_status = "mastered" if dur else "pending"

            chapters_manifest.append({
                "number": idx,
                "title": formatted_title,
                "raw_title": raw_title,
                "source_heading": raw_title,
                "status": ch_status,
                "duration_seconds": dur,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "stream_url": stream_url,
                "download_url": stream_url,
            })

        default_stream = (
            f"{project_id}/full/{quote(full_m4b_name)}"
            if full_m4b_name
            else (parts_list[-1]["download_url"] if parts_list else "")
        )

        status = "ready_full" if full_m4b_name else ("ready_partial" if parts_list else "in_progress")

        return {
            "project_id": project_id,
            "title": title or "Untitled",
            "author": author or "Unknown Author",
            "genre": genre or "",
            "year": year or "",
            "description": description or "",
            "isbn": isbn or "",
            "narrator": str(metadata.get("narrator") or "AI Ensemble"),
            "series": series or "",
            "part": part or "",
            "status": status,
            "total_chapters": total_chapters or len(chapters_manifest),
            "generated_chapters_count": len(chapters_manifest),
            "mastered_chapters_count": sum(1 for c in chapters_manifest if c.get("duration_seconds")),
            "total_duration_seconds": round(cumulative_offset, 2),
            "is_live_generating": status == "ready_partial",
            "cover_url": cover_url or "",
            "stream_url": default_stream,
            "download_url": default_stream,
            "file_size_bytes": full_m4b_size if full_m4b_name else None,
            "deliveries": parts_list,
            "chapters": chapters_manifest,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def rebuild_global_catalog(
        self,
        sftp: paramiko.SFTPClient | None = None,
        nas_root: str | None = None,
        active_project_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Scan all project folders on NAS and rewrite the global catalog.json."""
        if sftp is None:
            with self.sftp_session() as session:
                return self.rebuild_global_catalog(sftp=session, active_project_ids=active_project_ids)

        if nas_root is None:
            nas_root = self.resolve_nas_root(sftp)

        books: list[dict[str, Any]] = []

        try:
            entries = sftp.listdir(nas_root)
        except (IOError, OSError) as e:
            logger.error("Could not list NAS directory %s: %s", nas_root, e)
            return {"books": []}

        for entry in sorted(entries):
            if entry.startswith((".", "_")):
                continue
            if active_project_ids and entry not in active_project_ids and entry != "api":
                logger.info("Pruning stale project %s from NAS", entry)
                proj_dir = posixpath.join(nas_root, entry)
                try:
                    self._safe_rmtree_sftp(sftp, proj_dir)
                except Exception:
                    pass
                continue

            proj_dir = posixpath.join(nas_root, entry)
            try:
                # Must be a directory containing book.json
                book_json_path = posixpath.join(proj_dir, "book.json")
                with sftp.open(book_json_path, "r") as handle:
                    bdata = json.loads(handle.read().decode("utf-8"))
                    if isinstance(bdata, dict) and bdata.get("project_id"):
                        summary = {
                            "project_id": str(bdata.get("project_id") or ""),
                            "title": str(bdata.get("title") or "Untitled"),
                            "author": str(bdata.get("author") or "Unknown Author"),
                            "genre": str(bdata.get("genre") or ""),
                            "year": str(bdata.get("year") or ""),
                            "description": str(bdata.get("description") or ""),
                            "isbn": str(bdata.get("isbn") or ""),
                            "status": str(bdata.get("status") or "queued"),
                            "total_chapters": int(bdata.get("total_chapters") or len(bdata.get("chapters") or []) or 0),
                            "generated_chapters_count": int(bdata.get("generated_chapters_count") or len(bdata.get("chapters") or []) or 0),
                            "mastered_chapters_count": int(bdata.get("mastered_chapters_count") or 0),
                            "total_duration_seconds": float(bdata.get("total_duration_seconds") or 0.0),
                            "is_live_generating": bool(bdata.get("is_live_generating", False)),
                            "cover_url": bdata.get("cover_url") or "",
                            "stream_url": str(bdata.get("stream_url") or ""),
                            "download_url": str(bdata.get("download_url") or ""),
                            "file_size_bytes": bdata.get("file_size_bytes"),
                            "published_deliveries_count": len(bdata.get("deliveries") or []),
                            "updated_at": str(bdata.get("updated_at") or datetime.now(timezone.utc).isoformat()),
                        }
                        books.append(summary)
            except (IOError, OSError, ValueError, TypeError):
                continue

        catalog = {
            "version": "2.0",
            "server_name": "Crazy Audiobook Creator (NAS Shared Library)",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "total_books": len(books),
            "books": sorted(books, key=lambda b: b.get("updated_at", ""), reverse=True),
        }

        catalog_path = posixpath.join(nas_root, "catalog.json")
        self._atomic_write_json(sftp, catalog, catalog_path)
        # Also write API route alias for direct static serving
        api_catalog_path = posixpath.join(nas_root, "api/mobile/v1/catalog")
        self._atomic_write_json(sftp, catalog, api_catalog_path)
        logger.info("Rebuilt NAS global catalog with %d books at %s", len(books), catalog_path)
        return catalog

    def sync_all_projects(self, projects_root: Path | None = None) -> dict[str, Any]:
        """Scan local workstation projects and synchronize all ready books to NAS."""
        if not self.is_configured:
            return {"status": "error", "message": "NAS is not configured."}

        projects_root = Path(projects_root or "brain/projects").resolve()
        if not projects_root.is_dir():
            return {"status": "error", "message": f"Projects root {projects_root} not found."}

        # Query registered active projects from Creator database
        active_project_ids: set[str] = set()
        for db_name in ["pipeline_state.db", "projects.db", "jobs.db"]:
            db_file = projects_root / db_name
            if db_file.is_file():
                try:
                    import sqlite3
                    conn = sqlite3.connect(db_file)
                    cur = conn.cursor()
                    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                    if "projects" in tables:
                        for row in cur.execute("SELECT id FROM projects").fetchall():
                            if row and row[0]:
                                active_project_ids.add(str(row[0]))
                    if "jobs" in tables:
                        for row in cur.execute("SELECT project_id FROM jobs").fetchall():
                            if row and row[0]:
                                active_project_ids.add(str(row[0]))
                    conn.close()
                except Exception:
                    pass

        synced_books = []
        with self.sftp_session() as sftp:
            nas_root = self.resolve_nas_root(sftp)

            for p_dir in sorted(projects_root.iterdir()):
                if not p_dir.is_dir() or p_dir.name.startswith((".", "_")):
                    continue
                project_id = p_dir.name
                if active_project_ids and project_id not in active_project_ids:
                    logger.debug("Skipping un-registered / deleted project folder %s", project_id)
                    continue

                # Check if project has a full M4B or deliveries
                full_m4b = p_dir / f"{project_id}.m4b"
                if not full_m4b.is_file():
                    workspace_full = Path("workspace") / project_id / "output" / f"{project_id}.m4b"
                    if workspace_full.is_file():
                        full_m4b = workspace_full

                proj_remote_dir = posixpath.join(nas_root, project_id)
                self._upload_cover_if_needed(sftp, p_dir, proj_remote_dir)

                if full_m4b.is_file() and full_m4b.stat().st_size > 0:
                    full_remote_dir = posixpath.join(proj_remote_dir, "full")
                    remote_full_path = posixpath.join(full_remote_dir, full_m4b.name)
                    self._atomic_upload(sftp, full_m4b, remote_full_path)
                    synced_books.append({"project_id": project_id, "type": "full"})
                else:
                    # Check deliveries
                    deliv_dir = p_dir / "deliveries"
                    if deliv_dir.is_dir():
                        parts_remote_dir = posixpath.join(proj_remote_dir, "parts")
                        for m4b_part in deliv_dir.glob("*.m4b"):
                            remote_part = posixpath.join(parts_remote_dir, m4b_part.name)
                            self._atomic_upload(sftp, m4b_part, remote_part)
                            synced_books.append({"project_id": project_id, "type": "part", "file": m4b_part.name})

                # Write book.json and api/mobile/v1 alias
                book_manifest = self._generate_book_manifest(project_id, p_dir, sftp, proj_remote_dir)
                self._atomic_write_json(sftp, book_manifest, posixpath.join(proj_remote_dir, "book.json"))
                self._atomic_write_json(sftp, book_manifest, posixpath.join(nas_root, "api/mobile/v1/books", project_id))

            # Rebuild catalog
            catalog = self.rebuild_global_catalog(sftp=sftp, nas_root=nas_root, active_project_ids=active_project_ids)

        return {
            "status": "success",
            "synced_count": len(synced_books),
            "synced_books": synced_books,
            "catalog_total": catalog.get("total_books", 0),
        }

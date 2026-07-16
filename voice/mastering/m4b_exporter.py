"""M4B Exporter — Package mastered chapters into a single M4B audiobook.

Uses FFmpeg to:
  - Encode chapter WAVs to AAC
  - Concatenate with chapter markers
  - Embed metadata and cover art
  - Produce a final .m4b file
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from shared.models import (
    AudiobookMetadata,
    ExportChapterInfo,
    ExportConfig,
    ExportM4BResponse,
)

logger = logging.getLogger(__name__)


class M4BExporter:
    """Export mastered chapter audio files into a single M4B audiobook."""

    def export(
        self,
        project_id: str,
        metadata: AudiobookMetadata,
        chapters: list[ExportChapterInfo],
        cover_art: str | None = None,
        output_config: ExportConfig | None = None,
        workspace: Path = Path("workspace"),
    ) -> ExportM4BResponse:
        """Export all chapters as a single M4B audiobook file.

        Steps:
        1. Create FFmpeg concat input file
        2. Create FFmpeg chapter metadata file
        3. Run FFmpeg to encode and concatenate
        4. Embed cover art if available

        Args:
            project_id: Project identifier.
            metadata: Audiobook metadata.
            chapters: List of chapter info with file paths.
            cover_art: Path to cover image.
            output_config: Encoding configuration.
            workspace: Base workspace directory.

        Returns:
            ExportM4BResponse with output file info.
        """
        config = output_config or ExportConfig()
        project_dir = workspace / project_id
        output_dir = project_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        output_file = output_dir / f"{project_id}.m4b"

        logger.info(
            "Exporting M4B for '%s': %d chapters",
            metadata.title,
            len(chapters),
        )

        # Step 1: Create concat file (list of chapter WAV files)
        concat_file = project_dir / "concat.txt"
        self._write_concat_file(concat_file, chapters, workspace / project_id)

        # Step 2: Create chapter metadata file
        metadata_file = project_dir / "chapters.txt"
        chapter_durations = self._get_chapter_durations(chapters, workspace / project_id)
        self._write_chapter_metadata(metadata_file, chapters, chapter_durations)

        # Step 3: Run FFmpeg
        self._run_ffmpeg(
            concat_file=concat_file,
            metadata_file=metadata_file,
            output_file=output_file,
            book_metadata=metadata,
            cover_art=cover_art,
            config=config,
        )

        # Calculate final stats
        total_duration = sum(chapter_durations)
        file_size_mb = output_file.stat().st_size / (1024 * 1024) if output_file.exists() else 0

        hours = int(total_duration // 3600)
        minutes = int((total_duration % 3600) // 60)
        seconds = int(total_duration % 60)
        duration_str = f"{hours}:{minutes:02d}:{seconds:02d}"

        logger.info(
            "M4B export complete: %s (%s, %.1f MB)",
            output_file.name,
            duration_str,
            file_size_mb,
        )

        return ExportM4BResponse(
            status="success",
            output_file=str(output_file),
            total_duration=duration_str,
            total_chapters=len(chapters),
            file_size_mb=file_size_mb,
            download_url=f"/download/{project_id}/output/{project_id}.m4b",
        )

    def _write_concat_file(
        self,
        concat_file: Path,
        chapters: list[ExportChapterInfo],
        project_dir: Path,
    ) -> None:
        """Write FFmpeg concat demuxer input file."""
        with open(concat_file, "w", encoding="utf-8") as f:
            for chapter in chapters:
                chapter_path = project_dir / chapter.file
                # FFmpeg concat demuxer resolves relative paths relative to the concat file's directory.
                # Use absolute paths to avoid workspace/sample_book/workspace/sample_book/ duplication.
                abs_path = chapter_path.absolute()
                # FFmpeg requires forward slashes and escaped single quotes
                safe_path = str(abs_path).replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{safe_path}'\n")

    def _get_chapter_durations(
        self,
        chapters: list[ExportChapterInfo],
        project_dir: Path,
    ) -> list[float]:
        """Get the duration of each chapter file using ffprobe."""
        durations: list[float] = []

        for chapter in chapters:
            chapter_path = project_dir / chapter.file
            if not chapter_path.exists():
                logger.warning("Chapter file not found: %s", chapter_path)
                durations.append(0.0)
                continue

            try:
                result = subprocess.run(
                    [
                        "ffprobe",
                        "-v", "quiet",
                        "-show_entries", "format=duration",
                        "-of", "csv=p=0",
                        str(chapter_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                duration = float(result.stdout.strip())
                durations.append(duration)
            except Exception as e:
                logger.warning("Failed to get duration for %s: %s", chapter_path, e)
                durations.append(0.0)

        return durations

    def _write_chapter_metadata(
        self,
        metadata_file: Path,
        chapters: list[ExportChapterInfo],
        durations: list[float],
    ) -> None:
        """Write FFmpeg chapter metadata file."""
        with open(metadata_file, "w", encoding="utf-8") as f:
            f.write(";FFMETADATA1\n\n")

            current_time_ms = 0
            for chapter, duration in zip(chapters, durations):
                duration_ms = int(duration * 1000)
                f.write("[CHAPTER]\n")
                f.write("TIMEBASE=1/1000\n")
                f.write(f"START={current_time_ms}\n")
                f.write(f"END={current_time_ms + duration_ms}\n")
                f.write(f"title={chapter.title}\n")
                f.write("\n")
                current_time_ms += duration_ms

    def _run_ffmpeg(
        self,
        concat_file: Path,
        metadata_file: Path,
        output_file: Path,
        book_metadata: AudiobookMetadata,
        cover_art: str | None,
        config: ExportConfig,
    ) -> None:
        """Run FFmpeg to create the final M4B file."""
        cmd = [
            "ffmpeg",
            "-y",  # Overwrite output
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-i", str(metadata_file),
            "-map_metadata", "1",
        ]

        # Add cover art if available
        if cover_art and Path(cover_art).exists():
            cmd.extend(["-i", cover_art])
            cmd.extend(["-map", "0:a", "-map", "2:v"])
            cmd.extend(["-disposition:v", "attached_pic"])
        else:
            cmd.extend(["-map", "0:a"])

        # Audio encoding
        cmd.extend([
            "-c:a", config.codec,
            "-b:a", config.bitrate,
            "-ar", "44100",
            "-ac", str(config.channels),
        ])

        # Metadata
        cmd.extend([
            "-metadata", f"title={book_metadata.title}",
            "-metadata", f"artist={book_metadata.author}",
            "-metadata", f"album={book_metadata.title}",
            "-metadata", f"genre={book_metadata.genre}",
            "-metadata", f"comment={book_metadata.description or 'Generated by Crazy Audiobook Creator'}",
        ])

        if book_metadata.year:
            cmd.extend(["-metadata", f"date={book_metadata.year}"])

        cmd.append(str(output_file))

        logger.info("Running FFmpeg: %s", " ".join(cmd[:10]) + "...")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,  # 10 minute timeout
            )

            if result.returncode != 0:
                logger.error("FFmpeg failed:\n%s", result.stderr)
                raise RuntimeError(f"FFmpeg export failed: {result.stderr[:500]}")

            logger.info("FFmpeg completed successfully")

        except subprocess.TimeoutExpired:
            raise RuntimeError("FFmpeg timed out after 10 minutes")
        except FileNotFoundError:
            raise RuntimeError(
                "FFmpeg not found. Install it: sudo apt install ffmpeg"
            )

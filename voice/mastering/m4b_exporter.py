"""M4B Exporter — Package mastered chapters into a single M4B audiobook.

Uses FFmpeg to:
  - Encode chapter WAVs to AAC
  - Concatenate with chapter markers
  - Embed metadata and cover art
  - Produce a final .m4b file
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from shared import paths as shared_paths
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
        workspace: Path | None = None,
        output_name: str | None = None,
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
        # `workspace` defaulted to a bare relative Path("workspace"), which
        # silently depended on the process working directory. Resolve from the
        # repository root instead when the caller supplies nothing.
        if workspace is None:
            workspace = shared_paths.WORKSPACE_DIR
        project_dir = workspace / project_id
        output_dir = project_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        safe_output_name = Path(output_name or f"{project_id}.m4b").name
        if Path(safe_output_name).suffix.lower() != ".m4b":
            safe_output_name += ".m4b"
        output_file = output_dir / safe_output_name

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
        temporary_output = output_file.with_name(f".{output_file.name}.part.m4b")
        temporary_output.unlink(missing_ok=True)
        try:
            self._run_ffmpeg(
                concat_file=concat_file,
                metadata_file=metadata_file,
                output_file=temporary_output,
                book_metadata=metadata,
                cover_art=cover_art,
                config=config,
            )
            if not temporary_output.is_file() or temporary_output.stat().st_size == 0:
                raise RuntimeError("FFmpeg produced no M4B output")
            try:
                os.replace(temporary_output, output_file)
            except OSError:
                import shutil

                with open(output_file, "wb") as dst, open(temporary_output, "rb") as src:
                    shutil.copyfileobj(src, dst)
        finally:
            temporary_output.unlink(missing_ok=True)

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
            download_url=f"/download/{project_id}/output/{output_file.name}",
            book_loudness=self._book_loudness_report(chapters),
        )

    @staticmethod
    def _book_loudness_report(
        chapters: list[ExportChapterInfo],
    ) -> dict[str, Any]:
        """Summarize mastered chapter consistency without changing the audio."""
        measured = [
            (chapter.number, float(chapter.lufs), chapter.peak_dbfs) for chapter in chapters if chapter.lufs is not None
        ]
        if not measured:
            return {
                "status": "unavailable",
                "measured_chapters": 0,
                "spread_lu": None,
                "outlier_chapters": [],
            }
        import statistics

        loudness = [item[1] for item in measured]
        median_lufs = float(statistics.median(loudness))
        spread_lu = float(max(loudness) - min(loudness))
        outliers = [number for number, value, _ in measured if abs(value - median_lufs) > 0.75]
        peaks = [float(peak) for _, _, peak in measured if peak is not None]
        return {
            "status": "consistent" if spread_lu <= 1.0 else "warning",
            "measured_chapters": len(measured),
            "median_lufs": median_lufs,
            "minimum_lufs": float(min(loudness)),
            "maximum_lufs": float(max(loudness)),
            "spread_lu": spread_lu,
            "outlier_chapters": outliers,
            "highest_peak_dbfs": max(peaks) if peaks else None,
        }

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
                raise FileNotFoundError(f"Chapter file not found: {chapter_path}")

            try:
                result = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "quiet",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "csv=p=0",
                        str(chapter_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.strip() or "ffprobe failed")
                duration = float(result.stdout.strip())
                if duration <= 0:
                    raise RuntimeError("chapter duration is zero")
                durations.append(duration)
            except Exception as e:
                raise RuntimeError(f"Failed to inspect chapter audio {chapter_path}: {e}") from e

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
                formatted_title = self._format_chapter_title(chapter.number, chapter.title)
                f.write("[CHAPTER]\n")
                f.write("TIMEBASE=1/1000\n")
                f.write(f"START={current_time_ms}\n")
                f.write(f"END={current_time_ms + duration_ms}\n")
                f.write(f"title={self._escape_ffmetadata(formatted_title)}\n")
                f.write("\n")
                current_time_ms += duration_ms

    @staticmethod
    def _format_chapter_title(number: int, title: str | None) -> str:
        """Format chapter title for M4B metadata using the book chapter title."""
        clean_title = (title or "").strip()
        if clean_title:
            return clean_title
        return f"Chapter {number}"

    @staticmethod
    def _escape_ffmetadata(value: str) -> str:
        """Escape a value for FFmpeg's FFMETADATA format."""
        return (
            str(value)
            .replace("\\", "\\\\")
            .replace("\r", "")
            .replace("\n", "\\\n")
            .replace("=", "\\=")
            .replace(";", "\\;")
            .replace("#", "\\#")
        )

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
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-i",
            str(metadata_file),
            "-map_metadata",
            "1",
        ]

        # Add cover art if available
        if cover_art and Path(cover_art).exists():
            cmd.extend(["-i", cover_art])
            cmd.extend(["-map", "0:a", "-map", "2:v"])
            cmd.extend(["-disposition:v", "attached_pic"])
        else:
            cmd.extend(["-map", "0:a"])

        # Audio encoding
        cmd.extend(
            [
                "-c:a",
                config.codec,
                "-b:a",
                config.bitrate,
                "-ar",
                "44100",
                "-ac",
                str(config.channels),
            ]
        )

        # Metadata
        cmd.extend(
            [
                "-metadata",
                f"title={book_metadata.title}",
                "-metadata",
                f"artist={book_metadata.author}",
                "-metadata",
                f"album={book_metadata.title}",
                "-metadata",
                f"genre={book_metadata.genre}",
                "-metadata",
                f"comment={book_metadata.description or 'Generated by Crazy Audiobook Creator'}",
            ]
        )

        if book_metadata.year:
            cmd.extend(["-metadata", f"date={book_metadata.year}"])
        if book_metadata.isbn:
            cmd.extend(
                [
                    "-metadata",
                    f"isbn={book_metadata.isbn}",
                    "-metadata",
                    f"grouping=ISBN {book_metadata.isbn}",
                ]
            )

        cmd.extend(["-movflags", "+faststart"])

        cmd.append(str(output_file))

        logger.info("Running FFmpeg: %s", " ".join(cmd[:10]) + "...")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=7200,  # 2 hour timeout
            )

            if result.returncode != 0:
                logger.error("FFmpeg failed:\n%s", result.stderr)
                raise RuntimeError(f"FFmpeg export failed: {result.stderr[-1000:]}")

            logger.info("FFmpeg completed successfully")

        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("FFmpeg export timed out") from exc
        except FileNotFoundError as exc:
            raise RuntimeError("FFmpeg not found. Install FFmpeg and add it to PATH") from exc

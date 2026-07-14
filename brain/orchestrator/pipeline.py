"""Pipeline orchestrator — End-to-end audiobook production pipeline.

Coordinates all stages:
  ① Text Extraction → ② LLM Script Director → ③ Voice Bootstrapping →
  ④ TTS Generation → ⑤ Quality Validation → ⑥ Audio Mastering → ⑦ M4B Export

State is persisted to SQLite so the pipeline can resume after interruption.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from brain.director.character_analyzer import CharacterAnalyzer
from brain.director.ollama_client import OllamaClient
from brain.director.script_generator import ScriptGenerator
from brain.extractor.epub_parser import EpubParser
from brain.orchestrator.job_queue import JobQueue, JobState
from brain.orchestrator.ubuntu_client import UbuntuClient
from shared.constants import PipelineStage
from shared.models import (
    AudiobookMetadata,
    BootstrapVoicesRequest,
    BookScript,
    ExportChapterInfo,
    ExportM4BRequest,
    GenerateChapterRequest,
    MasterChapterRequest,
    MasterSegmentInfo,
    ProjectStatus,
)

logger = logging.getLogger(__name__)


class Pipeline:
    """End-to-end audiobook production pipeline."""

    def __init__(
        self,
        config_path: str | Path = "brain/config.yaml",
        projects_dir: str | Path = "brain/projects",
    ):
        self.config_path = Path(config_path)
        self.projects_dir = Path(projects_dir)
        self.projects_dir.mkdir(parents=True, exist_ok=True)

        self.config = self._load_config()
        self.job_queue = JobQueue(
            db_path=str(self.projects_dir / self.config.get("pipeline", {}).get("state_db", "pipeline_state.db"))
        )

        # Initialize clients
        ollama_cfg = self.config.get("ollama", {})
        self.ollama = OllamaClient(
            host=ollama_cfg.get("host", "http://localhost:11434"),
            model=ollama_cfg.get("model", "qwen3:32b"),
            timeout=ollama_cfg.get("timeout", 120),
            max_retries=ollama_cfg.get("max_retries", 3),
        )

        ubuntu_cfg = self.config.get("ubuntu", {})
        self.ubuntu = UbuntuClient(
            host=ubuntu_cfg.get("host", "http://192.168.1.100:8100"),
            timeout=ubuntu_cfg.get("timeout", 30),
            retries=ubuntu_cfg.get("retries", 3),
            retry_delay=ubuntu_cfg.get("retry_delay", 5),
            reconnect_interval=ubuntu_cfg.get("reconnect_interval", 60),
        )

        # Extraction config
        extract_cfg = self.config.get("extraction", {})
        self.parser = EpubParser(
            skip_toc=extract_cfg.get("skip_toc", True),
            skip_appendices=extract_cfg.get("skip_appendices", True),
            skip_front_matter=extract_cfg.get("skip_front_matter", True),
            min_chapter_words=extract_cfg.get("min_chapter_words", 100),
            max_chapter_words=extract_cfg.get("max_chapter_words", 20_000),
            chapter_detection=extract_cfg.get("chapter_detection", "auto"),
            preserve_poetry=extract_cfg.get("preserve_poetry", True),
        )

        # Director config
        self.character_analyzer = CharacterAnalyzer(
            ollama=self.ollama,
            temperature=ollama_cfg.get("temperature_pass1", 0.3),
            max_unique_voices=self.config.get("script", {}).get("max_unique_voices", 20),
        )

        script_cfg = self.config.get("script", {})
        self.script_generator = ScriptGenerator(
            ollama=self.ollama,
            temperature=ollama_cfg.get("temperature_pass2", 0.4),
            chunk_size_words=script_cfg.get("chunk_size_words", 5000),
            chunk_overlap_words=script_cfg.get("chunk_overlap_words", 500),
        )

        self._stop_flags: dict[str, bool] = {}

    def stop(self, project_id: str) -> None:
        """Signal the pipeline for a project to stop gracefully."""
        self._stop_flags[project_id] = True

    def _check_stop(self, project_id: str) -> None:
        """Raise KeyboardInterrupt if a stop was requested."""
        if self._stop_flags.get(project_id):
            raise KeyboardInterrupt("Pipeline stopped via API")

    def _load_config(self) -> dict[str, Any]:
        """Load pipeline configuration from YAML."""
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        logger.warning("Config file not found: %s — using defaults", self.config_path)
        return {}

    # ------------------------------------------------------------------
    # Project creation
    # ------------------------------------------------------------------

    def create_project(self, epub_path: str | Path) -> ProjectStatus:
        """Create a new audiobook project from an EPUB file.

        Steps:
        1. Parse EPUB to extract text and metadata
        2. Create project directory structure
        3. Save extracted text to project
        4. Initialize pipeline state in job queue

        Args:
            epub_path: Path to the EPUB file.

        Returns:
            Initial ProjectStatus.
        """
        # Stage ① — Text Extraction
        book = self.parser.parse(epub_path)

        # Create project ID from title
        project_id = self._make_project_id(book.metadata.title)
        project_dir = self.projects_dir / project_id
        project_dir.mkdir(parents=True, exist_ok=True)

        # Save extracted book
        book_path = project_dir / "book.json"
        with open(book_path, "w", encoding="utf-8") as f:
            f.write(book.model_dump_json(indent=2))

        # Copy EPUB cover if extracted
        if book.metadata.cover_image_path:
            cover_src = Path(book.metadata.cover_image_path)
            if cover_src.exists():
                cover_dest = project_dir / cover_src.name
                cover_dest.write_bytes(cover_src.read_bytes())
                book.metadata.cover_image_path = str(cover_dest)

        # Initialize job state
        status = ProjectStatus(
            project_id=project_id,
            title=book.metadata.title,
            author=book.metadata.author,
            status=PipelineStage.CREATED,
            total_chapters=book.metadata.total_chapters,
            total_lines=0,
            started_at=datetime.now(timezone.utc),
        )

        self.job_queue.create_job(project_id, status.model_dump())

        logger.info(
            "Created project '%s': %d chapters, %d words",
            project_id,
            book.metadata.total_chapters,
            book.metadata.total_words,
        )

        return status

    # ------------------------------------------------------------------
    # Full pipeline run
    # ------------------------------------------------------------------

    def run(self, project_id: str) -> ProjectStatus:
        """Run the full pipeline for a project.

        Resumes from the last completed stage if the pipeline was interrupted.

        Args:
            project_id: The project to process.

        Returns:
            Final ProjectStatus.
        """
        project_dir = self.projects_dir / project_id
        if not project_dir.exists():
            raise ValueError(f"Project not found: {project_id}")

        # Load current state
        state = self.job_queue.get_job(project_id)
        current_stage = state.get("status", PipelineStage.CREATED)

        start_time = time.time()
        self._stop_flags[project_id] = False
        logger.info("Starting pipeline for '%s' from stage: %s", project_id, current_stage)

        try:
            # Stage ②: LLM Script Director
            self._check_stop(project_id)
            if current_stage in (PipelineStage.CREATED, PipelineStage.EXTRACTING):
                self._run_script_director(project_id, project_dir)

            # Stage ③: Voice Bootstrapping
            self._check_stop(project_id)
            state = self.job_queue.get_job(project_id)
            if state.get("status") == PipelineStage.SCRIPTING:
                self._update_stage(project_id, PipelineStage.SCRIPTING)

            if state.get("status") in (PipelineStage.SCRIPTING, PipelineStage.BOOTSTRAPPING):
                self._run_voice_bootstrap(project_id, project_dir)

            # Stage ④-⑤: TTS Generation + Validation
            self._check_stop(project_id)
            state = self.job_queue.get_job(project_id)
            if state.get("status") in (PipelineStage.BOOTSTRAPPING, PipelineStage.GENERATING):
                self._run_generation(project_id, project_dir)

            # Stage ⑥: Audio Mastering
            self._check_stop(project_id)
            state = self.job_queue.get_job(project_id)
            if state.get("status") in (PipelineStage.GENERATING, PipelineStage.MASTERING):
                self._run_mastering(project_id, project_dir)

            # Stage ⑦: M4B Export
            self._check_stop(project_id)
            state = self.job_queue.get_job(project_id)
            if state.get("status") in (PipelineStage.MASTERING, PipelineStage.EXPORTING):
                self._run_export(project_id, project_dir)

            elapsed = time.time() - start_time
            self._update_stage(
                project_id,
                PipelineStage.COMPLETE,
                elapsed_seconds=elapsed,
            )
            logger.info(
                "Pipeline complete for '%s' in %.1f minutes",
                project_id,
                elapsed / 60,
            )

        except Exception as e:
            elapsed = time.time() - start_time
            logger.error("Pipeline failed for '%s': %s", project_id, e, exc_info=True)
            self._update_stage(
                project_id,
                PipelineStage.ERROR,
                error_message=str(e),
                elapsed_seconds=elapsed,
            )
            raise
        except KeyboardInterrupt:
            elapsed = time.time() - start_time
            logger.info("Pipeline paused for '%s' (interrupted)", project_id)
            self._update_stage(
                project_id,
                PipelineStage.PAUSED,
                error_message="Interrupted by user",
                elapsed_seconds=elapsed,
            )
            return ProjectStatus(**self.job_queue.get_job(project_id))

        return ProjectStatus(**self.job_queue.get_job(project_id))

    # ------------------------------------------------------------------
    # Stage runners
    # ------------------------------------------------------------------

    def _run_script_director(self, project_id: str, project_dir: Path) -> None:
        """Run Stage ②: LLM character analysis + script generation."""
        self._update_stage(project_id, PipelineStage.SCRIPTING)

        # Load extracted book
        book_path = project_dir / "book.json"
        from shared.models import ExtractedBook
        book = ExtractedBook.model_validate_json(book_path.read_text(encoding="utf-8"))

        # Pass 1: Character analysis
        logger.info("Pass 1: Analyzing characters...")
        registry = self.character_analyzer.analyze(book)

        # Save character registry
        chars_path = project_dir / "characters.json"
        with open(chars_path, "w", encoding="utf-8") as f:
            f.write(registry.model_dump_json(indent=2))

        # Pass 2: Script generation for each chapter
        logger.info("Pass 2: Generating scripts...")
        scripts_dir = project_dir / "script"
        scripts_dir.mkdir(exist_ok=True)

        chapter_scripts = self.script_generator.generate_all_chapters(
            book.chapters, registry
        )

        total_lines = 0
        for script in chapter_scripts:
            script_path = scripts_dir / f"chapter_{script.chapter_number:03d}.json"
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(script.model_dump_json(indent=2))
            total_lines += script.total_lines

        # Build complete BookScript for reference
        book_script = BookScript(
            metadata=book.metadata,
            character_registry=registry,
            chapters=chapter_scripts,
        )
        with open(project_dir / "book_script.json", "w", encoding="utf-8") as f:
            f.write(book_script.model_dump_json(indent=2))

        self.job_queue.update_job(project_id, {"total_lines": total_lines, "script_completed": True})
        logger.info("Script generation complete: %d total lines", total_lines)

    def _run_voice_bootstrap(self, project_id: str, project_dir: Path) -> None:
        """Run Stage ③: Generate voice reference clips on Ubuntu."""
        self._update_stage(project_id, PipelineStage.BOOTSTRAPPING)

        # Load character registry
        chars_path = project_dir / "characters.json"
        from shared.models import CharacterRegistry
        registry = CharacterRegistry.model_validate_json(
            chars_path.read_text(encoding="utf-8")
        )

        # Send to Ubuntu for voice bootstrapping
        request = BootstrapVoicesRequest(
            project_id=project_id,
            characters=registry.characters,
        )
        try:
            response = self.ubuntu.bootstrap_voices(request)
            self.job_queue.update_job(project_id, {"bootstrapping_completed": True})
            logger.info(
                "Voice bootstrapping complete: %d voices generated",
                len(response.voices_generated),
            )
        except Exception as e:
            logger.error("Failed to bootstrap voices: %s", e)
            raise

    def _run_generation(self, project_id: str, project_dir: Path) -> None:
        """Run Stages ④-⑤: TTS generation with quality validation."""
        self._update_stage(project_id, PipelineStage.GENERATING)

        scripts_dir = project_dir / "script"
        script_files = sorted(scripts_dir.glob("chapter_*.json"))

        from shared.models import ScriptChapter
        
        state = self.job_queue.get_job(project_id)
        completed_gen_chapters = state.get("completed_gen_chapters", [])

        for script_file in script_files:
            self._check_stop(project_id)
            chapter_script = ScriptChapter.model_validate_json(
                script_file.read_text(encoding="utf-8")
            )

            if chapter_script.chapter_number in completed_gen_chapters:
                logger.info("Skipping chapter %d (already generated)", chapter_script.chapter_number)
                continue

            try:
                request = GenerateChapterRequest(
                    project_id=project_id,
                    chapter_number=chapter_script.chapter_number,
                    lines=chapter_script.lines,
                    validate=True,
                    auto_retry=True,
                    max_retries=self.config.get("validation", {}).get("max_retries", 3),
                )

                response = self.ubuntu.generate_chapter(request)

                completed_gen_chapters.append(chapter_script.chapter_number)
                self.job_queue.update_job(project_id, {
                    "completed_gen_chapters": completed_gen_chapters,
                    "current_chapter": chapter_script.chapter_number,
                    "lines_generated": response.generated,
                    "lines_failed": response.failed_validation,
                })

                logger.info(
                    "Chapter %d generated: %d/%d lines, %d retried",
                    chapter_script.chapter_number,
                    response.generated,
                    response.total_lines,
                    response.retried,
                )
            except Exception as e:
                logger.error("Failed to generate chapter %d: %s", chapter_script.chapter_number, e)
                raise

    def _run_mastering(self, project_id: str, project_dir: Path) -> None:
        """Run Stage ⑥: Audio mastering."""
        self._update_stage(project_id, PipelineStage.MASTERING)

        scripts_dir = project_dir / "script"
        script_files = sorted(scripts_dir.glob("chapter_*.json"))

        from shared.models import ScriptChapter
        mastering_cfg = self.config.get("mastering", {})
        
        state = self.job_queue.get_job(project_id)
        completed_master_chapters = state.get("completed_master_chapters", [])

        for script_file in script_files:
            self._check_stop(project_id)
            chapter_script = ScriptChapter.model_validate_json(
                script_file.read_text(encoding="utf-8")
            )
            
            if chapter_script.chapter_number in completed_master_chapters:
                logger.info("Skipping chapter %d (already mastered)", chapter_script.chapter_number)
                continue

            segments = [
                MasterSegmentInfo(
                    line_id=line.line_id,
                    file=f"workspace/{project_id}/segments/{line.line_id}.wav",
                    pause_before_ms=line.pause_before_ms,
                    pause_after_ms=line.pause_after_ms,
                )
                for line in chapter_script.lines
            ]

            try:
                request = MasterChapterRequest(
                    project_id=project_id,
                    chapter_number=chapter_script.chapter_number,
                    segments=segments,
                )

                response = self.ubuntu.master_chapter(request)
                
                completed_master_chapters.append(chapter_script.chapter_number)
                self.job_queue.update_job(project_id, {
                    "completed_master_chapters": completed_master_chapters,
                })
                
                logger.info(
                    "Chapter %d mastered: %.1f seconds, %.1f LUFS",
                    chapter_script.chapter_number,
                    response.duration_seconds,
                    response.lufs,
                )
            except Exception as e:
                logger.error("Failed to master chapter %d: %s", chapter_script.chapter_number, e)
                raise

    def _run_export(self, project_id: str, project_dir: Path) -> None:
        """Run Stage ⑦: M4B export."""
        self._update_stage(project_id, PipelineStage.EXPORTING)

        # Load metadata
        book_path = project_dir / "book.json"
        from shared.models import ExtractedBook
        book = ExtractedBook.model_validate_json(book_path.read_text(encoding="utf-8"))

        # Build chapter list
        scripts_dir = project_dir / "script"
        script_files = sorted(scripts_dir.glob("chapter_*.json"))

        from shared.models import ScriptChapter
        chapters: list[ExportChapterInfo] = []
        for script_file in script_files:
            ch = ScriptChapter.model_validate_json(
                script_file.read_text(encoding="utf-8")
            )
            chapters.append(ExportChapterInfo(
                number=ch.chapter_number,
                title=ch.chapter_title,
                file=f"chapters/chapter_{ch.chapter_number:03d}.wav",
            ))

        request = ExportM4BRequest(
            project_id=project_id,
            metadata=AudiobookMetadata(
                title=book.metadata.title,
                author=book.metadata.author,
            ),
            chapters=chapters,
        )

        response = self.ubuntu.export_m4b(request)

        # Download the M4B file to the local project directory
        if response.download_url:
            local_m4b = project_dir / f"{project_id}.m4b"
            self.ubuntu.download_file(
                project_id,
                f"output/{project_id}.m4b",
                str(local_m4b),
            )
            logger.info("M4B saved to: %s", local_m4b)

        logger.info(
            "Export complete: %s, %s, %.1f MB",
            response.total_duration,
            f"{response.total_chapters} chapters",
            response.file_size_mb,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _update_stage(
        self,
        project_id: str,
        stage: PipelineStage,
        **extra: Any,
    ) -> None:
        """Update the pipeline stage in the job queue."""
        update = {"status": stage, **extra}
        self.job_queue.update_job(project_id, update)
        logger.info("Pipeline stage: %s → %s", project_id, stage)

    @staticmethod
    def _make_project_id(title: str) -> str:
        """Generate a URL-safe project ID from a book title."""
        import re
        # Convert to lowercase, replace spaces/special chars with hyphens
        project_id = title.lower().strip()
        project_id = re.sub(r"[^\w\s-]", "", project_id)
        project_id = re.sub(r"[-\s]+", "-", project_id)
        return project_id[:64]  # Cap length

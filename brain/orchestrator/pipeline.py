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
from brain.director.character_analyzer import CharacterAnalyzer
from brain.director.ollama_client import OllamaClient
from brain.director.script_generator import ScriptGenerator
from brain.extractor.epub_parser import EpubParser
from brain.orchestrator.job_queue import JobQueue, JobState
from brain.orchestrator.voice_client import VoiceClient
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

        voice_cfg = self.config.get("voice_server", {})
        self.voice_client = VoiceClient(
            host=voice_cfg.get("host", "http://127.0.0.1:8100"),
            timeout=voice_cfg.get("timeout", 3600),
            retries=voice_cfg.get("retries", 3),
            retry_delay=voice_cfg.get("retry_delay", 2),
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
        self._voice_server_proc = None

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
    # Voice Server Process Management
    # ------------------------------------------------------------------

    def _start_voice_server(self) -> None:
        """Start local Voice Server subprocess if auto_start is enabled."""
        voice_cfg = self.config.get("voice_server", {})
        if not voice_cfg.get("auto_start", True):
            logger.info("Voice server auto_start is disabled in config.")
            return

        try:
            health = self.voice_client.health_check()
            if health.status == "ok":
                logger.info("Voice server is already running and healthy.")
                return
        except Exception:
            pass

        venv_py = voice_cfg.get("venv", r"E:\PyTorch env\my_venv")
        python_exe = Path(venv_py) / "Scripts" / "python.exe"
        if not python_exe.exists():
            import sys
            python_exe = Path(sys.executable)

        logger.info("Starting local Voice Server subprocess via %s...", python_exe)
        import os
        import subprocess
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd())

        self._voice_server_proc = subprocess.Popen(
            [str(python_exe), "-m", "voice.tts_server.main"],
            cwd=str(Path.cwd()),
            env=env,
        )

        timeout = voice_cfg.get("startup_timeout_seconds", 120)
        if not self.voice_client.wait_for_server(max_wait_seconds=timeout):
            self._stop_voice_server()
            raise RuntimeError(f"Voice server subprocess failed to start within {timeout}s")

    def _stop_voice_server(self) -> None:
        """Stop Voice Server subprocess if managed by this pipeline."""
        if getattr(self, "_voice_server_proc", None) is not None:
            logger.info("Stopping Voice Server subprocess...")
            try:
                self._voice_server_proc.terminate()
                self._voice_server_proc.wait(timeout=10)
            except Exception as e:
                logger.warning("Force killing Voice Server subprocess: %s", e)
                try:
                    self._voice_server_proc.kill()
                except Exception:
                    pass
            finally:
                self._voice_server_proc = None

    # ------------------------------------------------------------------
    # Schedule & Deployment Controls
    # ------------------------------------------------------------------

    def _check_schedule(self, project_id: str) -> None:
        """Pause pipeline if outside configured working hours."""
        schedule_cfg = self.config.get("schedule", {})
        if not schedule_cfg.get("enabled", False):
            return

        windows = schedule_cfg.get("windows", [])
        if not windows:
            return

        import datetime
        now = datetime.datetime.now()
        current_day = now.strftime("%A")

        in_window = False
        for win in windows:
            days = win.get("days", [])
            if days and current_day not in days:
                continue
            start_str = win.get("start", "00:00")
            end_str = win.get("end", "23:59")
            start_time = datetime.datetime.strptime(start_str, "%H:%M").time()
            end_time = datetime.datetime.strptime(end_str, "%H:%M").time()
            now_time = now.time()

            if start_time <= end_time:
                if start_time <= now_time <= end_time:
                    in_window = True
                    break
            else:
                if now_time >= start_time or now_time <= end_time:
                    in_window = True
                    break

        if not in_window:
            logger.info("Outside working hours window — pausing pipeline (PAUSED_SCHEDULED)")
            self._update_stage(project_id, PipelineStage.PAUSED_SCHEDULED)
            while True:
                self._check_stop(project_id)
                time.sleep(30)
                self.config = self._load_config()
                schedule_cfg = self.config.get("schedule", {})
                if not schedule_cfg.get("enabled", False):
                    break
                now = datetime.datetime.now()
                current_day = now.strftime("%A")
                still_out = True
                for win in schedule_cfg.get("windows", []):
                    if win.get("days") and current_day not in win.get("days"):
                        continue
                    s_t = datetime.datetime.strptime(win.get("start", "00:00"), "%H:%M").time()
                    e_t = datetime.datetime.strptime(win.get("end", "23:59"), "%H:%M").time()
                    n_t = now.time()
                    if (s_t <= e_t and s_t <= n_t <= e_t) or (s_t > e_t and (n_t >= s_t or n_t <= e_t)):
                        still_out = False
                        break
                if not still_out:
                    break
            logger.info("Schedule window opened — resuming pipeline execution")

    def _check_deployment_pause(self, project_id: str) -> None:
        """Pause pipeline if user requested a safe deployment parking point."""
        state = self.job_queue.get_job(project_id)
        if state.get("deployment_requested", False):
            logger.info("Safe deployment pause requested — parking pipeline at chapter boundary")
            self._update_stage(project_id, PipelineStage.DEPLOY_PAUSED)
            try:
                from plyer import notification
                notification.notify(
                    title="Audiobook Creator — Safe Deployment",
                    message=f"Pipeline parked for project '{project_id}'. Safe to deploy updates.",
                    app_name="Audiobook Creator",
                )
            except Exception:
                pass

            while True:
                self._check_stop(project_id)
                time.sleep(5)
                st = self.job_queue.get_job(project_id)
                if not st.get("deployment_requested", False):
                    logger.info("Deployment pause cleared — resuming pipeline execution")
                    break

    # ------------------------------------------------------------------
    # Project creation
    # ------------------------------------------------------------------

    def create_project(self, epub_path: str | Path) -> ProjectStatus:
        """Create a new audiobook project from an EPUB file."""
        book = self.parser.parse(epub_path)
        project_id = self._make_project_id(book.metadata.title)
        
        base_id = project_id
        counter = 1
        while True:
            try:
                self.job_queue.get_job(project_id)
                project_id = f"{base_id}-{counter}"
                counter += 1
            except KeyError:
                break

        project_dir = self.projects_dir / project_id
        project_dir.mkdir(parents=True, exist_ok=True)

        book_path = project_dir / "book.json"
        with open(book_path, "w", encoding="utf-8") as f:
            f.write(book.model_dump_json(indent=2))

        if book.metadata.cover_image_path:
            cover_src = Path(book.metadata.cover_image_path)
            if cover_src.exists():
                cover_dest = project_dir / cover_src.name
                cover_dest.write_bytes(cover_src.read_bytes())
                book.metadata.cover_image_path = str(cover_dest)

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
        """Run the full pipeline for a project."""
        project_dir = self.projects_dir / project_id
        if not project_dir.exists():
            raise ValueError(f"Project not found: {project_id}")

        state = self.job_queue.get_job(project_id)
        current_stage = state.get("status", PipelineStage.CREATED)

        # When starting or re-running a pipeline:
        # Determine the appropriate stage to resume from based on completed phases.
        if current_stage in (PipelineStage.COMPLETE, PipelineStage.SELECTION_COMPLETE, PipelineStage.PAUSED, PipelineStage.ERROR, PipelineStage.PAUSED_SCHEDULED, PipelineStage.DEPLOY_PAUSED):
            if state.get("bootstrapping_completed", False):
                current_stage = PipelineStage.GENERATING
            elif state.get("script_completed", False):
                current_stage = PipelineStage.BOOTSTRAPPING
            else:
                current_stage = PipelineStage.CREATED
            self.job_queue.update_job(project_id, {"status": current_stage.value})

        start_time = time.time()
        self._stop_flags[project_id] = False
        logger.info("Starting pipeline for '%s' from stage: %s", project_id, current_stage)

        try:
            self._start_voice_server()

            # Stage ②: LLM Script Director
            self._check_stop(project_id)
            if current_stage in (PipelineStage.CREATED, PipelineStage.EXTRACTING, PipelineStage.SCRIPTING):
                self._run_script_director(project_id, project_dir)

            # Stage ③: Voice Bootstrapping
            self._check_stop(project_id)
            state = self.job_queue.get_job(project_id)
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
            selection = state.get("generation_chapter_selection")

            if selection is not None:
                # Selective run complete — do partial export
                self._run_export(project_id, project_dir, partial=True)
                self._update_stage(project_id, PipelineStage.SELECTION_COMPLETE)
                logger.info("Selection batch complete for '%s'", project_id)
            else:
                if state.get("status") in (PipelineStage.MASTERING, PipelineStage.EXPORTING):
                    self._run_export(project_id, project_dir, partial=False)

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
        finally:
            self._stop_voice_server()

        return ProjectStatus(**self.job_queue.get_job(project_id))

    # ------------------------------------------------------------------
    # Stage runners
    # ------------------------------------------------------------------

    def _run_script_director(self, project_id: str, project_dir: Path) -> None:
        """Run Stage ②: LLM character analysis + script generation."""
        self._update_stage(project_id, PipelineStage.SCRIPTING)

        book_path = project_dir / "book.json"
        from shared.models import ExtractedBook
        book = ExtractedBook.model_validate_json(book_path.read_text(encoding="utf-8"))

        t0 = time.time()
        pass1_elapsed = 0.0

        chars_path = project_dir / "characters.json"
        if chars_path.exists():
            from shared.models import CharacterRegistry
            registry = CharacterRegistry.model_validate_json(chars_path.read_text(encoding="utf-8"))
        else:
            registry = self.character_analyzer.analyze(book)
            pass1_elapsed = time.time() - t0

            with open(chars_path, "w", encoding="utf-8") as f:
                f.write(registry.model_dump_json(indent=2))

        scripts_dir = project_dir / "script"
        scripts_dir.mkdir(exist_ok=True)

        def on_chapter_scripted(chapter_script):
            self._check_stop(project_id)
            state = self.job_queue.get_job(project_id)
            scripted = state.get("scripted_chapters", [])
            if chapter_script.chapter_number not in scripted:
                scripted.append(chapter_script.chapter_number)
                self.job_queue.update_job(project_id, {
                    "scripted_chapters": scripted,
                    "current_script_chapter": chapter_script.chapter_number,
                })

        chapter_scripts = self.script_generator.generate_all_chapters(
            book.chapters, registry, scripts_dir=scripts_dir, progress_callback=on_chapter_scripted
        )

        total_lines = 0
        for script in chapter_scripts:
            script_path = scripts_dir / f"chapter_{script.chapter_number:03d}.json"
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(script.model_dump_json(indent=2))
            total_lines += script.total_lines

        book_script = BookScript(
            metadata=book.metadata,
            character_registry=registry,
            chapters=chapter_scripts,
        )
        with open(project_dir / "book_script.json", "w", encoding="utf-8") as f:
            f.write(book_script.model_dump_json(indent=2))

        total_elapsed = time.time() - t0
        self.job_queue.update_job(project_id, {
            "total_lines": total_lines,
            "script_completed": True,
            "current_script_chapter": None,
        })

    def _run_voice_bootstrap(self, project_id: str, project_dir: Path) -> None:
        """Run Stage ③: Generate voice reference clips."""
        self._update_stage(project_id, PipelineStage.BOOTSTRAPPING)

        chars_path = project_dir / "characters.json"
        from shared.models import CharacterRegistry
        registry = CharacterRegistry.model_validate_json(
            chars_path.read_text(encoding="utf-8")
        )

        request = BootstrapVoicesRequest(
            project_id=project_id,
            characters=registry.characters,
        )
        try:
            response = self.voice_client.bootstrap_voices(request)
            self.job_queue.update_job(project_id, {"bootstrapping_completed": True})
            logger.info("Voice bootstrapping complete: %d voices generated", len(response.voices_generated))
        except Exception as e:
            logger.error("Failed to bootstrap voices: %s", e)
            raise

    def _run_generation(self, project_id: str, project_dir: Path) -> None:
        """Run Stages ④-⑤: TTS generation with quality validation."""
        self._update_stage(project_id, PipelineStage.GENERATING)

        scripts_dir = project_dir / "script"
        script_files = sorted(scripts_dir.glob("chapter_*.json"))

        from shared.models import ScriptChapter
        import json
        import re

        pronunciation_dict = {}
        global_dict_path = Path("brain/pronunciation_dict.json")
        if global_dict_path.exists():
            try:
                pronunciation_dict.update(json.loads(global_dict_path.read_text(encoding="utf-8")))
            except Exception as e:
                logger.warning("Failed to load global pronunciation_dict.json: %s", e)

        proj_dict_path = project_dir / "pronunciation_dict.json"
        if proj_dict_path.exists():
            try:
                pronunciation_dict.update(json.loads(proj_dict_path.read_text(encoding="utf-8")))
            except Exception as e:
                logger.warning("Failed to load project pronunciation_dict.json: %s", e)

        compiled_pronunciations = [
            (re.compile(rf"\b{re.escape(w)}\b", re.IGNORECASE), r)
            for w, r in pronunciation_dict.items()
        ]

        state = self.job_queue.get_job(project_id)
        generated_chapters = list(state.get("generated_chapters", []))
        selection = state.get("generation_chapter_selection")

        # Sync generated_chapters with existing mastered chapter files on disk
        for disk_dir in (project_dir / "chapters", Path("workspace") / project_id / "chapters"):
            if disk_dir.exists():
                for wav in disk_dir.glob("chapter_*.wav"):
                    m = re.search(r"chapter_(\d+)\.wav", wav.name)
                    if m:
                        ch_num = int(m.group(1))
                        if ch_num not in generated_chapters:
                            generated_chapters.append(ch_num)
        self.job_queue.update_job(project_id, {"generated_chapters": generated_chapters, "mastered_chapters": generated_chapters})

        for script_file in script_files:
            self._check_stop(project_id)
            self._check_schedule(project_id)
            self._check_deployment_pause(project_id)

            chapter_script = ScriptChapter.model_validate_json(
                script_file.read_text(encoding="utf-8")
            )

            # Selection filter
            if selection is not None and chapter_script.chapter_number not in selection:
                logger.info("Skipping chapter %d (not in current generation selection)", chapter_script.chapter_number)
                continue

            if chapter_script.chapter_number in generated_chapters:
                logger.info("Skipping chapter %d (already generated)", chapter_script.chapter_number)
                continue

            self.job_queue.update_job(project_id, {"current_gen_chapter": chapter_script.chapter_number})

            if compiled_pronunciations:
                for line in chapter_script.lines:
                    for pattern, replacement in compiled_pronunciations:
                        line.text = pattern.sub(replacement, line.text)

            book_json_path = project_dir / "book.json"
            if book_json_path.exists():
                try:
                    book_data = json.loads(book_json_path.read_text(encoding="utf-8"))
                    registry = book_data.get("character_registry", {}).get("characters", {})
                    for line in chapter_script.lines:
                        char_info = registry.get(line.speaker)
                        if char_info and "voice_fx" in char_info and char_info["voice_fx"]:
                            from shared.models import VoiceFXSettings
                            line.voice_fx = VoiceFXSettings(**char_info["voice_fx"])
                except Exception as e:
                    logger.warning("Failed to inject voice_fx: %s", e)

            # Merge consecutive lines spoken by the same character to reduce total inference calls
            merged_lines = []
            for line in chapter_script.lines:
                if not merged_lines:
                    merged_lines.append(line.model_copy(deep=True))
                else:
                    prev = merged_lines[-1]
                    same_speaker = line.speaker == prev.speaker
                    same_emotion = (line.emotion or "").strip().lower() == (prev.emotion or "").strip().lower()
                    same_speed = getattr(line, "speed", 1.0) == getattr(prev, "speed", 1.0)
                    same_fx = getattr(line, "voice_fx", None) == getattr(prev, "voice_fx", None)
                    under_limit = len(prev.text.split()) + len(line.text.split()) < 250

                    if same_speaker and same_emotion and same_speed and same_fx and under_limit:
                        prev.text = prev.text.rstrip() + " " + line.text.lstrip()
                    else:
                        merged_lines.append(line.model_copy(deep=True))

            try:
                request = GenerateChapterRequest(
                    project_id=project_id,
                    chapter_number=chapter_script.chapter_number,
                    lines=merged_lines,
                    validate=True,
                    auto_retry=True,
                    max_retries=self.config.get("validation", {}).get("max_retries", 3),
                )

                response = self.voice_client.generate_chapter(request)

                generated_chapters.append(chapter_script.chapter_number)
                self.job_queue.update_job(project_id, {
                    "generated_chapters": generated_chapters,
                    "current_chapter": chapter_script.chapter_number,
                    "lines_generated": response.generated,
                    "lines_failed": response.failed_validation,
                })

                logger.info("Chapter %d generated: %d/%d lines", chapter_script.chapter_number, response.generated, response.total_lines)
            except Exception as e:
                logger.error("Failed to generate chapter %d: %s", chapter_script.chapter_number, e)
                raise

        self.job_queue.update_job(project_id, {"current_gen_chapter": None})

    def _run_mastering(self, project_id: str, project_dir: Path) -> None:
        """Run Stage ⑥: Audio mastering."""
        self._update_stage(project_id, PipelineStage.MASTERING)

        scripts_dir = project_dir / "script"
        script_files = sorted(scripts_dir.glob("chapter_*.json"))

        from shared.models import ScriptChapter
        state = self.job_queue.get_job(project_id)
        mastered_chapters = state.get("mastered_chapters", [])
        selection = state.get("generation_chapter_selection")

        for script_file in script_files:
            self._check_stop(project_id)
            self._check_schedule(project_id)
            self._check_deployment_pause(project_id)

            chapter_script = ScriptChapter.model_validate_json(
                script_file.read_text(encoding="utf-8")
            )

            if selection is not None and chapter_script.chapter_number not in selection:
                continue

            if chapter_script.chapter_number in mastered_chapters:
                logger.info("Skipping chapter %d (already mastered)", chapter_script.chapter_number)
                continue

            segments = [
                MasterSegmentInfo(
                    line_id=line.line_id,
                    file=f"{project_id}/segments/{line.line_id}.wav",
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

                response = self.voice_client.master_chapter(request)

                mastered_chapters.append(chapter_script.chapter_number)
                self.job_queue.update_job(project_id, {
                    "mastered_chapters": mastered_chapters,
                })

                logger.info("Chapter %d mastered: %.1f seconds, %.1f LUFS", chapter_script.chapter_number, response.duration_seconds, response.lufs)
            except Exception as e:
                logger.error("Failed to master chapter %d: %s", chapter_script.chapter_number, e)
                raise

    def _run_export(self, project_id: str, project_dir: Path, partial: bool = False) -> None:
        """Run Stage ⑦: M4B export."""
        self._update_stage(project_id, PipelineStage.EXPORTING)

        book_path = project_dir / "book.json"
        from shared.models import ExtractedBook, ScriptChapter
        book = ExtractedBook.model_validate_json(book_path.read_text(encoding="utf-8"))

        scripts_dir = project_dir / "script"
        script_files = sorted(scripts_dir.glob("chapter_*.json"))

        state = self.job_queue.get_job(project_id)
        mastered_chapters = set(state.get("mastered_chapters", []))

        chapters: list[ExportChapterInfo] = []
        for script_file in script_files:
            ch = ScriptChapter.model_validate_json(
                script_file.read_text(encoding="utf-8")
            )
            # Only include mastered chapters
            if partial and ch.chapter_number not in mastered_chapters:
                continue

            chapters.append(ExportChapterInfo(
                number=ch.chapter_number,
                title=ch.chapter_title,
                file=f"chapters/chapter_{ch.chapter_number:03d}.wav",
            ))

        if not chapters:
            logger.warning("No mastered chapters available for export.")
            return

        cover_art = project_dir / "cover.jpg"
        cover_path_str = str(cover_art) if cover_art.exists() else None

        request = ExportM4BRequest(
            project_id=project_id,
            metadata=AudiobookMetadata(
                title=book.metadata.title,
                author=book.metadata.author,
            ),
            chapters=chapters,
            cover_art=cover_path_str,
        )

        response = self.voice_client.export_m4b(request)

        import shutil
        suffix = f"_chapters_1-{max(ch.number for ch in chapters)}" if partial else ""
        local_m4b = project_dir / f"{project_id}{suffix}.m4b"

        if response.output_file and Path(response.output_file).exists():
            shutil.copy2(response.output_file, local_m4b)
            logger.info("M4B copied to: %s", local_m4b)
        elif response.download_url:
            self.voice_client.download_file(
                project_id,
                f"output/{project_id}.m4b",
                str(local_m4b),
            )
            logger.info("M4B downloaded to: %s", local_m4b)

        logger.info(
            "Export complete (%s): %s, %s, %.1f MB",
            "partial" if partial else "full",
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
        is_done_stage = stage in (
            PipelineStage.COMPLETE,
            PipelineStage.SELECTION_COMPLETE,
            PipelineStage.ERROR,
            PipelineStage.PAUSED,
            PipelineStage.PAUSED_SCHEDULED,
            PipelineStage.DEPLOY_PAUSED,
        )
        is_running = not is_done_stage
        update = {"status": stage, "running": is_running, **extra}
        self.job_queue.update_job(project_id, update)
        logger.info("Pipeline stage: %s → %s (running=%s)", project_id, stage, is_running)

    @staticmethod
    def _make_project_id(title: str) -> str:
        """Generate a URL-safe project ID from a book title."""
        import re
        project_id = title.lower().strip()
        project_id = re.sub(r"[^\w\s-]", "", project_id)
        project_id = re.sub(r"[-\s]+", "-", project_id)
        return project_id[:64]

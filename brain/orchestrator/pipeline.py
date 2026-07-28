"""Pipeline orchestrator — End-to-end audiobook production pipeline.

Coordinates all stages:
  ① Text Extraction → ② LLM Script Director → ③ Voice Bootstrapping →
  ④ TTS Generation → ⑤ Quality Validation → ⑥ Audio Mastering → ⑦ M4B Export

State is persisted to SQLite so the pipeline can resume after interruption.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml

from brain.director.character_analyzer import (
    CharacterAnalyzer,
    _SYSTEM_PROMPT as CHARACTER_SYSTEM_PROMPT,
)
from brain.director.ollama_client import OllamaClient
from brain.director.script_generator import ScriptGenerator
from brain.extractor.epub_parser import EpubParser
from brain.orchestrator.job_queue import JobQueue
from brain.orchestrator.voice_client import VoiceClient
from shared.artifacts import (
    atomic_write_json,
    atomic_write_text,
    build_segment_manifest,
    finalize_segment_manifest,
    fingerprint,
    format_chapter_set,
    hash_file,
    manifest_path,
    master_manifest_path,
)
from shared.constants import MASTERING_SCHEMA_VERSION, PipelineStage
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
from shared.voice_casting import build_voice_cast, speaking_character_ids

logger = logging.getLogger(__name__)


class Pipeline:
    """End-to-end audiobook production pipeline."""

    _SCRIPT_FILENAME = re.compile(r"^chapter_\d{3,}\.json$")

    @classmethod
    def _script_files(cls, scripts_dir: Path) -> list[Path]:
        """Return chapter scripts without matching adjacent metadata sidecars."""
        return sorted(
            path
            for path in scripts_dir.glob("chapter_*.json")
            if cls._SCRIPT_FILENAME.fullmatch(path.name)
        )

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
            max_retry_seconds=ollama_cfg.get("max_retry_seconds", 900),
            context_window=int(ollama_cfg.get("context_window", 8192)),
        )

        voice_cfg = self.config.get("voice_server", {})
        self.voice_client = VoiceClient(
            host=voice_cfg.get("host", "http://127.0.0.1:8100"),
            timeout=voice_cfg.get("timeout", 3600),
            retries=voice_cfg.get("retries", 3),
            retry_delay=voice_cfg.get("retry_delay", 2),
            api_token=voice_cfg.get("api_token", ""),
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
            group_utterances=script_cfg.get("group_utterances", True),
            utterance_target_chars=script_cfg.get(
                "utterance_target_chars", 260
            ),
            utterance_max_words=script_cfg.get("utterance_max_words", 45),
            narrator_target_chars=script_cfg.get(
                "narrator_target_chars", 340
            ),
            narrator_max_words=script_cfg.get("narrator_max_words", 58),
            expressive_target_chars=script_cfg.get(
                "expressive_target_chars", 180
            ),
            expressive_max_words=script_cfg.get(
                "expressive_max_words", 30
            ),
            speaker_confidence_threshold=script_cfg.get(
                "speaker_confidence_threshold", 0.55
            ),
        )

        self._stop_flags: dict[str, bool] = {}
        self._ollama_server_proc = None
        self._ollama_server_log_handle = None
        self._voice_server_proc = None

    def stop(self, project_id: str) -> None:
        """Signal the pipeline and interrupt an active Ollama stream."""
        self._stop_flags[project_id] = True
        self.ollama.cancel_current()

    def _check_stop(self, project_id: str) -> None:
        """Raise KeyboardInterrupt if a stop was requested."""
        if self._stop_flags.get(project_id):
            raise KeyboardInterrupt("Pipeline stopped via API")

    @staticmethod
    def _window_contains(win: dict[str, Any], now: datetime) -> bool:
        """Return whether *now* is inside a configured schedule window.

        Cross-midnight windows belong to the weekday on which they begin.
        """
        start_time = datetime.strptime(win.get("start", "00:00"), "%H:%M").time()
        end_time = datetime.strptime(win.get("end", "23:59"), "%H:%M").time()
        now_time = now.time()
        anchor = now

        if start_time <= end_time:
            in_time = start_time <= now_time <= end_time
        elif now_time >= start_time:
            in_time = True
        elif now_time <= end_time:
            in_time = True
            anchor = now - timedelta(days=1)
        else:
            in_time = False

        days = win.get("days", [])
        return in_time and (not days or anchor.strftime("%A") in days)

    def _pause_at_boundary(
        self,
        project_id: str,
        pause_stage: PipelineStage,
        reason: str,
        should_resume: Any,
        poll_seconds: int,
    ) -> None:
        """Park a live worker without losing the stage it must resume."""
        state = self.job_queue.get_job(project_id)
        active_stage = PipelineStage(state.get("active_stage") or state.get("status"))
        self.job_queue.update_job(
            project_id,
            {
                "status": pause_stage.value,
                "active_stage": active_stage.value,
                "pause_reason": reason,
                "running": True,
            },
        )
        logger.info("Pipeline parked at chapter boundary: %s", reason)
        while not should_resume():
            self._check_stop(project_id)
            time.sleep(poll_seconds)
        self.job_queue.update_job(
            project_id,
            {
                "status": active_stage.value,
                "active_stage": active_stage.value,
                "pause_reason": None,
                "running": True,
            },
        )
        logger.info("Pipeline resumed at stage %s", active_stage.value)

    def _load_config(self) -> dict[str, Any]:
        """Load pipeline configuration from YAML."""
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        logger.warning("Config file not found: %s — using defaults", self.config_path)
        return {}

    def _schedule_now(self, schedule_cfg: dict[str, Any]) -> datetime:
        zone_name = schedule_cfg.get("timezone", "Europe/Bucharest")
        try:
            return datetime.now(ZoneInfo(zone_name))
        except Exception:
            logger.warning("Invalid schedule timezone %r; using local time", zone_name)
            return datetime.now().astimezone()

    def schedule_is_open(self) -> bool:
        """Return whether automatic work is currently allowed."""
        self.config = self._load_config()
        schedule_cfg = self.config.get("schedule", {})
        if not schedule_cfg.get("enabled", False):
            return True
        windows = schedule_cfg.get("windows", [])
        if not windows:
            return False
        now = self._schedule_now(schedule_cfg)
        return any(self._window_contains(window, now) for window in windows)

    # ------------------------------------------------------------------
    # Ollama Process Management
    # ------------------------------------------------------------------

    def _start_ollama_server(self) -> None:
        """Start an isolated local Ollama server with the configured GPU."""
        ollama_cfg = self.config.get("ollama", {})
        if self.ollama.check_health(quiet=True):
            logger.info("Ollama is already running and the configured model is available.")
            return
        if not ollama_cfg.get("auto_start", False):
            raise RuntimeError(
                f"Ollama/model unavailable at {self.ollama.host}; "
                "start Ollama or enable ollama.auto_start"
            )

        import os
        import subprocess

        executable = str(ollama_cfg.get("executable", "")).strip()
        if not executable:
            executable = shutil.which("ollama") or ""
        if not executable and os.name == "nt":
            local_app_data = os.environ.get("LOCALAPPDATA", "")
            candidate = Path(local_app_data) / "Programs" / "Ollama" / "ollama.exe"
            if candidate.is_file():
                executable = str(candidate)
        if not executable or not Path(executable).is_file():
            raise RuntimeError(
                "Could not find ollama executable; set ollama.executable in brain/config.yaml"
            )

        parsed_host = urlsplit(self.ollama.host)
        if parsed_host.hostname not in {"127.0.0.1", "localhost"}:
            raise RuntimeError("Managed Ollama requires a loopback ollama.host")

        env = os.environ.copy()
        env["OLLAMA_HOST"] = parsed_host.netloc
        visible_devices = str(
            ollama_cfg.get("vulkan_visible_devices", "")
        ).strip()
        if visible_devices:
            env["GGML_VK_VISIBLE_DEVICES"] = visible_devices
        models_dir = str(ollama_cfg.get("models_dir", "")).strip()
        if models_dir:
            env["OLLAMA_MODELS"] = models_dir
        if ollama_cfg.get("debug", False):
            env["OLLAMA_DEBUG"] = "1"

        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        log_path = Path(
            ollama_cfg.get(
                "log_path",
                self.projects_dir / "ollama-managed.log",
            )
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._ollama_server_log_handle = open(
            log_path,
            "a",
            encoding="utf-8",
            buffering=1,
        )
        kwargs["stdout"] = self._ollama_server_log_handle
        kwargs["stderr"] = subprocess.STDOUT
        logger.info(
            "Starting managed Ollama at %s (Vulkan devices=%s, models=%s, log=%s)",
            self.ollama.host,
            visible_devices or "server default",
            models_dir or "Ollama default",
            log_path,
        )
        try:
            self._ollama_server_proc = subprocess.Popen(
                [executable, "serve"],
                cwd=str(Path(executable).parent),
                env=env,
                **kwargs,
            )
        except Exception:
            self._ollama_server_log_handle.close()
            self._ollama_server_log_handle = None
            raise

        timeout = int(ollama_cfg.get("startup_timeout_seconds", 90))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._ollama_server_proc.poll() is not None:
                code = self._ollama_server_proc.returncode
                self._ollama_server_proc = None
                if getattr(self, "_ollama_server_log_handle", None) is not None:
                    try:
                        self._ollama_server_log_handle.close()
                    except Exception:
                        pass
                    self._ollama_server_log_handle = None
                raise RuntimeError(
                    f"Managed Ollama exited during startup with code {code}"
                )
            if self.ollama.check_health(quiet=True):
                logger.info("Managed Ollama is ready: %s", self.ollama.model)
                return
            time.sleep(2)

        self._stop_ollama_server()
        raise RuntimeError(
            f"Managed Ollama/model did not become ready within {timeout}s"
        )

    def _stop_ollama_server(self) -> None:
        """Stop Ollama only when this pipeline launched the process."""
        process = getattr(self, "_ollama_server_proc", None)
        if process is None:
            log_handle = getattr(self, "_ollama_server_log_handle", None)
            if log_handle is not None:
                log_handle.close()
                self._ollama_server_log_handle = None
            return
        logger.info("Stopping managed Ollama subprocess...")
        try:
            self._terminate_managed_process_tree(process)
        except Exception as exc:
            logger.warning("Force killing managed Ollama subprocess: %s", exc)
            try:
                process.kill()
            except Exception:
                pass
        finally:
            self._ollama_server_proc = None
            log_handle = getattr(self, "_ollama_server_log_handle", None)
            if log_handle is not None:
                log_handle.close()
                self._ollama_server_log_handle = None

    @staticmethod
    def _terminate_managed_process_tree(process: Any) -> None:
        """Terminate an app-owned process and all of its descendants."""
        import os
        import subprocess

        process_id = getattr(process, "pid", None)
        if os.name == "nt" and process_id:
            result = subprocess.run(
                [
                    "taskkill",
                    "/PID",
                    str(process_id),
                    "/T",
                    "/F",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode not in (0, 128):
                raise RuntimeError(
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"taskkill failed with code {result.returncode}"
                )
            try:
                process.wait(timeout=5)
            except Exception:
                pass
            return
        process.terminate()
        process.wait(timeout=10)

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
            health = self.voice_client.health_check_once()
            if health.status == "ok":
                logger.info("Voice server is already running and healthy.")
                return
        except Exception:
            pass

        import os
        import subprocess
        import sys

        venv_py = Path(
            voice_cfg.get("venv", r"E:\PyTorch env\my_venv")
        )
        configured_python = (
            os.environ.get("CRAZY_AUDIOBOOK_VOICE_PYTHON")
            or voice_cfg.get("python_executable")
        )
        python_exe = (
            Path(configured_python)
            if configured_python
            else venv_py / "Scripts" / "python.exe"
        )
        launcher_works = False
        if python_exe.is_file():
            try:
                launcher_check = subprocess.run(
                    [str(python_exe), "-c", "import sys"],
                    capture_output=True,
                    timeout=10,
                )
                launcher_works = launcher_check.returncode == 0
            except (OSError, subprocess.SubprocessError):
                launcher_works = False
        if not launcher_works:
            fallback = Path(sys.executable)
            logger.warning(
                "Configured Voice Python is unavailable: %s; falling back "
                "to the dashboard interpreter %s",
                python_exe,
                fallback,
            )
            python_exe = fallback

        logger.info("Starting local Voice Server subprocess via %s...", python_exe)
        env = os.environ.copy()
        package_paths = [str(Path.cwd())]
        venv_packages = venv_py / "Lib" / "site-packages"
        if venv_packages.is_dir():
            package_paths.append(str(venv_packages))
        if env.get("PYTHONPATH"):
            package_paths.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(package_paths))
        env.setdefault(
            "ROCM_SDK_TARGET_FAMILY",
            str(voice_cfg.get("rocm_target_family", "custom")),
        )

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
                self._terminate_managed_process_tree(
                    self._voice_server_proc
                )
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

        if any(
            self._window_contains(win, self._schedule_now(schedule_cfg))
            for win in windows
        ):
            return

        def schedule_open() -> bool:
            self.config = self._load_config()
            current = self.config.get("schedule", {})
            if not current.get("enabled", False):
                return True
            current_windows = current.get("windows", [])
            if not current_windows:
                return False
            now = self._schedule_now(current)
            return any(
                self._window_contains(win, now)
                for win in current_windows
            )

        managed_voice_server = self._voice_server_proc is not None
        if managed_voice_server:
            # A schedule pause may last hours. Release the GPU instead of
            # keeping the local TTS model resident while no work is allowed.
            self._stop_voice_server()
        self._pause_at_boundary(
            project_id,
            PipelineStage.PAUSED_SCHEDULED,
            "outside configured working hours",
            schedule_open,
            30,
        )
        if managed_voice_server:
            self._start_voice_server()

    def _check_deployment_pause(self, project_id: str) -> None:
        """Pause pipeline if user requested a safe deployment parking point."""
        state = self.job_queue.get_job(project_id)
        if state.get("deployment_requested", False):
            try:
                from plyer import notification
                notification.notify(
                    title="Audiobook Creator — Safe Deployment",
                    message=f"Pipeline parked for project '{project_id}'. Safe to deploy updates.",
                    app_name="Audiobook Creator",
                )
            except Exception:
                pass

            self._pause_at_boundary(
                project_id,
                PipelineStage.DEPLOY_PAUSED,
                "safe deployment requested",
                lambda: not self.job_queue.get_job(project_id).get(
                    "deployment_requested", False
                ),
                5,
            )

    # ------------------------------------------------------------------
    # Project creation
    # ------------------------------------------------------------------

    def create_project(self, epub_path: str | Path) -> ProjectStatus:
        """Create a new audiobook project from an EPUB file."""
        max_projects = int(
            self.config.get("dashboard", {}).get("max_projects", 50)
        )
        if max_projects > 0 and len(self.job_queue.list_jobs()) >= max_projects:
            raise ValueError(
                f"Project limit reached ({max_projects}). Delete or archive an "
                "existing project before importing another book."
            )
        book = self.parser.parse(epub_path)
        project_id = self._make_project_id(book.metadata.title)
        
        base_id = project_id
        counter = 1
        while True:
            state_exists = True
            try:
                self.job_queue.get_job(project_id)
            except KeyError:
                state_exists = False
            if (
                not state_exists
                and not (self.projects_dir / project_id).exists()
            ):
                break
            project_id = f"{base_id}-{counter}"
            counter += 1

        project_dir = self.projects_dir / project_id
        project_dir.mkdir(parents=True, exist_ok=True)

        book_path = project_dir / "book.json"
        if book.metadata.cover_image_path:
            cover_src = Path(book.metadata.cover_image_path)
            if cover_src.exists():
                cover_dest = project_dir / cover_src.name
                cover_dest.write_bytes(cover_src.read_bytes())
                book.metadata.cover_image_path = str(cover_dest)
        atomic_write_text(book_path, book.model_dump_json(indent=2))

        status = ProjectStatus(
            project_id=project_id,
            title=book.metadata.title,
            author=book.metadata.author,
            status=PipelineStage.CREATED,
            total_chapters=book.metadata.total_chapters,
            total_lines=0,
            started_at=datetime.now(timezone.utc),
        )

        initial_state = status.model_dump()
        initial_state.update(
            {
                # Jobs without this key predate casting review and are
                # deliberately grandfathered.
                "voice_review_policy": "required_once",
                "voice_review_status": "pending",
                "voice_review_approved_at": None,
                "voice_review_approved_revision": None,
            }
        )
        self.job_queue.create_job(project_id, initial_state)

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
        if (
            state.get("script_completed", False)
            and not self._script_artifacts_current(project_dir)
        ):
            logger.info(
                "Script dependencies changed for '%s'; scheduling a book-wide "
                "script refresh before further audio generation",
                project_id,
            )
            self.job_queue.update_job(
                project_id,
                {
                    "script_completed": False,
                    "bootstrapping_completed": False,
                    "bootstrapping_fingerprint": None,
                    "scripted_chapters": [],
                    "status": PipelineStage.CREATED.value,
                    "active_stage": PipelineStage.CREATED.value,
                },
            )
            state = self.job_queue.get_job(project_id)
        current_stage = PipelineStage(
            state.get("active_stage") or state.get("status", PipelineStage.CREATED)
        )

        # When starting or re-running a pipeline:
        # Determine the appropriate stage to resume from based on completed phases.
        if current_stage in (PipelineStage.COMPLETE, PipelineStage.SELECTION_COMPLETE, PipelineStage.PAUSED, PipelineStage.ERROR, PipelineStage.PAUSED_SCHEDULED, PipelineStage.DEPLOY_PAUSED):
            if state.get("bootstrapping_completed", False):
                current_stage = PipelineStage.GENERATING
            elif state.get("script_completed", False):
                current_stage = PipelineStage.BOOTSTRAPPING
            elif state.get("scripted_chapters"):
                current_stage = PipelineStage.SCRIPTING
            else:
                current_stage = PipelineStage.CREATED
            self.job_queue.update_job(
                project_id,
                {
                    "status": current_stage.value,
                    "active_stage": current_stage.value,
                    "pause_reason": None,
                },
            )

        start_time = time.time()
        self._stop_flags[project_id] = False
        self.ollama.begin_run()
        ollama_used = False
        state = self.job_queue.get_job(project_id)
        self.job_queue.update_job(
            project_id,
            {
                "active_generation_chapter_selection": state.get(
                    "generation_chapter_selection"
                ),
                "last_run_started_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": 0.0,
                "error_message": None,
            },
        )
        logger.info("Starting pipeline for '%s' from stage: %s", project_id, current_stage)

        try:
            # Park before loading GPU models when automatic working hours are
            # closed. The same live worker resumes when a window opens.
            self._check_schedule(project_id)

            # Analysis and scripting are always book-wide. Audio generation and
            # mastering below respect generation_chapter_selection.
            self._check_stop(project_id)
            state = self.job_queue.get_job(project_id)
            if not state.get("script_completed", False):
                self._start_ollama_server()
                ollama_used = True
                self._run_script_director(project_id, project_dir)

            # Ollama and Qwen TTS share the GPU on the local setup. Do not load
            # the TTS model until book-wide LLM scripting has released its
            # working memory.
            if self.config.get("ollama", {}).get(
                "unload_after_scripting",
                True,
            ):
                self.ollama.unload_model()
            self._stop_ollama_server()
            ollama_used = False
            self._check_schedule(project_id)
            self._start_voice_server()

            self._check_stop(project_id)
            state = self.job_queue.get_job(project_id)
            if not state.get("bootstrapping_completed", False):
                self._run_voice_bootstrap(project_id, project_dir)

            state = self.job_queue.get_job(project_id)
            if (
                state.get("voice_review_policy", "grandfathered")
                == "required_once"
                and state.get("voice_review_status") != "approved"
            ):
                self.job_queue.update_job(
                    project_id,
                    {
                        "voice_review_status": "waiting",
                        "pause_reason": (
                            "Review and approve the speaking cast before audio "
                            "generation."
                        ),
                    },
                )
                self._update_stage(project_id, PipelineStage.VOICE_REVIEW)
                logger.info(
                    "Voice references are ready for '%s'; waiting for the "
                    "one-time casting review",
                    project_id,
                )
                return ProjectStatus(**self.job_queue.get_job(project_id))

            self._check_stop(project_id)
            self._run_generation(project_id, project_dir)

            self._check_stop(project_id)
            self._run_mastering(project_id, project_dir)

            # Stage ⑦: M4B Export
            self._check_stop(project_id)
            state = self.job_queue.get_job(project_id)
            selection = state.get("active_generation_chapter_selection")

            if selection is not None:
                self._run_export(
                    project_id,
                    project_dir,
                    partial=True,
                    chapter_selection=set(selection),
                )
                elapsed = time.time() - start_time
                self._update_stage(
                    project_id,
                    PipelineStage.SELECTION_COMPLETE,
                    elapsed_seconds=elapsed,
                )
                logger.info("Selection batch complete for '%s'", project_id)
            else:
                total_chapters = int(state.get("total_chapters") or 0)
                mastered = set(state.get("mastered_chapters", []))
                expected = set(range(1, total_chapters + 1))
                if mastered != expected:
                    raise RuntimeError(
                        "Full export refused because mastered chapters are missing: "
                        f"{sorted(expected - mastered)}"
                    )
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
            if ollama_used:
                self.ollama.unload_model()
            self._stop_ollama_server()
            try:
                self.voice_client.health_check_once()
                self.voice_client.unload_models()
            except Exception:
                # The Voice service is intentionally absent during scripting-only
                # runs and may already have exited after a cancellation.
                pass
            self._stop_voice_server()
            self.job_queue.update_job(
                project_id,
                {"active_generation_chapter_selection": None},
            )

        return ProjectStatus(**self.job_queue.get_job(project_id))

    # ------------------------------------------------------------------
    # Stage runners
    # ------------------------------------------------------------------

    def _script_artifacts_current(self, project_dir: Path) -> bool:
        """Validate character and chapter-script dependency fingerprints."""
        book_path = project_dir / "book.json"
        chars_path = project_dir / "characters.json"
        chars_meta_path = project_dir / "characters.meta.json"
        if not all(path.exists() for path in (book_path, chars_path, chars_meta_path)):
            return False
        try:
            from shared.models import CharacterRegistry, ExtractedBook

            book = ExtractedBook.model_validate_json(
                book_path.read_text(encoding="utf-8")
            )
            registry = CharacterRegistry.model_validate_json(
                chars_path.read_text(encoding="utf-8")
            )
            expected_character_fingerprint = fingerprint(
                {
                    "book": book.model_dump(mode="json"),
                    "model": self.ollama.model,
                    "prompt": CHARACTER_SYSTEM_PROMPT,
                    "max_unique_voices": self.character_analyzer.max_unique_voices,
                }
            )
            character_metadata = json.loads(
                chars_meta_path.read_text(encoding="utf-8")
            )
            if (
                character_metadata.get("fingerprint")
                != expected_character_fingerprint
            ):
                return False
            return self.script_generator.cached_scripts_are_current(
                book.chapters,
                registry,
                project_dir / "script",
            )
        except Exception:
            logger.exception("Could not validate script dependency fingerprints")
            return False

    def _run_script_director(self, project_id: str, project_dir: Path) -> None:
        """Run Stage ②: LLM character analysis + script generation."""
        self._update_stage(project_id, PipelineStage.SCRIPTING)

        book_path = project_dir / "book.json"
        from shared.models import ExtractedBook
        book = ExtractedBook.model_validate_json(book_path.read_text(encoding="utf-8"))

        t0 = time.time()
        pass1_elapsed = 0.0

        chars_path = project_dir / "characters.json"
        chars_meta_path = project_dir / "characters.meta.json"
        chars_fingerprint = fingerprint(
            {
                "book": book.model_dump(mode="json"),
                "model": self.ollama.model,
                "prompt": CHARACTER_SYSTEM_PROMPT,
                "max_unique_voices": self.character_analyzer.max_unique_voices,
            }
        )
        reuse_characters = False
        state = self.job_queue.get_job(project_id)
        force_character_analysis = bool(state.get("force_character_analysis"))
        if (
            not force_character_analysis
            and chars_path.exists()
            and chars_meta_path.exists()
        ):
            try:
                chars_meta = json.loads(
                    chars_meta_path.read_text(encoding="utf-8")
                )
                reuse_characters = chars_meta.get("fingerprint") == chars_fingerprint
            except Exception:
                reuse_characters = False
        if reuse_characters:
            from shared.models import CharacterRegistry
            registry = CharacterRegistry.model_validate_json(chars_path.read_text(encoding="utf-8"))
        else:
            registry = self.character_analyzer.analyze(book)
            pass1_elapsed = time.time() - t0

            atomic_write_text(chars_path, registry.model_dump_json(indent=2))
            atomic_write_json(
                chars_meta_path,
                {"fingerprint": chars_fingerprint},
            )
            self.job_queue.update_job(
                project_id,
                {
                    "scripted_chapters": [],
                    "bootstrapping_completed": False,
                    "character_analysis_fingerprint": chars_fingerprint,
                    "force_character_analysis": False,
                },
            )

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
            atomic_write_text(script_path, script.model_dump_json(indent=2))
            total_lines += script.total_lines

        book_script = BookScript(
            metadata=book.metadata,
            character_registry=registry,
            chapters=chapter_scripts,
        )
        atomic_write_text(
            project_dir / "book_script.json",
            book_script.model_dump_json(indent=2),
        )

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
        # Character analysis intentionally sees the whole book and may identify
        # named non-speaking entities. Only bootstrap reference voices that are
        # actually used by a completed script, following any shared voice_id
        # assignment made by the analyzer.
        from shared.models import ScriptChapter

        script_chapters: list[ScriptChapter] = []
        for script_path in self._script_files(project_dir / "script"):
            script_chapters.append(
                ScriptChapter.model_validate_json(
                    script_path.read_text(encoding="utf-8")
                )
            )
        speaking_ids = speaking_character_ids(script_chapters)

        voice_config_path = Path("voice/config.yaml")
        voice_config = (
            yaml.safe_load(voice_config_path.read_text(encoding="utf-8")) or {}
            if voice_config_path.exists()
            else {}
        )
        tts_config = voice_config.get("tts", {})
        design_model = tts_config.get(
            "voice_design_model",
            "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        )
        cast = build_voice_cast(
            project_id=project_id,
            registry=registry,
            speaking_ids=speaking_ids,
            design_model=design_model,
            design_config={
                "test_sentences": tts_config.get(
                    "voice_design_test_sentences", {}
                ),
                "language": tts_config.get("language", "English"),
            },
        )
        atomic_write_json(project_dir / "voice_cast.json", cast)

        request = BootstrapVoicesRequest(
            project_id=project_id,
            characters={
                voice_id: registry.characters[voice_id].model_copy(
                    update={
                        "voice_description": profile["effective_prompt"]
                    }
                )
                for voice_id, profile in cast["voices"].items()
            },
            force_regenerate=False,
            design_fingerprints={
                voice_id: profile["design_fingerprint"]
                for voice_id, profile in cast["voices"].items()
            },
        )
        try:
            response = self.voice_client.bootstrap_voices(request)
            for voice_id, result in response.voices_generated.items():
                profile = cast["voices"].get(voice_id)
                if not profile:
                    continue
                profile["quality"] = {
                    "transcription_wer": result.transcription_wer,
                    "acoustic_metrics": result.acoustic_metrics,
                }
                existing_warnings = profile.setdefault("warnings", [])
                existing_warnings.extend(
                    warning
                    for warning in result.warnings
                    if warning not in existing_warnings
                )
            atomic_write_json(project_dir / "voice_cast.json", cast)
            current_state = self.job_queue.get_job(project_id)
            review_status = current_state.get(
                "voice_review_status", "grandfathered"
            )
            if (
                current_state.get("voice_review_policy", "grandfathered")
                == "required_once"
                and review_status != "approved"
            ):
                review_status = "ready"
            self.job_queue.update_job(
                project_id,
                {
                    "bootstrapping_completed": True,
                    "bootstrapping_fingerprint": cast["fingerprint"],
                    "voice_cast_revision": cast["fingerprint"],
                    "voice_review_status": review_status,
                },
            )
            logger.info("Voice bootstrapping complete: %d voices generated", len(response.voices_generated))
        except Exception as e:
            logger.error("Failed to bootstrap voices: %s", e)
            raise

    def _run_generation(self, project_id: str, project_dir: Path) -> None:
        """Run Stages ④-⑤: TTS generation with quality validation."""
        self._update_stage(project_id, PipelineStage.GENERATING)

        scripts_dir = project_dir / "script"
        script_files = self._script_files(scripts_dir)

        from shared.models import ScriptChapter

        state = self.job_queue.get_job(project_id)
        generated_chapters, mastered_chapters = self._reconcile_artifacts(
            project_id, project_dir, script_files
        )
        selection = state.get("active_generation_chapter_selection")
        self.job_queue.update_job(
            project_id,
            {
                "generated_chapters": generated_chapters,
                "mastered_chapters": mastered_chapters,
            },
        )

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

            request_lines = self._prepare_generation_lines(
                chapter_script,
                project_dir,
            )

            generation_chapter = chapter_script.model_copy(
                update={"lines": request_lines},
                deep=True,
            )
            segment_manifest = build_segment_manifest(
                project_id,
                generation_chapter,
                self._voice_generation_config(project_id),
            )

            try:
                request = GenerateChapterRequest(
                    project_id=project_id,
                    chapter_number=chapter_script.chapter_number,
                    lines=request_lines,
                    validate=True,
                    auto_retry=True,
                    max_retries=self.config.get("validation", {}).get("max_retries", 3),
                    validation_terms=self._validation_terms(project_dir),
                )

                response = self.voice_client.generate_chapter(request)
                self.job_queue.clear_quality_logs(
                    project_id,
                    chapter_script.chapter_number,
                )
                for result in response.quality_results:
                    self.job_queue.log_quality(
                        project_id=project_id,
                        line_id=result.line_id,
                        chapter_number=chapter_script.chapter_number,
                        attempt=result.attempt,
                        wer=result.wer,
                        quality_score=result.quality_score,
                        status=result.status.value,
                        details=result.model_dump(mode="json"),
                    )
                quality_summary = (
                    self.job_queue.get_project_quality_summary(project_id)
                )
                expected_ids = {line.line_id for line in request_lines}
                generated_ids = set(response.generated_line_ids)
                failed_ids = set(response.failed_line_ids)
                if (
                    response.status != "success"
                    or response.generated != len(request_lines)
                    or generated_ids != expected_ids
                    or failed_ids
                    or response.failed_validation
                ):
                    raise RuntimeError(
                        f"Chapter {chapter_script.chapter_number} generation incomplete: "
                        f"generated={response.generated}/{len(request_lines)}, "
                        f"missing={sorted(expected_ids - generated_ids)}, "
                        f"failed={sorted(failed_ids)}, "
                        f"validation_failures={response.failed_validation}"
                    )

                segment_manifest = finalize_segment_manifest(segment_manifest)
                atomic_write_json(
                    manifest_path(project_dir, chapter_script.chapter_number),
                    segment_manifest,
                )
                generated_chapters = sorted(
                    set(generated_chapters) | {chapter_script.chapter_number}
                )
                current_state = self.job_queue.get_job(project_id)
                voice_revision_pending = sorted(
                    set(
                        current_state.get(
                            "voice_revision_pending_chapters",
                            [],
                        )
                    )
                    - {chapter_script.chapter_number}
                )
                self.job_queue.update_job(project_id, {
                    "generated_chapters": generated_chapters,
                    "current_chapter": chapter_script.chapter_number,
                    "lines_generated": response.generated,
                    "lines_failed": response.failed_validation,
                    "average_wer": quality_summary.get(
                        "average_wer", 0.0
                    ),
                    "validation_retries": quality_summary.get(
                        "total_retries", 0
                    ),
                    "validated_segments": quality_summary.get(
                        "total_segments", 0
                    ),
                    "voice_revision_pending_chapters": voice_revision_pending,
                })

                logger.info("Chapter %d generated: %d/%d lines", chapter_script.chapter_number, response.generated, response.total_lines)
            except Exception as e:
                logger.error("Failed to generate chapter %d: %s", chapter_script.chapter_number, e)
                if self._stop_flags.get(project_id):
                    raise KeyboardInterrupt("Pipeline stopped during chapter generation")
                raise

        self.job_queue.update_job(project_id, {"current_gen_chapter": None})

    def _run_mastering(self, project_id: str, project_dir: Path) -> None:
        """Run Stage ⑥: Audio mastering."""
        self._update_stage(project_id, PipelineStage.MASTERING)

        scripts_dir = project_dir / "script"
        script_files = self._script_files(scripts_dir)

        from shared.models import ScriptChapter
        state = self.job_queue.get_job(project_id)
        generated_chapters, mastered_chapters = self._reconcile_artifacts(
            project_id, project_dir, script_files
        )
        self.job_queue.update_job(
            project_id,
            {
                "generated_chapters": generated_chapters,
                "mastered_chapters": mastered_chapters,
            },
        )
        selection = state.get("active_generation_chapter_selection")

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

            if chapter_script.chapter_number not in generated_chapters:
                raise RuntimeError(
                    f"Cannot master chapter {chapter_script.chapter_number}: "
                    "its segment manifest is incomplete"
                )

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
                    chapter_title=chapter_script.chapter_title,
                    announce_chapter=True,
                )

                response = self.voice_client.master_chapter(request)
                if (
                    response.status != "success"
                    or not self._valid_audio(Path(response.output_file))
                ):
                    raise RuntimeError(
                        f"Chapter {chapter_script.chapter_number} mastering "
                        "did not produce valid audio"
                    )

                segment_manifest = json.loads(
                    manifest_path(
                        project_dir, chapter_script.chapter_number
                    ).read_text(encoding="utf-8")
                )
                atomic_write_json(
                    master_manifest_path(project_dir, chapter_script.chapter_number),
                    {
                        "chapter_number": chapter_script.chapter_number,
                        "segment_manifest_hash": segment_manifest["manifest_hash"],
                        "dependency_hash": self._master_dependency_hash(
                            project_id,
                            segment_manifest["manifest_hash"],
                            request,
                        ),
                        "output_file": response.output_file,
                        "output_hash": hash_file(response.output_file),
                    },
                )
                mastered_chapters = sorted(
                    set(mastered_chapters) | {chapter_script.chapter_number}
                )
                self.job_queue.update_job(project_id, {
                    "mastered_chapters": mastered_chapters,
                })

                logger.info("Chapter %d mastered: %.1f seconds, %.1f LUFS", chapter_script.chapter_number, response.duration_seconds, response.lufs)
            except Exception as e:
                logger.error("Failed to master chapter %d: %s", chapter_script.chapter_number, e)
                raise

    def _run_export(
        self,
        project_id: str,
        project_dir: Path,
        partial: bool = False,
        chapter_selection: set[int] | None = None,
    ) -> None:
        """Run Stage ⑦: M4B export."""
        self._update_stage(project_id, PipelineStage.EXPORTING)

        book_path = project_dir / "book.json"
        from shared.models import ExtractedBook, ScriptChapter
        book = ExtractedBook.model_validate_json(book_path.read_text(encoding="utf-8"))

        scripts_dir = project_dir / "script"
        script_files = self._script_files(scripts_dir)

        generated_chapters, reconciled_mastered = self._reconcile_artifacts(
            project_id,
            project_dir,
            script_files,
        )
        mastered_chapters = set(reconciled_mastered)
        self.job_queue.update_job(
            project_id,
            {
                "generated_chapters": generated_chapters,
                "mastered_chapters": reconciled_mastered,
            },
        )

        chapters: list[ExportChapterInfo] = []
        for script_file in script_files:
            ch = ScriptChapter.model_validate_json(
                script_file.read_text(encoding="utf-8")
            )
            if (
                partial
                and chapter_selection is not None
                and ch.chapter_number not in chapter_selection
            ):
                continue
            if ch.chapter_number not in mastered_chapters:
                if partial:
                    continue
                raise RuntimeError(
                    f"Full export refused: chapter {ch.chapter_number} is not mastered"
                )

            chapters.append(ExportChapterInfo(
                number=ch.chapter_number,
                title=ch.chapter_title,
                file=f"chapters/chapter_{ch.chapter_number:03d}.wav",
            ))

        if not chapters:
            raise RuntimeError("No mastered chapters are available for export")

        cover_art = (
            Path(book.metadata.cover_image_path)
            if book.metadata.cover_image_path
            else project_dir / "cover.jpg"
        )
        cover_path_str = str(cover_art) if cover_art.is_file() else None

        included_numbers = [chapter.number for chapter in chapters]
        output_name = (
            f"{project_id}_chapters_{format_chapter_set(included_numbers)}.m4b"
            if partial
            else f"{project_id}.m4b"
        )
        request = ExportM4BRequest(
            project_id=project_id,
            metadata=AudiobookMetadata(
                title=book.metadata.title,
                author=book.metadata.author,
                genre=book.metadata.genre or "Fantasy",
                year=book.metadata.year,
                description=book.metadata.description,
            ),
            chapters=chapters,
            cover_art=cover_path_str,
            output_name=output_name,
        )

        response = self.voice_client.export_m4b(request)

        import shutil
        suffix = (
            f"_chapters_{format_chapter_set(included_numbers)}" if partial else ""
        )
        local_m4b = project_dir / f"{project_id}{suffix}.m4b"

        if response.output_file and Path(response.output_file).exists():
            shutil.copy2(response.output_file, local_m4b)
            logger.info("M4B copied to: %s", local_m4b)
        elif response.download_url:
            self.voice_client.download_file(
                project_id,
                f"output/{output_name}",
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
            PipelineStage.VOICE_REVIEW,
        )
        is_running = not is_done_stage
        preserve_active_stage = stage in (
            PipelineStage.PAUSED,
            PipelineStage.PAUSING,
            PipelineStage.PAUSED_SCHEDULED,
            PipelineStage.DEPLOY_PAUSED,
            PipelineStage.ERROR,
        )
        if preserve_active_stage:
            state = self.job_queue.get_job(project_id)
            active_stage = state.get("active_stage") or state.get("status")
        else:
            active_stage = stage
        update = {
            "status": stage,
            "active_stage": active_stage,
            "running": is_running,
            **extra,
        }
        self.job_queue.update_job(project_id, update)
        logger.info("Pipeline stage: %s → %s (running=%s)", project_id, stage, is_running)

    @staticmethod
    def _make_project_id(title: str) -> str:
        """Generate a URL-safe project ID from a book title."""
        import re
        project_id = title.lower().strip()
        project_id = re.sub(r"[^\w\s-]", "", project_id)
        project_id = re.sub(r"[-\s]+", "-", project_id)
        project_id = project_id.strip("-_")
        return project_id[:64] or "book"

    @staticmethod
    def _valid_audio(path: Path) -> bool:
        if not path.is_file() or path.stat().st_size < 1000:
            return False
        try:
            import soundfile as sf

            info = sf.info(str(path))
            return info.frames > 0 and info.samplerate > 0 and info.duration > 0
        except Exception:
            return False

    def _prepare_generation_lines(
        self,
        chapter: Any,
        project_dir: Path,
    ) -> list[Any]:
        """Apply deterministic pronunciation and character-voice inputs."""
        import re
        from shared.models import VoiceFXSettings

        lines = [line.model_copy(deep=True) for line in chapter.lines]
        pronunciation_dict: dict[str, str] = {}
        for dictionary_path in (
            Path("brain/pronunciation_dict.json"),
            project_dir / "pronunciation_dict.json",
        ):
            if not dictionary_path.exists():
                continue
            try:
                pronunciation_dict.update(
                    json.loads(dictionary_path.read_text(encoding="utf-8"))
                )
            except Exception as exc:
                raise ValueError(
                    f"Invalid pronunciation dictionary: {dictionary_path}"
                ) from exc
        replacements = [
            (re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE), replacement)
            for word, replacement in pronunciation_dict.items()
        ]

        characters: dict[str, Any] = {}
        chars_file = project_dir / "characters.json"
        if chars_file.exists():
            characters = json.loads(chars_file.read_text(encoding="utf-8")).get(
                "characters", {}
            )

        for line in lines:
            for pattern, replacement in replacements:
                line.text = pattern.sub(replacement, line.text)
            char_info = characters.get(line.speaker, {})
            line.voice_id = char_info.get("voice_id") or line.speaker
            if char_info.get("voice_fx"):
                line.voice_fx = VoiceFXSettings(**char_info["voice_fx"])
        return lines

    @staticmethod
    def _validation_terms(project_dir: Path) -> list[str]:
        """Return explicit character/glossary terms eligible for fuzzy ASR."""
        terms: set[str] = set()
        characters_path = project_dir / "characters.json"
        if characters_path.exists():
            characters = json.loads(
                characters_path.read_text(encoding="utf-8")
            ).get("characters", {})
            for character_id, info in characters.items():
                terms.add(character_id.replace("_", " "))
                if isinstance(info, dict) and info.get("name"):
                    terms.add(str(info["name"]))
        for dictionary_path in (
            Path("brain/pronunciation_dict.json"),
            project_dir / "pronunciation_dict.json",
        ):
            if not dictionary_path.exists():
                continue
            values = json.loads(dictionary_path.read_text(encoding="utf-8"))
            terms.update(str(key) for key in values)
            terms.update(str(value) for value in values.values())
        return sorted(term for term in terms if term.strip())

    def _reconcile_artifacts(
        self,
        project_id: str,
        project_dir: Path,
        script_files: list[Path],
    ) -> tuple[list[int], list[int]]:
        """Derive generated/mastered chapters independently from artifacts."""
        from shared.models import ScriptChapter

        generated: list[int] = []
        mastered: list[int] = []
        workspace = Path("workspace") / project_id

        for script_file in script_files:
            chapter = ScriptChapter.model_validate_json(
                script_file.read_text(encoding="utf-8")
            )
            generation_chapter = chapter.model_copy(
                update={
                    "lines": self._prepare_generation_lines(
                        chapter,
                        project_dir,
                    )
                },
                deep=True,
            )
            expected_manifest = build_segment_manifest(
                project_id,
                generation_chapter,
                self._voice_generation_config(project_id),
            )
            segment_info_file = manifest_path(project_dir, chapter.chapter_number)
            if not segment_info_file.exists():
                continue
            try:
                stored_manifest = json.loads(
                    segment_info_file.read_text(encoding="utf-8")
                )
            except Exception:
                continue
            stored_payload = {
                key: value
                for key, value in stored_manifest.items()
                if key != "manifest_hash"
            }
            if stored_manifest.get("manifest_hash") != fingerprint(stored_payload):
                continue
            stored_dependency_payload = {
                key: value
                for key, value in stored_manifest.items()
                if key not in {"dependency_hash", "manifest_hash"}
            }
            stored_dependency_payload["segments"] = [
                {
                    key: value
                    for key, value in item.items()
                    if key != "output_hash"
                }
                for item in stored_manifest.get("segments", [])
            ]
            stored_dependency_hash = fingerprint(stored_dependency_payload)
            if not (
                stored_manifest.get("dependency_hash")
                == stored_dependency_hash
                == expected_manifest["dependency_hash"]
            ):
                continue

            stored_segments = stored_manifest.get("segments", [])
            segment_paths = [
                Path("workspace") / item["file"]
                for item in stored_segments
            ]
            if not segment_paths or not all(
                self._valid_audio(path) for path in segment_paths
            ):
                continue
            if any(
                item.get("output_hash") != hash_file(path)
                for item, path in zip(stored_segments, segment_paths)
            ):
                continue
            generated.append(chapter.chapter_number)

            master_file = (
                workspace / "chapters" / f"chapter_{chapter.chapter_number:03d}.wav"
            )
            master_info_file = master_manifest_path(
                project_dir, chapter.chapter_number
            )
            if not self._valid_audio(master_file) or not master_info_file.exists():
                continue
            try:
                master_info = json.loads(
                    master_info_file.read_text(encoding="utf-8")
                )
            except Exception:
                continue
            if (
                master_info.get("segment_manifest_hash")
                == stored_manifest["manifest_hash"]
                and master_info.get("dependency_hash")
                == self._master_dependency_hash(
                    project_id,
                    stored_manifest["manifest_hash"],
                    MasterChapterRequest(
                        project_id=project_id,
                        chapter_number=chapter.chapter_number,
                        segments=[
                            MasterSegmentInfo(
                                line_id=line.line_id,
                                file=(
                                    f"{project_id}/segments/"
                                    f"{line.line_id}.wav"
                                ),
                                pause_before_ms=line.pause_before_ms,
                                pause_after_ms=line.pause_after_ms,
                            )
                            for line in chapter.lines
                        ],
                        chapter_title=chapter.chapter_title,
                        announce_chapter=True,
                    ),
                )
                and master_info.get("output_hash") == hash_file(master_file)
            ):
                mastered.append(chapter.chapter_number)

        return sorted(generated), sorted(mastered)

    @staticmethod
    def _voice_generation_config(project_id: str | None = None) -> dict[str, Any]:
        """Return Voice settings that can change generated segment bytes."""
        config_path = Path("voice/config.yaml")
        if not config_path.exists():
            return {}
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        result = {
            "tts": config.get("tts", {}),
            "validation": config.get("validation", {}),
        }
        if project_id:
            voice_root = Path(
                config.get("storage", {}).get(
                    "voice_library_dir",
                    "voice_library",
                )
            )
            project_voice_dir = voice_root / project_id
            result["voice_reference_hashes"] = {
                path.stem: digest
                for path in sorted(project_voice_dir.glob("*.wav"))
                if (digest := hash_file(path))
            }
        return result

    @staticmethod
    def _master_dependency_hash(
        project_id: str,
        segment_manifest_hash: str,
        request: MasterChapterRequest,
    ) -> str:
        """Fingerprint every local input that can change a mastered chapter."""
        voice_config: dict[str, Any] = {}
        voice_config_path = Path("voice/config.yaml")
        if voice_config_path.exists():
            voice_config = yaml.safe_load(
                voice_config_path.read_text(encoding="utf-8")
            ) or {}
        narrator_reference = (
            Path(
                voice_config.get("storage", {}).get(
                    "voice_library_dir",
                    "voice_library",
                )
            )
            / project_id
            / "narrator.wav"
        )
        return fingerprint(
            {
                "schema": MASTERING_SCHEMA_VERSION,
                "segment_manifest_hash": segment_manifest_hash,
                "master_request": request.model_dump(mode="json"),
                "tts": voice_config.get("tts", {}),
                "mastering": voice_config.get("mastering", {}),
                "narrator_reference_hash": hash_file(narrator_reference),
            }
        )

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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml

from brain.director.attribution_audit import (
    audit_book_attribution,
    queue_attribution_audit_issues,
    repair_deterministic_named_attribution,
)
from brain.director.character_analyzer import (
    _SYSTEM_PROMPT as CHARACTER_SYSTEM_PROMPT,
)
from brain.director.character_analyzer import (
    CHARACTER_ANALYSIS_REVISION,
    CharacterAnalyzer,
)
from brain.director.ollama_client import OllamaClient
from brain.director.script_generator import ScriptGenerator
from brain.extractor.epub_parser import EpubParser
from brain.orchestrator.audio_candidates import preserve_candidate
from brain.orchestrator.delivery_manager import DeliveryManager
from brain.orchestrator.job_queue import JobQueue
from brain.orchestrator.notifier import notifier
from brain.orchestrator.review_gate import collect_review_gate, write_release_report
from brain.orchestrator.stage_runner import PipelineResumePlan
from brain.orchestrator.voice_client import VoiceClient
from brain.validators.gemini_validation import GeminiValidationService
from shared import paths as shared_paths
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
from shared.config_validation import validate_brain_config
from shared.constants import (
    DEFAULT_OLLAMA_MODEL,
    MASTERING_SCHEMA_VERSION,
    GenerationCancelled,
    PipelineStage,
    apply_torch_alloc_conf,
)
from shared.logging_utils import rotate_file
from shared.models import (
    AudiobookMetadata,
    BookScript,
    BootstrapVoicesRequest,
    ExportChapterInfo,
    ExportM4BRequest,
    GenerateChapterRequest,
    MasterChapterRequest,
    MasterSegmentInfo,
    ProjectStatus,
)
from shared.progress import ProgressEstimator
from shared.pronunciation import (
    apply_pronunciations,
    build_pronunciation_inventory,
    load_pronunciation_dictionary,
)
from shared.reference_selection import select_reference_text
from shared.single_instance import SingleInstanceLock
from shared.voice_casting import (
    build_voice_cast,
    required_voice_character_ids,
)

logger = logging.getLogger(__name__)


class _GracefulDeliveryPause(Exception):
    """Raised inside _run_incremental_delivery to pause after a batch completes.

    Using a dedicated exception (rather than StopIteration, which has special
    meaning in Python's iterator and generator protocol) makes the intent clear
    and prevents accidental silencing by comprehensions or generator internals.
    """


class _WaitingForReview(Exception):
    """Park the worker without treating a human release gate as a failure."""

    def __init__(self, item_ids: set[str], reason: str):
        super().__init__(reason)
        self.item_ids = sorted(item_ids)
        self.reason = reason


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
        config_path: str | Path = shared_paths.BRAIN_CONFIG_PATH,
        projects_dir: str | Path = shared_paths.PROJECTS_DIR,
        workspace_dir: str | Path | None = None,
    ):
        self.config_path = Path(config_path)
        self.projects_dir = Path(projects_dir)
        # Audio intermediates root. Injectable so it is not a hidden global:
        # tests and alternate layouts can point it elsewhere, and production
        # resolves it from the repository root rather than the working
        # directory.
        self.workspace_dir = Path(
            workspace_dir
            if workspace_dir is not None
            else shared_paths.WORKSPACE_DIR
        )
        self.projects_dir.mkdir(parents=True, exist_ok=True)

        self.config = self._load_config()
        self.job_queue = JobQueue(
            db_path=str(self.projects_dir / self.config.get("pipeline", {}).get("state_db", "pipeline_state.db"))
        )

        # Initialize clients
        ollama_cfg = self.config.get("ollama", {})
        self.ollama = OllamaClient(
            host=ollama_cfg.get("host", "http://localhost:11434"),
            model=ollama_cfg.get("model", DEFAULT_OLLAMA_MODEL),
            fallback_models=ollama_cfg.get("fallback_models", []),
            timeout=ollama_cfg.get("timeout", 120),
            max_retries=ollama_cfg.get("max_retries", 3),
            max_retry_seconds=ollama_cfg.get("max_retry_seconds", 900),
            context_window=int(ollama_cfg.get("context_window", 8192)),
            max_output_tokens=int(ollama_cfg.get("max_output_tokens", 8192)),
            max_generation_seconds=int(
                ollama_cfg.get("max_generation_seconds", 600)
            ),
            repetition_window_chars=int(
                ollama_cfg.get("repetition_window_chars", 512)
            ),
            repetition_count=int(ollama_cfg.get("repetition_count", 4)),
            think=ollama_cfg.get("think"),
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
            skip_preface=extract_cfg.get("skip_preface", True),
            min_chapter_words=extract_cfg.get("min_chapter_words", 100),
            max_chapter_words=extract_cfg.get("max_chapter_words", 20_000),
            chapter_detection=extract_cfg.get("chapter_detection", "auto"),
            preserve_poetry=extract_cfg.get("preserve_poetry", True),
        )

        self.external_validator = GeminiValidationService(
            dict(self.config.get("external_validation", {})),
            self.projects_dir,
            event_sink=self.job_queue.log_external_validation,
        )

        # Director config
        self.character_analyzer = CharacterAnalyzer(
            ollama=self.ollama,
            temperature=ollama_cfg.get("temperature_pass1", 0.3),
            max_unique_voices=self.config.get("script", {}).get("max_unique_voices", 0),
            external_validator=self.external_validator,
        )

        script_cfg = self.config.get("script", {})
        self.script_generator = ScriptGenerator(
            ollama=self.ollama,
            temperature=ollama_cfg.get("temperature_pass2", 0.2),
            chunk_size_words=script_cfg.get("chunk_size_words", 450),
            chunk_overlap_words=script_cfg.get("chunk_overlap_words", 0),
            max_fragments_per_chunk=script_cfg.get("max_fragments_per_chunk", 60),
            adaptive_split_enabled=script_cfg.get(
                "adaptive_split_enabled", True
            ),
            adaptive_split_max_depth=script_cfg.get(
                "adaptive_split_max_depth", 2
            ),
            adaptive_split_min_fragments=script_cfg.get(
                "adaptive_split_min_fragments", 8
            ),
            dialogue_focused_schema=script_cfg.get(
                "dialogue_focused_schema", False
            ),
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
        self._voice_server_log_handle = None
        self._progress_estimator = ProgressEstimator(window_size=20)

    def stop(self, project_id: str) -> None:
        """Immediately interrupt work; completed checkpoints remain reusable."""
        self._stop_flags[project_id] = True
        self.ollama.cancel_current(force=True)

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

        if start_time == end_time:
            return False
        if start_time < end_time:
            in_time = start_time <= now_time < end_time
        elif now_time >= start_time:
            in_time = True
        elif now_time < end_time:
            in_time = True
            anchor = now - timedelta(days=1)
        else:
            in_time = False

        days = win.get("days", [])
        return in_time and bool(days) and anchor.strftime("%A") in days

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
            with open(self.config_path, encoding="utf-8") as f:
                return validate_brain_config(yaml.safe_load(f) or {})
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

        # --- GPU selection ------------------------------------------------
        # `GGML_VK_VISIBLE_DEVICES` selects the Vulkan backend. `HIP_VISIBLE_DEVICES`
        # selects ROCm/hipBLAS, which on RDNA3 is usually markedly faster for
        # *prompt processing* (prefill) and is where flash attention is actually
        # implemented. Both achieve the same device isolation; the backend is
        # now an explicit choice rather than an implicit consequence of which
        # variable happens to be set.
        backend = str(ollama_cfg.get("gpu_backend", "vulkan")).strip().lower()
        visible_devices = str(
            ollama_cfg.get(
                "visible_devices",
                ollama_cfg.get("vulkan_visible_devices", ""),
            )
        ).strip()
        if visible_devices:
            if backend == "rocm":
                env["HIP_VISIBLE_DEVICES"] = visible_devices
                env["ROCR_VISIBLE_DEVICES"] = visible_devices
            else:
                env["GGML_VK_VISIBLE_DEVICES"] = visible_devices

        if ollama_cfg.get("flash_attention", True):
            env["OLLAMA_FLASH_ATTENTION"] = "1"

        # --- Slot count and KV cache --------------------------------------
        # This was previously left to Ollama's autodetection, which commonly
        # chooses 4. Two costs follow from that:
        #
        #  1. KV cache is reserved *per slot*, so `num_ctx: 16384` with 4 slots
        #     reserves up to 65,536 tokens of cache. On a 27B model that can
        #     force layers off the GPU despite `"num_gpu": 99`.
        #  2. Sequential chunk requests can land on different slots, which
        #     defeats prefix-cache reuse of the system prompt. That prompt is
        #     byte-identical across every chunk of a chapter (see
        #     `ScriptGenerator._process_fragments`), so re-evaluating it is
        #     pure waste -- and measured `prompt_eval_duration` has been ~33%
        #     of a request's working time.
        #
        # One slot is correct here: the pipeline serializes GPU work anyway.
        env["OLLAMA_NUM_PARALLEL"] = str(
            int(ollama_cfg.get("num_parallel", 1))
        )

        kv_cache_type = str(ollama_cfg.get("kv_cache_type", "")).strip()
        if kv_cache_type:
            # Requires flash attention. Quantizing the KV cache cuts its VRAM
            # substantially, which helps keep a 27B model fully offloaded at a
            # 16k context.
            env["OLLAMA_KV_CACHE_TYPE"] = kv_cache_type

        keep_alive = str(ollama_cfg.get("keep_alive", "")).strip()
        if keep_alive:
            # The pipeline unloads explicitly at stage boundaries, so the
            # default 5-minute idle unload only risks paying an unnecessary
            # ~45s model reload if a stage stalls mid-run.
            env["OLLAMA_KEEP_ALIVE"] = keep_alive

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
        rotate_file(log_path)
        self._ollama_server_log_handle = open(
            log_path,
            "a",
            encoding="utf-8",
            buffering=1,
        )
        kwargs["stdout"] = self._ollama_server_log_handle
        kwargs["stderr"] = subprocess.STDOUT
        logger.info(
            "Starting managed Ollama at %s (backend=%s, devices=%s, "
            "num_parallel=%s, kv_cache=%s, models=%s, log=%s)",
            self.ollama.host,
            backend,
            visible_devices or "server default",
            env.get("OLLAMA_NUM_PARALLEL"),
            kv_cache_type or "default",
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
        # This subprocess is the one that loads and unloads models repeatedly,
        # and is where the fragmentation crashes in `voice_crash.log` occurred.
        apply_torch_alloc_conf(env)

        log_path = self.projects_dir / "voice-server-managed.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        rotate_file(log_path)
        self._voice_server_log_handle = open(
            log_path,
            "a",
            encoding="utf-8",
            buffering=1,
        )

        logger.info("Managed Voice Server output: %s", log_path)
        try:
            self._voice_server_proc = subprocess.Popen(
                [str(python_exe), "-m", "voice.tts_server.main"],
                cwd=str(Path.cwd()),
                env=env,
                stdout=self._voice_server_log_handle,
                stderr=subprocess.STDOUT,
            )
        except Exception:
            self._voice_server_log_handle.close()
            self._voice_server_log_handle = None
            raise

        timeout = voice_cfg.get("startup_timeout_seconds", 120)
        if not self.voice_client.wait_for_server(max_wait_seconds=timeout):
            self._stop_voice_server()
            raise RuntimeError(f"Voice server subprocess failed to start within {timeout}s")

    def _assert_attribution_audit(
        self,
        project_dir: Path,
        *,
        enforce: bool = True,
        chapter_numbers: set[int] | None = None,
    ) -> dict[str, Any]:
        """Persist the source-grounded audit and optionally enforce its gate."""
        from shared.models import CharacterRegistry, ExtractedBook, ScriptChapter

        book = ExtractedBook.model_validate_json(
            (project_dir / "book.json").read_text(encoding="utf-8")
        )
        registry = CharacterRegistry.model_validate_json(
            (project_dir / "characters.json").read_text(encoding="utf-8")
        )
        scripts = [
            ScriptChapter.model_validate_json(path.read_text(encoding="utf-8"))
            for path in self._script_files(project_dir / "script")
        ]
        if chapter_numbers is not None:
            scripts = [
                script for script in scripts
                if script.chapter_number in chapter_numbers
            ]
            book = book.model_copy(update={
                "chapters": [
                    chapter for chapter in book.chapters
                    if chapter.number in chapter_numbers
                ]
            })
        report = audit_book_attribution(
            book,
            registry,
            scripts,
            confidence_threshold=self.script_generator.speaker_confidence_threshold,
        )
        report["chapter_scope"] = (
            sorted(chapter_numbers) if chapter_numbers is not None else None
        )
        report_path = project_dir / "attribution_audit.json"
        atomic_write_json(report_path, report)
        if enforce and not report["passed"]:
            from brain.orchestrator.review_gate import collect_review_gate
            blocking_items = [
                item
                for item in collect_review_gate(
                    project_dir.name, project_dir, self.job_queue
                ).blocking_items
                if item.category == "attribution"
                and (chapter_numbers is None or item.chapter_number in chapter_numbers)
            ]
            if blocking_items:
                count = len(blocking_items)
                raise RuntimeError(
                    f"Speaker attribution release gate failed with {count} "
                    f"blocking issue(s); inspect {report_path}"
                )
        return report

    def _update_long_form_audio_quality(
        self,
        project_id: str,
        project_dir: Path,
    ) -> dict[str, Any]:
        """Persist book-level voice drift/prosody trends from paid-for metrics."""
        from brain.orchestrator.quality_trends import build_long_form_quality_report
        from shared.models import ScriptChapter

        scripts = [
            ScriptChapter.model_validate_json(path.read_text(encoding="utf-8"))
            for path in self._script_files(project_dir / "script")
        ]
        report = build_long_form_quality_report(
            self.job_queue.get_quality_report(project_id),
            scripts,
        )
        atomic_write_json(project_dir / "long_form_audio_quality.json", report)
        return report

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
                log_handle = getattr(self, "_voice_server_log_handle", None)
                if log_handle is not None:
                    log_handle.close()
                    self._voice_server_log_handle = None
        else:
            log_handle = getattr(self, "_voice_server_log_handle", None)
            if log_handle is not None:
                log_handle.close()
                self._voice_server_log_handle = None

    # ------------------------------------------------------------------
    # Schedule & Deployment Controls
    # ------------------------------------------------------------------

    def _check_schedule(self, project_id: str) -> None:
        """Pause pipeline if outside configured working hours."""
        schedule_cfg = self.config.get("schedule", {})
        if not schedule_cfg.get("enabled", False):
            return

        state = self.job_queue.get_job(project_id)
        if state.get("schedule_override_active"):
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
            current_state = self.job_queue.get_job(project_id)
            if current_state.get("schedule_override_active"):
                return True
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

        managed_voice_server = getattr(self, "_voice_server_proc", None) is not None
        if managed_voice_server:
            # A schedule pause may last hours. Release the GPU instead of
            # keeping the local TTS model resident while no work is allowed.
            self._stop_voice_server()
        self._pause_at_boundary(
            project_id,
            PipelineStage.PAUSED_SCHEDULED,
            "outside configured working hours",
            schedule_open,
            2,
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

        # Preserve the immutable import source so an explicit re-extraction can
        # be performed later.  Older projects without this file are rejected by
        # reextract_project rather than having their only book.json deleted.
        shutil.copy2(Path(epub_path), project_dir / "source.epub")

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
            started_at=datetime.now(UTC),
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

    def reextract_project(self, project_id: str) -> ProjectStatus:
        """Rebuild book.json from the preserved project source EPUB."""
        project_dir = self.projects_dir / project_id
        source_path = project_dir / "source.epub"
        if not source_path.is_file():
            raise FileNotFoundError(
                "This project predates source preservation and cannot be "
                "re-extracted. Import the EPUB as a new project instead."
            )
        state = self.job_queue.get_job(project_id)
        book = self.parser.parse(source_path)
        if state.get("title"):
            book.metadata.title = str(state["title"])
        if state.get("author"):
            book.metadata.author = str(state["author"])
        if book.metadata.cover_image_path:
            cover_src = Path(book.metadata.cover_image_path)
            if cover_src.exists():
                cover_dest = project_dir / cover_src.name
                if cover_src.resolve() != cover_dest.resolve():
                    shutil.copy2(cover_src, cover_dest)
                book.metadata.cover_image_path = str(cover_dest)
        atomic_write_text(
            project_dir / "book.json",
            book.model_dump_json(indent=2),
        )
        self.job_queue.update_job(
            project_id,
            {
                "total_chapters": len(book.chapters),
                "total_words": book.metadata.total_words,
            },
        )
        state = self.job_queue.get_job(project_id)
        return ProjectStatus(**state)

    # ------------------------------------------------------------------
    # Full pipeline run
    # ------------------------------------------------------------------

    def run(self, project_id: str) -> ProjectStatus:
        """Run one globally serialized pipeline worker."""
        run_lock = SingleInstanceLock("crazy-audiobook-pipeline.lock")
        if not run_lock.acquire():
            raise RuntimeError(
                "Another audiobook pipeline worker still owns the GPU lease"
            )
        try:
            return self._run_exclusive(project_id)
        finally:
            run_lock.release()

    def _run_exclusive(self, project_id: str) -> ProjectStatus:
        """Run the pipeline after the process-wide GPU lease is acquired."""
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
        if current_stage in (PipelineStage.COMPLETE, PipelineStage.SELECTION_COMPLETE, PipelineStage.PAUSED, PipelineStage.ERROR, PipelineStage.PAUSED_SCHEDULED, PipelineStage.DEPLOY_PAUSED, PipelineStage.WAITING_FOR_REVIEW):
            current_stage = PipelineResumePlan.from_state(state).stage
            self.job_queue.update_job(
                project_id,
                {
                    "status": current_stage.value,
                    "active_stage": current_stage.value,
                    "pause_reason": None,
                },
            )

        state = self.job_queue.get_job(project_id)
        prev_elapsed = float(state.get("elapsed_seconds") or 0.0)
        start_time = time.time() - prev_elapsed
        self._stop_flags[project_id] = False
        self.ollama.begin_run()
        ollama_used = False
        active_selection = (
            state.get("active_generation_chapter_selection")
            if state.get("resume_after_restart")
            else state.get("generation_chapter_selection")
        )
        self.job_queue.update_job(
            project_id,
            {
                "active_generation_chapter_selection": active_selection,
                "resume_after_restart": False,
                "last_run_started_at": datetime.now(UTC).isoformat(),
                "elapsed_seconds": prev_elapsed,
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
                self.job_queue.update_job(
                    project_id,
                    {
                        "voice_review_status": "pending",
                        "voice_review_approved": False,
                        "voice_review_approved_at": None,
                        "bootstrapping_completed": False,
                    },
                )
                self._start_ollama_server()
                ollama_used = True
                self._run_script_director(project_id, project_dir)

                # Do not spend time designing voices for a script that already
                # contradicts explicit source attribution. The review queue is
                # persisted by _run_script_director; this gate parks the worker
                # before the voice service is started.
                attribution_audit = self._assert_attribution_audit(
                    project_dir,
                    enforce=False,
                )
                if not attribution_audit["passed"]:
                    attribution_items = [
                        item.item_id
                        for item in collect_review_gate(
                            project_id, project_dir, self.job_queue
                        ).blocking_items
                        if item.category == "attribution"
                    ]
                    if not attribution_items:
                        attribution_items = sorted(
                            {
                                str(issue.get("line_id"))
                                for issue in attribution_audit.get("issues", [])
                                if issue.get("line_id")
                            }
                        )
                    raise _WaitingForReview(
                        attribution_items,
                        f"{len(attribution_items)} speaker attribution(s) "
                        "require review before voice bootstrapping.",
                    )

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
            cast_path = project_dir / "voice_cast.json"
            needs_bootstrap = (
                not state.get("bootstrapping_completed", False)
                or bool(state.get("force_voice_regeneration", False))
            )
            if cast_path.exists():
                try:
                    cast_data = json.loads(cast_path.read_text(encoding="utf-8"))
                    if any(not v.get("ready") for v in cast_data.get("voices", {}).values()):
                        needs_bootstrap = True
                except Exception:
                    needs_bootstrap = True
            if needs_bootstrap:
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
            state = self.job_queue.get_job(project_id)
            incremental_cfg = state.get("incremental_delivery") or {}
            incremental_enabled = (
                incremental_cfg.get("enabled", False)
                if isinstance(incremental_cfg, dict)
                else getattr(incremental_cfg, "enabled", False)
            )
            selection = state.get("active_generation_chapter_selection")

            if incremental_enabled:
                self._run_incremental_delivery(project_id, project_dir, current_stage)
                elapsed = time.time() - start_time
                self._update_stage(
                    project_id,
                    PipelineStage.COMPLETE,
                    elapsed_seconds=elapsed,
                )
                logger.info(
                    "Pipeline complete (incremental delivery) for '%s' in %.1f minutes",
                    project_id,
                    elapsed / 60,
                )
                return ProjectStatus(**self.job_queue.get_job(project_id))

            if current_stage not in {
                PipelineStage.MASTERING,
                PipelineStage.EXPORTING,
            }:
                self._run_generation(project_id, project_dir)

            self._check_stop(project_id)
            if current_stage != PipelineStage.EXPORTING:
                release = write_release_report(project_id, project_dir, self.job_queue)
                if not release["release_ready"]:
                    raise _WaitingForReview(
                        [item["item_id"] for item in release["items"] if item["blocking"]],
                        f"{release['blocking_count']} item(s) require review before mastering.",
                    )
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

        except _WaitingForReview as review_pause:
            elapsed = time.time() - start_time
            logger.info(
                "Pipeline waiting for review for '%s': %s",
                project_id,
                review_pause.item_ids,
            )
            self._update_stage(
                project_id,
                PipelineStage.WAITING_FOR_REVIEW,
                elapsed_seconds=elapsed,
            )
            self.job_queue.update_job(
                project_id,
                {
                    "pause_reason": review_pause.reason,
                    "review_blocking_item_ids": review_pause.item_ids,
                    "error_message": None,
                },
            )
            try:
                job_info = self.job_queue.get_job(project_id) or {}
                p_title = job_info.get("title") or project_id
                char_count = len(review_pause.item_ids or [])
                notifier.notify_review_required(
                    project_id=project_id,
                    project_title=p_title,
                    reason=review_pause.reason,
                    item_count=char_count,
                    item_ids=review_pause.item_ids or [],
                )
            except Exception as notif_exc:
                logger.debug("Notification error: %s", notif_exc)
            return ProjectStatus(**self.job_queue.get_job(project_id))
        except _GracefulDeliveryPause as _gdp:
            elapsed = time.time() - start_time
            logger.info(
                "Pipeline paused after incremental delivery batch '%s' for '%s'",
                _gdp,
                project_id,
            )
            self._update_stage(
                project_id,
                PipelineStage.PAUSED,
                elapsed_seconds=elapsed,
            )
            try:
                job_info = self.job_queue.get_job(project_id) or {}
                p_title = job_info.get("title") or project_id
                notifier.notify_paused_after_delivery(project_id, p_title, completed_part=str(_gdp))
            except Exception as notif_exc:
                logger.debug("Notification error: %s", notif_exc)
            return ProjectStatus(**self.job_queue.get_job(project_id))
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error("Pipeline failed for '%s': %s", project_id, e, exc_info=True)
            self._update_stage(
                project_id,
                PipelineStage.ERROR,
                error_message=str(e),
                elapsed_seconds=elapsed,
            )
            try:
                job_info = self.job_queue.get_job(project_id) or {}
                p_title = job_info.get("title") or project_id
                curr_ch = job_info.get("current_chapter")
                notifier.notify_generation_error(project_id, p_title, str(e), chapter=curr_ch)
            except Exception as notif_exc:
                logger.debug("Notification error: %s", notif_exc)
            raise
        except (GenerationCancelled, KeyboardInterrupt):
            # `GenerationCancelled` is an operator pause propagated from the
            # Ollama client; `KeyboardInterrupt` is a real Ctrl-C. Both mean
            # "park this run and keep the completed checkpoints", and both are
            # BaseException subclasses so they tunnel past the broad
            # `except Exception` handlers above.
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
            character_source_fingerprint = self._character_source_fingerprint(book)
            expected_character_fingerprint = fingerprint(
                {
                    "source_fingerprint": character_source_fingerprint,
                    "model": self.ollama.model,
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

    def _joint_script_analysis_enabled(self) -> bool:
        return bool(self.config.get("script", {}).get("joint_analysis", False))

    def _character_augmentation_dependency(self) -> dict[str, Any]:
        """Return serializable configuration that can change character output."""
        external = dict(self.config.get("external_validation", {}))
        api = dict(external.get("api", {}))
        character = dict(external.get("character_augmentation", {}))
        return {
            "enabled": bool(external.get("enabled", False)),
            "character_enabled": bool(character.get("enabled", False)),
            "auto_accept": float(external.get("auto_accept_confidence", 0.9)),
            "triage_model": str(
                character.get(
                    "triage_model",
                    api.get("triage_model", "gemini-3.5-flash-lite"),
                )
            ),
            "adjudication_model": str(
                character.get(
                    "adjudication_model",
                    api.get("adjudication_model", "gemini-3.5-flash"),
                )
            ),
            "policy": "source-grounded-confidence-v1",
        }

    def _character_source_fingerprint(self, book) -> str:
        """Fingerprint character inputs independently of the runtime model.

        An in-progress joint-analysis checkpoint is a transaction boundary: a
        model/runtime migration may continue from it when the book and every
        character-analysis policy input are unchanged. Keeping this provenance
        separate avoids either discarding completed chapter work or pretending
        that an old registry was produced by the newly configured model.
        """
        return fingerprint(
            {
                "book": book.model_dump(mode="json"),
                "prompt": CHARACTER_SYSTEM_PROMPT,
                "analysis_revision": CHARACTER_ANALYSIS_REVISION,
                "max_unique_voices": self.character_analyzer.max_unique_voices,
                "single_pass_threshold": self.character_analyzer.single_pass_threshold,
                "character_augmentation": self._character_augmentation_dependency(),
            }
        )

    @staticmethod
    def _joint_checkpoint_is_compatible(
        checkpoint: dict[str, Any],
        *,
        character_fingerprint: str,
        source_fingerprint: str,
    ) -> bool:
        """Accept exact checkpoints or source-equivalent runtime migrations."""
        return bool(
            checkpoint.get("fingerprint") == character_fingerprint
            or checkpoint.get("source_fingerprint") == source_fingerprint
        )

    @staticmethod
    def _apply_character_overrides(project_dir: Path, registry) -> bool:
        """Reapply explicit human corrections after automated analysis."""
        overrides_path = project_dir / "character_overrides.json"
        if not overrides_path.is_file():
            return False
        try:
            overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Ignoring invalid character overrides: %s", overrides_path)
            return False
        allowed = {"gender", "age_range", "voice_description", "speaking_style"}
        changed = False
        for character_id, fields in overrides.get("characters", {}).items():
            character = registry.characters.get(character_id)
            if character is None or not isinstance(fields, dict):
                continue
            safe_fields = {key: value for key, value in fields.items() if key in allowed}
            if not safe_fields:
                continue
            try:
                registry.characters[character_id] = type(character).model_validate(
                    {**character.model_dump(mode="json"), **safe_fields}
                )
                changed = True
            except ValueError:
                logger.warning(
                    "Ignoring invalid override fields for character '%s'",
                    character_id,
                )
        return changed

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
        chars_source_fingerprint = self._character_source_fingerprint(book)
        chars_fingerprint = fingerprint(
            {
                "source_fingerprint": chars_source_fingerprint,
                "model": self.ollama.model,
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

        self._progress_estimator.reset(f"{project_id}:character_analysis")
        self.job_queue.update_progress(
            project_id,
            self._progress_estimator.snapshot(
                f"{project_id}:character_analysis",
                stage=PipelineStage.SCRIPTING.value,
                phase=(
                    "fragment_annotation"
                    if reuse_characters
                    else "character_discovery"
                ),
                message=(
                    "Preparing chapter scripting..."
                    if reuse_characters
                    else "Pass 1: Discovering characters and voice profiles..."
                ),
                completed_units=0,
                total_units=len(book.chapters),
                chapter=1,
                chapter_position=1,
                chapter_total=len(book.chapters),
            ),
        )

        joint_discovery = self._joint_script_analysis_enabled()
        joint_checkpoint_path = project_dir / "characters.joint.checkpoint.json"
        discovery_checkpoint_chapters: set[int] = set()
        def _analyzer_check():
            self._check_stop(project_id)
            self._check_schedule(project_id)
            self._check_deployment_pause(project_id)

        if joint_discovery:
            if reuse_characters:
                from shared.models import CharacterRegistry
                registry = CharacterRegistry.model_validate_json(chars_path.read_text(encoding="utf-8"))
            else:
                from shared.models import CharacterRegistry
                registry = self.character_analyzer.create_joint_seed_registry(book)
                if joint_checkpoint_path.is_file():
                    try:
                        checkpoint = json.loads(
                            joint_checkpoint_path.read_text(encoding="utf-8")
                        )
                        if self._joint_checkpoint_is_compatible(
                            checkpoint,
                            character_fingerprint=chars_fingerprint,
                            source_fingerprint=chars_source_fingerprint,
                        ):
                            registry = CharacterRegistry.model_validate(
                                checkpoint["registry"]
                            )
                            discovery_checkpoint_chapters = {
                                int(number)
                                for number in checkpoint.get(
                                    "completed_chapters",
                                    [],
                                )
                            }
                            logger.info(
                                "Loaded source-compatible joint character checkpoint "
                                "through chapter %d%s",
                                max(discovery_checkpoint_chapters, default=0),
                                (
                                    " after a runtime-model migration"
                                    if checkpoint.get("fingerprint") != chars_fingerprint
                                    else ""
                                ),
                            )
                    except (OSError, ValueError, TypeError, KeyError):
                        logger.warning(
                            "Ignoring invalid joint character checkpoint for '%s'",
                            project_id,
                        )
        else:
            if reuse_characters:
                from shared.models import CharacterRegistry
                registry = CharacterRegistry.model_validate_json(chars_path.read_text(encoding="utf-8"))
            else:
                chars_ckpt_path = project_dir / "characters.checkpoint.json"
                analysis_tick = time.perf_counter()

                def _analyzer_progress(percent: float, message: str, ch_num: int, unit_idx: int, total_units: int) -> None:
                    """Publish character-discovery progress via the canonical snapshot."""
                    nonlocal analysis_tick
                    key = f"{project_id}:character_analysis"
                    now = time.perf_counter()
                    if unit_idx > 1:
                        self._progress_estimator.observe(key, 1.0, now - analysis_tick)
                    analysis_tick = now
                    self.job_queue.update_progress(
                        project_id,
                        self._progress_estimator.snapshot(
                            key,
                            stage=PipelineStage.SCRIPTING.value,
                            phase="character_discovery",
                            message=message,
                            completed_units=max(0, unit_idx),
                            total_units=max(1, total_units),
                            chapter=ch_num,
                            chapter_position=ch_num,
                            chapter_total=len(book.chapters),
                            line_position=unit_idx,
                            line_total=total_units,
                        ),
                    )

                registry = self.character_analyzer.analyze(
                    book,
                    check_callback=_analyzer_check,
                    checkpoint_path=chars_ckpt_path,
                    checkpoint_fingerprint=chars_fingerprint,
                    reference_audit_path=(
                        project_dir / "character_reference_audit.json"
                    ),
                    project_dir=project_dir,
                    progress_callback=_analyzer_progress,
                )
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

        if self._apply_character_overrides(project_dir, registry):
            atomic_write_text(chars_path, registry.model_dump_json(indent=2))

        scripts_dir = project_dir / "script"
        scripts_dir.mkdir(exist_ok=True)

        current_script_chapter = 1
        script_tick = time.perf_counter()

        def on_chapter_start(chapter_num: int):
            nonlocal current_script_chapter, script_tick
            self._check_stop(project_id)
            self._check_schedule(project_id)
            self._check_deployment_pause(project_id)
            current_script_chapter = chapter_num
            script_tick = time.perf_counter()

            state = self.job_queue.get_job(project_id)
            updates = {"current_script_chapter": chapter_num}
            for key in ["scripted_chapters", "generated_chapters", "mastered_chapters"]:
                if key in state and chapter_num in state[key]:
                    state[key].remove(chapter_num)
                    updates[key] = state[key]
            self.job_queue.update_job(project_id, updates)

            # Remove stale chapter from book_script.json so UI doesn't show old lines
            merged_script = project_dir / "book_script.json"
            if merged_script.exists():
                try:
                    import json
                    merged_data = json.loads(merged_script.read_text(encoding="utf-8"))
                    if "chapters" in merged_data:
                        merged_data["chapters"] = [c for c in merged_data["chapters"] if c.get("chapter_number") != chapter_num]
                        from brain.utils.file_utils import atomic_write_text
                        atomic_write_text(merged_script, json.dumps(merged_data, indent=2, ensure_ascii=False))
                except Exception:
                    pass
            completed = len(self.job_queue.get_job(project_id).get("scripted_chapters", []))
            ch_idx = chapter_num - 1
            ch_obj = book.chapters[ch_idx] if 0 <= ch_idx < len(book.chapters) else None
            ch_title = getattr(ch_obj, "title", None) or f"chapter {chapter_num}"
            self.job_queue.update_progress(
                project_id,
                self._progress_estimator.snapshot(
                    f"{project_id}:scripting",
                    stage=PipelineStage.SCRIPTING.value,
                    phase="chapter_start",
                    message=f"Preparing script for {ch_title} ({chapter_num} of {len(book.chapters)})",
                    completed_units=completed,
                    total_units=len(book.chapters),
                    chapter=chapter_num,
                    chapter_position=chapter_num,
                    chapter_total=len(book.chapters),
                ),
            )

        def on_script_chunk(chunk_num: int, chunk_total: int) -> None:
            nonlocal script_tick
            now = time.perf_counter()
            if chunk_num > 1:
                self._progress_estimator.observe(
                    f"{project_id}:scripting",
                    1.0 / max(chunk_total, 1),
                    now - script_tick,
                )
            script_tick = now
            completed_chapters = len(
                self.job_queue.get_job(project_id).get("scripted_chapters", [])
            )
            completed = completed_chapters + max(0, chunk_num - 1) / max(
                chunk_total, 1
            )
            ch_idx = current_script_chapter - 1
            ch_obj = book.chapters[ch_idx] if 0 <= ch_idx < len(book.chapters) else None
            ch_title = getattr(ch_obj, "title", None) or f"chapter {current_script_chapter}"
            self.job_queue.update_progress(
                project_id,
                self._progress_estimator.snapshot(
                    f"{project_id}:scripting",
                    stage=PipelineStage.SCRIPTING.value,
                    phase="fragment_annotation",
                    message=(
                        f"Scripting {ch_title}: "
                        f"fragment batch {chunk_num} of {chunk_total}"
                    ),
                    completed_units=completed,
                    total_units=len(book.chapters),
                    chapter=current_script_chapter,
                    chapter_position=current_script_chapter,
                    chapter_total=len(book.chapters),
                    line_position=chunk_num,
                    line_total=chunk_total,
                ),
            )

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

        def on_registry_progress(
            current_registry,
            chapter_number: int,
        ) -> None:
            discovery_checkpoint_chapters.add(chapter_number)
            atomic_write_json(
                joint_checkpoint_path,
                {
                    "fingerprint": chars_fingerprint,
                    "source_fingerprint": chars_source_fingerprint,
                    "completed_chapters": sorted(discovery_checkpoint_chapters),
                    "registry": current_registry.model_dump(mode="json"),
                },
            )

        chapter_scripts = self.script_generator.generate_all_chapters(
            book.chapters,
            registry,
            scripts_dir=scripts_dir,
            progress_callback=on_chapter_scripted,
            chapter_start_callback=on_chapter_start,
            chunk_progress_callback=on_script_chunk,
            allow_character_discovery=joint_discovery,
            discovery_checkpoint_chapters=(
                discovery_checkpoint_chapters if joint_discovery else None
            ),
            registry_progress_callback=(
                on_registry_progress if joint_discovery else None
            ),
        )

        if joint_discovery:
            reconcile_started = time.time()
            registry, speaker_remap = self.character_analyzer.finalize_joint_registry(
                registry,
                book,
                check_callback=_analyzer_check if not reuse_characters else None,
                reference_audit_path=(
                    project_dir / "character_reference_audit.json"
                ),
                project_dir=project_dir,
            )
            self._apply_character_overrides(project_dir, registry)
            self.script_generator.remap_reconciled_speakers(
                chapter_scripts,
                registry,
                speaker_remap,
            )
            self.character_analyzer._assign_voice_ids(registry.characters)
            atomic_write_text(chars_path, registry.model_dump_json(indent=2))
            atomic_write_json(
                chars_meta_path,
                {"fingerprint": chars_fingerprint},
            )
            joint_checkpoint_path.unlink(missing_ok=True)
            self.job_queue.update_job(
                project_id,
                {
                    "scripted_chapters": [c.chapter_number for c in chapter_scripts],
                    "bootstrapping_completed": False,
                    "character_analysis_fingerprint": chars_fingerprint,
                    "force_character_analysis": False,
                },
            )

        deterministic_before = repair_deterministic_named_attribution(
            book,
            registry,
            chapter_scripts,
            confidence_threshold=self.script_generator.speaker_confidence_threshold,
        )
        if deterministic_before["repaired"]:
            logger.info(
                "[AttributionRepair] Corrected %d line(s) from explicit named tags",
                len(deterministic_before["repaired"]),
            )

        # --- Tiered dialogue attribution repair (Tier 1 Local Qwen) ---
        ext_cfg = self.config.get("external_validation", {})
        tiered_cfg = ext_cfg.get("tiered_attribution", {})
        if tiered_cfg.get("enabled", True):
            try:
                from brain.director.attribution_detector import detect_suspicious_turns
                from brain.validators.tiered_adjudicator import TieredAttributionAdjudicator

                min_conf = float(tiered_cfg.get("min_detection_confidence", 0.70))
                max_short = int(tiered_cfg.get("max_short_response_chars", 80))
                local_auto_accept = float(tiered_cfg.get("local_auto_accept_confidence", 0.95))
                ollama_temp = float(tiered_cfg.get("ollama_temperature", 0.1))
                is_dry_run = bool(tiered_cfg.get("dry_run", False))

                suspicious = detect_suspicious_turns(
                    chapter_scripts,
                    min_confidence=min_conf,
                    max_short_response_chars=max_short,
                )
                if suspicious:
                    logger.info(
                        "[TieredAttribution] Detected %d suspicious dialogue turn(s) across %d chapter(s)",
                        len(suspicious),
                        len(chapter_scripts),
                    )
                    adjudicator = TieredAttributionAdjudicator(
                        ollama=self.ollama,
                        external_validator=self.external_validator,
                        registry=registry,
                        local_auto_accept=local_auto_accept,
                        ollama_temperature=ollama_temp,
                    )
                    tier1_report = adjudicator.adjudicate(
                        suspicious,
                        project_dir=project_dir,
                        chapters=chapter_scripts,
                        dry_run=is_dry_run,
                    )
                    logger.info(
                        "[TieredAttribution] Tier 1 adjudication finished: %s",
                        tier1_report.summary,
                    )
            except Exception as exc:
                logger.warning("[TieredAttribution] Tiered adjudication encountered error: %s", exc)

        post_repair_audit = audit_book_attribution(
            book,
            registry,
            chapter_scripts,
            confidence_threshold=self.script_generator.speaker_confidence_threshold,
        )
        queued_audit_lines = queue_attribution_audit_issues(
            post_repair_audit,
            chapter_scripts,
            confidence_threshold=self.script_generator.speaker_confidence_threshold,
        )
        if queued_audit_lines:
            logger.info(
                "[AttributionEscalation] Queued %d deterministic audit contradiction(s)",
                len(queued_audit_lines),
            )

        escalation = self.external_validator.resolve_attributions(
            project_dir=project_dir,
            chapters=chapter_scripts,
            character_ids=set(registry.characters),
            character_context={
                character_id: character.model_dump(
                    include={"id", "name", "aliases", "gender", "age_range"},
                    mode="json",
                )
                for character_id, character in registry.characters.items()
            },
        )
        if escalation["attempted"]:
            logger.info("[AttributionEscalation] %s", escalation)

        # External validation cannot override an unambiguous registered name in
        # the source. Re-run the deterministic pass defensively before saving.
        deterministic_after = repair_deterministic_named_attribution(
            book,
            registry,
            chapter_scripts,
            confidence_threshold=self.script_generator.speaker_confidence_threshold,
        )
        if deterministic_after["repaired"]:
            logger.warning(
                "[AttributionRepair] Reverted %d externally introduced named-tag contradiction(s)",
                len(deterministic_after["repaired"]),
            )

        total_lines = 0
        for script in chapter_scripts:
            script_path = scripts_dir / f"chapter_{script.chapter_number:03d}.json"
            atomic_write_text(script_path, script.model_dump_json(indent=2))
            source_chapter = next(
                chapter
                for chapter in book.chapters
                if chapter.number == script.chapter_number
            )
            atomic_write_json(
                scripts_dir / f"chapter_{script.chapter_number:03d}.meta.json",
                self.script_generator.chapter_dependency_metadata(
                    source_chapter,
                    registry,
                    script,
                ),
            )
            total_lines += script.total_lines

        # Persist unresolved attribution as a review queue without discarding
        # completed script work. Generation/export call this gate with enforce=True.
        self._assert_attribution_audit(project_dir, enforce=False)

        book_script = BookScript(
            metadata=book.metadata,
            character_registry=registry,
            chapters=chapter_scripts,
        )
        atomic_write_text(chars_path, registry.model_dump_json(indent=2))
        atomic_write_text(
            project_dir / "book_script.json",
            book_script.model_dump_json(indent=2),
        )
        atomic_write_json(
            project_dir / "pronunciation_candidates.json",
            build_pronunciation_inventory(project_dir, client=self.ollama),
        )

        total_elapsed = time.time() - t0
        self.job_queue.update_job(project_id, {
            "total_lines": total_lines,
            "script_completed": True,
            "current_script_chapter": None,
        })
        self._append_performance_metric(
            project_dir,
            {
                "event": "script_generation",
                "director_mode": "joint" if joint_discovery else "two_pass",
                "pass1_seconds": round(pass1_elapsed, 6),
                "pass2_seconds": round(max(0.0, total_elapsed - pass1_elapsed), 6),
                "total_seconds": round(total_elapsed, 6),
                "chapters": len(chapter_scripts),
                "segments": total_lines,
                "calls": list(self.script_generator.call_metrics),
            },
        )

    def _run_voice_bootstrap(self, project_id: str, project_dir: Path) -> None:
        """Run Stage ③: Generate voice reference clips."""
        self._update_stage(project_id, PipelineStage.BOOTSTRAPPING)
        self.job_queue.update_progress(
            project_id,
            self._progress_estimator.snapshot(
                f"{project_id}:bootstrap",
                stage=PipelineStage.BOOTSTRAPPING.value,
                phase="reference_generation",
                message="Preparing reusable voice references",
                completed_units=0,
                total_units=1,
            ),
        )

        chars_path = project_dir / "characters.json"
        from shared.models import CharacterRegistry
        registry = CharacterRegistry.model_validate_json(
            chars_path.read_text(encoding="utf-8")
        )
        # Character analysis intentionally sees the whole book and may identify
        # named non-speaking entities. Only bootstrap reference voices that are
        # actually used by a completed script, following any shared voice_id
        # assignment made by the analyzer.
        from shared.constants import Gender
        from shared.models import ScriptChapter

        script_chapters: list[ScriptChapter] = []
        for script_path in self._script_files(project_dir / "script"):
            script_chapters.append(
                ScriptChapter.model_validate_json(
                    script_path.read_text(encoding="utf-8")
                )
            )
        speaking_ids = required_voice_character_ids(script_chapters, registry)

        voice_config = shared_paths.voice_config()
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
                "reference_text_policy": "ranked-actual-dialogue-v2",
            },
        )
        # Extract actual script lines for each character
        character_script_lines: dict[str, list[str]] = {}
        speaker_script_lines: dict[str, list[str]] = {}
        for ch in script_chapters:
            for line in ch.lines:
                vid = line.voice_id or line.speaker
                character_script_lines.setdefault(vid, []).append(line.text)
                speaker_script_lines.setdefault(line.speaker, []).append(line.text)

        characters_for_request = {}
        for voice_id, profile in cast["voices"].items():
            owner_id = profile.get("owner_character_id", voice_id)
            base_char = registry.characters[owner_id]

            real_lines = list(character_script_lines.get(voice_id, []))
            if not real_lines:
                for speaker_id in profile.get("assigned_characters", []):
                    real_lines.extend(speaker_script_lines.get(speaker_id, []))
            if not real_lines:
                real_lines = list(character_script_lines.get(owner_id, []))
            reference_selection = select_reference_text(
                real_lines,
                seed_text=base_char.test_sentence or "",
                minimum_words=15,
                maximum_words=38,
            )
            ts = reference_selection.text

            dialogue_turns = sum(
                len(speaker_script_lines.get(speaker_id, []))
                for speaker_id in profile.get("assigned_characters", [])
            )
            effective_importance = (
                "major"
                if owner_id == "narrator"
                or base_char.importance == "major"
                or dialogue_turns >= 5
                else "minor"
            )
            characters_for_request[voice_id] = base_char.model_copy(
                update={
                    "id": voice_id,
                    "name": profile.get("name") or voice_id,
                    "gender": Gender(profile["gender"]),
                    "voice_description": profile["effective_prompt"],
                    "test_sentence": ts,
                    "importance": effective_importance,
                }
            )
            # The actual reference text is part of the voice-design artifact.
            # Update the profile and its fingerprint after selecting real script
            # dialogue so cache reuse cannot cross a reference-text change.
            profile["test_sentence"] = ts
            profile["reference_selection"] = {
                "policy": "ranked-actual-dialogue-v2",
                "source_line_count": reference_selection.source_line_count,
                "used_seed_text": reference_selection.used_seed_text,
                "text_score": reference_selection.score,
            }
            profile["importance"] = effective_importance
            profile["dialogue_turns"] = dialogue_turns
            profile["design_fingerprint"] = fingerprint(
                {
                    "schema": profile.get("schema", cast.get("schema", "1")),
                    "voice_id": voice_id,
                    "gender": profile["gender"],
                    "age_range": profile["age_range"],
                    "effective_prompt": profile["effective_prompt"],
                    "test_sentence": ts,
                    "design_model": profile.get("design_model", ""),
                    "design_config": profile.get("design_config", {}),
                }
            )

        request = BootstrapVoicesRequest(
            project_id=project_id,
            characters=characters_for_request,
            force_regenerate=bool(
                self.job_queue.get_job(project_id).get(
                    "force_voice_regeneration", False
                )
            ),
            design_fingerprints={
                voice_id: profile["design_fingerprint"]
                for voice_id, profile in cast["voices"].items()
            },
            candidate_counts={
                voice_id: (
                    1
                    if profile.get("owner_character_id") == "narrator"
                    else 3
                    if characters_for_request[voice_id].importance == "major"
                    else 1
                )
                for voice_id, profile in cast["voices"].items()
            },
        )
        try:
            response = self.voice_client.bootstrap_voices(request)
            for voice_id, result in response.voices_generated.items():
                profile = cast["voices"].get(voice_id)
                if not profile:
                    continue

                profile["ready"] = True
                profile["quality"] = {
                    "transcription_wer": result.transcription_wer,
                    "acoustic_metrics": result.acoustic_metrics,
                }
                existing_warnings = [
                    w for w in profile.get("warnings", [])
                    if not w.startswith("Sounds very similar to")
                ]
                for warning in result.warnings:
                    if warning not in existing_warnings:
                        existing_warnings.append(warning)
                profile["warnings"] = existing_warnings

                # Phase 3.2: Inject alternative candidates into the voice cast
                for cand in result.candidates:
                    if cand.id == voice_id or cand.id in cast["voices"]:
                        continue
                    import copy
                    cand_profile = copy.deepcopy(profile)
                    cand_profile["voice_id"] = cand.id
                    if "_cand" in cand.id:
                        cand_profile["name"] = f"Candidate {cand.id.split('_cand')[-1]}"
                    else:
                        cand_profile["name"] = f"Alternative ({cand.id})"
                    cand_profile["design_fingerprint"] = ""
                    cand_profile["assigned_characters"] = []
                    cand_profile["ready"] = True
                    cand_profile["quality"] = {
                        "transcription_wer": cand.transcription_wer,
                        "acoustic_metrics": cand.acoustic_metrics,
                    }
                    cand_profile["warnings"] = list(cand.warnings)
                    # For UI grouping, the alternative retains the original owner's ID
                    cand_profile["owner_character_id"] = profile.get("owner_character_id", voice_id)
                    cast["voices"][cand.id] = cand_profile

            cast["quality"] = {
                "distinctness_status": "current",
                "stale_voice_ids": [],
                "cast_pair_diagnostics": [
                    item.model_dump() for item in response.cast_diagnostics
                ],
                "similar_pairs": sum(
                    item.status == "similar"
                    for item in response.cast_diagnostics
                ),
            }
            cast.pop("fingerprint", None)
            cast["fingerprint"] = fingerprint(cast)
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
                    # Keep the job-state view used by /status in lockstep with
                    # the durable post-bootstrap artifact.  Otherwise the UI
                    # can keep presenting the previous cast until a separate
                    # project-details request happens to refresh it.
                    "voice_cast": cast,
                    "voice_review_status": review_status,
                    "force_voice_regeneration": False,
                },
            )
            logger.info("Voice bootstrapping complete: %d voices generated", len(response.voices_generated))
        except Exception as e:
            logger.error("Failed to bootstrap voices: %s", e)
            raise

    def _run_incremental_delivery(self, project_id: str, project_dir: Path, current_stage: PipelineStage) -> None:
        """Run incremental batching, generating, mastering, and publishing."""
        dm = DeliveryManager(project_dir)
        scripts_dir = project_dir / "script"
        script_files = self._script_files(scripts_dir)
        from shared.models import ScriptChapter as _ScriptChapter
        scripts_by_number = {
            _ScriptChapter.model_validate_json(
                path.read_text(encoding="utf-8")
            ).chapter_number: path
            for path in script_files
        }
        chapter_numbers = list(scripts_by_number)
        state = self.job_queue.get_job(project_id)
        selection = state.get("active_generation_chapter_selection")
        if selection is not None:
            selected_set = set(selection)
            chapter_numbers = [num for num in chapter_numbers if num in selected_set]
        if not chapter_numbers:
            raise RuntimeError("Incremental delivery requires at least one chapter script")
        incremental_cfg = state.get("incremental_delivery") or {}
        if isinstance(incremental_cfg, dict):
            batch_size = int(incremental_cfg.get("batch_size") or 5)
        else:
            # IncrementalDeliverySettings model instance or similar
            batch_size = int(getattr(incremental_cfg, "batch_size", 5))
        script_dependency_fingerprint = fingerprint(
            {
                str(number): hash_file(scripts_by_number[number])
                for number in chapter_numbers
            }
        )
        index, batches = dm.ensure_plan(
            chapter_numbers,
            batch_size,
            script_dependency_fingerprint=script_dependency_fingerprint,
        )
        book_data = json.loads(
            (project_dir / "book.json").read_text(encoding="utf-8")
        )
        book_meta = book_data.get("metadata", {})
        cover_candidate = Path(str(book_meta.get("cover_image_path") or ""))
        metadata_fingerprint = fingerprint(
            {
                "metadata": book_meta,
                "cover_hash": hash_file(cover_candidate),
            }
        )

        def master_dependencies(batch_numbers: list[int]) -> tuple[dict[str, str], dict[str, Any]]:
            hashes: dict[str, str] = {}
            qualities: dict[str, Any] = {}
            for number in batch_numbers:
                manifest_file = master_manifest_path(project_dir, number)
                manifest_hash = hash_file(manifest_file)
                if not manifest_hash:
                    return {}, {}
                hashes[str(number)] = manifest_hash
                try:
                    qualities[str(number)] = json.loads(
                        manifest_file.read_text(encoding="utf-8")
                    ).get("mastering_quality", {})
                except (OSError, ValueError, TypeError):
                    qualities[str(number)] = {}
            return hashes, qualities

        def update_publication_state(latest_id: str | None = None) -> None:
            current_index = dm.load_index()
            published = [
                part for part in current_index.deliveries
                if part.status == "published"
            ]
            updates: dict[str, Any] = {
                "published_delivery_count": len(published),
            }
            if latest_id:
                updates["latest_published_delivery_id"] = latest_id
            self.job_queue.update_job(project_id, updates)

        def pause_if_requested(delivery_id: str) -> None:
            current = self.job_queue.get_job(project_id)
            if not current.get("pause_after_delivery_requested", False):
                return
            self.job_queue.update_job(
                project_id,
                {
                    "pause_reason": "Graceful pause after delivery batch.",
                    "pause_after_delivery_requested": False,
                    "active_delivery_id": None,
                    "active_delivery_chapters": [],
                },
            )
            self._update_stage(project_id, PipelineStage.PAUSED)
            raise _GracefulDeliveryPause(delivery_id)

        for batch in batches:
            self._check_stop(project_id)

            self.job_queue.update_job(
                project_id,
                {
                    "active_delivery_id": batch.delivery_id,
                    "active_delivery_chapters": batch.chapter_numbers,
                },
            )

            current_master_hashes, current_quality = master_dependencies(
                batch.chapter_numbers
            )
            if dm.is_published_and_valid(
                batch,
                plan_fingerprint=index.plan_fingerprint,
                master_manifest_hashes=current_master_hashes,
                metadata_fingerprint=metadata_fingerprint,
            ):
                logger.info(
                    "Delivery %s is already published and valid; skipping",
                    batch.delivery_id,
                )
                update_publication_state(batch.delivery_id)
                pause_if_requested(batch.delivery_id)
                continue

            batch_chapter_set = set(batch.chapter_numbers)

            # Generate audio for this batch
            self._run_generation(project_id, project_dir, batch_chapter_set)
            self._check_stop(project_id)

            # Check review gate for this batch before mastering / publishing
            release = write_release_report(project_id, project_dir, self.job_queue)
            batch_blocking_items = [
                item["item_id"]
                for item in release["items"]
                if item["blocking"] and (
                    item.get("chapter_number") is None
                    or item.get("chapter_number") in batch_chapter_set
                )
            ]
            if batch_blocking_items:
                raise _WaitingForReview(
                    batch_blocking_items,
                    f"{len(batch_blocking_items)} item(s) in delivery {batch.delivery_id} require review before mastering.",
                )

            # Master this batch
            self._run_mastering(project_id, project_dir, batch_chapter_set)
            self._check_stop(project_id)

            current_master_hashes, current_quality = master_dependencies(
                batch.chapter_numbers
            )
            if len(current_master_hashes) != len(batch.chapter_numbers):
                raise RuntimeError(
                    f"Delivery {batch.delivery_id} has incomplete mastering dependencies"
                )

            # Export and index under the same project-scoped packaging lock.
            temp_export_path = project_dir / f".temp_export_{batch.delivery_id}.m4b"
            with dm.packaging_lock(wait=True):
                export_result = self._run_export(
                    project_id,
                    project_dir,
                    partial=True,
                    chapter_selection=batch_chapter_set,
                    temp_output=temp_export_path,
                    acquire_packaging_lock=False,
                ) or {}
                title = book_meta.get("title") or project_id
                published_part = dm.publish_delivery(
                    batch=batch,
                    temp_artifact_path=temp_export_path,
                    duration_seconds=float(export_result.get("duration_seconds") or 0.0),
                    master_manifest_hashes=current_master_hashes,
                    metadata_fingerprint=metadata_fingerprint,
                    book_title=title,
                    plan_fingerprint=index.plan_fingerprint,
                    quality=current_quality,
                )
                try:
                    from brain.orchestrator.nas_syncer import NASSyncer
                    nas_syncer = NASSyncer()
                    if nas_syncer.is_configured and nas_syncer.auto_sync:
                        part_file = dm.resolve_artifact(published_part.artifact)
                        nas_syncer.sync_delivery_part(
                            project_id=project_id,
                            project_dir=project_dir,
                            part_artifact_path=part_file,
                            part_info=published_part.model_dump(),
                        )
                except Exception as nas_exc:
                    logger.warning("NAS sync for delivery %s failed: %s", batch.delivery_id, nas_exc)
                try:
                        notifier.notify_delivery_published(
                        project_id=project_id,
                        project_title=title,
                        part_title=f"Part {batch.ordinal:02d}",
                        chapters=batch.chapter_numbers,
                    )
                except Exception as notif_exc:
                    logger.debug("Notification error on delivery: %s", notif_exc)
            update_publication_state(batch.delivery_id)
            pause_if_requested(batch.delivery_id)

            self._check_schedule(project_id)

        self._check_stop(project_id)
        self.job_queue.update_job(
            project_id,
            {"active_delivery_id": None, "active_delivery_chapters": []},
        )
        # Produce the full export once every batch has been published
        if selection is not None:
            self._run_export(
                project_id,
                project_dir,
                partial=True,
                chapter_selection=set(selection),
            )
        else:
            self._run_export(project_id, project_dir)

    def _run_generation(self, project_id: str, project_dir: Path, chapter_numbers: set[int] | None = None) -> None:
        """Run Stages ④-⑤: TTS generation with quality validation."""
        attribution_audit = self._assert_attribution_audit(
            project_dir,
            enforce=False,
            chapter_numbers=chapter_numbers,
        )
        if not attribution_audit["passed"]:
            attribution_items = [
                item.item_id
                for item in collect_review_gate(
                    project_id, project_dir, self.job_queue
                ).blocking_items
                if item.category == "attribution"
            ]
            if attribution_items:
                raise _WaitingForReview(
                    attribution_items,
                    f"{len(attribution_items)} speaker attribution(s) require review before generation.",
                )
        self._update_stage(project_id, PipelineStage.GENERATING)

        scripts_dir = project_dir / "script"
        script_files = self._script_files(scripts_dir)

        from shared.models import ScriptChapter

        state = self.job_queue.get_job(project_id)
        project_language: str | None = None
        try:
            project_language = str(
                json.loads((project_dir / "book.json").read_text(encoding="utf-8"))
                .get("metadata", {})
                .get("language")
                or ""
            ).strip() or None
        except (OSError, ValueError, TypeError):
            project_language = None
        generated_chapters, mastered_chapters = self._reconcile_artifacts(
            project_id, project_dir, script_files
        )
        selection = (
            set(chapter_numbers)
            if chapter_numbers is not None
            else (
                set(state["active_generation_chapter_selection"])
                if state.get("active_generation_chapter_selection") is not None
                else None
            )
        )
        selected_numbers = sorted(
            selection
            if selection is not None
            else [
                ScriptChapter.model_validate_json(
                    path.read_text(encoding="utf-8")
                ).chapter_number
                for path in script_files
            ]
        )
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
                progress_ticks: dict[str, float] = {}
                # Which `generate_chapter` stream attempt the ticks above were
                # measured on. A retry restarts the chapter, invalidating them.
                stream_state: dict[str, int] = {"attempt": 1}
                request = GenerateChapterRequest(
                    project_id=project_id,
                    chapter_number=chapter_script.chapter_number,
                    lines=request_lines,
                    validate=True,
                    auto_retry=True,
                    max_retries=self.config.get("validation", {}).get("max_retries", 3),
                    validation_terms=self._validation_terms(project_dir),
                    validation_revision=str(state.get("validation_revision") or ""),
                    language=project_language,
                )

                # `chapter_script`, `request_lines` and `progress_ticks` are
                # bound as defaults rather than captured. The callback is
                # consumed synchronously inside this iteration today, so the
                # capture is currently harmless -- but harmless by accident:
                # the moment generation is deferred or made async, a late event
                # would be reported against the *next* chapter's script.
                def _on_generation_progress(
                    msg: dict,
                    *,
                    chapter_script=chapter_script,
                    request_lines=request_lines,
                    progress_ticks=progress_ticks,
                    stream_state=stream_state,
                ) -> None:
                    phase = msg.get("phase", "synthesis")
                    now = time.perf_counter()
                    estimator_key = (
                        f"{project_id}:chapter:{chapter_script.chapter_number}:"
                        f"{phase}"
                    )
                    # A stream retry restarts the chapter from its first
                    # utterance. Carrying the pre-failure samples forward would
                    # blend two different runs into one rate estimate and skew
                    # the ETA for the remainder of the chapter, so drop them and
                    # re-measure.
                    stream_attempt = int(msg.get("stream_attempt", 1) or 1)
                    if stream_state["attempt"] != stream_attempt:
                        logger.info(
                            "Voice stream restarted (attempt %d); resetting "
                            "chapter %d progress estimate",
                            stream_attempt,
                            chapter_script.chapter_number,
                        )
                        stream_state["attempt"] = stream_attempt
                        progress_ticks.clear()

                    previous_tick = progress_ticks.get(phase)
                    if previous_tick is None:
                        self._progress_estimator.reset(estimator_key)
                    else:
                        self._progress_estimator.observe(
                            estimator_key, 1.0, now - previous_tick
                        )
                    progress_ticks[phase] = now
                    completed = int(msg.get("progress", 0) or 0)
                    total = int(msg.get("total", len(request_lines)) or 0)
                    stage_value = (
                        PipelineStage.VALIDATING.value
                        if phase == "validation"
                        else PipelineStage.GENERATING.value
                    )
                    chapter_position = (
                        selected_numbers.index(chapter_script.chapter_number) + 1
                        if chapter_script.chapter_number in selected_numbers
                        else None
                    )
                    ch_title = getattr(chapter_script, "title", None) or f"chapter {chapter_script.chapter_number}"
                    self.job_queue.update_progress(
                        project_id,
                        self._progress_estimator.snapshot(
                            estimator_key,
                            stage=stage_value,
                            phase=phase,
                            message=(
                                f"{phase.title()} {ch_title}: "
                                f"utterance {completed} of {total}"
                            ),
                            completed_units=completed,
                            total_units=total,
                            chapter=chapter_script.chapter_number,
                            chapter_position=chapter_position,
                            chapter_total=len(selected_numbers),
                            line_id=str(msg.get("line_id") or ""),
                            line_position=completed,
                            line_total=total,
                            attempt=int(msg.get("attempt", 1) or 1),
                            cache_hit=bool(msg.get("cache_hit")),
                        ),
                    )
                    progress_update = {
                        "current_work_phase": phase,
                        "total_lines": msg.get("total", len(request_lines)),
                        "current_line_id": msg.get("line_id"),
                        "current_attempt": msg.get("attempt", 1),
                        "current_cache_hit": bool(msg.get("cache_hit")),
                    }
                    if phase == "validation":
                        progress_update["lines_validated"] = msg.get("progress", 0)
                        progress_update["active_stage"] = PipelineStage.VALIDATING.value
                    else:
                        progress_update["lines_generated"] = msg.get("progress", 0)
                        progress_update["active_stage"] = PipelineStage.GENERATING.value
                    self.job_queue.update_job(project_id, progress_update)

                response = self.voice_client.generate_chapter(request, progress_callback=_on_generation_progress)
                logger.info(
                    "[AudioGenerationMetrics] chapter=%d synthesis_cache_hits=%d "
                    "synthesis_cache_misses=%d validation_cache_hits=%d "
                    "validation_cache_misses=%d timings=%s",
                    chapter_script.chapter_number,
                    response.synthesis_cache_hits,
                    response.synthesis_cache_misses,
                    response.validation_cache_hits,
                    response.validation_cache_misses,
                    json.dumps(response.timings_seconds, sort_keys=True),
                )
                self._append_performance_metric(
                    project_dir,
                    {
                        "event": "chapter_generation",
                        "chapter_number": chapter_script.chapter_number,
                        "segments": len(request_lines),
                        "synthesis_cache_hits": response.synthesis_cache_hits,
                        "synthesis_cache_misses": response.synthesis_cache_misses,
                        "validation_cache_hits": response.validation_cache_hits,
                        "validation_cache_misses": response.validation_cache_misses,
                        "retries": response.retried,
                        "accepted_with_warning": response.accepted_with_warning,
                        "failed_validation": response.failed_validation,
                        "audio_duration_seconds": response.total_duration_seconds,
                        "peak_vram_gb": response.peak_vram_gb,
                        "risk_adjusted_line_ids": response.risk_adjusted_line_ids,
                        "timings_seconds": response.timings_seconds,
                        "segment_metrics": response.segment_metrics,
                    },
                )
                external_audio_retry = 0
                candidate_pool: dict[str, list[Any]] = {}
                max_external_audio_retries = max(
                    0,
                    int(
                        self.config.get("external_validation", {}).get(
                            "max_audio_regenerations",
                            1,
                        )
                    ),
                )
                while True:
                    external_review_ids, auto_regenerate_ids = (
                        self._apply_external_audio_validation(
                            project_id=project_id,
                            project_dir=project_dir,
                            request_lines=request_lines,
                            response=response,
                        )
                    )
                    segment_dir = Path(response.segment_files_dir)
                    for candidate_result in response.quality_results:
                        candidate_audio = segment_dir / f"{candidate_result.line_id}.wav"
                        if candidate_result.selected and candidate_audio.is_file():
                            preserved = preserve_candidate(
                                project_dir, candidate_audio, candidate_result, retain=2
                            )
                            if preserved is not None:
                                pool = candidate_pool.setdefault(candidate_result.line_id, [])
                                pool.append(preserved)
                                candidate_pool[candidate_result.line_id] = pool[-2:]
                    if not auto_regenerate_ids or external_audio_retry >= max_external_audio_retries:
                        break
                    for line_id in auto_regenerate_ids:
                        audio_path = segment_dir / f"{line_id}.wav"
                        try:
                            audio_path.unlink(missing_ok=True)
                        except OSError:
                            try:
                                open(audio_path, "wb").close()
                            except OSError:
                                pass
                        try:
                            audio_path.with_suffix(".pt").unlink(missing_ok=True)
                        except OSError:
                            pass
                    external_audio_retry += 1
                    logger.warning(
                        "[ExternalAudioQA] Regenerating chapter %d segments %s (attempt %d/%d)",
                        chapter_script.chapter_number,
                        sorted(auto_regenerate_ids),
                        external_audio_retry,
                        max_external_audio_retries,
                    )
                    response = self.voice_client.generate_chapter(
                        request,
                        progress_callback=_on_generation_progress,
                    )
                # Rank all retained attempts using deterministic hard gates plus
                # Gemini confidence. Restore the strongest artifact before logs
                # and mastering consume it.
                for line_id, candidates in candidate_pool.items():
                    valid_candidates = [
                        c for c in candidates
                        if c is not None and Path(c.audio_path).is_file()
                    ]
                    if not valid_candidates:
                        continue
                    winner = max(valid_candidates, key=lambda candidate: candidate.score)
                    final_audio = Path(response.segment_files_dir) / f"{line_id}.wav"
                    final_audio.parent.mkdir(parents=True, exist_ok=True)
                    current = next(
                        (item for item in response.quality_results if item.line_id == line_id and item.selected),
                        None,
                    )
                    current_candidate = valid_candidates[-1]
                    if current is None or winner.score > current_candidate.score:
                        if Path(winner.audio_path).is_file() and final_audio != Path(winner.audio_path):
                            try:
                                shutil.copy2(winner.audio_path, final_audio)
                            except OSError as copy_exc:
                                logger.warning("Could not restore winning candidate %s: %s", winner.audio_path, copy_exc)
                        for item in response.quality_results:
                            if item.line_id == line_id:
                                item.selected = False
                        winner.result.selected = True
                        response.quality_results.append(winner.result)
                review_by_id = {
                    item["item_id"]: item
                    for item in self.job_queue.get_review_items(project_id, "segment")
                }
                for result in response.quality_results:
                    if not result.selected:
                        continue
                    human_review = review_by_id.get(result.line_id)
                    if result.manual_review_required:
                        self.job_queue.set_review_item(
                            project_id,
                            "segment",
                            result.line_id,
                            "unreviewed",
                            result.manual_review_reason,
                        )
                    elif human_review and human_review["disposition"] in {
                        "regenerate",
                        "unreviewed",
                    }:
                        # A later automatic pass can resolve an earlier review
                        # request (for example after a provider recovers). Do
                        # not leave that stale item blocking the release gate.
                        self.job_queue.delete_review_item(
                            project_id,
                            "segment",
                            result.line_id,
                        )
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
                self._update_long_form_audio_quality(project_id, project_dir)
                quality_summary = (
                    self.job_queue.get_project_quality_summary(project_id)
                )
                expected_ids = {line.line_id for line in request_lines}
                generated_ids = set(response.generated_line_ids)
                accepted_review_ids = {
                    item_id
                    for item_id, item in review_by_id.items()
                    if item.get("disposition") in {"acceptable", "needs_remaster", "approved", "accepted"}
                }
                failed_ids = set(response.failed_line_ids) - accepted_review_ids
                if response.failed_validation > 0:
                    logger.warning(
                        "[AudioGeneration] Chapter %d generated %d/%d lines with %d WER validation warning(s) logged to database",
                        chapter_script.chapter_number,
                        response.generated,
                        len(request_lines),
                        response.failed_validation,
                    )
                active_review_ids = {
                    result.line_id
                    for result in response.quality_results
                    if result.selected and result.manual_review_required
                }
                fatal_failures = failed_ids - active_review_ids
                if (
                    response.generated != len(request_lines)
                    or generated_ids != expected_ids
                    or fatal_failures
                ):
                    raise RuntimeError(
                        f"Chapter {chapter_script.chapter_number} generation incomplete: "
                        f"generated={response.generated}/{len(request_lines)}, "
                        f"missing={sorted(expected_ids - generated_ids)}, "
                        f"fatal_failures={sorted(fatal_failures)}, "
                        f"manual_audio_review={sorted(active_review_ids)}, "
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
                project_quality = self.job_queue.get_project_quality_summary(project_id)
                self.job_queue.update_job(project_id, {
                    "generated_chapters": generated_chapters,
                    "current_chapter": chapter_script.chapter_number,
                    "lines_generated": response.generated,
                    "lines_failed": response.failed_validation,
                    "lines_accepted_with_warning": response.accepted_with_warning,
                    "average_wer": project_quality.get(
                        "average_wer", quality_summary.get("average_wer", 0.0)
                    ),
                    "validation_retries": project_quality.get(
                        "total_retries", quality_summary.get("total_retries", 0)
                    ),
                    "validated_segments": project_quality.get(
                        "total_segments", quality_summary.get("total_segments", 0)
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

    def _run_mastering(self, project_id: str, project_dir: Path, chapter_numbers: set[int] | None = None) -> None:
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
        selection = (
            set(chapter_numbers)
            if chapter_numbers is not None
            else (
                set(state["active_generation_chapter_selection"])
                if state.get("active_generation_chapter_selection") is not None
                else None
            )
        )
        selected_numbers = sorted(
            selection
            if selection is not None
            else [
                ScriptChapter.model_validate_json(
                    path.read_text(encoding="utf-8")
                ).chapter_number
                for path in script_files
            ]
        )
        narrator_voice_id = self._selected_narrator_voice_id(project_dir)

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

            chapter_position = (
                selected_numbers.index(chapter_script.chapter_number) + 1
                if chapter_script.chapter_number in selected_numbers
                else None
            )
            ch_title = getattr(chapter_script, "title", None) or f"chapter {chapter_script.chapter_number}"
            self.job_queue.update_progress(
                project_id,
                self._progress_estimator.snapshot(
                    f"{project_id}:mastering",
                    stage=PipelineStage.MASTERING.value,
                    phase="chapter_mastering",
                    message=(
                        f"Mastering {ch_title} "
                        f"({chapter_position} of {len(selected_numbers)})"
                    ),
                    completed_units=len(mastered_chapters),
                    total_units=len(selected_numbers),
                    chapter=chapter_script.chapter_number,
                    chapter_position=chapter_position,
                    chapter_total=len(selected_numbers),
                ),
            )

            segments = [
                MasterSegmentInfo(
                    line_id=line.line_id,
                    file=f"{project_id}/segments/{line.line_id}.wav",
                    pause_before_ms=line.pause_before_ms,
                    pause_after_ms=line.pause_after_ms,
                    utterance_group_id=line.utterance_group_id,
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
                    narrator_voice_id=narrator_voice_id,
                )

                mastering_started = time.perf_counter()
                response = self.voice_client.master_chapter(request)
                mastering_seconds = time.perf_counter() - mastering_started
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
                        "mastering_quality": {
                            "lufs": response.lufs,
                            "peak_dbfs": response.peak_dbfs,
                            "join_warnings": response.join_warnings,
                            "join_diagnostics": response.join_diagnostics,
                        },
                    },
                )
                if getattr(response, "timeline", None):
                    timeline_file = project_dir / "manifests" / f"chapter_{chapter_script.chapter_number:03d}.timeline.json"
                    atomic_write_json(timeline_file, response.timeline)
                mastered_chapters = sorted(
                    set(mastered_chapters) | {chapter_script.chapter_number}
                )
                self.job_queue.update_job(project_id, {
                    "mastered_chapters": mastered_chapters,
                })
                self._progress_estimator.observe(
                    f"{project_id}:mastering", 1.0, mastering_seconds
                )
                self._append_performance_metric(
                    project_dir,
                    {
                        "event": "chapter_mastering",
                        "chapter_number": chapter_script.chapter_number,
                        "wall_seconds": round(mastering_seconds, 6),
                        "audio_duration_seconds": response.duration_seconds,
                        "lufs": response.lufs,
                        "peak_dbfs": response.peak_dbfs,
                        "join_warnings": response.join_warnings,
                    },
                )

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
        temp_output: Path | None = None,
        acquire_packaging_lock: bool = True,
    ) -> dict[str, Any]:
        """Run Stage ⑦: M4B export."""
        if acquire_packaging_lock:
            with DeliveryManager(project_dir).packaging_lock(wait=True):
                return self._run_export(
                    project_id,
                    project_dir,
                    partial=partial,
                    chapter_selection=chapter_selection,
                    temp_output=temp_output,
                    acquire_packaging_lock=False,
                )
        # A partial package is an explicitly selected deliverable and should
        # only be blocked by attribution issues inside that selection. Full
        # exports continue to enforce the complete book audit.
        self._assert_attribution_audit(
            project_dir,
            chapter_numbers=chapter_selection if partial else None,
        )
        self._update_stage(project_id, PipelineStage.EXPORTING)
        self._progress_estimator.reset(f"{project_id}:export")
        self.job_queue.update_progress(
            project_id,
            self._progress_estimator.snapshot(
                f"{project_id}:export",
                stage=PipelineStage.EXPORTING.value,
                phase="package",
                message="Packaging mastered chapters into M4B",
                completed_units=0,
                total_units=1,
            ),
        )

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

            mastering_quality: dict[str, Any] = {}
            chapter_master_manifest = master_manifest_path(
                project_dir, ch.chapter_number
            )
            if chapter_master_manifest.is_file():
                mastering_quality = json.loads(
                    chapter_master_manifest.read_text(encoding="utf-8")
                ).get("mastering_quality", {})
            chapters.append(ExportChapterInfo(
                number=ch.chapter_number,
                title=ch.chapter_title,
                file=f"chapters/chapter_{ch.chapter_number:03d}.wav",
                lufs=mastering_quality.get("lufs"),
                peak_dbfs=mastering_quality.get("peak_dbfs"),
            ))

        if not chapters:
            raise RuntimeError("No mastered chapters are available for export")

        included_numbers = [chapter.number for chapter in chapters]
        if partial and chapter_selection is not None:
            missing = set(chapter_selection) - set(included_numbers)
            extra = set(included_numbers) - set(chapter_selection)
            if missing or extra:
                raise RuntimeError(
                    "Partial export refused because its mastered chapter set does not "
                    f"match the requested batch (missing={sorted(missing)}, "
                    f"extra={sorted(extra)})"
                )

        cover_art = (
            Path(book.metadata.cover_image_path)
            if book.metadata.cover_image_path
            else project_dir / "cover.jpg"
        )
        cover_path_str = str(cover_art) if cover_art.is_file() else None

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
                genre=book.metadata.genre,
                year=book.metadata.year,
                description=book.metadata.description,
                isbn=book.metadata.isbn,
            ),
            chapters=chapters,
            cover_art=cover_path_str,
            output_name=output_name,
        )

        export_started = time.perf_counter()
        response = self.voice_client.export_m4b(request)
        export_seconds = time.perf_counter() - export_started
        if response.status != "success":
            raise RuntimeError(f"M4B exporter returned status '{response.status}'")

        import shutil
        suffix = (
            f"_chapters_{format_chapter_set(included_numbers)}" if partial else ""
        )
        local_m4b = temp_output if temp_output else project_dir / f"{project_id}{suffix}.m4b"
        local_m4b.parent.mkdir(parents=True, exist_ok=True)

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
        else:
            raise RuntimeError("M4B exporter returned neither a file nor a download URL")

        if not local_m4b.is_file() or local_m4b.stat().st_size == 0:
            raise RuntimeError(f"M4B export is missing or empty: {local_m4b}")

        logger.info(
            "Export complete (%s): %s, %s, %.1f MB",
            "partial" if partial else "full",
            response.total_duration,
            f"{response.total_chapters} chapters",
            response.file_size_mb,
        )
        self._append_performance_metric(
            project_dir,
            {
                "event": "m4b_export",
                "partial": partial,
                "chapters": included_numbers,
                "wall_seconds": round(export_seconds, 6),
                "total_duration": response.total_duration,
                "file_size_mb": response.file_size_mb,
                "book_loudness": response.book_loudness,
            },
        )
        atomic_write_json(
            project_dir / (
                f"export_quality{suffix}.json"
            ),
            {
                "partial": partial,
                "chapters": included_numbers,
                "book_loudness": response.book_loudness,
                "output_file": str(local_m4b),
            },
        )
        self.job_queue.update_job(project_id, {"export_stale": False})
        if not partial:
            try:
                from brain.orchestrator.nas_syncer import NASSyncer
                nas_syncer = NASSyncer()
                if nas_syncer.is_configured and nas_syncer.auto_sync:
                    nas_syncer.sync_full_export(
                        project_id=project_id,
                        project_dir=project_dir,
                        full_m4b_path=local_m4b,
                        prune_parts=True,
                    )
                try:
                    job_info = self.job_queue.get_job(project_id) or {}
                    p_title = job_info.get("title") or project_id
                    notifier.notify_full_book_ready(
                        project_id=project_id,
                        project_title=p_title,
                        total_chapters=len(included_numbers),
                        total_duration_seconds=self._parse_duration_seconds(response.total_duration),
                    )
                except Exception as notif_exc:
                    logger.debug("Notification error on full book export: %s", notif_exc)
            except Exception as nas_exc:
                logger.warning("NAS sync for full export of %s failed: %s", project_id, nas_exc)
        return {
            "output_file": str(local_m4b),
            "chapters": included_numbers,
            "duration_seconds": self._parse_duration_seconds(response.total_duration),
            "file_size_bytes": local_m4b.stat().st_size,
            "book_loudness": response.book_loudness,
        }

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

    @staticmethod
    def _parse_duration_seconds(value: str | float | int | None) -> float:
        """Parse exporter durations such as H:MM:SS into seconds."""
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return max(0.0, float(value))
        text = str(value).strip()
        if not text:
            return 0.0
        try:
            parts = [float(part) for part in text.split(":")]
        except ValueError:
            return 0.0
        if len(parts) == 3:
            return max(0.0, parts[0] * 3600 + parts[1] * 60 + parts[2])
        if len(parts) == 2:
            return max(0.0, parts[0] * 60 + parts[1])
        if len(parts) == 1:
            return max(0.0, parts[0])
        return 0.0

    def _apply_external_audio_validation(
        self,
        *,
        project_id: str,
        project_dir: Path,
        request_lines: list[Any],
        response: Any,
    ) -> tuple[set[str], set[str]]:
        """Apply audio escalation and identify automatic vs human follow-up."""
        if not getattr(self.external_validator, "enabled", True):
            return set(), set()
        line_by_id = {line.line_id: line for line in request_lines}
        review_by_id = {
            item["item_id"]: item
            for item in self.job_queue.get_review_items(project_id, "segment")
        }
        manual_review_ids: set[str] = set()
        auto_regenerate_ids: set[str] = set()
        segment_dir = Path(response.segment_files_dir)
        reference_manager = None
        try:
            from voice.tts_server.voice_library import VoiceLibraryManager
            voice_config = shared_paths.voice_config()
            reference_manager = VoiceLibraryManager(
                Path(
                    voice_config.get("storage", {}).get(
                        "voice_library_dir",
                        "voice_library",
                    )
                )
            )
        except Exception as exc:
            logger.warning("External audio QA could not load voice references: %s", exc)
        for result in response.quality_results:
            if not result.selected:
                continue
            line = line_by_id.get(result.line_id)
            audio_path = segment_dir / f"{result.line_id}.wav"
            human_review = review_by_id.get(result.line_id)
            if human_review and human_review["disposition"] == "acceptable":
                result.manual_review_required = False
                result.manual_review_reason = "Accepted by human review"
                result.validation_confidence = 1.0
                result.external_validation_provider = "human"
            elif line is not None and audio_path.is_file():
                reference_path = None
                if reference_manager is not None:
                    try:
                        reference_path = reference_manager.get_voice_path(
                            project_id,
                            line.voice_id or line.speaker,
                        )
                    except Exception:
                        reference_path = None
                self.external_validator.validate_audio(
                    project_dir=project_dir,
                    audio_path=audio_path,
                    line_text=line.text,
                    result=result,
                    expected_speaker=line.speaker,
                    expected_emotion=line.emotion,
                    reference_audio_path=reference_path,
                )
            high_confidence_reject = (
                result.external_validation_decision == "reject"
                and result.external_validation_confidence is not None
                and result.external_validation_confidence
                >= self.external_validator.auto_accept
            )
            if high_confidence_reject and not (
                human_review and human_review["disposition"] == "acceptable"
            ):
                auto_regenerate_ids.add(result.line_id)
            if result.manual_review_required:
                manual_review_ids.add(result.line_id)
        return manual_review_ids, auto_regenerate_ids

    def _prepare_generation_lines(
        self,
        chapter: Any,
        project_dir: Path,
    ) -> list[Any]:
        """Apply deterministic pronunciation and character-voice inputs."""
        from shared.models import VoiceFXSettings

        lines = [line.model_copy(deep=True) for line in chapter.lines]
        pronunciation_dict, _ = load_pronunciation_dictionary(project_dir)

        characters: dict[str, Any] = {}
        chars_file = project_dir / "characters.json"
        if chars_file.exists():
            characters = json.loads(chars_file.read_text(encoding="utf-8")).get(
                "characters", {}
            )

        speaker_to_voice: dict[str, str] = {}
        cast_file = project_dir / "voice_cast.json"
        if cast_file.exists():
            cast_data = json.loads(cast_file.read_text(encoding="utf-8"))
            for voice_id, profile in cast_data.get("voices", {}).items():
                for assigned_speaker in profile.get("assigned_characters", []):
                    speaker_id = assigned_speaker.get("id") if isinstance(assigned_speaker, dict) else assigned_speaker
                    if speaker_id:
                        speaker_to_voice[speaker_id] = voice_id

        for line in lines:
            spoken_text = apply_pronunciations(line.text, pronunciation_dict)
            line.spoken_text = (
                spoken_text if spoken_text != line.text else None
            )
            char_info = characters.get(line.speaker, {})
            line.voice_id = speaker_to_voice.get(line.speaker) or char_info.get("voice_id") or line.speaker
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
        values, _ = load_pronunciation_dictionary(project_dir)
        terms.update(str(key) for key in values)
        terms.update(str(value) for value in values.values())

        # Casting intentionally excludes non-speaking entities, but those
        # entities still need pronunciation-aware validation. Repeated
        # capitalized tokens in the completed book script provide a generic,
        # book-local glossary for places, peoples, creatures, and other proper
        # names without asking the reader to curate the source book.
        script_path = project_dir / "book_script.json"
        script_dir = project_dir / "script"
        if script_path.exists() or script_dir.is_dir():
            candidate_counts: dict[str, int] = {}
            mid_sentence_candidates: set[str] = set()

            def collect_text(node: Any) -> None:
                if isinstance(node, dict):
                    text = node.get("text")
                    if isinstance(text, str):
                        for match in re.finditer(
                            r"\b[A-Z][A-Za-z'’-]{3,}\b",
                            text,
                        ):
                            candidate = match.group(0)
                            candidate_counts[candidate] = (
                                candidate_counts.get(candidate, 0) + 1
                            )
                            prefix = text[:match.start()].rstrip()
                            while prefix and prefix[-1] in '"\'“”‘’([{':
                                prefix = prefix[:-1].rstrip()
                            if prefix and prefix[-1] not in ".!?":
                                mid_sentence_candidates.add(candidate)
                    for value in node.values():
                        collect_text(value)
                elif isinstance(node, list):
                    for value in node:
                        collect_text(value)

            if script_path.exists():
                try:
                    script_payload = json.loads(script_path.read_text(encoding="utf-8"))
                    collect_text(script_payload.get("chapters", []))
                except Exception:
                    pass

            if script_dir.is_dir():
                for ch_file in script_dir.glob("chapter_*.json"):
                    try:
                        ch_payload = json.loads(ch_file.read_text(encoding="utf-8"))
                        collect_text(ch_payload.get("lines", []))
                    except Exception:
                        pass

            sentence_words = {
                "After", "Again", "Because", "Before", "Could", "Every",
                "Finally", "First", "From", "Here", "However", "Instead",
                "Perhaps", "Something", "That", "Their", "Then", "There",
                "These", "They", "This", "Those", "Though", "Through",
                "Until", "What", "Whatever", "When", "Where", "Whether",
                "Which", "While", "With", "Without", "Would",
            }
            terms.update(
                candidate
                for candidate, count in candidate_counts.items()
                if (
                    count >= 2 or candidate in mid_sentence_candidates
                ) and candidate not in sentence_words
            )
        return sorted(term for term in terms if term.strip())

    @staticmethod
    def _append_performance_metric(
        project_dir: Path,
        payload: dict[str, Any],
    ) -> None:
        """Append one durable, failure-isolated performance measurement."""
        try:
            metric = {
                "schema_version": 2,
                "timestamp": datetime.now(UTC).isoformat(),
                **payload,
            }
            metrics_path = project_dir / "performance_metrics.jsonl"
            with metrics_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(metric, ensure_ascii=False, sort_keys=True)
                    + "\n"
                )
                handle.flush()
        except Exception as exc:
            logger.warning("Could not persist performance metric: %s", exc)

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
        workspace = self.workspace_dir / project_id
        narrator_voice_id = self._selected_narrator_voice_id(project_dir)

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
                self.workspace_dir / item["file"]
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
                                utterance_group_id=line.utterance_group_id,
                            )
                            for line in chapter.lines
                        ],
                        chapter_title=chapter.chapter_title,
                        announce_chapter=True,
                        narrator_voice_id=narrator_voice_id,
                    ),
                )
                and master_info.get("output_hash") == hash_file(master_file)
            ):
                mastered.append(chapter.chapter_number)

        return sorted(generated), sorted(mastered)

    @staticmethod
    def _selected_narrator_voice_id(project_dir: Path) -> str:
        """Resolve the approved voice assigned to the narrator character."""
        cast_path = project_dir / "voice_cast.json"
        if cast_path.is_file():
            try:
                voices = json.loads(
                    cast_path.read_text(encoding="utf-8")
                ).get("voices", {})
                for voice_id, profile in voices.items():
                    if "narrator" in profile.get("assigned_characters", []):
                        return str(voice_id)
            except (OSError, json.JSONDecodeError, AttributeError):
                logger.warning(
                    "Could not resolve narrator assignment from %s",
                    cast_path,
                )
        return "narrator"

    @staticmethod
    def _voice_generation_config(project_id: str | None = None) -> dict[str, Any]:
        """Return Voice settings that can change generated segment bytes."""
        config = shared_paths.voice_config()
        if not config:
            return {}
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
        voice_config: dict[str, Any] = shared_paths.voice_config()
        from voice.tts_server.voice_library import VoiceLibraryManager

        narrator_reference = VoiceLibraryManager(
            Path(
                voice_config.get("storage", {}).get(
                    "voice_library_dir",
                    "voice_library",
                )
            )
        ).get_voice_path(project_id, request.narrator_voice_id)
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

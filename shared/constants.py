"""Shared constants and enums used across Brain and Voice services."""

from enum import StrEnum

# ---------------------------------------------------------------------------
# Pipeline & Orchestration
# ---------------------------------------------------------------------------


class PipelineStage(StrEnum):
    """Execution stages of the audiobook generation pipeline."""

    CREATED = "created"
    EXTRACTING = "extracting"
    ANALYZING = "analyzing"
    SCRIPTING = "scripting"
    VOICE_REVIEW = "voice_review"
    BOOTSTRAPPING = "bootstrapping"
    GENERATING = "generating"
    VALIDATING = "validating"
    MASTERING = "mastering"
    EXPORTING = "exporting"
    SELECTION_COMPLETE = "selection_complete"
    COMPLETE = "complete"
    PAUSED = "paused"
    PAUSING = "pausing"
    PAUSED_SCHEDULED = "paused_scheduled"
    DEPLOY_PAUSED = "deploy_paused"
    WAITING_FOR_REVIEW = "waiting_for_review"
    ERROR = "error"


class ValidationStatus(StrEnum):
    """Result of a quality validation check."""

    PASS = "pass"
    ACCEPTED_WITH_WARNING = "accepted_with_warning"
    FAIL = "fail"
    FLAGGED = "flagged"


class Gender(StrEnum):
    """Character gender options."""

    MALE = "male"
    FEMALE = "female"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Audio defaults
# ---------------------------------------------------------------------------

DEFAULT_SAMPLE_RATE = 24000          # Qwen3-TTS native output rate (Hz)
OUTPUT_SAMPLE_RATE = 44100           # Audiobook standard (Hz)
OUTPUT_BIT_DEPTH = 16                # Audiobook standard

# Loudness & mastering
TARGET_LUFS = -19.0                  # Audiobook standard range: -18 to -23
PEAK_LIMIT_DBFS = -1.0               # True peak limit
NOISE_GATE_THRESHOLD_DB = -50.0      # Noise gate threshold

# Cross-fade
DEFAULT_CROSSFADE_MS = 30            # Between adjacent segments

# ---------------------------------------------------------------------------
# TTS defaults
# ---------------------------------------------------------------------------

VOICE_DESIGN_DURATION_SECONDS = 10   # Reference clip length
MAX_TEXT_LENGTH_CHARS = 500          # Max chars per TTS call
DEFAULT_SPEED = 1.0
MIN_SPEED = 0.7
MAX_SPEED = 1.3

# Test sentences for voice design. Keep these short enough to finish inside the
# default 10-second reference window; truncated references poison Full-ICL
# cloning because the registered transcript no longer matches the audio.
VOICE_DESIGN_TEST_SENTENCES = {
    "male": (
        "The ancient tower stood against the darkening sky as rain "
        "swept across the weathered stone."
    ),
    "female": (
        "She walked through the moonlit garden, listening as fallen "
        "leaves whispered beneath each careful step."
    ),
    "other": (
        "The library was vast and silent, filled with old paper, dust, "
        "and half-forgotten memories."
    ),
}

# ---------------------------------------------------------------------------
# Validation thresholds
# ---------------------------------------------------------------------------

DEFAULT_WER_THRESHOLD = 0.20         # 20% word error rate
MAX_VALIDATION_RETRIES = 3
ARTIFACT_NOISE_THRESHOLD_DB = -50.0
CLIPPING_THRESHOLD_DBFS = -0.5
MIN_SEGMENT_DURATION_SECONDS = 0.3
MAX_SILENCE_SECONDS = 3.0
DURATION_TOLERANCE = 0.3             # ±30%
AVERAGE_WORDS_PER_MINUTE = 150.0     # Audiobook narration baseline

# Quality score weights
QUALITY_WEIGHT_WER = 0.6
QUALITY_WEIGHT_ARTIFACT = 0.3
QUALITY_WEIGHT_DURATION = 0.1
QUALITY_SCORE_PASS_THRESHOLD = 0.7

# Bump these when a change intentionally invalidates previously generated
# metadata or audio. They are included in artifact fingerprints.
SCRIPT_SCHEMA_VERSION = "4"
GENERATION_SCHEMA_VERSION = "3"
VALIDATION_SCHEMA_VERSION = "3"
MASTERING_SCHEMA_VERSION = "2"
VOICE_CAST_SCHEMA_VERSION = "1"

# ---------------------------------------------------------------------------
# Script generation defaults
# ---------------------------------------------------------------------------

MAX_SEGMENT_SENTENCES = 4
MIN_SEGMENT_WORDS = 3
CONTEXT_WINDOW_PARAGRAPHS = 10       # 5 before + 5 after

# Pause defaults (milliseconds)
DEFAULT_NARRATOR_PAUSE_MS = 500
DEFAULT_DIALOGUE_PAUSE_MS = 300
DEFAULT_SCENE_TRANSITION_PAUSE_MS = 1500
DEFAULT_CHAPTER_START_PAUSE_MS = 1000
DEFAULT_CHAPTER_END_PAUSE_MS = 2000
DEFAULT_PARAGRAPH_PAUSE_MS = 600

# Character limits
MAX_UNIQUE_VOICES = 20
MINOR_CHARACTER_LINE_THRESHOLD = 3   # ≤ this many lines → generic voice

# Chunking for long chapters
CHUNK_SIZE_WORDS = 1200
CHUNK_OVERLAP_WORDS = 0

# ---------------------------------------------------------------------------
# Export defaults
# ---------------------------------------------------------------------------

DEFAULT_AUDIO_CODEC = "aac"
DEFAULT_AUDIO_BITRATE = "128k"
DEFAULT_AUDIO_CHANNELS = 1           # Mono (audiobook standard)
CHAPTER_SILENCE_MS = 2000

# ---------------------------------------------------------------------------
# Network / API
# ---------------------------------------------------------------------------

DEFAULT_BRAIN_PORT = 8000
DEFAULT_VOICE_PORT = 8100
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_RETRIES = 3
DEFAULT_RETRY_DELAY_SECONDS = 5
DEFAULT_RECONNECT_INTERVAL_SECONDS = 60

# ---------------------------------------------------------------------------
# Local model identity
# ---------------------------------------------------------------------------

# Single source of truth for the fallback Ollama tag used when
# `brain/config.yaml` is missing or unreadable.
#
# This previously existed as four separate literals across the codebase, two of
# which disagreed (`pipeline.py` defaulted to `qwen3:32b` while
# `shared/pronunciation.py`, `scripts/repair_attributions.py` and
# `brain/validators/tiered_adjudicator.py` used `qwen3.8:27b`). A config-read
# failure therefore silently ran attribution, pronunciation and scripting on
# different models within one book, which contradicts the
# `ollama.fallback_models: []` policy of never substituting a model.
#
# Keep this aligned with `ollama.model` in `brain/config.yaml`.
DEFAULT_OLLAMA_MODEL = "qwen3.8:27b"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11435"


# ---------------------------------------------------------------------------
# Cooperative cancellation
# ---------------------------------------------------------------------------


class GenerationCancelled(BaseException):
    """Raised to unwind a stage when the operator requests a pause.

    Deliberately derived from ``BaseException`` rather than ``Exception``. The
    pipeline contains well over a hundred broad ``except Exception`` handlers,
    many of which legitimately swallow errors; a cancellation must tunnel
    through all of them to the stage runner instead of being absorbed as a
    recoverable failure.

    `OllamaClient` previously reused the built-in ``KeyboardInterrupt`` for
    this, which worked for the same reason but conflated a programmatic pause
    with a real Ctrl-C from the terminal, and made the intent unreadable at the
    catch site. The Voice service already had its own ``GenerationCancelled``;
    this is the shared definition both sides can use.
    """


# ---------------------------------------------------------------------------
# GPU allocator configuration
# ---------------------------------------------------------------------------

# Applied to every process that loads a Torch model on this workstation.
#
# Evidence: `voice_crash.log` records three crashes of the form
#
#     HIP out of memory. Tried to allocate 3.39 GiB. GPU 0 has a total
#     capacity of 23.98 GiB of which 23.33 GiB is free.
#
# raised inside `transformers.modeling_utils.caching_allocator_warmup`. Failing
# a 3.4 GiB allocation with 23 GiB free is not exhaustion -- it is HIP caching
# allocator fragmentation, and the error text names this setting as the remedy.
#
# `expandable_segments` lets a segment grow in place instead of requiring one
# contiguous block per size class. That is exactly the pattern the Voice
# service stresses: it loads, unloads and reloads Qwen3-TTS, the VoiceDesign
# helper and Whisper within a single process at every stage boundary, so the
# allocator's arena is repeatedly carved up and released.
#
# Set with `setdefault` at every site, never assignment: an operator debugging
# an allocator problem must be able to override it from the environment.
TORCH_ALLOC_CONF = "expandable_segments:True"

# ROCm builds read the HIP name; upstream Torch reads the CUDA name and ROCm
# honours it too. Setting both keeps the value effective across a Torch upgrade
# that changes which one wins.
TORCH_ALLOC_ENV_VARS = ("PYTORCH_CUDA_ALLOC_CONF", "PYTORCH_HIP_ALLOC_CONF")


def apply_torch_alloc_conf(env: dict[str, str]) -> dict[str, str]:
    """Set the allocator configuration on ``env`` in place and return it.

    Existing values are preserved so an operator override always wins.
    """
    for name in TORCH_ALLOC_ENV_VARS:
        env.setdefault(name, TORCH_ALLOC_CONF)
    return env

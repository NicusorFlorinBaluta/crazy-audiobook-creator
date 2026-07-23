"""Constants and enumerations shared across the pipeline."""

from enum import StrEnum


# ---------------------------------------------------------------------------
# Pipeline status enum
# ---------------------------------------------------------------------------

class PipelineStage(StrEnum):
    """Stages of the audiobook production pipeline."""

    CREATED = "created"
    EXTRACTING = "extracting"
    SCRIPTING = "scripting"
    BOOTSTRAPPING = "bootstrapping"
    GENERATING = "generating"
    VALIDATING = "validating"
    MASTERING = "mastering"
    EXPORTING = "exporting"
    COMPLETE = "complete"
    ERROR = "error"
    PAUSED = "paused"
    PAUSED_SCHEDULED = "paused_scheduled"
    DEPLOY_PAUSED = "deploy_paused"
    SELECTION_COMPLETE = "selection_complete"


class ValidationStatus(StrEnum):
    """Result of a quality validation check."""

    PASS = "pass"
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

# Test sentences for voice design (phoneme-rich, emotionally neutral)
VOICE_DESIGN_TEST_SENTENCES = {
    "male": (
        "The ancient tower stood against the darkening sky, "
        "its stones weathered by centuries of wind and rain."
    ),
    "female": (
        "She walked through the moonlit garden, "
        "her footsteps barely disturbing the fallen leaves."
    ),
    "other": (
        "The library was vast and silent, "
        "filled with the scent of old paper and forgotten memories."
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
CHUNK_SIZE_WORDS = 600
CHUNK_OVERLAP_WORDS = 150

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

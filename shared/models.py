"""Pydantic data models shared between Brain (Windows) and Voice (Ubuntu).

These models define the data contracts for REST API communication,
pipeline state persistence, and inter-stage data flow.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from shared.constants import (
    DEFAULT_SPEED,
    Gender,
    PipelineStage,
    ValidationStatus,
)


# ===================================================================
# Book & Chapter Models (Stage ①: Text Extraction)
# ===================================================================


class BookMetadata(BaseModel):
    """Metadata extracted from an EPUB file."""

    title: str
    author: str
    language: str = "en"
    total_chapters: int = 0
    total_words: int = 0
    cover_image_path: str | None = None


class ExtractedChapter(BaseModel):
    """A single chapter of clean text extracted from an EPUB."""

    number: int
    title: str
    text: str
    word_count: int = 0


class ExtractedBook(BaseModel):
    """Complete book text after EPUB extraction."""

    metadata: BookMetadata
    chapters: list[ExtractedChapter]


# ===================================================================
# Character & Voice Models (Stage ②: LLM Director, Stage ③: Voices)
# ===================================================================


class VoiceFXSettings(BaseModel):
    """Post-processing controls for TTS generation."""
    pitch_semitones: float = Field(default=0.0, ge=-12.0, le=12.0)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    tone: str = Field(default="neutral", description="neutral | warm | bright")

    def is_identity(self) -> bool:
        """Return True when the settings would not alter the audio."""
        return (
            abs(self.pitch_semitones) < 1e-3
            and abs(self.speed - 1.0) < 1e-3
            and self.tone == "neutral"
        )


class Character(BaseModel):
    """A character identified by the LLM with voice description."""

    id: str = Field(description="Unique identifier, e.g. 'narrator', 'kvothe'")
    name: str = Field(description="Display name, e.g. 'Kvothe (young)'")
    gender: Gender
    age_range: str = Field(description="e.g. '40s', 'late teens'")
    personality_traits: list[str] = Field(default_factory=list)
    voice_description: str = Field(
        description="Natural language voice description for TTS Voice Design"
    )
    speaking_style: str = Field(
        default="",
        description="How the character typically speaks",
    )
    discovered_in_pass2: bool = Field(
        default=False,
        description="True if this character was discovered during script generation, not initial analysis",
    )
    voice_fx: VoiceFXSettings | None = Field(
        default=None,
        description="Voice FX settings (pitch, speed, tone) for this character",
    )


class CharacterRegistry(BaseModel):
    """All characters identified in a book, keyed by character ID."""

    book_title: str
    book_author: str
    genre: str = "fantasy"
    tone: str = ""
    characters: dict[str, Character]


# ===================================================================
# Script Models (Stage ②: LLM Director)
# ===================================================================


class ScriptLine(BaseModel):
    """A single speech segment in the audiobook script."""

    line_id: str = Field(description="Unique ID, e.g. 'ch01_001'")
    speaker: str = Field(description="Character ID from the registry")
    text: str = Field(description="The text to speak")
    emotion: str = Field(
        default="neutral",
        description="Emotional state described in natural language, e.g. 'contemplative, somber'",
    )
    speed: float = Field(
        default=DEFAULT_SPEED,
        ge=0.5,
        le=2.0,
        description="Delivery speed multiplier (0.8=slow, 1.0=normal, 1.2=fast)",
    )
    voice_fx: VoiceFXSettings | None = Field(
        default=None,
        description="Optional Voice FX settings to apply during generation",
    )
    pause_before_ms: int = Field(
        default=0,
        ge=0,
        le=5000,
        description="Silence before this segment (ms)",
    )
    pause_after_ms: int = Field(
        default=500,
        ge=0,
        le=5000,
        description="Silence after this segment (ms)",
    )


class ScriptChapter(BaseModel):
    """A fully annotated chapter script ready for TTS generation."""

    chapter_number: int
    chapter_title: str
    chapter_summary: str = Field(
        default="",
        description="1-2 sentence summary for continuity with next chapter",
    )
    lines: list[ScriptLine]

    @property
    def total_lines(self) -> int:
        return len(self.lines)


class BookScript(BaseModel):
    """Complete audiobook script for an entire book."""

    metadata: BookMetadata
    character_registry: CharacterRegistry
    chapters: list[ScriptChapter]


# ===================================================================
# Voice Library Models (Stage ③: Voice Bootstrapping)
# ===================================================================


class VoiceReference(BaseModel):
    """A saved voice reference clip for a character."""

    character_id: str
    name: str
    file_path: str
    description: str
    gender: Gender
    duration_seconds: float = 0.0
    sample_rate: int = 24000
    generated_at: datetime = Field(default_factory=datetime.utcnow)


class VoiceLibrary(BaseModel):
    """Collection of voice reference clips for a project."""

    project_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    voices: dict[str, VoiceReference] = Field(default_factory=dict)


# ===================================================================
# TTS Generation Models (Stage ④: TTS Generation)
# ===================================================================


class GenerateLineRequest(BaseModel):
    """Request to generate audio for a single script line."""

    project_id: str
    line: ScriptLine
    voice_fx: VoiceFXSettings | None = None


class GenerateLineResponse(BaseModel):
    """Response after generating audio for a single line."""

    status: str = "success"
    line_id: str
    audio_file: str
    duration_seconds: float
    sample_rate: int = 24000


class GenerateChapterRequest(BaseModel):
    """Request to generate audio for an entire chapter."""

    project_id: str
    chapter_number: int
    lines: list[ScriptLine]
    validate: bool = True
    auto_retry: bool = True
    max_retries: int = 3


class GenerateChapterResponse(BaseModel):
    """Response after generating audio for a full chapter."""

    status: str = "success"
    chapter_number: int
    total_lines: int
    generated: int
    failed_validation: int = 0
    retried: int = 0
    total_duration_seconds: float = 0.0
    quality_report: ChapterQualityReport | None = None
    segment_files_dir: str = ""


# ===================================================================
# Voice Bootstrap Models (Stage ③)
# ===================================================================


class BootstrapVoicesRequest(BaseModel):
    """Request to generate voice reference clips for all characters."""

    project_id: str
    characters: dict[str, Character]
    force_regenerate: bool = False


class BootstrapVoiceResult(BaseModel):
    """Result of generating a single voice reference clip."""

    file: str
    duration_seconds: float
    sample_rate: int = 24000


class BootstrapVoicesResponse(BaseModel):
    """Response after generating all voice reference clips."""

    status: str = "success"
    project_id: str
    voices_generated: dict[str, BootstrapVoiceResult]


# ===================================================================
# Quality Validation Models (Stage ⑤)
# ===================================================================


class ValidateRequest(BaseModel):
    """Request to validate a generated audio segment."""

    audio_file: str
    expected_text: str


class QualityResult(BaseModel):
    """Quality validation result for a single audio segment."""

    line_id: str
    status: ValidationStatus
    wer: float = Field(ge=0.0, le=1.0)
    transcribed_text: str = ""
    duration_seconds: float = 0.0
    expected_duration_seconds: float = 0.0
    peak_dbfs: float = 0.0
    noise_floor_db: float = 0.0
    clipping_detected: bool = False
    quality_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Composite quality score",
    )
    attempt: int = 1


class ChapterQualityReport(BaseModel):
    """Aggregated quality metrics for a full chapter."""

    chapter_number: int
    total_segments: int = 0
    passed: int = 0
    failed: int = 0
    flagged: int = 0
    total_retries: int = 0
    average_wer: float = 0.0
    worst_wer: float = 0.0
    average_quality_score: float = 0.0
    flagged_lines: list[str] = Field(default_factory=list)
    artifact_detections: int = 0


class BookQualityReport(BaseModel):
    """Aggregated quality metrics for the entire book."""

    project_id: str
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    total_segments: int = 0
    passed: int = 0
    failed: int = 0
    flagged: int = 0
    total_retries: int = 0
    average_wer: float = 0.0
    median_wer: float = 0.0
    worst_wer: float = 0.0
    average_quality_score: float = 0.0
    total_audio_duration: str = ""
    total_generation_time: str = ""
    by_chapter: list[ChapterQualityReport] = Field(default_factory=list)
    flagged_segments: list[QualityResult] = Field(default_factory=list)


# ===================================================================
# Mastering Models (Stage ⑥)
# ===================================================================


class MasterChapterRequest(BaseModel):
    """Request to master (assemble + normalize) a chapter's audio."""

    project_id: str
    chapter_number: int
    segments: list[MasterSegmentInfo]
    mastering_config: MasteringConfig | None = None


class MasterSegmentInfo(BaseModel):
    """Info about a segment to include in the mastered chapter."""

    line_id: str
    file: str
    pause_before_ms: int = 0
    pause_after_ms: int = 500


class MasteringConfig(BaseModel):
    """Mastering parameters (overrides config.yaml if provided)."""

    target_lufs: float = -19.0
    peak_limit_dbfs: float = -1.0
    crossfade_ms: int = 30
    output_sample_rate: int = 44100


class MasterChapterResponse(BaseModel):
    """Response after mastering a chapter."""

    status: str = "success"
    chapter_number: int
    output_file: str
    duration_seconds: float = 0.0
    lufs: float = 0.0
    peak_dbfs: float = 0.0
    file_size_mb: float = 0.0


# ===================================================================
# M4B Export Models (Stage ⑦)
# ===================================================================


class ExportM4BRequest(BaseModel):
    """Request to export all chapters as a single M4B audiobook."""

    project_id: str
    metadata: AudiobookMetadata
    chapters: list[ExportChapterInfo]
    cover_art: str | None = None
    output_config: ExportConfig | None = None


class AudiobookMetadata(BaseModel):
    """Metadata for the final audiobook file."""

    title: str
    author: str
    narrator: str = "AI Generated"
    genre: str = "Fantasy"
    year: str = ""
    description: str = ""


class ExportChapterInfo(BaseModel):
    """Chapter info for M4B export."""

    number: int
    title: str
    file: str


class ExportConfig(BaseModel):
    """Export encoding settings."""

    codec: str = "aac"
    bitrate: str = "128k"
    channels: int = 1


class ExportM4BResponse(BaseModel):
    """Response after exporting the M4B file."""

    status: str = "success"
    output_file: str
    total_duration: str = ""
    total_chapters: int = 0
    file_size_mb: float = 0.0
    download_url: str = ""


# ===================================================================
# Pipeline / Project Status (Stage ⑧: Dashboard)
# ===================================================================


class ProjectStatus(BaseModel):
    """Current status of an audiobook project pipeline."""

    project_id: str
    title: str = ""
    author: str = ""
    status: PipelineStage = PipelineStage.CREATED
    current_chapter: int | None = None
    total_chapters: int = 0
    current_line: int | None = None
    total_lines: int = 0
    lines_generated: int = 0
    lines_failed: int = 0
    average_wer: float = 0.0
    elapsed_seconds: float = 0.0
    eta_seconds: float | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None


class ProjectSummary(BaseModel):
    """Brief summary of a project for listing."""

    project_id: str
    title: str
    author: str
    status: PipelineStage
    total_chapters: int
    total_words: int
    estimated_audio_hours: float = 0.0
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ===================================================================
# WebSocket Messages
# ===================================================================


class WSProgressMessage(BaseModel):
    """WebSocket message for progress updates."""

    type: Literal["progress"] = "progress"
    project_id: str
    chapter: int
    line_id: str = ""
    progress: int = 0
    total: int = 0
    percent: float = 0.0
    eta_seconds: float | None = None
    current_speaker: str = ""
    current_emotion: str = ""


class WSQualityMessage(BaseModel):
    """WebSocket message for quality validation results."""

    type: Literal["quality"] = "quality"
    line_id: str
    wer: float
    status: ValidationStatus
    retrying: bool = False


class WSChapterCompleteMessage(BaseModel):
    """WebSocket message when a chapter finishes."""

    type: Literal["chapter_complete"] = "chapter_complete"
    chapter: int
    duration_seconds: float = 0.0


class WSPipelineCompleteMessage(BaseModel):
    """WebSocket message when the entire pipeline finishes."""

    type: Literal["pipeline_complete"] = "pipeline_complete"
    total_duration: str = ""
    file_size_mb: float = 0.0


class WSErrorMessage(BaseModel):
    """WebSocket message for errors."""

    type: Literal["error"] = "error"
    message: str
    retrying_in: int | None = None


# ===================================================================
# Health Check
# ===================================================================


class VoiceHealthResponse(BaseModel):
    """Health check response from the Voice (Ubuntu) server."""

    status: str = "ok"
    gpu: str = ""
    vram_total_gb: float = 0.0
    vram_used_gb: float = 0.0
    model_loaded: str = ""
    uptime_seconds: float = 0.0

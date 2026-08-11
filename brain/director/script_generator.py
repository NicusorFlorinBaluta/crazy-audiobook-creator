"""Script Generator — Pass 2 of the LLM Script Director.

Processes each chapter through the LLM with a sliding context window
to produce a line-by-line audiobook script with:
  - Speaker attribution (narrator vs. character ID)
  - Emotion tags based on surrounding context
  - Speed/pacing instructions
  - Pause durations
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from brain.director.ollama_client import OllamaClient
from shared.constants import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS, Gender
from shared.artifacts import (
    assert_script_covers_source,
    atomic_write_json,
    atomic_write_text,
    script_fingerprint,
)
from shared.models import (
    CharacterRegistry,
    ExtractedChapter,
    ScriptChapter,
    ScriptLine,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceFragment:
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class AttributionIssue:
    """One source-grounded problem with dialogue metadata."""

    kind: str
    fragment_index: int
    fragment_id: int
    submitted_speaker: str
    message: str
    exact_speaker: str | None = None


class MetadataAttributionError(ValueError):
    """Structured semantic failure retained as a ValueError for callers."""

    def __init__(self, issues: list[AttributionIssue]):
        if not issues:
            raise ValueError("MetadataAttributionError requires at least one issue")
        self.issues = tuple(issues)
        super().__init__("; ".join(issue.message for issue in issues))

_PROMPT_DIR = Path(__file__).parent / "prompts"

_SYSTEM_PROMPT = """You are a STRICT AUDIOBOOK SCRIPT METADATA ANNOTATOR. Your ONLY job is to assign the correct speaker, emotion, and reading speed to an array of pre-extracted text fragments.

## Context

### Character Registry
{character_registry}

### Previous Chapter Summary (for emotional continuity)
{previous_summary}

## Script Tagging Task

### Audio Direction Guidelines

#### Speaker Attribution Guidelines
- CRITICAL: EVERY fragment that is not marked as dialogue is narration and its speaker MUST be "narrator".
- Dialogue tags (e.g., "he said", "she whispered", "the captain replied", "the child looked at her") are NARRATOR lines -> speaker MUST be "narrator".
- Dialogue may use straight/curly double quotes, typographic single quotes, or an em dash at the start of a dialogue turn.
- ONLY the spoken fragment gets a character speaker ID.
- Identify the dialogue speaker from surrounding context and explicit dialogue
  tags. Do not guess from a name, gender stereotype, nearby named entity, or
  personification. If a named place/object is mentioned near dialogue, assign
  it as speaker only when the text explicitly establishes that it literally
  speaks.
- Resolve ambiguous dialogue from surrounding turns, dialogue tags, aliases,
  and previous-chapter context. Use "narrator" for a quote only when no
  character actually speaks it (for example, a sign or document). Never
  select a character from gender alone or merely because they are nearby.

#### Scene-Level Prosody Plan
First, analyze the text and group it into logical scenes. Generate a constrained scene state for each.
Line controls (emotion, speed) MUST derive from the active scene state with bounded changes. Do not make abrupt jumps in speed or emotion without a new scene or explicit narrative transition.

#### Emotion Mapping & Inflection Taxonomy
Provide a rich, specific emotion directive matching TTS performance capabilities:
- **Whispers/Secrets:** "hushed whisper", "conspiratorial whisper", "soft comfort"
- **Action/Intensity:** "panicked shout", "angry demand", "breathless urgency", "terrified cry"
- **Reflective/Somber:** "somber reflection", "weary sigh", "thoughtful contemplation", "sad nostalgia"
- **Humor/Warmth:** "warm chuckle", "playful banter", "sarcastic retort", "gentle reassurance"
- **Narration:** "neutral", "authoritative", "suspenseful", "reflective narration"

#### Pacing (Speed) & Pauses
- Default narration: 1.0 (pause_after_ms: 500)
- Action / panicked / urgent: 1.15-1.25 (pause_after_ms: 250)
- Whispered / secret / breathless: 0.85-0.90 (pause_after_ms: 600)
- Weary / somber / reflective: 0.80-0.90 (pause_after_ms: 700)

---
## Output Schema

CRITICAL REMINDER: You MUST output ONLY valid JSON matching the Output Schema below. Do NOT output any conversational text, essays, explanations, or markdown fences. Just the raw JSON object starting with {{ and ending with }}.

{{
  "chapter_number": {chapter_number},
  "chapter_title": "{chapter_title}",
  "chapter_summary": "1-2 sentence summary for continuity with next chapter",
  "scenes": [
    {{
      "mood": "overall scene mood (e.g., tense, melancholic)",
      "tension": "tension level (high, building, low)",
      "narrator_pace": 1.0,
      "character_state": "general state of characters in the scene",
      "transition_intent": "how this scene transitions to the next"
    }}
  ],
  "lines": [
    {{
      "id": 0,
      "scene_index": 0,
      "speaker": "character_id",
      "speaker_confidence": 0.95,
      "speaker_evidence": "short dialogue tag or context cue; empty for narration",
      "emotion": "descriptive emotion state",
      "speed": 1.0,
      "pause_before_ms": 0,
      "pause_after_ms": 500
    }}
  ]
}}
"""

_USER_PROMPT = """## Source Text Fragments

{chapter_text_json}

Provide the metadata (speaker, emotion, speed) for EACH fragment ID in the JSON array above. Ensure every single ID is accounted for in your output `lines` array.

CRITICAL: YOU MUST ONLY OUTPUT A SINGLE VALID JSON OBJECT ENCLOSED IN {{}}. DO NOT ADD ANY CONVERSATIONAL TEXT BEFORE OR AFTER THE JSON.
"""


class ScriptGenerator:
    """Pass 2: Generate line-by-line scripts for each chapter."""

    def __init__(
        self,
        ollama: OllamaClient,
        temperature: float = 0.2,
        chunk_size_words: int = CHUNK_SIZE_WORDS,
        chunk_overlap_words: int = CHUNK_OVERLAP_WORDS,
        max_fragments_per_chunk: int = 60,
        group_utterances: bool = True,
        utterance_target_chars: int = 260,
        utterance_max_words: int = 45,
        narrator_target_chars: int = 340,
        narrator_max_words: int = 58,
        expressive_target_chars: int = 180,
        expressive_max_words: int = 30,
        speaker_confidence_threshold: float = 0.55,
    ):
        self.ollama = ollama
        self.temperature = temperature
        self.chunk_size_words = chunk_size_words
        self.chunk_overlap_words = chunk_overlap_words
        self.max_fragments_per_chunk = max(1, max_fragments_per_chunk)
        self.group_utterances = group_utterances
        self.utterance_target_chars = max(80, utterance_target_chars)
        self.utterance_max_words = max(10, utterance_max_words)
        self.narrator_target_chars = max(
            self.utterance_target_chars, narrator_target_chars
        )
        self.narrator_max_words = max(
            self.utterance_max_words, narrator_max_words
        )
        self.expressive_target_chars = max(80, expressive_target_chars)
        self.expressive_max_words = max(10, expressive_max_words)
        self.speaker_confidence_threshold = max(
            0.0, min(1.0, speaker_confidence_threshold)
        )
        self.call_metrics: list[dict[str, Any]] = []

    def chapter_fingerprint(
        self,
        chapter: ExtractedChapter,
        registry: CharacterRegistry,
    ) -> str:
        """Fingerprint every input that can change one script artifact."""
        return script_fingerprint(
            source_text=chapter.text,
            # Only attribution context rendered into _SYSTEM_PROMPT belongs in
            # the script dependency. Voice descriptions, assignments, and FX
            # affect audio manifests—not speaker/emotion metadata.
            registry=self._format_registry(registry),
            model_name=getattr(self.ollama, "model", "unknown"),
            prompt_text=(
                _SYSTEM_PROMPT
                + _USER_PROMPT
                + "\nGROUPING_POLICY=narrator-tag-utterance-groups-v3"
                + "\nATTRIBUTION_REPAIR_POLICY=focused-exact-evidence-v1"
            ),
            chunk_size_words=self.chunk_size_words,
            group_utterances=self.group_utterances,
            utterance_target_chars=self.utterance_target_chars,
            utterance_max_words=self.utterance_max_words,
            narrator_target_chars=self.narrator_target_chars,
            narrator_max_words=self.narrator_max_words,
            expressive_target_chars=self.expressive_target_chars,
            expressive_max_words=self.expressive_max_words,
            speaker_confidence_threshold=self.speaker_confidence_threshold,
        )

    def cached_scripts_are_current(
        self,
        chapters: list[ExtractedChapter],
        registry: CharacterRegistry,
        scripts_dir: Path,
    ) -> bool:
        """Return whether every cached chapter matches current dependencies."""
        for chapter in chapters:
            script_path = scripts_dir / f"chapter_{chapter.number:03d}.json"
            metadata_path = scripts_dir / f"chapter_{chapter.number:03d}.meta.json"
            if not script_path.exists() or not metadata_path.exists():
                return False
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if metadata.get("fingerprint") != self.chapter_fingerprint(
                    chapter,
                    registry,
                ):
                    return False
                script = ScriptChapter.model_validate_json(
                    script_path.read_text(encoding="utf-8")
                )
                assert_script_covers_source(script, chapter.text)
            except Exception:
                return False
        return True

    def generate_chapter_script(
        self,
        chapter: ExtractedChapter,
        registry: CharacterRegistry,
        previous_summary: str = "",
        chunk_progress_callback: Callable[[int, int], None] | None = None,
    ) -> ScriptChapter:
        """Generate a full script for a single chapter.

        For chapters that exceed chunk_size_words, splits into overlapping
        chunks, processes each, and merges the results.

        Args:
            chapter: The chapter text to process.
            registry: Character registry from Pass 1.
            previous_summary: Summary of the previous chapter for continuity.
            chunk_progress_callback: Callback receiving (chunk_number, total_chunks)

        Returns:
            ScriptChapter with all lines annotated.
        """
        logger.info(
            "Generating script for Chapter %d: '%s' (%d words)",
            chapter.number,
            chapter.title,
            chapter.word_count,
        )

        fragments = self._split_into_fragment_spans(chapter.text)
        if not fragments and chapter.text.strip():
            raise ValueError(f"Chapter {chapter.number} could not be fragmented")

        if sum(len(fragment.text.split()) for fragment in fragments) <= self.chunk_size_words:
            script = self._process_fragments(
                fragments,
                chapter.number,
                chapter.title,
                registry,
                previous_summary,
                id_offset=0,
            )
        else:
            script = self._process_chunked(chapter, registry, previous_summary, chunk_progress_callback)

        if self.group_utterances:
            script = self._group_adjacent_utterances(script, chapter.text)
        assert_script_covers_source(script, chapter.text)
        return script

    def generate_all_chapters(
        self,
        chapters: list[ExtractedChapter],
        registry: CharacterRegistry,
        scripts_dir: Path | None = None,
        progress_callback: Callable[[ScriptChapter], None] = None,
        chapter_start_callback: Callable[[int], None] | None = None,
        chunk_progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[ScriptChapter]:
        """Generate scripts for all chapters sequentially with incremental saving."""
        scripts: list[ScriptChapter] = []
        previous_summary = ""
        total_words = sum(ch.word_count for ch in chapters)

        logger.info(
            "[ScriptGenerator] Starting Pass 2: %d chapters | %d total words",
            len(chapters),
            total_words,
        )

        import time as _time
        pipeline_t0 = _time.time()
        self.call_metrics = []

        for i, chapter in enumerate(chapters):
            logger.info(
                "[ScriptGenerator] ---- Chapter %d/%d: '%s' (%d words) ----",
                i + 1,
                len(chapters),
                chapter.title,
                chapter.word_count,
            )

            # Check if chapter is already generated
            script_path = None
            if scripts_dir:
                script_path = scripts_dir / f"chapter_{chapter.number:03d}.json"
                metadata_path = (
                    scripts_dir / f"chapter_{chapter.number:03d}.meta.json"
                )
                expected_fingerprint = self.chapter_fingerprint(
                    chapter,
                    registry,
                )
                if script_path.exists() and metadata_path.exists():
                    try:
                        metadata = json.loads(
                            metadata_path.read_text(encoding="utf-8")
                        )
                        if metadata.get("fingerprint") != expected_fingerprint:
                            raise ValueError("script dependency fingerprint changed")
                        script = ScriptChapter.model_validate_json(script_path.read_text(encoding="utf-8"))
                        assert_script_covers_source(script, chapter.text)
                        logger.info(
                            "[ScriptGenerator] Reusing Chapter %d (fingerprint matches)",
                            chapter.number,
                        )
                        scripts.append(script)
                        previous_summary = script.chapter_summary
                        if progress_callback:
                            progress_callback(script)
                        continue
                    except Exception as e:
                        logger.warning("Failed to load existing script %s, regenerating. Error: %s", script_path, e)

            if chapter_start_callback:
                chapter_start_callback(chapter.number)

            ch_t0 = _time.time()
            script = self.generate_chapter_script(
                chapter, registry, previous_summary, chunk_progress_callback
            )
            ch_elapsed = _time.time() - ch_t0

            scripts.append(script)
            previous_summary = script.chapter_summary

            # Validate attribution before committing a resumable script artifact.
            self._detect_new_characters(script, registry)

            # Save incrementally
            if script_path:
                atomic_write_text(script_path, script.model_dump_json(indent=2))
                atomic_write_json(
                    scripts_dir / f"chapter_{chapter.number:03d}.meta.json",
                    {"fingerprint": expected_fingerprint},
                )
                logger.info("[ScriptGenerator] Incrementally saved %s", script_path.name)

            logger.info(
                "[ScriptGenerator] Chapter %d/%d done in %.1fs | %d lines | summary: %r",
                i + 1,
                len(chapters),
                ch_elapsed,
                len(script.lines),
                (script.chapter_summary or "")[:80],
            )

            if progress_callback:
                progress_callback(script)

        total_elapsed = _time.time() - pipeline_t0
        total_lines = sum(len(s.lines) for s in scripts)
        logger.info(
            "[ScriptGenerator] Pass 2 complete: %d chapters | %d total lines | %.1fs total (avg %.1fs/ch)",
            len(scripts),
            total_lines,
            total_elapsed,
            total_elapsed / len(chapters) if chapters else 0,
        )

        return scripts

    def _process_fragments(
        self,
        fragments: list[SourceFragment],
        chapter_number: int,
        chapter_title: str,
        registry: CharacterRegistry,
        previous_summary: str,
        id_offset: int,
    ) -> ScriptChapter:
        """Annotate a non-overlapping set of immutable source fragments."""
        char_summary = self._format_registry(registry)

        system_prompt = _SYSTEM_PROMPT.format(
            character_registry=char_summary,
            previous_summary=previous_summary or "None",
            chapter_number=chapter_number,
            chapter_title=chapter_title,
            chapter_number_padded=f"{chapter_number:02d}",
        )
        fragment_dicts = [
            {
                "id": i,
                "text": fragment.text,
                "dialogue": self._is_dialogue_fragment(fragment.text),
            }
            for i, fragment in enumerate(fragments)
        ]
        chapter_text_json = json.dumps(fragment_dicts, indent=2)
        
        prompt = _USER_PROMPT.format(chapter_text_json=chapter_text_json)

        prompt_kb = (len(system_prompt) + len(prompt)) / 1024
        if prompt_kb > 80:
            logger.warning(
                "[ScriptGenerator] Chapter %d prompt is very large (%.1f KB) — LLM may struggle",
                chapter_number,
                prompt_kb,
            )

        logger.info(
            "[ScriptGenerator] Ch%d '%s' → LLM | %.1f KB prompt | %d fragments",
            chapter_number,
            chapter_title[:40],
            prompt_kb,
            len(fragments),
        )

        import time as _time
        t0 = _time.time()
        raw = None
        last_error: Exception | None = None
        used_fallback = False
        allowed_speakers = set(registry.characters)
        full_attempts = 0
        structural_failures = 0
        focused_retries = 0
        local_repairs = 0
        fragment_fallbacks = 0
        issue_counts: dict[str, int] = {}
        request_metrics: list[dict[str, Any]] = []
        for attempt in range(1, 4):
            full_attempts = attempt
            try:
                request_prompt = prompt
                if last_error is not None:
                    request_prompt += (
                        "\n\nCORRECTION REQUIRED: Your previous metadata was "
                        f"invalid: {last_error}. For dialogue, use only one of "
                        f"these exact speaker IDs: "
                        f"{', '.join(sorted(allowed_speakers))}. Re-evaluate "
                        "the complete local conversation and provide short, "
                        "source-grounded speaker_evidence. Never choose from "
                        "gender or proximity alone. Use 'narrator' only for "
                        "quoted material that no character actually speaks. "
                        "Do not invent generic speaker labels."
                    )
                request_started = _time.perf_counter()
                request_succeeded = False
                try:
                    candidate = self.ollama.generate_json(
                        request_prompt,
                        temperature=self.temperature if attempt == 1 else 0.1,
                        system=system_prompt,
                    )
                    request_succeeded = True
                finally:
                    request_metrics.append(
                        {
                            "request_kind": "full_chunk",
                            "attempt": attempt,
                            "wall_seconds": round(
                                _time.perf_counter() - request_started,
                                6,
                            ),
                            "success": request_succeeded,
                            "ollama": (
                                dict(
                                    getattr(
                                        self.ollama,
                                        "last_generation_metrics",
                                        {},
                                    )
                                    or {}
                                )
                                if request_succeeded
                                else {}
                            ),
                        }
                    )
                self._validate_metadata_ids(candidate, len(fragments))

                issues = self._collect_metadata_speaker_issues(
                    candidate,
                    fragments,
                    allowed_speakers,
                    registry=registry,
                    id_offset=id_offset,
                    confidence_threshold=self.speaker_confidence_threshold,
                )
                self._record_issue_counts(issue_counts, issues)
                local_repairs += self._apply_deterministic_attribution_repairs(
                    candidate,
                    issues,
                )

                issues = self._collect_metadata_speaker_issues(
                    candidate,
                    fragments,
                    allowed_speakers,
                    registry=registry,
                    id_offset=id_offset,
                    confidence_threshold=self.speaker_confidence_threshold,
                )
                for issue in issues:
                    focused_retries += 1
                    try:
                        replacement = self._retry_fragment_metadata(
                            issue,
                            candidate,
                            fragments,
                            allowed_speakers,
                            registry,
                            char_summary,
                            request_metrics,
                            id_offset=id_offset,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Focused attribution retry failed for chapter %d "
                            "fragment %d: %s",
                            chapter_number,
                            issue.fragment_id,
                            exc,
                        )
                        continue
                    self._replace_metadata_line(
                        candidate,
                        issue.fragment_index,
                        replacement,
                    )

                remaining_issues = self._collect_metadata_speaker_issues(
                    candidate,
                    fragments,
                    allowed_speakers,
                    registry=registry,
                    id_offset=id_offset,
                    confidence_threshold=self.speaker_confidence_threshold,
                )
                if remaining_issues:
                    used_fallback = True
                    for issue in remaining_issues:
                        fallback = self._fallback_fragment_metadata(
                            issue.fragment_index,
                            candidate,
                            fragments,
                            allowed_speakers,
                            registry,
                        )
                        self._replace_metadata_line(
                            candidate,
                            issue.fragment_index,
                            fallback,
                        )
                        fragment_fallbacks += 1
                    logger.warning(
                        "Chapter %d used conservative metadata fallback for %d "
                        "fragment(s) after focused attribution retry",
                        chapter_number,
                        len(remaining_issues),
                    )

                self._validate_metadata_ids(candidate, len(fragments))
                raw = candidate
                break
            except Exception as exc:
                last_error = exc
                structural_failures += 1
                logger.warning(
                    "Full metadata annotation attempt %d failed for chapter %d: %s",
                    attempt,
                    chapter_number,
                    exc,
                )
        if raw is None:
            used_fallback = True
            fragment_fallbacks = len(fragments)
            logger.warning(
                "LLM metadata annotation failed for chapter %d after retries. "
                "Using conservative exact-evidence fallback metadata.",
                chapter_number,
            )
            fallback_lines = []
            for i, fragment in enumerate(fragments):
                fallback_lines.append(
                    self._fallback_fragment_metadata(
                        i,
                        {"lines": []},
                        fragments,
                        allowed_speakers,
                        registry,
                    )
                )
            raw = {
                "chapter_number": chapter_number,
                "chapter_title": chapter_title,
                "chapter_summary": "",
                "lines": fallback_lines,
            }
        elapsed = _time.time() - t0

        result = self._parse_script_chapter(
            raw,
            chapter_number,
            chapter_title,
            fragments,
            id_offset=id_offset,
            allowed_speakers=allowed_speakers,
            registry=registry,
        )
        logger.info(
            "[ScriptGenerator] Ch%d LLM done in %.1fs | %d lines generated",
            chapter_number,
            elapsed,
            len(result.lines),
        )
        self.call_metrics.append(
            {
                "chapter_number": chapter_number,
                "chapter_title": chapter_title,
                "fragment_count": len(fragments),
                "source_words": sum(len(item.text.split()) for item in fragments),
                "prompt_characters": len(system_prompt) + len(prompt),
                "wall_seconds": round(elapsed, 6),
                "attempts": full_attempts,
                "full_attempts": full_attempts,
                "structural_failures": structural_failures,
                "structural_retries": max(0, full_attempts - 1),
                "full_semantic_retries": 0,
                "focused_retries": focused_retries,
                "local_repairs": local_repairs,
                "fragment_fallbacks": fragment_fallbacks,
                "attribution_issue_counts": issue_counts,
                "requests": request_metrics,
                "used_fallback": used_fallback,
                "ollama": dict(
                    getattr(self.ollama, "last_generation_metrics", {}) or {}
                ),
            }
        )
        return result

    @staticmethod
    def _record_issue_counts(
        counts: dict[str, int],
        issues: list[AttributionIssue],
    ) -> None:
        for issue in issues:
            counts[issue.kind] = counts.get(issue.kind, 0) + 1

    @staticmethod
    def _metadata_line_map(raw: dict[str, Any]) -> dict[int, dict[str, Any]]:
        result: dict[int, dict[str, Any]] = {}
        for item in raw.get("lines", []):
            if not isinstance(item, dict) or "id" not in item:
                continue
            try:
                result[int(item["id"])] = item
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _replace_metadata_line(
        raw: dict[str, Any],
        fragment_index: int,
        replacement: dict[str, Any],
    ) -> None:
        for index, item in enumerate(raw.get("lines", [])):
            if not isinstance(item, dict):
                continue
            try:
                item_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if item_id == fragment_index:
                updated = dict(item)
                updated.update(replacement)
                updated["id"] = fragment_index
                raw["lines"][index] = updated
                return
        raise ValueError(f"No metadata row exists for fragment {fragment_index}")

    @staticmethod
    def _apply_deterministic_attribution_repairs(
        raw: dict[str, Any],
        issues: list[AttributionIssue],
    ) -> int:
        repairs = 0
        repaired_fragments: set[int] = set()
        for issue in issues:
            if issue.exact_speaker is None:
                continue
            if issue.fragment_index in repaired_fragments:
                continue
            ScriptGenerator._replace_metadata_line(
                raw,
                issue.fragment_index,
                {
                    "speaker": issue.exact_speaker,
                    "speaker_confidence": 0.99,
                    "speaker_evidence": (
                        "Deterministic correction from the attached source "
                        f"dialogue tag ({issue.kind})."
                    ),
                },
            )
            repaired_fragments.add(issue.fragment_index)
            repairs += 1
        return repairs

    def _retry_fragment_metadata(
        self,
        issue: AttributionIssue,
        raw: dict[str, Any],
        fragments: list[SourceFragment],
        allowed_speakers: set[str],
        registry: CharacterRegistry,
        character_summary: str,
        request_metrics: list[dict[str, Any]],
        *,
        id_offset: int,
    ) -> dict[str, Any]:
        """Request one bounded semantic correction instead of a whole chunk."""
        start = max(0, issue.fragment_index - 2)
        end = min(len(fragments), issue.fragment_index + 3)
        context = [
            {
                "id": index,
                "text": fragments[index].text,
                "dialogue": self._is_dialogue_fragment(fragments[index].text),
            }
            for index in range(start, end)
        ]
        current = self._metadata_line_map(raw).get(issue.fragment_index, {})
        focused_prompt = (
            "Correct the speaker metadata for exactly one audiobook source "
            "fragment. Return JSON with a `lines` array containing exactly one "
            f"row whose id is {issue.fragment_index}. Preserve that id.\n\n"
            f"Allowed speaker IDs: {', '.join(sorted(allowed_speakers))}\n"
            f"Rejected metadata reason: {issue.message}\n"
            "Use explicit dialogue tags, aliases, and the bounded conversation "
            "context. Never select from gender or proximity alone. Include "
            "only id, speaker, speaker_confidence, and speaker_evidence. The "
            "existing delivery metadata will be preserved.\n\n"
            f"Context fragments:\n{json.dumps(context, indent=2)}\n\n"
            f"Current metadata:\n{json.dumps(current, indent=2)}"
        )
        focused_system = (
            "You are a strict audiobook speaker-attribution corrector. Output "
            "only JSON. Do not rewrite source text or invent speakers.\n\n"
            f"Character registry:\n{character_summary}"
        )
        request_started = time.perf_counter()
        request_succeeded = False
        try:
            response = self.ollama.generate_json(
                focused_prompt,
                temperature=0.1,
                system=focused_system,
            )
            request_succeeded = True
        finally:
            request_metrics.append(
                {
                    "request_kind": "focused_fragment",
                    "fragment_id": issue.fragment_id,
                    "wall_seconds": round(
                        time.perf_counter() - request_started,
                        6,
                    ),
                    "success": request_succeeded,
                    "ollama": (
                        dict(
                            getattr(
                                self.ollama,
                                "last_generation_metrics",
                                {},
                            )
                            or {}
                        )
                        if request_succeeded
                        else {}
                    ),
                }
            )
        response_lines = response.get("lines") if isinstance(response, dict) else None
        if not isinstance(response_lines, list) or len(response_lines) != 1:
            raise ValueError("Focused attribution response must contain exactly one line")
        replacement = response_lines[0]
        if not isinstance(replacement, dict):
            raise ValueError("Focused attribution response line is invalid")
        try:
            replacement_id = int(replacement.get("id"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Focused attribution response has an invalid id") from exc
        if replacement_id != issue.fragment_index:
            raise ValueError(
                "Focused attribution response changed fragment id "
                f"{issue.fragment_index} to {replacement_id}"
            )
        replacement = {
            key: replacement[key]
            for key in (
                "id",
                "speaker",
                "speaker_confidence",
                "speaker_evidence",
            )
            if key in replacement
        }

        trial = dict(raw)
        trial["lines"] = [
            dict(item) if isinstance(item, dict) else item
            for item in raw.get("lines", [])
        ]
        self._replace_metadata_line(trial, issue.fragment_index, replacement)
        remaining = self._collect_metadata_speaker_issues(
            trial,
            fragments,
            allowed_speakers,
            registry=registry,
            id_offset=id_offset,
            confidence_threshold=self.speaker_confidence_threshold,
        )
        if any(item.fragment_index == issue.fragment_index for item in remaining):
            raise MetadataAttributionError(
                [
                    item
                    for item in remaining
                    if item.fragment_index == issue.fragment_index
                ]
            )
        return dict(replacement)

    @staticmethod
    def _fallback_fragment_metadata(
        fragment_index: int,
        raw: dict[str, Any],
        fragments: list[SourceFragment],
        allowed_speakers: set[str],
        registry: CharacterRegistry,
    ) -> dict[str, Any]:
        """Build conservative metadata for one unresolved source fragment."""
        fragment = fragments[fragment_index]
        speaker = "narrator"
        evidence = "Conservative narration fallback."
        confidence = 0.0
        if ScriptGenerator._is_dialogue_fragment(fragment.text):
            next_text = (
                fragments[fragment_index + 1].text
                if fragment_index + 1 < len(fragments)
                else ""
            )
            exact_speaker, evidence_kind, _ = ScriptGenerator._dialogue_tag_evidence(
                next_text,
                registry,
            )
            if exact_speaker in allowed_speakers:
                speaker = exact_speaker
                confidence = 0.99
                evidence = f"Exact attached dialogue-tag fallback ({evidence_kind})."
            else:
                speaker = ScriptGenerator._resolve_dialogue_speaker(
                    fragment_index,
                    fragments,
                    ScriptGenerator._metadata_line_map(raw),
                    allowed_speakers,
                )
                confidence = 0.50 if speaker != "narrator" else 0.0
                evidence = "Conservative unresolved-dialogue fallback."

        existing = ScriptGenerator._metadata_line_map(raw).get(fragment_index, {})
        return {
            "id": fragment_index,
            "speaker": speaker,
            "speaker_confidence": confidence,
            "speaker_evidence": evidence,
            "emotion": existing.get("emotion", "neutral"),
            "speed": existing.get("speed", 1.0),
            "pause_before_ms": existing.get("pause_before_ms", 0),
            "pause_after_ms": existing.get(
                "pause_after_ms",
                400 if speaker != "narrator" else 380,
            ),
        }

    def _process_chunked(
        self,
        chapter: ExtractedChapter,
        registry: CharacterRegistry,
        previous_summary: str,
        chunk_progress_callback: Callable[[int, int], None] | None = None,
    ) -> ScriptChapter:
        """Process complete source fragments in non-overlapping batches."""
        fragments = self._split_into_fragment_spans(chapter.text)
        all_lines: list[ScriptLine] = []
        all_scenes = []
        summaries: list[str] = []
        chunks = self._chunk_fragments(fragments)

        offset = 0
        for chunk_num, chunk in enumerate(chunks, 1):
            if chunk_progress_callback:
                chunk_progress_callback(chunk_num, len(chunks))
            context_summary = " ".join(summaries)[-2000:]

            logger.info(
                "Processing fragment chunk %d/%d (%d fragments)",
                chunk_num,
                len(chunks),
                len(chunk),
            )

            chunk_script = self._process_fragments(
                chunk,
                chapter.number,
                chapter.title,
                registry,
                previous_summary
                if chunk_num == 1
                else f"{previous_summary}\nCurrent chapter so far: {context_summary}",
                id_offset=offset,
            )
            all_lines.extend(chunk_script.lines)
            if hasattr(chunk_script, 'scenes'): all_scenes.extend(chunk_script.scenes)
            if chunk_script.chapter_summary:
                summaries.append(chunk_script.chapter_summary)
            offset += len(chunk)

        return ScriptChapter(
            chapter_number=chapter.number,
            chapter_title=chapter.title,
            chapter_summary=" ".join(summaries)[-2000:],
            scenes=all_scenes,
            lines=all_lines,
        )

    def _chunk_fragments(
        self,
        fragments: list[SourceFragment],
    ) -> list[list[SourceFragment]]:
        """Bound both source words and JSON metadata rows per LLM response."""
        chunks: list[list[SourceFragment]] = []
        current: list[SourceFragment] = []
        current_words = 0
        for fragment in fragments:
            fragment_words = max(1, len(fragment.text.split()))
            if current and (
                current_words + fragment_words > self.chunk_size_words
                or len(current) >= self.max_fragments_per_chunk
            ):
                chunks.append(current)
                current = []
                current_words = 0
            current.append(fragment)
            current_words += fragment_words
        if current:
            chunks.append(current)
        return chunks

    def _detect_new_characters(
        self,
        script: ScriptChapter,
        registry: CharacterRegistry,
    ) -> None:
        """Resolve exact aliases and reject invented Pass 2 speakers."""
        known_ids = set(registry.characters.keys())
        for line in script.lines:
            spk = line.speaker.lower().replace(" ", "_").strip("_")
            if not spk or spk == "narrator":
                continue
            
            # Check canonical speaker resolution (aliases, display names, name variants)
            canonical = spk
            if spk not in known_ids:
                for cid, char in registry.characters.items():
                    aliases = getattr(char, "aliases", [])
                    alias_norms = [a.lower().replace(" ", "_") for a in aliases]
                    char_name_norm = char.name.lower().replace(" ", "_")
                    if spk in alias_norms or spk == char_name_norm:
                        canonical = cid
                        break
            
            if canonical != spk:
                line.speaker = canonical
                continue

            if spk not in known_ids:
                raise ValueError(
                    f"Chapter {script.chapter_number} contains unknown speaker "
                    f"'{spk}'; Pass 2 may not create cast members"
                )

    @staticmethod
    def _format_registry(registry: CharacterRegistry) -> str:
        """Format character registry as a readable string for the LLM prompt."""
        lines: list[str] = []
        for char_id, char in registry.characters.items():
            aliases = ", ".join(char.aliases) if char.aliases else "none"
            lines.append(
                f"- **{char.name}** (id: `{char_id}`, {char.gender}, {char.age_range}): "
                f"aliases={aliases}; {char.speaking_style}"
            )
        return "\n".join(lines)

    @staticmethod
    def _split_into_fragments(text: str) -> list[str]:
        """Compatibility wrapper returning immutable fragment text."""
        return [
            fragment.text
            for fragment in ScriptGenerator._split_into_fragment_spans(text)
        ]

    @staticmethod
    def _split_into_fragment_spans(text: str) -> list[SourceFragment]:
        """Split source without rewriting it and retain exact character spans."""
        quote_pattern = re.compile(
            r'"(?:[^"\n]|\\")*?"|“[^”\n]*?”|‘[^’\n]*?’|'
            r"(?<!\w)'[^'\n]+?'(?!\w)"
        )
        fragments: list[SourceFragment] = []

        def append_trimmed(start: int, end: int) -> None:
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
            if end > start:
                fragments.append(SourceFragment(text[start:end], start, end))

        def append_narrative(start: int, end: int) -> None:
            if start >= end:
                return
            segment = text[start:end]
            cursor = 0
            sentence_pattern = re.compile(
                r".+?(?:[.!?…]+(?:[\"”’])?(?=\s|$)|$)",
                re.DOTALL,
            )
            for match in sentence_pattern.finditer(segment):
                append_trimmed(start + match.start(), start + match.end())
                cursor = match.end()
            if cursor < len(segment):
                append_trimmed(start + cursor, end)

        for line_match in re.finditer(r"[^\n]+", text):
            line_start, line_end = line_match.span()
            cursor = line_start
            for quote_match in quote_pattern.finditer(text, line_start, line_end):
                append_narrative(cursor, quote_match.start())
                append_trimmed(quote_match.start(), quote_match.end())
                cursor = quote_match.end()
            append_narrative(cursor, line_end)

        if not fragments and text.strip():
            start = len(text) - len(text.lstrip())
            end = len(text.rstrip())
            fragments.append(SourceFragment(text[start:end], start, end))

        from shared.artifacts import normalize_for_coverage

        if normalize_for_coverage("".join(f.text for f in fragments)) != (
            normalize_for_coverage(text)
        ):
            raise ValueError("Fragmentation did not cover source text exactly once")
        return fragments

    @staticmethod
    def _is_dialogue_fragment(text: str) -> bool:
        value = text.strip()
        if value.startswith(("—", "–")):
            return True
        pairs = (('"', '"'), ("“", "”"), ("‘", "’"), ("'", "'"))
        return any(
            value.startswith(opening) and value.endswith(closing)
            for opening, closing in pairs
        )

    @staticmethod
    def _validate_metadata_ids(raw: dict[str, Any], expected_count: int) -> None:
        raw_lines = raw.get("lines")
        if not isinstance(raw_lines, list):
            raise ValueError("LLM response has no lines array")
        ids: list[int] = []
        for line in raw_lines:
            if not isinstance(line, dict) or "id" not in line:
                raise ValueError("LLM response contains an invalid metadata item")
            ids.append(int(line["id"]))
        expected = list(range(expected_count))
        if sorted(ids) != expected or len(ids) != len(set(ids)):
            raise ValueError(
                "Fragment metadata IDs are incomplete or duplicated; "
                f"expected={expected}, received={sorted(ids)}"
            )

    @staticmethod
    def _normalize_speaker_id(value: object) -> str:
        return (
            re.sub(r"[^\w]+", "_", str(value or "narrator").lower()).strip("_")
            or "narrator"
        )


    @staticmethod
    def _validate_metadata_speakers(
        raw: dict[str, Any],
        fragments: list[SourceFragment],
        allowed_speakers: set[str],
        *,
        registry: CharacterRegistry | None = None,
        id_offset: int = 0,
        confidence_threshold: float = 0.55,
    ) -> None:
        """Reject invalid dialogue metadata with structured source evidence."""
        issues = ScriptGenerator._collect_metadata_speaker_issues(
            raw,
            fragments,
            allowed_speakers,
            registry=registry,
            id_offset=id_offset,
            confidence_threshold=confidence_threshold,
        )
        if issues:
            raise MetadataAttributionError(issues)

    @staticmethod
    def _collect_metadata_speaker_issues(
        raw: dict[str, Any],
        fragments: list[SourceFragment],
        allowed_speakers: set[str],
        *,
        registry: CharacterRegistry | None = None,
        id_offset: int = 0,
        confidence_threshold: float = 0.55,
    ) -> list[AttributionIssue]:
        """Return at most one highest-priority attribution issue per dialogue."""
        metadata_map = {
            int(item["id"]): item
            for item in raw.get("lines", [])
            if isinstance(item, dict) and "id" in item
        }
        issues: list[AttributionIssue] = []
        for i, fragment in enumerate(fragments):
            if not ScriptGenerator._is_dialogue_fragment(fragment.text):
                continue
            speaker = ScriptGenerator._normalize_speaker_id(
                metadata_map.get(i, {}).get("speaker", "narrator")
            )
            fragment_id = id_offset + i
            next_text = fragments[i + 1].text if i + 1 < len(fragments) else ""
            exact_speaker: str | None = None
            evidence_kind: str | None = None
            evidence_gender: Gender | None = None
            if ScriptGenerator._is_pure_dialogue_tag(next_text):
                exact_speaker, evidence_kind, evidence_gender = (
                    ScriptGenerator._dialogue_tag_evidence(next_text, registry)
                )

            if speaker not in allowed_speakers:
                issues.append(
                    AttributionIssue(
                        kind="unknown_speaker",
                        fragment_index=i,
                        fragment_id=fragment_id,
                        submitted_speaker=speaker,
                        exact_speaker=exact_speaker,
                        message=(
                            f"Fragment {fragment_id} uses unknown speaker "
                            f"'{speaker}'"
                        ),
                    )
                )
                continue

            if ScriptGenerator._is_pure_dialogue_tag(next_text):
                if exact_speaker is not None and speaker != exact_speaker:
                    label = (
                        "names"
                        if evidence_kind == "named_tag"
                        else "identifies"
                    )
                    issues.append(
                        AttributionIssue(
                            kind=evidence_kind or "exact_tag_contradiction",
                            fragment_index=i,
                            fragment_id=fragment_id,
                            submitted_speaker=speaker,
                            exact_speaker=exact_speaker,
                            message=(
                                f"Fragment {fragment_id} assigns '{speaker}', but "
                                f"its attached dialogue tag {label} "
                                f"'{exact_speaker}'"
                            ),
                        )
                    )
                    continue
                if speaker == "narrator":
                    issues.append(
                        AttributionIssue(
                            kind="narrator_dialogue_tag",
                            fragment_index=i,
                            fragment_id=fragment_id,
                            submitted_speaker=speaker,
                            exact_speaker=exact_speaker,
                            message=(
                                f"Fragment {fragment_id} is spoken dialogue followed "
                                "by a dialogue tag but is assigned to narrator"
                            ),
                        )
                    )
                    continue
                if evidence_gender is not None and registry is not None:
                    character = registry.characters.get(speaker)
                    if (
                        character is not None
                        and character.gender in (Gender.MALE, Gender.FEMALE)
                        and character.gender != evidence_gender
                    ):
                        gender_source = (
                            "pronouns"
                            if evidence_kind == "pronoun_gender"
                            else "speaker description"
                        )
                        issues.append(
                            AttributionIssue(
                                kind="gender_contradiction",
                                fragment_index=i,
                                fragment_id=fragment_id,
                                submitted_speaker=speaker,
                                message=(
                                    f"Fragment {fragment_id} assigns '{speaker}', but "
                                    f"its attached dialogue tag identifies a "
                                    f"{evidence_gender.value} speaker through "
                                    f"{gender_source}"
                                ),
                            )
                        )
                        continue

            confidence = metadata_map.get(i, {}).get("speaker_confidence")
            if confidence is None:
                issues.append(
                    AttributionIssue(
                        kind="missing_confidence",
                        fragment_index=i,
                        fragment_id=fragment_id,
                        submitted_speaker=speaker,
                        exact_speaker=(
                            exact_speaker if exact_speaker == speaker else None
                        ),
                        message=f"Fragment {fragment_id} has no speaker confidence",
                    )
                )
                continue
            if confidence is not None:
                try:
                    parsed_confidence = float(confidence)
                except (TypeError, ValueError):
                    issues.append(
                        AttributionIssue(
                            kind="invalid_confidence",
                            fragment_index=i,
                            fragment_id=fragment_id,
                            submitted_speaker=speaker,
                            exact_speaker=(
                                exact_speaker if exact_speaker == speaker else None
                            ),
                            message=(
                                f"Fragment {fragment_id} has invalid speaker "
                                "confidence"
                            ),
                        )
                    )
                    continue
                if parsed_confidence < confidence_threshold:
                    issues.append(
                        AttributionIssue(
                            kind="low_confidence",
                            fragment_index=i,
                            fragment_id=fragment_id,
                            submitted_speaker=speaker,
                            exact_speaker=(
                                exact_speaker if exact_speaker == speaker else None
                            ),
                            message=(
                                f"Fragment {fragment_id} assigns '{speaker}' with "
                                f"low confidence ({parsed_confidence:.2f}); "
                                "re-evaluate the dialogue using source evidence"
                            ),
                        )
                    )
        return issues

    @staticmethod
    def _dialogue_tag_evidence(
        tag_text: str,
        registry: CharacterRegistry | None,
    ) -> tuple[str | None, str | None, Gender | None]:
        """Resolve only explicit, unique speaker evidence from a dialogue tag."""
        if registry is None:
            return None, None, None

        tag = tag_text.casefold()
        speech_verbs = (
            r"said|asked|replied|whispered|shouted|murmured|exclaimed|"
            r"continued|agreed|added|called|demanded|warned|answered|cried"
        )
        named_matches: list[tuple[int, str]] = []
        for character_id, candidate in registry.characters.items():
            if character_id == "narrator":
                continue
            names = {
                value.strip().casefold().replace("_", " ")
                for value in [character_id, candidate.name, *candidate.aliases]
                if value.strip()
            }
            for name in names:
                if re.search(
                    r"\b" + re.escape(name) + r"\b(?:\s+\w+){0,2}\s+(?:"
                    + speech_verbs
                    + r")\b",
                    tag,
                ):
                    named_matches.append((len(name), character_id))

        if named_matches:
            longest = max(length for length, _ in named_matches)
            named_speakers = {
                character_id
                for length, character_id in named_matches
                if length == longest
            }
            if len(named_speakers) == 1:
                return next(iter(named_speakers)), "named_tag", None

        generic_match = re.search(
            r"\b(?:the|a)\s+(boy|girl|man|woman)\b(?:\s+\w+){0,2}\s+(?:"
            + speech_verbs
            + r")\b",
            tag,
        )
        if generic_match:
            noun = generic_match.group(1)
            role_specs = {
                "boy": ("child_male", Gender.MALE),
                "girl": ("child_female", Gender.FEMALE),
                "man": ("minor_male", Gender.MALE),
                "woman": ("minor_female", Gender.FEMALE),
            }
            preferred_id, gender = role_specs[noun]
            if preferred_id in registry.characters:
                return preferred_id, "generic_role_tag", gender
            matching_roles = []
            for character_id, character in registry.characters.items():
                role_names = {
                    character_id.casefold().replace("_", " "),
                    character.name.casefold(),
                    *(alias.casefold() for alias in character.aliases),
                }
                if noun in role_names and character.gender == gender:
                    matching_roles.append(character_id)
            if len(matching_roles) == 1:
                return matching_roles[0], "generic_role_tag", gender
            return None, "generic_gender", gender

        if re.search(r"\bshe\b", tag):
            return None, "pronoun_gender", Gender.FEMALE
        if re.search(r"\bhe\b", tag):
            return None, "pronoun_gender", Gender.MALE
        return None, None, None

    @staticmethod
    def _validate_dialogue_tag_attribution(
        speaker: str,
        tag_text: str,
        registry: CharacterRegistry | None,
        fragment_id: int,
    ) -> None:
        """Reject tag evidence that deterministically contradicts a speaker."""
        if registry is None:
            return
        character = registry.characters.get(speaker)
        if character is None:
            return
        exact_speaker, evidence_kind, evidence_gender = (
            ScriptGenerator._dialogue_tag_evidence(tag_text, registry)
        )
        if exact_speaker is not None and speaker != exact_speaker:
            label = "names" if evidence_kind == "named_tag" else "identifies"
            raise ValueError(
                f"Fragment {fragment_id} assigns '{speaker}', but its attached "
                f"dialogue tag {label} '{exact_speaker}'"
            )
        if (
            evidence_gender is not None
            and character.gender in (Gender.MALE, Gender.FEMALE)
            and character.gender != evidence_gender
        ):
            gender_evidence = (
                "pronouns" if evidence_kind == "pronoun_gender" else "speaker description"
            )
            raise ValueError(
                f"Fragment {fragment_id} assigns '{speaker}', but its attached "
                f"dialogue tag identifies a {evidence_gender.value} speaker "
                f"through {gender_evidence}"
            )

    @staticmethod
    def _is_pure_dialogue_tag(text: str) -> bool:
        """Check if narrative text is a short dialogue tag attached to speech."""
        val = text.strip()
        words = val.split()
        if len(words) > 12:
            return False
        verbs = (
            "said", "asked", "replied", "whispered", "shouted", "murmured",
            "exclaimed", "continued", "agreed", "smiled", "nodded", "added",
            "called", "demanded", "warned", "answered", "thought", "cried", "gasp"
        )
        return any(re.search(r"\b" + v, val, re.IGNORECASE) for v in verbs)

    @staticmethod
    def _resolve_dialogue_speaker(
        frag_idx: int,
        fragments: list[SourceFragment],
        metadata_map: dict[int, dict],
        allowed_speakers: set[str],
    ) -> str:
        """Infer character speaker for a dialogue fragment if LLM assigned narrator."""
        next_text = fragments[frag_idx + 1].text if frag_idx + 1 < len(fragments) else ""
        prev_text = fragments[frag_idx - 1].text if frag_idx > 0 else ""
        combined = (next_text + " " + prev_text).lower()

        # 1. Exact named character match in adjacent text
        for spk in allowed_speakers:
            if spk != "narrator" and re.search(r"\b" + re.escape(spk) + r"\b", combined):
                return spk

        # Pronouns, gender and turn proximity are not identity evidence. The
        # conservative automatic fallback is narrator, not an arbitrary cast
        # member selected from an unordered set.
        return "narrator"

    @staticmethod
    def _parse_script_chapter(
        raw: dict,
        fallback_number: int,
        fallback_title: str,
        fragments: list[SourceFragment] | None = None,
        *,
        id_offset: int = 0,
        allowed_speakers: set[str] | None = None,
        registry: CharacterRegistry | None = None,
    ) -> ScriptChapter:
        """Parse LLM JSON metadata output into a ScriptChapter using static fragments."""
        raw_lines = raw.get("lines", [])
        lines: list[ScriptLine] = []
        
        fragments = fragments or []
        metadata_map = {}
        
        for raw_line in raw_lines:
            if not isinstance(raw_line, dict):
                continue
            line_id_val = raw_line.get("id")
            if line_id_val is not None:
                try:
                    metadata_map[int(line_id_val)] = raw_line
                except (ValueError, TypeError):
                    pass

        allowed_speakers = allowed_speakers if allowed_speakers is not None else {"narrator"}
        for i, fragment in enumerate(fragments):
            meta = metadata_map.get(i, {})
            
            try:
                scene_idx = int(meta.get("scene_index", 0))
            except (ValueError, TypeError):
                scene_idx = 0
            
            scenes = raw.get("scenes", [])
            base_pace = 1.0
            if scenes and 0 <= scene_idx < len(scenes):
                try:
                    scene_pace = scenes[scene_idx].get("narrator_pace")
                    if scene_pace is not None:
                        base_pace = float(scene_pace)
                except (ValueError, TypeError):
                    pass
            
            is_dialogue = ScriptGenerator._is_dialogue_fragment(fragment.text)
            
            # Apply bounds to speed based on the scene pace
            try:
                raw_speed = float(meta.get("speed", base_pace))
            except (ValueError, TypeError):
                raw_speed = base_pace
                
            # Allow a tighter bound for narrator, looser for expressive dialogue
            bound_offset = 0.25 if is_dialogue else 0.15
            speed = max(base_pace - bound_offset, min(base_pace + bound_offset, raw_speed))
            speed = max(0.5, min(2.0, speed))  # Absolute bounds
            speaker = ScriptGenerator._normalize_speaker_id(
                meta.get("speaker", "narrator")
            )
            if not is_dialogue:
                speaker = "narrator"
            else:
                # Check canonical alias resolution against allowed_speakers
                if speaker not in allowed_speakers and registry:
                    for cid, char in registry.characters.items():
                        aliases = getattr(char, "aliases", [])
                        alias_norms = [a.lower().replace(" ", "_") for a in aliases]
                        char_name_norm = char.name.lower().replace(" ", "_")
                        if speaker in alias_norms or speaker == char_name_norm:
                            speaker = cid
                            break

                if speaker not in allowed_speakers:
                    logger.warning(
                        "[ScriptGenerator] Unknown speaker '%s' for fragment %d — mapping to narrator",
                        speaker,
                        id_offset + i,
                    )
                    speaker = "narrator"

            try:
                pause_before_raw = int(
                    float(meta.get("pause_before_ms", 0) or 0)
                )
            except (TypeError, ValueError):
                pause_before_raw = 0
            try:
                pause_after_raw = int(
                    float(meta.get("pause_after_ms", 500) or 500)
                )
            except (TypeError, ValueError):
                pause_after_raw = 500
            pause_before = max(0, min(5000, pause_before_raw))
            pause_after = max(0, min(5000, pause_after_raw))
            global_id = id_offset + i
            try:
                speaker_confidence = (
                    max(
                        0.0,
                        min(1.0, float(meta["speaker_confidence"])),
                    )
                    if meta.get("speaker_confidence") is not None
                    else None
                )
            except (TypeError, ValueError):
                speaker_confidence = None

            lines.append(
                ScriptLine(
                    line_id=f"ch{fallback_number:02d}_{global_id:04d}",
                    speaker=speaker,
                    speaker_confidence=speaker_confidence,
                    speaker_evidence=str(
                        meta.get("speaker_evidence", "")
                    )[:500],
                    text=fragment.text,
                    emotion=str(meta.get("emotion", "neutral"))[:200],
                    speed=speed,
                    pause_before_ms=pause_before,
                    pause_after_ms=pause_after,
                    source_fragment_id=global_id,
                    source_fragment_ids=[global_id],
                    source_start=fragment.start,
                    source_end=fragment.end,
                )
            )

        return ScriptChapter(
            chapter_number=fallback_number,
            chapter_title=fallback_title,
            chapter_summary=raw.get("chapter_summary", ""),
            scenes=raw.get("scenes", []),
            lines=lines,
        )

    def _group_adjacent_utterances(
        self,
        script: ScriptChapter,
        source_text: str,
    ) -> ScriptChapter:
        """Merge bounded adjacent turns without crossing speaker/paragraph edges."""
        if len(script.lines) < 2:
            return script

        grouped: list[ScriptLine] = []
        bucket: list[ScriptLine] = []

        expressive_terms = (
            "shout",
            "scream",
            "panic",
            "terrified",
            "cry",
            "whisper",
            "breathless",
            "urgent",
            "angry",
        )

        def prosody_family(emotion: str) -> str:
            mood = (emotion or "neutral").casefold()
            families = (
                ("whispered", ("whisper", "hushed", "secret", "breathless")),
                ("urgent", ("shout", "scream", "panic", "urgent", "angry", "terrified")),
                ("somber", ("somber", "sad", "weary", "grief", "mourn", "reflective")),
                ("bright", ("joy", "happy", "excited", "playful", "warm")),
            )
            for family, terms in families:
                if any(term in mood for term in terms):
                    return family
            return "neutral"

        def limits(lines: list[ScriptLine]) -> tuple[int, int]:
            if any(
                any(term in line.emotion.lower() for term in expressive_terms)
                for line in lines
            ):
                return (
                    self.expressive_target_chars,
                    self.expressive_max_words,
                )
            if all(line.speaker == "narrator" for line in lines):
                return self.narrator_target_chars, self.narrator_max_words
            return self.utterance_target_chars, self.utterance_max_words

        def flush() -> None:
            if not bucket:
                return
            if len(bucket) == 1:
                line = bucket[0].model_copy(deep=True)
                if not line.source_fragment_ids and line.source_fragment_id is not None:
                    line.source_fragment_ids = [line.source_fragment_id]
                grouped.append(line)
                bucket.clear()
                return

            first, last = bucket[0], bucket[-1]
            if first.source_start is None or last.source_end is None:
                grouped.extend(line.model_copy(deep=True) for line in bucket)
                bucket.clear()
                return
            text = source_text[first.source_start:last.source_end]
            longest = max(bucket, key=lambda line: len(line.text))
            total_chars = max(1, sum(len(line.text) for line in bucket))
            speed = sum(
                line.speed * len(line.text) for line in bucket
            ) / total_chars
            fragment_ids = [
                fragment_id
                for line in bucket
                for fragment_id in (
                    line.source_fragment_ids
                    or (
                        [line.source_fragment_id]
                        if line.source_fragment_id is not None
                        else []
                    )
                )
            ]
            confidences = [
                line.speaker_confidence
                for line in bucket
                if line.speaker_confidence is not None
            ]
            evidence = "; ".join(
                dict.fromkeys(
                    line.speaker_evidence.strip()
                    for line in bucket
                    if line.speaker_evidence.strip()
                )
            )[:500]
            grouped.append(
                first.model_copy(
                    update={
                        "text": text,
                        "emotion": longest.emotion,
                        "speed": round(speed, 3),
                        "pause_after_ms": last.pause_after_ms,
                        "speaker_confidence": (
                            min(confidences) if confidences else None
                        ),
                        "speaker_evidence": evidence,
                        "source_fragment_ids": fragment_ids,
                        "source_end": last.source_end,
                    },
                    deep=True,
                )
            )
            bucket.clear()

        for line in script.lines:
            if not bucket:
                bucket.append(line)
                continue
            previous = bucket[-1]
            between = ""
            if previous.source_end is not None and line.source_start is not None:
                between = source_text[previous.source_end:line.source_start]
            candidate_chars = (
                (line.source_end or 0) - (bucket[0].source_start or 0)
                if line.source_end is not None
                and bucket[0].source_start is not None
                else sum(len(item.text) for item in bucket) + len(line.text)
            )
            candidate_words = sum(
                len(item.text.split()) for item in [*bucket, line]
            )
            same_fx = (
                previous.voice_fx.model_dump() if previous.voice_fx else None
            ) == (
                line.voice_fx.model_dump() if line.voice_fx else None
            )
            target_chars, max_words = limits([*bucket, line])
            same_speaker = (
                line.speaker == previous.speaker
                and (line.voice_id or line.speaker) == (previous.voice_id or previous.speaker)
            )
            speed_span = max(item.speed for item in [*bucket, line]) - min(
                item.speed for item in [*bucket, line]
            )
            prosody_families = {
                prosody_family(item.emotion) for item in [*bucket, line]
            }
            compatible_prosody = (
                len(prosody_families) == 1 and speed_span <= 0.12
            )
            can_merge = (
                same_speaker
                and compatible_prosody
                and same_fx
                and "\n\n" not in between
                and candidate_chars <= target_chars
                and candidate_words <= max_words
            )
            if not can_merge:
                flush()
            bucket.append(line)
        flush()
        # Keep short narrator tags in the narrator voice while marking the
        # quote/tag boundary as one tightly connected utterance. This metadata
        # also tells mastering not to crossfade the two independently rendered
        # voices.
        for idx in range(len(grouped) - 1):
            dialogue = grouped[idx]
            narration = grouped[idx + 1]
            between = ""
            if dialogue.source_end is not None and narration.source_start is not None:
                between = source_text[dialogue.source_end:narration.source_start]
            if (
                dialogue.speaker != "narrator"
                and narration.speaker == "narrator"
                and ScriptGenerator._is_pure_dialogue_tag(narration.text)
                and "\n\n" not in between
                and "\r\n\r\n" not in between
            ):
                group_id = f"utterance_{dialogue.line_id}"
                dialogue.utterance_group_id = group_id
                narration.utterance_group_id = group_id
                dialogue.pause_after_ms = 0
                narration.pause_before_ms = 0

        # Apply dynamic contextual pauses across grouped lines
        for idx in range(len(grouped)):
            curr_line = grouped[idx]
            if idx + 1 < len(grouped):
                next_line = grouped[idx + 1]
                between = ""
                if curr_line.source_end is not None and next_line.source_start is not None:
                    between = source_text[curr_line.source_end:next_line.source_start]

                same_utterance_group = (
                    curr_line.utterance_group_id is not None
                    and curr_line.utterance_group_id == next_line.utterance_group_id
                )
                if same_utterance_group:
                    curr_line.pause_after_ms = 0
                    next_line.pause_before_ms = 0
                elif "\n\n" in between or "\r\n\r\n" in between:
                    curr_line.pause_after_ms = 900
                elif curr_line.speaker != next_line.speaker:
                    if curr_line.speaker != "narrator" and next_line.speaker == "narrator":
                        curr_line.pause_after_ms = 400
                    else:
                        curr_line.pause_after_ms = 450
                elif curr_line.speaker == "narrator":
                    curr_line.pause_after_ms = 380
                else:
                    curr_line.pause_after_ms = 250
            else:
                curr_line.pause_after_ms = 1200

        if len(grouped) < len(script.lines):
            logger.info(
                "Grouped chapter %d from %d fragments into %d TTS utterances",
                script.chapter_number,
                len(script.lines),
                len(grouped),
            )
        return script.model_copy(update={"lines": grouped}, deep=True)

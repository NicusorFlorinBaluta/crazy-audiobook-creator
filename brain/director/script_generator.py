"""Script Generator — Pass 2 of the LLM Script Director.

Processes each chapter through the LLM with a sliding context window
to produce a line-by-line audiobook script with:
  - Speaker attribution (narrator vs. character ID)
  - Emotion tags based on surrounding context
  - Speed/pacing instructions
  - Pause durations
"""

from __future__ import annotations

import logging
from pathlib import Path

from brain.director.ollama_client import OllamaClient
from shared.constants import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS
from shared.models import (
    Character,
    CharacterRegistry,
    ExtractedChapter,
    ScriptChapter,
    ScriptLine,
)

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).parent / "prompts"

# Inline prompt template
_SCRIPT_PROMPT = """You are an expert audiobook script annotator. Convert the following chapter text into a structured audiobook script with speaker attribution, emotion tags, and pacing instructions.

## Context

### Character Registry
{character_registry}

### Previous Chapter Summary (for emotional continuity)
{previous_summary}

## Instructions

1. Process the chapter text into individual speech segments
2. For each segment, determine:
   - **speaker**: Character ID from the registry, or "narrator" for non-dialogue
   - **text**: The exact text to speak (strip dialogue attribution like "he said")
   - **emotion**: Natural language description of the emotional state, considering context
   - **speed**: Delivery speed (0.8 = slow/dramatic, 1.0 = normal, 1.2 = fast/excited)
   - **pause_before_ms**: Silence before this segment (0-2000)
   - **pause_after_ms**: Silence after this segment (0-2000)

## Rules

### Speaker Attribution
- Anything inside quotation marks is dialogue — identify the speaker from context
- Everything outside quotation marks is narrator
- Internal monologue (often in italics or without quotes but clearly a character's thoughts) → assign to the thinking character with emotion "internal, thoughtful"
- If you can't determine the speaker, use "narrator"

### Emotion Tagging
- Consider the SURROUNDING CONTEXT, not just the words themselves
- "Fine" can be dismissive, relieved, or lying — the context tells you which
- Use 2-3 descriptive words: "angry, barely controlled" not just "angry"
- For narrator segments, the emotion reflects the SCENE MOOD, not a character's feelings

### Pacing
- Scene transitions: 1500-2000ms pause
- Paragraph breaks in narration: 500-800ms pause
- Between dialogue lines in conversation: 200-400ms pause
- Dramatic pauses within dialogue: 300-600ms pause_before
- After a dramatic revelation: 1000-1500ms pause_after
- Chapter opening: 1000ms pause_before on first segment
- Chapter ending: 2000ms pause_after on last segment

### Speed
- Normal narration: 1.0
- Action sequences: 1.05-1.1
- Dramatic/emotional moments: 0.85-0.9
- Whispered/secretive: 0.85
- Excited/panicked: 1.1-1.15
- Contemplative/sad: 0.85-0.9

### Text Cleaning
- Remove dialogue attribution ("he said", "she whispered", "Kvothe replied")
- Keep the actual spoken words only
- For narration, keep the full text
- Preserve special punctuation (ellipses, em-dashes) as they affect TTS pacing

### Segment Length
- Keep segments between 1-4 sentences
- Never split mid-sentence
- Split long narration paragraphs at natural breathing points
- Each dialogue utterance is one segment (even if it's one word)

## Output Schema

Output ONLY valid JSON:
{{
  "chapter_number": {chapter_number},
  "chapter_title": "{chapter_title}",
  "chapter_summary": "1-2 sentence summary for continuity with next chapter",
  "lines": [
    {{
      "line_id": "ch{chapter_number_padded}_001",
      "speaker": "character_id",
      "text": "The spoken text",
      "emotion": "descriptive emotion state",
      "speed": 1.0,
      "pause_before_ms": 0,
      "pause_after_ms": 500
    }}
  ]
}}

## Chapter Text

{chapter_text}"""


class ScriptGenerator:
    """Pass 2: Generate line-by-line scripts for each chapter."""

    def __init__(
        self,
        ollama: OllamaClient,
        temperature: float = 0.4,
        chunk_size_words: int = CHUNK_SIZE_WORDS,
        chunk_overlap_words: int = CHUNK_OVERLAP_WORDS,
    ):
        self.ollama = ollama
        self.temperature = temperature
        self.chunk_size_words = chunk_size_words
        self.chunk_overlap_words = chunk_overlap_words

    def generate_chapter_script(
        self,
        chapter: ExtractedChapter,
        registry: CharacterRegistry,
        previous_summary: str = "",
    ) -> ScriptChapter:
        """Generate a full script for a single chapter.

        For chapters that exceed chunk_size_words, splits into overlapping
        chunks, processes each, and merges the results.

        Args:
            chapter: The chapter text to process.
            registry: Character registry from Pass 1.
            previous_summary: Summary of the previous chapter for continuity.

        Returns:
            ScriptChapter with all lines annotated.
        """
        logger.info(
            "Generating script for Chapter %d: '%s' (%d words)",
            chapter.number,
            chapter.title,
            chapter.word_count,
        )

        if chapter.word_count <= self.chunk_size_words:
            # Process entire chapter in one shot
            return self._process_chunk(
                chapter.text,
                chapter.number,
                chapter.title,
                registry,
                previous_summary,
            )
        else:
            # Split into overlapping chunks and merge
            return self._process_chunked(chapter, registry, previous_summary)

    def generate_all_chapters(
        self,
        chapters: list[ExtractedChapter],
        registry: CharacterRegistry,
    ) -> list[ScriptChapter]:
        """Generate scripts for all chapters sequentially.

        Each chapter receives the previous chapter's summary for
        emotional continuity.

        Args:
            chapters: List of chapters to process.
            registry: Character registry from Pass 1.

        Returns:
            List of ScriptChapters.
        """
        scripts: list[ScriptChapter] = []
        previous_summary = ""

        for i, chapter in enumerate(chapters):
            logger.info(
                "Processing chapter %d/%d: '%s'",
                i + 1,
                len(chapters),
                chapter.title,
            )

            script = self.generate_chapter_script(
                chapter, registry, previous_summary
            )
            scripts.append(script)
            previous_summary = script.chapter_summary

            # Check for new characters discovered during script generation
            self._detect_new_characters(script, registry)

        return scripts

    def _process_chunk(
        self,
        text: str,
        chapter_number: int,
        chapter_title: str,
        registry: CharacterRegistry,
        previous_summary: str,
    ) -> ScriptChapter:
        """Process a single chunk of text through the LLM."""
        # Build the character registry summary for the prompt
        char_summary = self._format_registry(registry)

        prompt = _SCRIPT_PROMPT.format(
            character_registry=char_summary,
            previous_summary=previous_summary or "This is the first chapter.",
            chapter_number=chapter_number,
            chapter_title=chapter_title,
            chapter_number_padded=f"{chapter_number:02d}",
            chapter_text=text,
        )

        logger.debug("Script prompt: %.1f KB", len(prompt) / 1024)

        raw = self.ollama.generate_json(
            prompt,
            temperature=self.temperature,
        )

        return self._parse_script_chapter(raw, chapter_number, chapter_title)

    def _process_chunked(
        self,
        chapter: ExtractedChapter,
        registry: CharacterRegistry,
        previous_summary: str,
    ) -> ScriptChapter:
        """Process a long chapter by splitting into overlapping chunks."""
        words = chapter.text.split()
        total_words = len(words)
        all_lines: list[ScriptLine] = []
        chunk_num = 0
        summary = ""

        i = 0
        while i < total_words:
            chunk_num += 1
            end = min(i + self.chunk_size_words, total_words)
            chunk_text = " ".join(words[i:end])

            logger.info(
                "Processing chunk %d (words %d-%d of %d)",
                chunk_num,
                i,
                end,
                total_words,
            )

            chunk_script = self._process_chunk(
                chunk_text,
                chapter.number,
                chapter.title,
                registry,
                previous_summary if chunk_num == 1 else summary,
            )

            if chunk_num == 1:
                # First chunk: take all lines
                all_lines.extend(chunk_script.lines)
            else:
                # Subsequent chunks: skip overlap lines
                overlap_line_count = self._estimate_overlap_lines(
                    chunk_script.lines, all_lines
                )
                all_lines.extend(chunk_script.lines[overlap_line_count:])

            summary = chunk_script.chapter_summary
            i = end - self.chunk_overlap_words if end < total_words else total_words

        # Re-number all line IDs
        for idx, line in enumerate(all_lines, 1):
            line.line_id = f"ch{chapter.number:02d}_{idx:03d}"

        return ScriptChapter(
            chapter_number=chapter.number,
            chapter_title=chapter.title,
            chapter_summary=summary,
            lines=all_lines,
        )

    def _estimate_overlap_lines(
        self,
        new_lines: list[ScriptLine],
        existing_lines: list[ScriptLine],
    ) -> int:
        """Estimate how many lines at the start of new_lines overlap with existing.

        Uses text similarity to detect duplicate segments from the overlap region.
        """
        if not existing_lines or not new_lines:
            return 0

        # Get the last few existing lines' text for comparison
        last_texts = {line.text.strip().lower()[:80] for line in existing_lines[-20:]}

        overlap_count = 0
        for line in new_lines:
            prefix = line.text.strip().lower()[:80]
            if prefix in last_texts:
                overlap_count += 1
            else:
                break  # No more overlap

        return overlap_count

    def _detect_new_characters(
        self,
        script: ScriptChapter,
        registry: CharacterRegistry,
    ) -> None:
        """Check for speakers not in the registry (discovered in Pass 2)."""
        known_ids = set(registry.characters.keys())

        for line in script.lines:
            speaker = line.speaker.lower().replace(" ", "_")
            if speaker not in known_ids:
                logger.info(
                    "New character discovered in chapter %d: '%s'",
                    script.chapter_number,
                    speaker,
                )
                # Add a placeholder character
                registry.characters[speaker] = Character(
                    id=speaker,
                    name=speaker.replace("_", " ").title(),
                    gender="other",
                    age_range="unknown",
                    personality_traits=[],
                    voice_description=(
                        f"A neutral voice for the character {speaker.replace('_', ' ')}."
                    ),
                    speaking_style="",
                    discovered_in_pass2=True,
                )
                known_ids.add(speaker)

    @staticmethod
    def _format_registry(registry: CharacterRegistry) -> str:
        """Format character registry as a readable string for the LLM prompt."""
        lines: list[str] = []
        for char_id, char in registry.characters.items():
            lines.append(
                f"- **{char.name}** (id: `{char_id}`, {char.gender}, {char.age_range}): "
                f"{char.speaking_style}"
            )
        return "\n".join(lines)

    @staticmethod
    def _parse_script_chapter(
        raw: dict,
        fallback_number: int,
        fallback_title: str,
    ) -> ScriptChapter:
        """Parse LLM JSON output into a ScriptChapter."""
        raw_lines = raw.get("lines", [])
        lines: list[ScriptLine] = []

        for i, raw_line in enumerate(raw_lines, 1):
            if not isinstance(raw_line, dict):
                continue

            line_id = raw_line.get(
                "line_id",
                f"ch{fallback_number:02d}_{i:03d}",
            )
            text = str(raw_line.get("text", "")).strip()
            if not text:
                continue

            lines.append(
                ScriptLine(
                    line_id=line_id,
                    speaker=str(raw_line.get("speaker", "narrator")).lower(),
                    text=text,
                    emotion=str(raw_line.get("emotion", "neutral")),
                    speed=float(raw_line.get("speed", 1.0)),
                    pause_before_ms=int(raw_line.get("pause_before_ms", 0)),
                    pause_after_ms=int(raw_line.get("pause_after_ms", 500)),
                )
            )

        return ScriptChapter(
            chapter_number=raw.get("chapter_number", fallback_number),
            chapter_title=raw.get("chapter_title", fallback_title),
            chapter_summary=raw.get("chapter_summary", ""),
            lines=lines,
        )

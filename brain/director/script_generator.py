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
import time
from pathlib import Path
from typing import Any, Callable

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

_SYSTEM_PROMPT = """You are a STRICT AUDIOBOOK SCRIPT METADATA ANNOTATOR. Your ONLY job is to assign the correct speaker, emotion, and reading speed to an array of pre-extracted text fragments.

## Context

### Character Registry
{character_registry}

### Previous Chapter Summary (for emotional continuity)
{previous_summary}

## Script Tagging Task

### Audio Direction Guidelines

#### Speaker Attribution Guidelines
- CRITICAL: EVERY fragment that is NOT inside quotation marks ("...") is NARRATION -> speaker MUST be "narrator".
- Dialogue tags (e.g., "he said", "she whispered", "Vathi replied", "Dusk looked at her") are NARRATOR lines -> speaker MUST be "narrator".
- ONLY spoken words inside quotation marks ("...") get a character speaker ID!
- Identify the dialogue speaker from the surrounding context and dialogue tags (e.g. if tag says "she whispered", speaker MUST be a female character like vathi or frond, NEVER male dusk).
- If you cannot determine the speaker with high confidence, use "narrator".

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
  "lines": [
    {{
      "id": 0,
      "speaker": "character_id",
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
        scripts_dir: Path | None = None,
        progress_callback: Callable[[ScriptChapter], None] = None,
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
                if script_path.exists():
                    logger.info("[ScriptGenerator] Skipping Chapter %d (already exists)", chapter.number)
                    try:
                        script = ScriptChapter.model_validate_json(script_path.read_text(encoding="utf-8"))
                        scripts.append(script)
                        previous_summary = script.chapter_summary
                        if progress_callback:
                            progress_callback(script)
                        continue
                    except Exception as e:
                        logger.warning("Failed to load existing script %s, regenerating. Error: %s", script_path, e)

            ch_t0 = _time.time()
            script = self.generate_chapter_script(
                chapter, registry, previous_summary
            )
            ch_elapsed = _time.time() - ch_t0

            scripts.append(script)
            previous_summary = script.chapter_summary

            # Save incrementally
            if script_path:
                with open(script_path, "w", encoding="utf-8") as f:
                    f.write(script.model_dump_json(indent=2))
                logger.info("[ScriptGenerator] Incrementally saved %s", script_path.name)

            logger.info(
                "[ScriptGenerator] Chapter %d/%d done in %.1fs | %d lines | summary: %r",
                i + 1,
                len(chapters),
                ch_elapsed,
                len(script.lines),
                (script.chapter_summary or "")[:80],
            )

            # Check for new characters discovered during script generation
            self._detect_new_characters(script, registry)

            if progress_callback:
                try:
                    progress_callback(script)
                except Exception as e:
                    logger.warning("Progress callback failed: %s", e)

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

    def _process_chunk(
        self,
        text: str,
        chapter_number: int,
        chapter_title: str,
        registry: CharacterRegistry,
        previous_summary: str,
    ) -> ScriptChapter:
        """Process a single chunk of text through the LLM."""
        char_summary = self._format_registry(registry)

        system_prompt = _SYSTEM_PROMPT.format(
            character_registry=char_summary,
            previous_summary=previous_summary or "None",
            chapter_number=chapter_number,
            chapter_title=chapter_title,
            chapter_number_padded=f"{chapter_number:02d}",
        )
        # Pre-process text into static fragments
        fragments = self._split_into_fragments(text)
        fragment_dicts = [{"id": i, "text": f} for i, f in enumerate(fragments)]
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
        raw = self.ollama.generate_json(
            prompt,
            temperature=self.temperature,
            system=system_prompt,
        )
        elapsed = _time.time() - t0

        result = self._parse_script_chapter(raw, chapter_number, chapter_title, fragments)
        logger.info(
            "[ScriptGenerator] Ch%d LLM done in %.1fs | %d lines generated",
            chapter_number,
            elapsed,
            len(result.lines),
        )
        return result

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
        new_found = []

        for line in script.lines:
            speaker = line.speaker.lower().replace(" ", "_")
            if speaker not in known_ids:
                new_found.append(speaker)
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

        if new_found:
            logger.info(
                "[ScriptGenerator] Ch%d: %d new character(s) discovered in Pass 2: %s",
                script.chapter_number,
                len(new_found),
                new_found,
            )
        else:
            logger.info(
                "[ScriptGenerator] Ch%d: no new characters (all speakers known)",
                script.chapter_number,
            )

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
    def _split_into_fragments(text: str) -> list[str]:
        """Split text into an array of sentence and dialogue fragments."""
        import re
        paragraphs = [p.strip() for p in text.split('\n') if p.strip()]
        fragments = []
        for p in paragraphs:
            # Split by quotes first to isolate dialogue blocks
            quote_pattern = re.compile(r'([\"“”].*?[\"“”])')
            parts = quote_pattern.split(p)
            for part in parts:
                if not part.strip():
                    continue
                is_quote = bool(re.match(r'^[\"“”].*[\"“”]$', part.strip()))
                if is_quote:
                    fragments.append(part.strip())
                else:
                    # Split narrative text into sentences
                    pattern = re.compile(r'.*?(?:[.!?]+(?=\s|$)|$)', re.DOTALL)
                    sentences = [match.group(0).strip() for match in pattern.finditer(part) if match.group(0).strip()]
                    fragments.extend(sentences)
        return fragments

    @staticmethod
    def _parse_script_chapter(
        raw: dict,
        fallback_number: int,
        fallback_title: str,
        fragments: list[str] = None,
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

        # Reconstruct exactly from static fragments to guarantee 100% text fidelity
        import re
        quote_pattern = re.compile(r'^[\"“”\'‘].*[\"“”\'’]$', re.DOTALL)

        for i, text in enumerate(fragments):
            meta = metadata_map.get(i, {})
            
            # Safely parse speed which might come back as a string like "normal"
            try:
                speed = float(meta.get("speed", 1.0))
            except (ValueError, TypeError):
                speed = 1.0

            text_trimmed = text.strip()
            is_quote = bool(quote_pattern.match(text_trimmed))
            
            # CRITICAL RULE: Non-dialogue text outside quotation marks MUST be narrator!
            speaker = str(meta.get("speaker", "narrator")).lower().replace(" ", "_")
            if not is_quote:
                speaker = "narrator"
                
            lines.append(
                ScriptLine(
                    line_id=f"ch{fallback_number:02d}_{i:03d}",
                    speaker=speaker,
                    text=text,
                    emotion=str(meta.get("emotion", "neutral")),
                    speed=speed,
                    pause_before_ms=int(meta.get("pause_before_ms", 0) or 0),
                    pause_after_ms=int(meta.get("pause_after_ms", 500) or 500),
                )
            )

        return ScriptChapter(
            chapter_number=raw.get("chapter_number", fallback_number),
            chapter_title=raw.get("chapter_title", fallback_title),
            chapter_summary=raw.get("chapter_summary", ""),
            lines=lines,
        )

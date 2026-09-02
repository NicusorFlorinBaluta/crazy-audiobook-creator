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
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from brain.director.ollama_client import OllamaClient, OllamaGenerationLimitError
from shared.constants import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS, Gender
from shared.artifacts import (
    assert_script_covers_source,
    atomic_write_json,
    atomic_write_text,
    script_fingerprint,
)
from shared.models import (
    Character,
    CharacterRegistry,
    ExtractedChapter,
    ScriptChapter,
    ScriptLine,
)

logger = logging.getLogger(__name__)

JOINT_SCRIPT_ANALYSIS_REVISION = 4
DIALOGUE_DELIVERY_POLICY_REVISION = 1
ADAPTIVE_CHUNK_POLICY_REVISION = 1

_PRONOUN_STOPWORDS = {
    "she", "he", "it", "they", "him", "her", "his", "hers", "them",
    "male", "female", "man", "woman", "boy", "girl", "person", "someone", "speaker",
}


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
    exact_dialogue_kind: str | None = None


@dataclass(frozen=True)
class DeliveryIssue:
    """A missing or invalid creative delivery decision for one fragment."""

    fragment_index: int
    fragment_id: int
    fields: tuple[str, ...]
    message: str


class MetadataAttributionError(ValueError):
    """Structured semantic failure retained as a ValueError for callers."""

    def __init__(self, issues: list[AttributionIssue]):
        if not issues:
            raise ValueError("MetadataAttributionError requires at least one issue")
        self.issues = tuple(issues)
        super().__init__("; ".join(issue.message for issue in issues))


class UnresolvedAttributionError(MetadataAttributionError):
    """Terminal semantic failure that must not trigger full-chunk retries."""


_SPEECH_VERBS = (
    "said", "says", "asked", "asks", "replied", "replies", "whispered",
    "whispers", "shouted", "shouts", "murmured", "murmurs", "exclaimed",
    "exclaims", "continued", "continues", "agreed", "agrees", "added",
    "adds", "called", "calls", "demanded", "demands", "warned", "warns",
    "answered", "answers", "cried", "cries", "thought", "thinks",
    "gasped", "gasps", "snapped", "snaps", "muttered", "mutters",
    "growled", "growls", "barked", "barks", "hissed", "hisses",
    "yelled", "yells", "screamed", "screams", "insisted", "insists",
    "urged", "urges", "chuckled", "chuckles", "sighed", "sighs",
    "groaned", "groans", "pleaded", "pleads", "begged", "begs",
    "declared", "declares", "announced", "announces", "stammered",
    "stammers", "stuttered", "stutters", "intoned", "intones",
    "snarled", "snarls", "commanded", "commands", "ordered", "orders",
    "breathed", "breathes", "roared", "roars", "shrieked", "shrieks",
    "chided", "chides", "teased", "teases", "scoffed", "scoffs",
)

_SPEECH_VERB_PATTERN = "|".join(re.escape(item) for item in _SPEECH_VERBS)
_SPEECH_VERB_SET = frozenset(_SPEECH_VERBS)
_PREPOSITIONS_OBJECTS = frozenset({
    "to", "toward", "towards", "at", "from", "with", "for",
    "about", "against", "of", "into", "onto", "like", "by", "near",
})
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _word_tokens(value: str) -> tuple[str, ...]:
    return tuple(_WORD_RE.findall(value.casefold().replace("_", " ")))


def _subsequence_starts(
    tokens: tuple[str, ...],
    needle: tuple[str, ...],
) -> tuple[int, ...]:
    if not needle or len(needle) > len(tokens):
        return ()
    width = len(needle)
    return tuple(
        index
        for index in range(len(tokens) - width + 1)
        if tokens[index:index + width] == needle
    )


@lru_cache(maxsize=4096)
def _evidence_name_patterns(name: str) -> tuple[re.Pattern[str], ...]:
    escaped = re.escape(name)
    return (
        re.compile(
            rf"\b(?:continuation\s+of|spoken\s+by|said\s+by|uttered\s+by|"
            rf"line\s+of|dialogue\s+of)\s+(?:the\s+)?{escaped}\b"
        ),
        re.compile(
            rf"\b{escaped}(?:'s)?\s+(?:dialogue|line|words|speech|turn|"
            rf"question|response|reply)\b"
        ),
        re.compile(rf"\b(?:as|like)\s+{escaped}\b"),
    )

_UNSAFE_SPEAKER_ALIASES = {
    "she", "he", "it", "they", "him", "her", "his", "hers", "them",
    "male", "female", "character", "unidentified", "man", "woman",
    "boy", "girl", "person", "someone", "speaker", "narrator",
}

_GENERIC_SPEAKER_ALIASES = {
    "character_female": "minor_female",
    "female_speaker": "minor_female",
    "unnamed_female": "minor_female",
    "unnamed_woman": "minor_female",
    "woman": "minor_female",
    "character_male": "minor_male",
    "male_speaker": "minor_male",
    "unnamed_male": "minor_male",
    "unnamed_man": "minor_male",
    "man": "minor_male",
    "unnamed_girl": "child_female",
    "girl": "child_female",
    "unnamed_boy": "child_male",
    "boy": "child_male",
}

_GENERIC_SPEAKER_DEFINITIONS: dict[str, dict[str, Any]] = {
    "minor_male": {
        "name": "Unnamed Man",
        "gender": Gender.MALE,
        "age_range": "adult",
        "personality_traits": ["unnamed", "generic"],
        "voice_description": (
            "male speaker, adult age. medium pitch, moderate volume, "
            "natural speed. clear texture, high clarity, natural fluency. "
            "neutral emotion, conversational tone, grounded personality."
        ),
        "speaking_style": "Natural dialogue matching the source context",
        "test_sentence": "I know what I saw, and I can explain it if you listen.",
    },
    "minor_female": {
        "name": "Unnamed Woman",
        "gender": Gender.FEMALE,
        "age_range": "adult",
        "personality_traits": ["unnamed", "generic"],
        "voice_description": (
            "female speaker, adult age. medium pitch, moderate volume, "
            "natural speed. clear texture, high clarity, natural fluency. "
            "neutral emotion, conversational tone, grounded personality."
        ),
        "speaking_style": "Natural dialogue matching the source context",
        "test_sentence": "I know what I saw, and I can explain it if you listen.",
    },
    "child_male": {
        "name": "Unnamed Boy",
        "gender": Gender.MALE,
        "age_range": "child",
        "personality_traits": ["unnamed", "generic"],
        "voice_description": (
            "male child speaker, child age. medium-high pitch, moderate volume, "
            "natural speed. clear texture, high clarity, natural fluency. "
            "curious emotion, direct tone, youthful personality."
        ),
        "speaking_style": "Natural dialogue matching the source context",
        "test_sentence": "I know what I saw, and I can explain it if you listen.",
    },
    "child_female": {
        "name": "Unnamed Girl",
        "gender": Gender.FEMALE,
        "age_range": "child",
        "personality_traits": ["unnamed", "generic"],
        "voice_description": (
            "female child speaker, child age. high pitch, moderate volume, "
            "natural speed. clear texture, high clarity, natural fluency. "
            "curious emotion, direct tone, youthful personality."
        ),
        "speaking_style": "Natural dialogue matching the source context",
        "test_sentence": "I know what I saw, and I can explain it if you listen.",
    },
    "crowd": {
        "name": "Crowd",
        "gender": Gender.OTHER,
        "age_range": "adult",
        "personality_traits": ["crowd", "collective", "generic"],
        "voice_description": "multiple voices speaking in unison, crowd chants, group murmurs.",
        "speaking_style": "Choral or crowd dialogue",
        "test_sentence": "We stand together!",
    },
    "collective": {
        "name": "Collective",
        "gender": Gender.OTHER,
        "age_range": "adult",
        "personality_traits": ["collective", "generic"],
        "voice_description": "collective or choral speech.",
        "speaking_style": "Choral or collective dialogue",
        "test_sentence": "We stand together!",
    },
}

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
- PAY CLOSE ATTENTION to split dialogue turns. When a character's quote is
  interrupted by a narrator tag (e.g. "What?" she said. "Really?"), the
  dialogue fragments BEFORE and AFTER the narrator tag belong to the SAME
  speaker. Do not alternate speakers for the second half of a split quote!
- Track the conversation flow carefully. In back-and-forth dialogue without
  explicit tags, speakers alternate (A-B-A-B). However, an intervening narrator
  tag describing the *current* speaker's action means the turn has NOT passed
  to the other person.
- Resolve ambiguous dialogue from surrounding turns, dialogue tags, aliases,
  and previous-chapter context.
- `speaker_evidence` must identify a concrete nearby source cue (for example,
  an exact speech tag, named action, or the two speakers in an established
  exchange). Generic claims such as "context from previous fragments" or
  "conversation flow" are not evidence and must not receive high confidence.
- CRITICAL: "narrator" MUST NEVER be used as the speaker for character dialogue (spoken words). If you cannot determine the speaker of a quote, make your best guess from the characters present. Use "narrator" for a quote ONLY when no character actually speaks it (for example, a written sign or document).
  select a character from gender alone or merely because they are nearby.
- Classify every quoted fragment with dialogue_kind. Use "spoken" whenever a
  character says or thinks the words. Use "non_spoken_quote" only for text no
  character voices (for example, a quoted sign, label, written word, or term),
  assign it to narrator, and cite that explicit source fact in speaker_evidence.
  Use "reported_collective_speech" only when an adjacent source tag attributes
  the quotation to an anonymous plural group (for example, "they said"); keep
  that short reported quotation with narrator and cite the collective tag.

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
## Compact Output Schema

CRITICAL REMINDER: You MUST output ONLY valid JSON matching the Output Schema below. Do NOT output any conversational text, essays, explanations, or markdown fences. Just the raw JSON object starting with {{ and ending with }}.

Quality is the primary requirement. Compactness MUST NOT remove a speaker
decision, confidence, evidence, emotion, or speed that the rules below require.
Return minified JSON without indentation or decorative whitespace.

For EVERY fragment, return:
- `id`.
- `scene_index` on the first row and only when the scene changes afterward.

For dialogue fragments, also return:
- `speaker`, `speaker_confidence`, and short source-grounded `speaker_evidence`.
- `emotion` and `speed`; character delivery remains a per-turn decision.
- Omit `dialogue_kind` for ordinary character speech; it is deterministically
  restored as `spoken`. Return it explicitly for `non_spoken_quote` and
  `reported_collective_speech`, including their required evidence.

For non-dialogue fragments, omit speaker attribution fields. Omit `emotion`
and `speed` when the scene's `narrator_emotion` and `narrator_pace` apply;
include them only for a material within-scene delivery change. The application
deterministically restores narrator ownership and scene defaults. Omit `pause_before_ms` and
`pause_after_ms` when the standard emotion/speed pacing rule applies. Include a
pause field only for a deliberate exception that materially improves delivery.

{{
  "chapter_number": {chapter_number},
  "chapter_title": "{chapter_title}",
  "chapter_summary": "1-2 sentence summary for continuity with next chapter",
  "scenes": [
    {{
      "mood": "overall scene mood (e.g., tense, melancholic)",
      "tension": "tension level (high, building, low)",
      "narrator_emotion": "specific default narrator delivery for this scene",
      "narrator_pace": 1.0,
      "character_state": "general state of characters in the scene",
      "transition_intent": "how this scene transitions to the next"
    }}
  ],
  "lines": [
    {{
      "id": 0,
      "scene_index": 0,
      "emotion": "descriptive emotion state",
      "speed": 1.0
    }},
    {{
      "id": 1,
      "speaker": "character_id",
      "speaker_confidence": 0.95,
      "speaker_evidence": "short source cue",
      "emotion": "descriptive dialogue emotion",
      "speed": 1.0
    }}
  ]
}}
"""

_USER_PROMPT = """## Source Text Fragments

{chapter_text_json}

Provide quality-preserving compact metadata for EACH fragment ID in the JSON array above. Ensure every single ID is accounted for in your output `lines` array. Never omit emotion or speed for dialogue. Narration may inherit those fields only from an explicit scene default. Never omit speaker, confidence, or evidence for dialogue.

CRITICAL: YOU MUST ONLY OUTPUT ONE MINIFIED VALID JSON OBJECT ENCLOSED IN {{}}. DO NOT ADD CONVERSATIONAL TEXT OR MARKDOWN.
"""

_DIALOGUE_FOCUSED_SCHEMA_PROMPT = """

## Dialogue-Focused Output Schema v5 (experimental)

This section replaces the earlier requirement to emit one `lines` row for
every fragment. Keep the same scene and line fields, with these changes:
- Every scene MUST include `start_id`, the first local fragment ID in that
  scene. Scene starts must be ordered, unique, and the first must be 0.
- `lines` MUST include every dialogue fragment ID, with the complete speaker,
  confidence, evidence, emotion, and speed decision required above.
- For narration, emit a `lines` row ONLY when it needs emotion, speed, or pause
  values that materially differ from its scene's narrator defaults.
- Do not emit routine narration rows. The application reconstructs narrator
  ownership, scene membership, inherited delivery, and routine pauses
  deterministically. Never omit a dialogue row to save tokens.
"""

_JOINT_CHARACTER_DISCOVERY_PROMPT = """

## Joint Character Discovery

While annotating these same fragments, update the speaking-character registry without
another pass over the source. Existing character IDs must be reused. If a dialogue
speaker is not in the registry, add exactly one entry to `character_updates` and use
that same snake_case ID on its spoken lines. Never add a person or entity that does
not speak in these fragments.

Every new entry must include `evidence_fragment_ids` pointing to local fragment IDs
whose surrounding source explicitly names or describes that speaker, plus a
`discovery_confidence` from 0 to 1. Do not use pronouns, proximity, gender, or a
nearby named entity as identity evidence. Later appearances may update an existing
ID with explicit aliases or stronger voice evidence.

Add this sibling field to the normal output object:
"character_updates": [
  {
    "character_id": "snake_case_id",
    "name": "display name",
    "aliases": ["explicit source alias"],
    "gender": "male|female|other",
    "age_range": "source-supported age or unknown",
    "personality_traits": ["brief trait"],
    "voice_description": "under 50 words using gender/age, pitch, volume, speed, accent, texture/clarity, fluency, emotion/tone/personality",
    "speaking_style": "brief source-grounded description",
    "test_sentence": "invented spoiler-free 15 to 25 word sentence",
    "evidence_fragment_ids": [0],
    "discovery_confidence": 0.95
  }
]
Use an empty array when the fragment batch reveals no new or updated speaker.
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
        adaptive_split_enabled: bool = True,
        adaptive_split_max_depth: int = 2,
        adaptive_split_min_fragments: int = 8,
        dialogue_focused_schema: bool = False,
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
        self.adaptive_split_enabled = bool(adaptive_split_enabled)
        self.adaptive_split_max_depth = max(0, adaptive_split_max_depth)
        self.adaptive_split_min_fragments = max(2, adaptive_split_min_fragments)
        self.dialogue_focused_schema = bool(dialogue_focused_schema)
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
        speaker_ids: set[str] | None = None,
    ) -> str:
        """Fingerprint every input that can change one script artifact."""
        dependency_ids = self._get_chapter_scoped_speakers(
            chapter.text,
            registry,
            fallback_all=False,
        )
        if speaker_ids:
            dependency_ids.update(speaker_ids)
        registry_dependency = []
        for character_id in sorted(dependency_ids):
            character = registry.characters.get(character_id)
            if character is None:
                continue
            registry_dependency.append(
                {
                    "id": character_id,
                    "name": character.name,
                    "gender": character.gender.value,
                    "age_range": character.age_range,
                    "aliases": sorted(character.aliases),
                    "speaking_style": character.speaking_style,
                }
            )
        return script_fingerprint(
            source_text=chapter.text,
            # Only attribution context rendered into _SYSTEM_PROMPT belongs in
            # the script dependency. Voice descriptions, assignments, and FX
            # affect audio manifests—not speaker/emotion metadata.
            registry=registry_dependency,
            model_name=getattr(self.ollama, "model", "unknown"),
            prompt_text=(
                _SYSTEM_PROMPT
                + _USER_PROMPT
                + "\nGROUPING_POLICY=narrator-tag-utterance-groups-v3"
                + "\nATTRIBUTION_REPAIR_POLICY=focused-exact-evidence-v1"
                + "\nDIALOGUE_CLASSIFICATION_POLICY=spoken-or-evidenced-nonspoken-v1"
                + f"\nJOINT_SCRIPT_ANALYSIS_REVISION={JOINT_SCRIPT_ANALYSIS_REVISION}"
                + f"\nDIALOGUE_DELIVERY_POLICY_REVISION={DIALOGUE_DELIVERY_POLICY_REVISION}"
                + f"\nADAPTIVE_CHUNK_POLICY_REVISION={ADAPTIVE_CHUNK_POLICY_REVISION}"
                + (
                    _DIALOGUE_FOCUSED_SCHEMA_PROMPT
                    if self.dialogue_focused_schema
                    else ""
                )
            ),
            chunk_size_words=self.chunk_size_words,
            max_fragments_per_chunk=self.max_fragments_per_chunk,
            adaptive_split_enabled=self.adaptive_split_enabled,
            adaptive_split_max_depth=self.adaptive_split_max_depth,
            adaptive_split_min_fragments=self.adaptive_split_min_fragments,
            group_utterances=self.group_utterances,
            utterance_target_chars=self.utterance_target_chars,
            utterance_max_words=self.utterance_max_words,
            narrator_target_chars=self.narrator_target_chars,
            narrator_max_words=self.narrator_max_words,
            expressive_target_chars=self.expressive_target_chars,
            expressive_max_words=self.expressive_max_words,
            speaker_confidence_threshold=self.speaker_confidence_threshold,
        )

    def chapter_dependency_metadata(
        self,
        chapter: ExtractedChapter,
        registry: CharacterRegistry,
        script: ScriptChapter,
    ) -> dict[str, Any]:
        """Return stable, chapter-local dependency metadata for a saved script."""
        speaker_ids = {
            self._normalize_speaker_id(line.speaker)
            for line in script.lines
            if line.speaker
        }
        return {
            "dependency_schema": 2,
            "speaker_dependency_ids": sorted(speaker_ids),
            "fingerprint": self.chapter_fingerprint(
                chapter,
                registry,
                speaker_ids,
            ),
        }

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
                script = ScriptChapter.model_validate_json(
                    script_path.read_text(encoding="utf-8")
                )
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                speaker_ids = set(
                    metadata.get("speaker_dependency_ids")
                    or [line.speaker for line in script.lines]
                )
                if metadata.get("fingerprint") != self.chapter_fingerprint(
                    chapter,
                    registry,
                    speaker_ids,
                ):
                    return False
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
        *,
        allow_character_discovery: bool = False,
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

        planned_chunks = self._chunk_fragments(fragments)
        if len(planned_chunks) == 1:
            script = self._process_fragments(
                fragments,
                chapter.number,
                chapter.title,
                registry,
                previous_summary,
                id_offset=0,
                allow_character_discovery=allow_character_discovery,
                chapter_text=chapter.text,
            )
        else:
            script = self._process_chunked(
                chapter,
                registry,
                previous_summary,
                chunk_progress_callback,
                allow_character_discovery=allow_character_discovery,
            )

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
        *,
        allow_character_discovery: bool = False,
        discovery_checkpoint_chapters: set[int] | None = None,
        registry_progress_callback: Callable[[CharacterRegistry, int], None] | None = None,
        fingerprint_override: str | None = None,
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
                if script_path.exists() and metadata_path.exists():
                    try:
                        metadata = json.loads(
                            metadata_path.read_text(encoding="utf-8")
                        )
                        script = ScriptChapter.model_validate_json(
                            script_path.read_text(encoding="utf-8")
                        )
                        speaker_ids = set(
                            metadata.get("speaker_dependency_ids")
                            or [line.speaker for line in script.lines]
                        )
                        expected_fingerprint = fingerprint_override or self.chapter_fingerprint(
                            chapter,
                            registry,
                            speaker_ids,
                        )
                        checkpoint_reusable = (
                            allow_character_discovery
                            and discovery_checkpoint_chapters is not None
                            and chapter.number in discovery_checkpoint_chapters
                        )
                        if (
                            metadata.get("fingerprint") != expected_fingerprint
                            and not checkpoint_reusable
                        ):
                            raise ValueError("script dependency fingerprint changed")
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
                chapter,
                registry,
                previous_summary,
                chunk_progress_callback,
                allow_character_discovery=allow_character_discovery,
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
                    (
                        {"fingerprint": fingerprint_override}
                        if fingerprint_override
                        else self.chapter_dependency_metadata(
                            chapter,
                            registry,
                            script,
                        )
                    ),
                )
                logger.info("[ScriptGenerator] Incrementally saved %s", script_path.name)

            if registry_progress_callback:
                registry_progress_callback(registry, chapter.number)

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

    def repair_chapter_attribution(
        self,
        chapter: ExtractedChapter,
        script: ScriptChapter,
        registry: CharacterRegistry,
    ) -> tuple[ScriptChapter, dict[str, Any]]:
        """Repair only suspect source fragments while preserving delivery metadata."""
        fragments = self._split_into_fragment_spans(chapter.text)
        owners: dict[int, ScriptLine] = {}
        for line in script.lines:
            fragment_ids = list(line.source_fragment_ids)
            if not fragment_ids and line.source_fragment_id is not None:
                fragment_ids = [line.source_fragment_id]
            for fragment_id in fragment_ids:
                owners[fragment_id] = line

        rows: list[dict[str, Any]] = []
        for index, fragment in enumerate(fragments):
            owner = owners.get(index)
            is_dialogue = self._is_dialogue_fragment(fragment.text)
            speaker = owner.speaker if owner is not None else "narrator"
            rows.append(
                {
                    "id": index,
                    "speaker": speaker if is_dialogue else "narrator",
                    "speaker_confidence": (
                        owner.speaker_confidence if owner is not None else None
                    ),
                    "speaker_evidence": (
                        owner.speaker_evidence if owner is not None else ""
                    ),
                    "dialogue_kind": (
                        owner.dialogue_kind
                        if owner is not None and owner.dialogue_kind is not None
                        else (
                            "spoken"
                            if is_dialogue and speaker != "narrator"
                            else None
                        )
                    ),
                    "emotion": owner.emotion if owner is not None else "neutral",
                    "speed": owner.speed if owner is not None else 1.0,
                    "pause_before_ms": (
                        owner.pause_before_ms if owner is not None else 0
                    ),
                    "pause_after_ms": (
                        owner.pause_after_ms if owner is not None else 500
                    ),
                }
            )
        raw: dict[str, Any] = {
            "chapter_number": chapter.number,
            "chapter_title": script.chapter_title,
            "chapter_summary": script.chapter_summary,
            "scenes": [scene.model_dump(mode="json") for scene in script.scenes],
            "lines": rows,
        }
        original_attribution = {
            int(row["id"]): (row.get("speaker"), row.get("dialogue_kind"))
            for row in rows
        }
        allowed_speakers = set(registry.characters)
        issues = self._collect_metadata_speaker_issues(
            raw,
            fragments,
            allowed_speakers,
            registry=registry,
            confidence_threshold=self.speaker_confidence_threshold,
            chapter_text=chapter.text,
        )
        initial_issue_count = len(issues)
        local_repairs = self._apply_deterministic_attribution_repairs(raw, issues)
        issues = self._collect_metadata_speaker_issues(
            raw,
            fragments,
            allowed_speakers,
            registry=registry,
            confidence_threshold=self.speaker_confidence_threshold,
            chapter_text=chapter.text,
        )
        request_metrics: list[dict[str, Any]] = []
        for semantic_round in range(1, 4):
            if not issues:
                break
            last_error: Exception | None = None
            replacements: dict[int, dict[str, Any]] | None = None
            for schema_attempt in range(1, 3):
                try:
                    replacements = self._retry_attribution_batch(
                        issues,
                        raw,
                        fragments,
                        allowed_speakers,
                        registry,
                        self._format_registry(registry),
                        request_metrics,
                        id_offset=0,
                    )
                    break
                except ValueError as exc:
                    last_error = exc
                    logger.warning(
                        "Selective attribution repair schema attempt %d failed "
                        "for chapter %d: %s",
                        schema_attempt,
                        chapter.number,
                        exc,
                    )
            if replacements is None:
                assert last_error is not None
                raise last_error
            for fragment_index, replacement in replacements.items():
                self._replace_metadata_line(raw, fragment_index, replacement)
            post_issues = self._collect_metadata_speaker_issues(
                raw,
                fragments,
                allowed_speakers,
            registry=registry,
            confidence_threshold=self.speaker_confidence_threshold,
            chapter_text=chapter.text,
        )
            local_repairs += self._apply_deterministic_attribution_repairs(
                raw,
                post_issues,
            )
            issues = self._collect_metadata_speaker_issues(
                raw,
                fragments,
                allowed_speakers,
            registry=registry,
            confidence_threshold=self.speaker_confidence_threshold,
            chapter_text=chapter.text,
        )
            if issues:
                logger.warning(
                    "Selective attribution semantic round %d left %d issue(s) "
                    "for chapter %d; retrying only those fragments",
                    semantic_round,
                    len(issues),
                    chapter.number,
                )
        remaining = self._collect_metadata_speaker_issues(
            raw,
            fragments,
            allowed_speakers,
            registry=registry,
            confidence_threshold=self.speaker_confidence_threshold,
            chapter_text=chapter.text,
        )
        if remaining:
            logger.warning("repair_chapter_attribution failed to resolve all issues; proceeding with fallback metadata. Issues: %s", remaining)

        repaired = self._parse_script_chapter(
            raw,
            chapter.number,
            script.chapter_title,
            fragments,
            allowed_speakers=allowed_speakers,
            registry=registry,
        )
        if self.group_utterances:
            repaired = self._group_adjacent_utterances(repaired, chapter.text)
        assert_script_covers_source(repaired, chapter.text)
        changed_fragments = [
            index for index, row in self._metadata_line_map(raw).items()
            if original_attribution[index]
            != (
                row.get("speaker"),
                row.get("dialogue_kind"),
            )
        ]
        return repaired, {
            "chapter_number": chapter.number,
            "initial_issues": initial_issue_count,
            "local_repairs": local_repairs,
            "changed_fragments": changed_fragments,
            "requests": request_metrics,
        }

    def _process_fragments(
        self,
        fragments: list[SourceFragment],
        chapter_number: int,
        chapter_title: str,
        registry: CharacterRegistry,
        previous_summary: str,
        id_offset: int = 0,
        *,
        strict_validation: bool = False,
        allow_character_discovery: bool = False,
        allowed_speakers: set[str] | None = None,
        chapter_text: str | None = None,
        prior_turn_context: list[dict[str, Any]] | None = None,
        adaptive_split_depth: int = 0,
    ) -> ScriptChapter:
        full_text = "".join(fragment.text for fragment in fragments)
        allowed_speakers = allowed_speakers or self._get_chapter_scoped_speakers(
            full_text,
            registry,
        )
        scoped_registry = (
            registry
            if len(allowed_speakers) >= len(registry.characters)
            else CharacterRegistry(
                book_title=getattr(registry, "book_title", ""),
                book_author=getattr(registry, "book_author", ""),
                genre=getattr(registry, "genre", "fantasy"),
                tone=getattr(registry, "tone", ""),
                characters={
                    cid: c
                    for cid, c in registry.characters.items()
                    if cid in allowed_speakers
                },
            )
        )
        char_summary = self._format_registry(scoped_registry)

        system_prompt = _SYSTEM_PROMPT.format(
            character_registry=char_summary,
            previous_summary=previous_summary or "None",
            chapter_number=chapter_number,
            chapter_title=chapter_title,
            chapter_number_padded=f"{chapter_number:02d}",
        )
        if self.dialogue_focused_schema:
            system_prompt += _DIALOGUE_FOCUSED_SCHEMA_PROMPT
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
        dialogue_ids = [
            item["id"] for item in fragment_dicts if item["dialogue"]
        ]
        if dialogue_ids:
            prompt += (
                "\n\nMANDATORY DIALOGUE DELIVERY IDS: "
                + json.dumps(dialogue_ids)
                + "\nEvery listed ID MUST contain its own non-empty emotion "
                "and numeric speed. Scene narrator defaults NEVER satisfy "
                "dialogue delivery and omitting either field makes the response "
                "invalid."
            )
        if prior_turn_context:
            prompt += (
                "\n\nCHUNK-BOUNDARY CONTEXT (already processed; do not emit "
                "metadata for these rows):\n"
                + json.dumps(prior_turn_context[-10:], ensure_ascii=False)
                + "\nUse this only to preserve the active conversation and split-turn "
                "speaker across the boundary. Source tags in the current chunk "
                "still take precedence."
            )
        if allow_character_discovery:
            prompt += _JOINT_CHARACTER_DISCOVERY_PROMPT

        prompt_kb = (len(system_prompt) + len(prompt)) / 1024
        if prompt_kb > 80:
            logger.warning(
                "[ScriptGenerator] Chapter %d prompt is very large (%.1f KB) — LLM may struggle",
                chapter_number,
                prompt_kb,
            )

        logger.info(
            "[ScriptGenerator] Ch%d '%s' → LLM | %.1f KB prompt | %d fragments | %d active characters",
            chapter_number,
            chapter_title[:40],
            prompt_kb,
            len(fragments),
            len(scoped_registry.characters),
        )

        import time as _time
        t0 = _time.time()
        raw = None
        last_error: Exception | None = None
        used_fallback = False
        full_attempts = 0
        structural_failures = 0
        focused_retries = 0
        strict_attribution_retries = 0
        delivery_focused_retries = 0
        delivery_focused_rounds = 0
        delivery_issue_counts: dict[str, int] = {}
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
                        "Do not invent generic speaker labels. Pay close attention "
                        "to split dialogue: fragments before and after a narrator "
                        "interruption belong to the SAME speaker."
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
                            ),
                        }
                    )
                sparse_stats = None
                if self.dialogue_focused_schema:
                    sparse_stats = self._inflate_dialogue_focused_metadata(
                        candidate,
                        fragments,
                    )
                    request_metrics[-1]["dialogue_focused_metadata"] = sparse_stats
                self._validate_metadata_ids(candidate, len(fragments))
                delivery_issues = self._collect_delivery_metadata_issues(
                    candidate,
                    fragments,
                    id_offset=id_offset,
                )
                for delivery_issue in delivery_issues:
                    for field in delivery_issue.fields:
                        delivery_issue_counts[field] = (
                            delivery_issue_counts.get(field, 0) + 1
                        )
                for delivery_round in range(1, 3):
                    if not delivery_issues:
                        break
                    delivery_focused_rounds += 1
                    delivery_focused_retries += len(delivery_issues)
                    try:
                        replacements = self._retry_delivery_batch(
                            delivery_issues,
                            candidate,
                            fragments,
                            request_metrics,
                            id_offset=id_offset,
                            round_number=delivery_round,
                        )
                    except Exception as exc:
                        if delivery_round >= 2:
                            raise
                        logger.warning(
                            "Focused delivery repair round %d failed for chapter "
                            "%d fragment(s) %s: %s. Trying one final focused "
                            "repair before any full-chunk retry.",
                            delivery_round,
                            chapter_number,
                            ", ".join(
                                str(item.fragment_id) for item in delivery_issues
                            ),
                            exc,
                        )
                        continue
                    for fragment_index, replacement in replacements.items():
                        self._replace_metadata_line(
                            candidate,
                            fragment_index,
                            replacement,
                        )
                    delivery_issues = self._collect_delivery_metadata_issues(
                        candidate,
                        fragments,
                        id_offset=id_offset,
                    )
                if delivery_issues:
                    raise ValueError(
                        "Focused delivery repair remained invalid: "
                        + "; ".join(issue.message for issue in delivery_issues)
                    )

                compact_stats = self._expand_compact_metadata(candidate, fragments)
                request_metrics[-1]["compact_metadata"] = compact_stats
                logger.info(
                    "[ScriptGenerator] Compact metadata for chapter %d: "
                    "%d received chars -> %d canonical chars (%.1f%% avoided)",
                    chapter_number,
                    compact_stats["received_characters"],
                    compact_stats["canonical_characters"],
                    compact_stats["character_savings_ratio"] * 100,
                )
                self._validate_metadata_ids(candidate, len(fragments))

                if allow_character_discovery:
                    self._apply_joint_character_updates(
                        candidate,
                        fragments,
                        registry,
                    )
                    allowed_speakers = set(registry.characters)
                    char_summary = self._format_registry(registry)

                issues = self._collect_metadata_speaker_issues(
                    candidate,
                    fragments,
                    allowed_speakers,
                    registry=registry,
                    id_offset=id_offset,
                    confidence_threshold=self.speaker_confidence_threshold,
                    chapter_text=chapter_text,
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
                    chapter_text=chapter_text,
                )
                if issues:
                    focused_retries += len(issues)
                    try:
                        replacements = self._retry_attribution_batch(
                            issues,
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
                            "fragment(s) %s: %s",
                            chapter_number,
                            ", ".join(str(item.fragment_id) for item in issues),
                            exc,
                        )
                    else:
                        for fragment_index, replacement in replacements.items():
                            self._replace_metadata_line(
                                candidate,
                                fragment_index,
                                replacement,
                            )
                        # Apply contextual repairs on returned replacements if still conflicting
                        post_retry_issues = self._collect_metadata_speaker_issues(
                            candidate,
                            fragments,
                            allowed_speakers,
                            registry=registry,
                            id_offset=id_offset,
                            confidence_threshold=self.speaker_confidence_threshold,
                            chapter_text=chapter_text,
                        )
                        if post_retry_issues:
                            local_repairs += (
                                self._apply_deterministic_attribution_repairs(
                                    candidate,
                                    post_retry_issues,
                                    fragments=fragments,
                                    registry=registry,
                                    allowed_speakers=allowed_speakers,
                                )
                            )

                post_retry_issues = self._collect_metadata_speaker_issues(
                    candidate,
                    fragments,
                    allowed_speakers,
                    registry=registry,
                    id_offset=id_offset,
                    confidence_threshold=self.speaker_confidence_threshold,
                    chapter_text=chapter_text,
                )
                strict_issues = [
                    issue
                    for issue in post_retry_issues
                    if issue.kind
                    in {"narrator_spoken_dialogue", "narrator_dialogue_tag"}
                ]
                if strict_issues:
                    strict_attribution_retries += len(strict_issues)
                    try:
                        replacements = self._retry_attribution_batch(
                            strict_issues,
                            candidate,
                            fragments,
                            allowed_speakers,
                            registry,
                            char_summary,
                            request_metrics,
                            id_offset=id_offset,
                            strict_spoken=True,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Strict spoken-dialogue retry failed for chapter %d "
                            "fragment(s) %s: %s",
                            chapter_number,
                            ", ".join(
                                str(item.fragment_id) for item in strict_issues
                            ),
                            exc,
                        )
                    else:
                        for fragment_index, replacement in replacements.items():
                            self._replace_metadata_line(
                                candidate,
                                fragment_index,
                                replacement,
                            )
                        strict_remaining = self._collect_metadata_speaker_issues(
                            candidate,
                            fragments,
                            allowed_speakers,
                            registry=registry,
                            id_offset=id_offset,
                            confidence_threshold=self.speaker_confidence_threshold,
                            chapter_text=chapter_text,
                        )
                        if strict_remaining:
                            local_repairs += (
                                self._apply_deterministic_attribution_repairs(
                                    candidate,
                                    strict_remaining,
                                    fragments=fragments,
                                    registry=registry,
                                    allowed_speakers=allowed_speakers,
                                )
                            )

                remaining_issues = self._collect_metadata_speaker_issues(
                    candidate,
                    fragments,
                    allowed_speakers,
                    registry=registry,
                    id_offset=id_offset,
                    confidence_threshold=self.speaker_confidence_threshold,
                    chapter_text=chapter_text,
                )
                if remaining_issues:
                    if strict_validation:
                        raise MetadataAttributionError(remaining_issues)
                    # Attribution uncertainty is not a structural failure. A
                    # second full-chunk generation is both expensive and less
                    # targeted than the focused repair that already ran. Keep
                    # the usable response and surface each unresolved fragment
                    # as a low-confidence review candidate instead.
                    unresolved_indexes = {
                        issue.fragment_index for issue in remaining_issues
                    }
                    fragment_fallbacks += len(unresolved_indexes)
                    for fragment_index in sorted(unresolved_indexes):
                        fragment_issues = [
                            issue
                            for issue in remaining_issues
                            if issue.fragment_index == fragment_index
                        ]
                        logger.warning(
                            "Focused attribution remained unresolved for chapter "
                            "%d fragment %d (%s). Applying conservative review "
                            "metadata without regenerating the full chunk.",
                            chapter_number,
                            id_offset + fragment_index,
                            ", ".join(
                                sorted({issue.kind for issue in fragment_issues})
                            ),
                        )
                        fallback_meta = self._fallback_fragment_metadata(
                            fragment_index,
                            candidate,
                            fragments,
                            allowed_speakers,
                            registry,
                        )
                        self._replace_metadata_line(
                            candidate,
                            fragment_index,
                            fallback_meta,
                        )

                self._validate_metadata_ids(candidate, len(fragments))
                raw = candidate
                break
            except OllamaGenerationLimitError as exc:
                last_error = exc
                structural_failures += 1
                can_split = (
                    self.adaptive_split_enabled
                    and adaptive_split_depth < self.adaptive_split_max_depth
                    and len(fragments) >= self.adaptive_split_min_fragments
                )
                if can_split:
                    split_index = self._adaptive_fragment_split_index(fragments)
                    logger.warning(
                        "Metadata generation limit reached for chapter %d; "
                        "adaptively splitting %d fragments into %d + %d "
                        "(depth %d/%d) instead of falling back.",
                        chapter_number,
                        len(fragments),
                        split_index,
                        len(fragments) - split_index,
                        adaptive_split_depth + 1,
                        self.adaptive_split_max_depth,
                    )
                    self.call_metrics.append(
                        {
                            "chapter_number": chapter_number,
                            "chapter_title": chapter_title,
                            "fragment_count": len(fragments),
                            "source_words": sum(
                                len(item.text.split()) for item in fragments
                            ),
                            "prompt_characters": len(system_prompt) + len(prompt),
                            "wall_seconds": round(_time.time() - t0, 6),
                            "attempts": full_attempts,
                            "full_attempts": full_attempts,
                            "structural_failures": structural_failures,
                            "structural_retries": 0,
                            "full_semantic_retries": 0,
                            "focused_retries": focused_retries,
                            "strict_attribution_retries": strict_attribution_retries,
                            "delivery_focused_retries": delivery_focused_retries,
                            "delivery_focused_rounds": delivery_focused_rounds,
                            "delivery_issue_counts": delivery_issue_counts,
                            "local_repairs": local_repairs,
                            "fragment_fallbacks": 0,
                            "attribution_issue_counts": issue_counts,
                            "requests": request_metrics,
                            "used_fallback": False,
                            "adaptive_split_triggered": True,
                            "adaptive_split_depth": adaptive_split_depth + 1,
                            "adaptive_split_children": [
                                split_index,
                                len(fragments) - split_index,
                            ],
                            "ollama": dict(
                                getattr(
                                    self.ollama,
                                    "last_generation_metrics",
                                    {},
                                )
                                or {}
                            ),
                        }
                    )
                    return self._process_adaptive_fragment_split(
                        fragments,
                        split_index,
                        chapter_number,
                        chapter_title,
                        registry,
                        previous_summary,
                        id_offset=id_offset,
                        strict_validation=strict_validation,
                        allow_character_discovery=allow_character_discovery,
                        allowed_speakers=allowed_speakers,
                        chapter_text=chapter_text,
                        prior_turn_context=prior_turn_context,
                        adaptive_split_depth=adaptive_split_depth + 1,
                    )
                # Reissuing the same oversized or looping prompt can consume
                # the safeguard budget again. Once bounded splitting is no
                # longer safe, fall back conservatively and surface confidence.
                logger.error(
                    "Runaway metadata generation stopped for chapter %d: %s. "
                    "Adaptive splitting is unavailable or exhausted; skipping "
                    "duplicate full-request retries.",
                    chapter_number,
                    exc,
                )
                break
            except MetadataAttributionError as exc:
                if strict_validation:
                    raise
                last_error = exc
                structural_failures += 1
                logger.warning(
                    "Metadata attribution attempt %d failed for chapter %d: %s",
                    attempt,
                    chapter_number,
                    exc,
                )
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
            fallback_issues = self._collect_metadata_speaker_issues(
                raw,
                fragments,
                allowed_speakers,
                registry=registry,
                id_offset=id_offset,
                confidence_threshold=self.speaker_confidence_threshold,
                chapter_text=chapter_text,
            )
            self._apply_deterministic_attribution_repairs(
                raw,
                fallback_issues,
                fragments=fragments,
                registry=registry,
                allowed_speakers=allowed_speakers,
            )
            fallback_issues = self._collect_metadata_speaker_issues(
                raw,
                fragments,
                allowed_speakers,
                registry=registry,
                id_offset=id_offset,
                confidence_threshold=self.speaker_confidence_threshold,
                chapter_text=chapter_text,
            )
            if fallback_issues:
                for rem_issue in fallback_issues:
                    fallback_meta = self._fallback_fragment_metadata(
                        rem_issue.fragment_index,
                        raw,
                        fragments,
                        allowed_speakers,
                        registry,
                    )
                    self._replace_metadata_line(
                        raw,
                        rem_issue.fragment_index,
                        fallback_meta,
                    )
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
                "strict_attribution_retries": strict_attribution_retries,
                "delivery_focused_retries": delivery_focused_retries,
                "delivery_focused_rounds": delivery_focused_rounds,
                "delivery_issue_counts": delivery_issue_counts,
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

    @classmethod
    def _adaptive_fragment_split_index(
        cls,
        fragments: list[SourceFragment],
    ) -> int:
        """Choose a balanced boundary without separating dialogue from its tag."""
        if len(fragments) < 2:
            raise ValueError("Adaptive splitting requires at least two fragments")
        weights = [max(1, len(item.text.split())) + 8 for item in fragments]
        target = sum(weights) / 2
        prefix = 0
        ranked: list[tuple[float, int]] = []
        for index in range(1, len(fragments)):
            prefix += weights[index - 1]
            ranked.append((abs(prefix - target), index))
        ranked.sort()
        for _, index in ranked:
            if not cls._is_dialogue_fragment(fragments[index - 1].text):
                return index
        return ranked[0][1]

    def _process_adaptive_fragment_split(
        self,
        fragments: list[SourceFragment],
        split_index: int,
        chapter_number: int,
        chapter_title: str,
        registry: CharacterRegistry,
        previous_summary: str,
        *,
        id_offset: int,
        strict_validation: bool,
        allow_character_discovery: bool,
        allowed_speakers: set[str],
        chapter_text: str | None,
        prior_turn_context: list[dict[str, Any]] | None,
        adaptive_split_depth: int,
    ) -> ScriptChapter:
        """Retry one failed batch as two bounded, contiguous source ranges."""
        left_fragments = fragments[:split_index]
        right_fragments = fragments[split_index:]
        left_script = self._process_fragments(
            left_fragments,
            chapter_number,
            chapter_title,
            registry,
            previous_summary,
            id_offset=id_offset,
            strict_validation=strict_validation,
            allow_character_discovery=allow_character_discovery,
            allowed_speakers=set(allowed_speakers),
            chapter_text=chapter_text,
            prior_turn_context=prior_turn_context,
            adaptive_split_depth=adaptive_split_depth,
        )
        right_context = list(prior_turn_context or [])
        right_context.extend(
            {
                "speaker": line.speaker,
                "dialogue_kind": line.dialogue_kind,
                "text": line.text,
            }
            for line in left_script.lines[-10:]
        )
        right_summary = previous_summary
        if left_script.chapter_summary:
            right_summary = (
                f"{previous_summary}\nCurrent batch so far: "
                f"{left_script.chapter_summary}"
            ).strip()
        right_script = self._process_fragments(
            right_fragments,
            chapter_number,
            chapter_title,
            registry,
            right_summary,
            id_offset=id_offset + split_index,
            strict_validation=strict_validation,
            allow_character_discovery=allow_character_discovery,
            allowed_speakers=(
                set(allowed_speakers) | set(registry.characters)
            ),
            chapter_text=chapter_text,
            prior_turn_context=right_context[-10:],
            adaptive_split_depth=adaptive_split_depth,
        )
        merged = ScriptChapter(
            chapter_number=chapter_number,
            chapter_title=chapter_title,
            chapter_summary=(
                f"{left_script.chapter_summary} {right_script.chapter_summary}"
            ).strip()[-2000:],
            scenes=[*left_script.scenes, *right_script.scenes],
            lines=[*left_script.lines, *right_script.lines],
        )
        return merged

    @staticmethod
    def _record_issue_counts(
        counts: dict[str, int],
        issues: list[AttributionIssue],
    ) -> None:
        for issue in issues:
            counts[issue.kind] = counts.get(issue.kind, 0) + 1

    @staticmethod
    def _collect_delivery_metadata_issues(
        raw: dict[str, Any],
        fragments: list[SourceFragment],
        *,
        id_offset: int = 0,
    ) -> list[DeliveryIssue]:
        """Find creative delivery fields that cannot be safely derived."""
        ScriptGenerator._validate_metadata_ids(raw, len(fragments))
        metadata_map = ScriptGenerator._metadata_line_map(raw)
        scenes = raw.get("scenes") if isinstance(raw.get("scenes"), list) else []
        issues: list[DeliveryIssue] = []
        current_scene = 0
        for fragment_index in range(len(fragments)):
            item = metadata_map[fragment_index]
            if "scene_index" in item:
                try:
                    current_scene = int(item["scene_index"])
                except (TypeError, ValueError):
                    current_scene = -1
            scene = (
                scenes[current_scene]
                if 0 <= current_scene < len(scenes)
                and isinstance(scenes[current_scene], dict)
                else {}
            )
            is_dialogue = ScriptGenerator._is_dialogue_fragment(
                fragments[fragment_index].text
            )
            invalid_fields: list[str] = []
            inherited_emotion = str(scene.get("narrator_emotion") or "").strip()
            if not str(item.get("emotion") or "").strip() and (
                is_dialogue or not inherited_emotion
            ):
                invalid_fields.append("emotion")
            try:
                speed = float(
                    item["speed"]
                    if "speed" in item
                    else (
                        None
                        if is_dialogue
                        else scene.get("narrator_pace")
                    )
                )
                valid_speed = 0.5 <= speed <= 2.0
            except (KeyError, TypeError, ValueError):
                valid_speed = False
            if not valid_speed:
                invalid_fields.append("speed")
            if invalid_fields:
                fragment_id = id_offset + fragment_index
                issues.append(
                    DeliveryIssue(
                        fragment_index=fragment_index,
                        fragment_id=fragment_id,
                        fields=tuple(invalid_fields),
                        message=(
                            f"Fragment {fragment_id} has invalid required delivery "
                            f"field(s): {', '.join(invalid_fields)}"
                        ),
                    )
                )
        return issues

    def _retry_delivery_batch(
        self,
        issues: list[DeliveryIssue],
        raw: dict[str, Any],
        fragments: list[SourceFragment],
        request_metrics: list[dict[str, Any]],
        *,
        id_offset: int,
        round_number: int,
    ) -> dict[int, dict[str, Any]]:
        """Repair only omitted/invalid delivery decisions in one bounded call."""
        if not issues:
            return {}
        target_indexes = {issue.fragment_index for issue in issues}
        context_indexes: set[int] = set()
        for target in target_indexes:
            context_indexes.update(
                range(max(0, target - 6), min(len(fragments), target + 7))
            )
        metadata_map = self._metadata_line_map(raw)
        context = [
            {
                "local_id": index,
                "text": fragments[index].text,
                "dialogue": self._is_dialogue_fragment(fragments[index].text),
                "current_emotion": metadata_map[index].get("emotion"),
                "current_speed": metadata_map[index].get("speed"),
            }
            for index in sorted(context_indexes)
        ]
        targets = [
            {
                "local_id": issue.fragment_index,
                "chapter_fragment_id": issue.fragment_id,
                "required_fields": list(issue.fields),
            }
            for issue in issues
        ]
        retry_instruction = (
            " This is the final focused attempt: be especially strict about "
            "returning every requested field with a valid value."
            if round_number > 1
            else ""
        )
        focused_prompt = (
            "Repair only the listed missing or invalid audiobook delivery "
            "fields. Infer emotion and pacing from the complete local source "
            "context so delivery remains coherent. Return exactly one row per "
            "listed local_id and no other rows. Each row must contain id plus "
            "every field named in required_fields: emotion must be a concise, "
            "specific delivery direction and speed must be a number from 0.5 "
            "through 2.0. Do not return speaker fields or rewrite source text. "
            "Use local_id as id; chapter_fragment_id is informational only."
            + retry_instruction
            + "\n\nTargets:\n"
            + json.dumps(targets, indent=2)
            + "\n\nLocal context:\n"
            + json.dumps(context, indent=2)
        )
        request_started = time.perf_counter()
        request_succeeded = False
        try:
            response = self.ollama.generate_json(
                focused_prompt,
                temperature=0.1,
                system=(
                    "You are a strict audiobook delivery-metadata repairer. "
                    "Output only JSON with a lines array. Preserve meaning and "
                    "prior valid metadata."
                ),
            )
            request_succeeded = True
        finally:
            request_metrics.append(
                {
                    "request_kind": "focused_delivery_batch",
                    "round": round_number,
                    "fragment_ids": sorted(issue.fragment_id for issue in issues),
                    "wall_seconds": round(
                        time.perf_counter() - request_started,
                        6,
                    ),
                    "success": request_succeeded,
                    "ollama": dict(
                        getattr(self.ollama, "last_generation_metrics", {}) or {}
                    ),
                }
            )
        response_lines = response.get("lines") if isinstance(response, dict) else None
        if not isinstance(response_lines, list):
            raise ValueError("Focused delivery response must contain a lines array")
        issue_map = {issue.fragment_index: issue for issue in issues}
        replacements: dict[int, dict[str, Any]] = {}
        for item in response_lines:
            if not isinstance(item, dict):
                raise ValueError("Focused delivery response line is invalid")
            try:
                item_id = int(item.get("id"))
            except (TypeError, ValueError) as exc:
                raise ValueError("Focused delivery response has an invalid id") from exc
            if item_id not in target_indexes or item_id in replacements:
                raise ValueError(
                    f"Focused delivery returned an unexpected or duplicate id {item_id}"
                )
            issue = issue_map[item_id]
            missing = [field for field in issue.fields if field not in item]
            if missing:
                raise ValueError(
                    f"Focused delivery omitted required fields for {item_id}: {missing}"
                )
            replacements[item_id] = {
                field: item[field] for field in issue.fields
            }
        if set(replacements) != target_indexes:
            raise ValueError(
                "Focused delivery response IDs differ from requested IDs; "
                f"expected={sorted(target_indexes)}, received={sorted(replacements)}"
            )
        return replacements

    @staticmethod
    def _standard_pause_after_ms(
        emotion: str,
        speed: float,
        *,
        is_dialogue: bool,
    ) -> int:
        """Derive only the routine pacing values formerly repeated in JSON."""
        value = emotion.casefold()
        if speed >= 1.12 or any(
            term in value
            for term in (
                "urgent", "panic", "shout", "angry", "demand", "action",
                "terrified", "breathless urgency",
            )
        ):
            return 250
        if any(
            term in value
            for term in ("whisper", "secret", "hushed", "conspiratorial")
        ):
            return 600
        if speed <= 0.9 or any(
            term in value
            for term in (
                "somber", "weary", "reflective", "nostalgia", "sad",
                "contemplation", "solemn",
            )
        ):
            return 700
        return 400 if is_dialogue else 500

    @classmethod
    def _inflate_dialogue_focused_metadata(
        cls,
        raw: dict[str, Any],
        fragments: list[SourceFragment],
    ) -> dict[str, int]:
        """Expand schema-v5 sparse rows without inventing creative decisions.

        Dialogue rows are never derivable and therefore remain mandatory.
        Missing narration rows are safe to reconstruct because scene starts and
        narrator defaults are explicit model outputs.
        """
        if not isinstance(raw, dict):
            raise ValueError("LLM metadata response must be an object")
        scenes = raw.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise ValueError("Dialogue-focused metadata requires scenes")

        scene_starts: list[int] = []
        for scene_index, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                raise ValueError(f"Scene {scene_index} must be an object")
            try:
                start_id = int(scene["start_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Scene {scene_index} requires a valid start_id"
                ) from exc
            if start_id < 0 or start_id >= len(fragments):
                raise ValueError(
                    f"Scene {scene_index} start_id is outside the fragment range"
                )
            if scene_starts and start_id <= scene_starts[-1]:
                raise ValueError("Scene start_id values must be strictly increasing")
            scene_starts.append(start_id)
        if scene_starts[0] != 0:
            raise ValueError("The first scene start_id must be 0")

        raw_lines = raw.get("lines")
        if not isinstance(raw_lines, list):
            raise ValueError("LLM response has no lines array")
        line_map: dict[int, dict[str, Any]] = {}
        for item in raw_lines:
            if not isinstance(item, dict):
                raise ValueError("Every sparse metadata row must be an object")
            try:
                fragment_id = int(item["id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Every sparse metadata row requires an integer id") from exc
            if fragment_id < 0 or fragment_id >= len(fragments):
                raise ValueError(f"Fragment id {fragment_id} is outside the batch")
            if fragment_id in line_map:
                raise ValueError(f"Duplicate fragment id {fragment_id}")
            line_map[fragment_id] = item

        dialogue_ids = {
            index
            for index, fragment in enumerate(fragments)
            if cls._is_dialogue_fragment(fragment.text)
        }
        missing_dialogue = sorted(dialogue_ids - set(line_map))
        if missing_dialogue:
            raise ValueError(
                "Dialogue-focused metadata omitted mandatory dialogue IDs: "
                f"{missing_dialogue}"
            )

        scene_index = 0
        synthesized = 0
        for fragment_id in range(len(fragments)):
            while (
                scene_index + 1 < len(scene_starts)
                and fragment_id >= scene_starts[scene_index + 1]
            ):
                scene_index += 1
            item = line_map.get(fragment_id)
            if item is None:
                item = {"id": fragment_id}
                line_map[fragment_id] = item
                synthesized += 1
            explicit_scene = item.get("scene_index")
            if explicit_scene is not None and int(explicit_scene) != scene_index:
                raise ValueError(
                    f"Fragment {fragment_id} scene_index contradicts scene start_id"
                )
            item["scene_index"] = scene_index

        for scene in scenes:
            scene.pop("start_id", None)
        raw["lines"] = [line_map[index] for index in range(len(fragments))]
        return {
            "received_rows": len(raw_lines),
            "synthesized_narration_rows": synthesized,
            "dialogue_rows": len(dialogue_ids),
            "canonical_rows": len(fragments),
        }

    @classmethod
    def _expand_compact_metadata(
        cls,
        raw: dict[str, Any],
        fragments: list[SourceFragment],
    ) -> dict[str, Any]:
        """Restore derivable fields while refusing to default creative intent.

        Speaker decisions for dialogue plus emotion and speed remain model
        outputs. Only structurally known narration ownership, ordinary spoken
        classification, repeated scene indexes, and routine pauses are filled
        here.
        """
        if not isinstance(raw, dict):
            raise ValueError("LLM metadata response must be an object")
        received_characters = len(
            json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        )
        raw_lines = raw.get("lines")
        if not isinstance(raw_lines, list):
            raise ValueError("LLM response has no lines array")
        cls._validate_metadata_ids(raw, len(fragments))

        current_scene = 0
        for item in sorted(raw_lines, key=lambda row: int(row["id"])):
            fragment_index = int(item["id"])

            if "scene_index" in item:
                try:
                    current_scene = int(item["scene_index"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"Fragment {fragment_index} has an invalid scene_index"
                    ) from exc
                if current_scene < 0:
                    raise ValueError(
                        f"Fragment {fragment_index} has a negative scene_index"
                    )
            item["scene_index"] = current_scene

            scenes = raw.get("scenes") or []
            scene = (
                scenes[current_scene]
                if 0 <= current_scene < len(scenes)
                and isinstance(scenes[current_scene], dict)
                else {}
            )
            is_dialogue = cls._is_dialogue_fragment(
                fragments[fragment_index].text
            )
            emotion = str(
                item.get("emotion")
                or (
                    ""
                    if is_dialogue
                    else scene.get("narrator_emotion") or ""
                )
            ).strip()
            if not emotion:
                raise ValueError(
                    f"Fragment {fragment_index} is missing required emotion"
                )
            item["emotion"] = emotion[:200]
            try:
                speed = float(
                    item["speed"]
                    if "speed" in item
                    else (
                        None
                        if is_dialogue
                        else scene.get("narrator_pace")
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Fragment {fragment_index} is missing valid required speed"
                ) from exc
            if not 0.5 <= speed <= 2.0:
                raise ValueError(
                    f"Fragment {fragment_index} speed is outside 0.5-2.0"
                )
            item["speed"] = speed

            if not is_dialogue:
                # These values are facts of the source-fragment structure, not
                # creative decisions worth spending model tokens on.
                item["speaker"] = "narrator"
                item["speaker_confidence"] = None
                item["speaker_evidence"] = ""
                item["dialogue_kind"] = None
            else:
                speaker = cls._normalize_speaker_id(item.get("speaker"))
                if speaker != "narrator" and not item.get("dialogue_kind"):
                    item["dialogue_kind"] = "spoken"

            item.setdefault("pause_before_ms", 0)
            item.setdefault(
                "pause_after_ms",
                cls._standard_pause_after_ms(
                    emotion,
                    speed,
                    is_dialogue=is_dialogue,
                ),
            )

        canonical_characters = len(
            json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        )
        savings_ratio = (
            max(0.0, 1.0 - received_characters / canonical_characters)
            if canonical_characters
            else 0.0
        )
        return {
            "received_characters": received_characters,
            "canonical_characters": canonical_characters,
            "character_savings": max(
                0, canonical_characters - received_characters
            ),
            "character_savings_ratio": round(savings_ratio, 6),
        }

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
        *,
        fragments: list[SourceFragment] | None = None,
        registry: CharacterRegistry | None = None,
        allowed_speakers: set[str] | None = None,
    ) -> int:
        repairs = 0
        repaired_fragments: set[int] = set()
        metadata_map = ScriptGenerator._metadata_line_map(raw)
        for issue in issues:
            if issue.fragment_index in repaired_fragments:
                continue
            target_speaker = issue.exact_speaker
            target_kind = issue.exact_dialogue_kind

            if target_speaker is None and fragments is not None and registry is not None:
                if issue.kind in {"gender_contradiction", "pronoun_gender", "generic_gender"}:
                    req_gender = None
                    if "male" in issue.message.lower():
                        req_gender = Gender.MALE
                    elif "female" in issue.message.lower():
                        req_gender = Gender.FEMALE
                    resolved = ScriptGenerator._resolve_dialogue_speaker(
                        issue.fragment_index,
                        fragments,
                        metadata_map,
                        allowed_speakers or set(registry.characters.keys()),
                        registry=registry,
                        target_gender=req_gender,
                    )
                    if resolved and resolved != "narrator":
                        target_speaker = resolved
                        target_kind = "spoken"

            if target_speaker is None:
                continue

            ScriptGenerator._replace_metadata_line(
                raw,
                issue.fragment_index,
                {
                    "speaker": target_speaker,
                    "speaker_confidence": 0.99,
                    "speaker_evidence": (
                        "Deterministic correction from explicit adjacent source evidence "
                        f"({issue.kind})."
                    ),
                    "attribution_review_required": False,
                    "attribution_review_reason": "",
                    "dialogue_kind": (
                        target_kind
                        or (
                            "spoken"
                            if target_speaker != "narrator"
                            else None
                        )
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
        start = max(0, issue.fragment_index - 12)
        end = min(len(fragments), issue.fragment_index + 13)
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
            "Use explicit dialogue tags, aliases, and the complete bounded "
            "conversation block, including untagged alternating turns. Never "
            "select from gender or proximity alone. Classify dialogue_kind as "
            "spoken; as non_spoken_quote only when the source explicitly "
            "shows that nobody voices the quotation; or as "
            "reported_collective_speech with narrator only when an adjacent "
            "source tag identifies an anonymous plural group. Include only id, speaker, "
            "speaker_confidence, speaker_evidence, and dialogue_kind. The "
            "existing delivery metadata will be preserved.\n"
            "CRITICAL: If the rejected metadata reason says you assigned spoken dialogue to the narrator, YOU MUST pick a character. The narrator cannot speak dialogue!\n\n"
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
                "dialogue_kind",
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

    def _retry_attribution_batch(
        self,
        issues: list[AttributionIssue],
        raw: dict[str, Any],
        fragments: list[SourceFragment],
        allowed_speakers: set[str],
        registry: CharacterRegistry,
        character_summary: str,
        request_metrics: list[dict[str, Any]],
        *,
        id_offset: int,
        strict_spoken: bool = False,
    ) -> dict[int, dict[str, Any]]:
        """Correct all suspect turns in one bounded conversation-aware call."""
        if not issues:
            return {}
        target_indexes = {issue.fragment_index for issue in issues}
        global_to_local = {
            issue.fragment_id: issue.fragment_index for issue in issues
        }
        context_indexes: set[int] = set()
        for target in target_indexes:
            context_indexes.update(
                range(max(0, target - 12), min(len(fragments), target + 13))
            )
        context = [
            {
                "local_id": index,
                "chapter_fragment_id": id_offset + index,
                "text": fragments[index].text,
                "dialogue": self._is_dialogue_fragment(fragments[index].text),
                "current_speaker": self._metadata_line_map(raw)
                .get(index, {})
                .get("speaker"),
            }
            for index in sorted(context_indexes)
        ]
        reasons = []
        for fragment_index in sorted(target_indexes):
            fragment_issues = [
                issue for issue in issues
                if issue.fragment_index == fragment_index
            ]
            reasons.append(
                {
                    "local_id": fragment_index,
                    "chapter_fragment_id": id_offset + fragment_index,
                    "reasons": [issue.message for issue in fragment_issues],
                }
            )
        example_id = min(target_indexes)
        focused_prompt = (
            "Correct speaker metadata only for the listed suspect audiobook "
            "fragments. Return one row for every listed local_id and no other "
            "rows. Copy local_id into the response field named id. The "
            "chapter_fragment_id is informational only and MUST NOT be returned "
            "as id. Evaluate each complete local conversation, "
            "including untagged alternating turns. Use explicit tags, aliases, "
            "turn continuity, and source context; never select from gender or "
            "mere proximity. Classify dialogue_kind as spoken; as "
            "non_spoken_quote only when the source explicitly shows that nobody "
            "voices it; or as reported_collective_speech with narrator only "
            "when an adjacent source tag identifies an anonymous plural group. "
            "Each row may contain only id, speaker, "
            "speaker_confidence, speaker_evidence, and dialogue_kind. The id "
            "MUST be the integer local_id from the suspect list; it "
            "is never a speaker name. Output exactly this JSON shape: "
            f'{{"lines":[{{"id":{example_id},"speaker":"character_id",'
            '"speaker_confidence":0.95,"speaker_evidence":"source cue",'
            '"dialogue_kind":"spoken"}]}. Do not use an object keyed by '
            "speaker, and do not swap id with speaker.\n\n"
            f"Allowed speaker IDs: {', '.join(sorted(allowed_speakers))}\n"
            f"Suspect fragments:\n{json.dumps(reasons, indent=2)}\n\n"
            f"Conversation context:\n{json.dumps(context, indent=2)}"
        )
        if strict_spoken:
            non_narrator_speakers = sorted(
                speaker for speaker in allowed_speakers if speaker != "narrator"
            )
            focused_prompt += (
                "\n\nSTRICT SPOKEN-DIALOGUE CORRECTION: Source validation has "
                "already established that every suspect is a voiced character "
                "turn. 'narrator', non_spoken_quote, and "
                "reported_collective_speech are forbidden. Choose the best "
                "source-grounded non-narrator speaker, keep confidence honest, "
                "and cite the local continuity or tag evidence. Allowed "
                "non-narrator IDs: "
                + ", ".join(non_narrator_speakers)
            )
        focused_system = (
            "You are a strict audiobook speaker-attribution corrector. Output "
            "only JSON with a lines array. Do not rewrite source text or invent "
            "speakers.\n\n"
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
                    "request_kind": (
                        "strict_spoken_attribution"
                        if strict_spoken
                        else (
                            "focused_fragment"
                            if len(target_indexes) == 1
                            else "focused_attribution_batch"
                        )
                    ),
                    "fragment_ids": sorted(
                        issue.fragment_id for issue in issues
                    ),
                    "wall_seconds": round(
                        time.perf_counter() - request_started,
                        6,
                    ),
                    "success": request_succeeded,
                    "ollama": (
                        dict(
                            getattr(self.ollama, "last_generation_metrics", {})
                            or {}
                        )
                    ),
                }
            )
        response_lines = response.get("lines") if isinstance(response, dict) else None
        if not isinstance(response_lines, list):
            raise ValueError("Focused attribution response must contain a lines array")
        replacements: dict[int, dict[str, Any]] = {}
        allowed_fields = {
            "id", "speaker", "speaker_confidence", "speaker_evidence", "dialogue_kind"
        }
        for item in response_lines:
            if not isinstance(item, dict):
                raise ValueError("Focused attribution response line is invalid")
            try:
                item_id = int(item.get("id"))
            except (TypeError, ValueError) as exc:
                raise ValueError("Focused attribution response has an invalid id") from exc
            # The prompt contract is batch-local. Older prompts exposed the
            # chapter-global ID too prominently, however, and some models copy
            # that value. Accept it defensively and canonicalize before any
            # metadata is replaced.
            if item_id in target_indexes:
                local_id = item_id
            elif item_id in global_to_local:
                local_id = global_to_local[item_id]
            else:
                raise ValueError(
                    "Focused attribution returned an unknown fragment id "
                    f"{item_id}"
                )
            if local_id in replacements:
                raise ValueError(
                    "Focused attribution duplicated fragment id "
                    f"{local_id}"
                )
            replacements[local_id] = {
                key: value for key, value in item.items() if key in allowed_fields
            }
            replacements[local_id]["id"] = local_id
        if set(replacements) != target_indexes:
            raise ValueError(
                "Focused attribution response IDs differ from requested IDs; "
                f"expected={sorted(target_indexes)}, received={sorted(replacements)}"
            )

        return replacements

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
        review_required = False
        review_reason = ""
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
                inferred = ScriptGenerator._resolve_dialogue_speaker(
                    fragment_index,
                    fragments,
                    ScriptGenerator._metadata_line_map(raw),
                    allowed_speakers,
                    registry=registry,
                )
                existing = ScriptGenerator._metadata_line_map(raw).get(
                    fragment_index, {}
                )
                existing_speaker = ScriptGenerator._normalize_speaker_id(
                    existing.get("speaker", "narrator")
                )
                if existing_speaker in allowed_speakers and existing_speaker != "narrator":
                    speaker = existing_speaker
                elif inferred in allowed_speakers and inferred != "narrator":
                    speaker = inferred
                else:
                    speaker = "narrator"
                confidence = 0.25 if speaker != "narrator" else 0.0
                evidence = f"Unresolved dialogue retained as a review candidate ({speaker})."
                review_required = True
                review_reason = (
                    "No unique explicit source tag identified the speaker; confirm this "
                    "dialogue turn before audio generation."
                )

        existing = ScriptGenerator._metadata_line_map(raw).get(fragment_index, {})
        return {
            "id": fragment_index,
            "speaker": speaker,
            "speaker_confidence": confidence,
            "speaker_evidence": evidence,
            "attribution_review_required": review_required,
            "attribution_review_reason": review_reason,
            "dialogue_kind": ("spoken" if speaker != "narrator" else None),
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
        *,
        allow_character_discovery: bool = False,
    ) -> ScriptChapter:
        """Process complete source fragments in non-overlapping batches."""
        fragments = self._split_into_fragment_spans(chapter.text)
        all_lines: list[ScriptLine] = []
        all_scenes = []
        summaries: list[str] = []
        chunks = self._chunk_fragments(fragments)
        chapter_speakers = self._get_chapter_scoped_speakers(
            chapter.text,
            registry,
        )

        offset = 0
        prior_turn_context: list[dict[str, Any]] = []
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
                allow_character_discovery=allow_character_discovery,
                allowed_speakers=chapter_speakers,
                chapter_text=chapter.text,
                prior_turn_context=prior_turn_context,
            )
            all_lines.extend(chunk_script.lines)
            chapter_speakers.update(
                self._get_chapter_scoped_speakers(chapter.text, registry)
            )
            chapter_speakers.update(
                line.speaker for line in chunk_script.lines if line.speaker
            )
            if hasattr(chunk_script, 'scenes'): all_scenes.extend(chunk_script.scenes)
            if chunk_script.chapter_summary:
                summaries.append(chunk_script.chapter_summary)
            prior_turn_context = [
                {
                    "speaker": line.speaker,
                    "dialogue_kind": line.dialogue_kind,
                    "text": line.text,
                }
                for line in chunk_script.lines[-10:]
            ]
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
        """Resolve exact aliases, generic archetypes, and reject invented Pass 2 speakers."""
        known_ids = set(registry.characters.keys())
        for line in script.lines:
            spk = ScriptGenerator._normalize_speaker_id(line.speaker)
            if not spk or spk == "narrator":
                continue
            
            # 1. Check canonical speaker resolution (exact ID, aliases, display names, name variants)
            canonical = spk
            if spk not in known_ids:
                for cid, char in registry.characters.items():
                    aliases = getattr(char, "aliases", [])
                    alias_norms = [ScriptGenerator._normalize_speaker_id(a) for a in aliases]
                    char_name_norm = ScriptGenerator._normalize_speaker_id(char.name)
                    if spk in alias_norms or spk == char_name_norm:
                        canonical = cid
                        break

            # 2. If still unresolved in registry, check generic speaker aliases
            if canonical not in known_ids:
                canonical = _GENERIC_SPEAKER_ALIASES.get(canonical, canonical)

            # 3. Auto-provision standard universal generic archetypes if used
            if canonical not in known_ids and canonical in _GENERIC_SPEAKER_DEFINITIONS:
                spec = _GENERIC_SPEAKER_DEFINITIONS[canonical]
                registry.characters[canonical] = Character(
                    id=canonical,
                    name=spec["name"],
                    gender=spec["gender"],
                    age_range=spec["age_range"],
                    personality_traits=spec["personality_traits"],
                    voice_description=spec["voice_description"],
                    speaking_style=spec["speaking_style"],
                    dialogue_count=1,
                    test_sentence=spec["test_sentence"],
                )
                known_ids.add(canonical)
                logger.info(
                    "[ScriptGenerator] Auto-provisioned generic speaker archetype '%s' for Chapter %d",
                    canonical,
                    script.chapter_number,
                )
            
            if canonical != line.speaker:
                line.speaker = canonical

            if canonical not in known_ids:
                raise ValueError(
                    f"Chapter {script.chapter_number} contains unknown speaker "
                    f"'{spk}'; Pass 2 may not create cast members"
                )

    @staticmethod
    def sync_dialogue_counts(
        scripts: list[ScriptChapter],
        registry: CharacterRegistry,
    ) -> None:
        """Synchronize character registry dialogue counts with actual script lines."""
        for character in registry.characters.values():
            character.dialogue_count = 0
        for script in scripts:
            for line in script.lines:
                speaker = ScriptGenerator._normalize_speaker_id(line.speaker)
                if speaker != "narrator" and speaker in registry.characters:
                    registry.characters[speaker].dialogue_count += 1

    @staticmethod
    def remap_reconciled_speakers(
        scripts: list[ScriptChapter],
        registry: CharacterRegistry,
        remap: dict[str, str],
    ) -> None:
        """Apply conservative identity reconciliation to completed scripts."""
        for character in registry.characters.values():
            character.dialogue_count = 0
        for script in scripts:
            for line in script.lines:
                original = ScriptGenerator._normalize_speaker_id(line.speaker)
                speaker = remap.get(original, original)
                if speaker not in registry.characters:
                    raise ValueError(
                        f"Chapter {script.chapter_number} retains unknown reconciled "
                        f"speaker '{speaker}'"
                    )
                line.speaker = speaker
                if speaker != "narrator":
                    registry.characters[speaker].dialogue_count += 1

    def _apply_joint_character_updates(
        self,
        raw: dict[str, Any],
        fragments: list[SourceFragment],
        registry: CharacterRegistry,
    ) -> None:
        """Admit only source-evidenced speakers discovered by the joint pass."""
        updates = raw.get("character_updates", [])
        if not isinstance(updates, list):
            raise ValueError("character_updates must be an array in joint mode")
        line_map = self._metadata_line_map(raw)
        dialogue_by_speaker: dict[str, list[int]] = {}
        for index, item in line_map.items():
            if index < 0 or index >= len(fragments):
                continue
            if not self._is_dialogue_fragment(fragments[index].text):
                continue
            speaker = self._normalize_speaker_id(item.get("speaker"))
            if speaker != "narrator":
                dialogue_by_speaker.setdefault(speaker, []).append(index)

        for update in updates:
            if not isinstance(update, dict):
                raise ValueError("character_updates contains a non-object entry")
            character_id = self._normalize_speaker_id(
                update.get("character_id") or update.get("id")
            )
            if character_id == "narrator":
                continue
            confidence = max(
                0.0,
                min(1.0, float(update.get("discovery_confidence", 0.0) or 0.0)),
            )
            evidence_ids: list[int] = []
            for value in update.get("evidence_fragment_ids", []):
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    continue
                if 0 <= parsed < len(fragments):
                    evidence_ids.append(parsed)

            name = str(
                update.get("name") or character_id.replace("_", " ").title()
            ).strip()[:160]
            raw_aliases = update.get("aliases", [])
            if isinstance(raw_aliases, str):
                raw_aliases = [raw_aliases]
            aliases = [
                str(value).strip()[:160]
                for value in raw_aliases
                if str(value).strip()
            ][:20]
            identity_terms = [name, character_id.replace("_", " "), *aliases]
            identity_terms = [
                term for term in identity_terms
                if len(term) >= 3
                and self._normalize_speaker_id(term) not in _UNSAFE_SPEAKER_ALIASES
            ]
            assigned_dialogue = dialogue_by_speaker.get(character_id, [])
            evidence_excerpts: list[str] = []
            for evidence_id in evidence_ids:
                start = max(0, evidence_id - 1)
                end = min(len(fragments), evidence_id + 2)
                context = " ".join(
                    fragment.text for fragment in fragments[start:end]
                ).strip()
                context_folded = context.casefold()
                identity_present = any(
                    re.search(
                        rf"(?<!\w){re.escape(term.casefold())}(?!\w)",
                        context_folded,
                    )
                    for term in identity_terms
                )
                dialogue_nearby = any(
                    abs(dialogue_id - evidence_id) <= 1
                    for dialogue_id in assigned_dialogue
                )
                if identity_present and dialogue_nearby:
                    evidence_excerpts.append(context[:500])

            existing = registry.characters.get(character_id)
            if existing is None and (
                confidence < self.speaker_confidence_threshold
                or not assigned_dialogue
                or not evidence_excerpts
            ):
                logger.warning(
                    "[JointDirector] Rejected unsupported speaker discovery '%s' "
                    "(confidence=%.2f, dialogue=%d, evidence=%d)",
                    character_id,
                    confidence,
                    len(assigned_dialogue),
                    len(evidence_excerpts),
                )
                continue

            evidence_text = " ".join(evidence_excerpts).casefold()
            aliases = [
                alias for alias in aliases
                if re.search(
                    rf"(?<!\w){re.escape(alias.casefold())}(?!\w)",
                    evidence_text,
                )
            ]

            gender_value = str(update.get("gender", "other")).casefold()
            if gender_value in {"male", "man", "boy"}:
                gender = Gender.MALE
            elif gender_value in {"female", "woman", "girl"}:
                gender = Gender.FEMALE
            else:
                gender = Gender.OTHER
            voice_description = str(update.get("voice_description", "")).strip()
            if not voice_description:
                voice_description = (
                    f"{gender.value} speaker, unknown adult age. medium pitch, "
                    "moderate volume, measured speed. neutral accent, clear texture, "
                    "high clarity, natural fluency, neutral emotion and tone."
                )

            if existing is None:
                registry.characters[character_id] = Character(
                    id=character_id,
                    name=name,
                    gender=gender,
                    age_range=str(update.get("age_range", "unknown"))[:80],
                    personality_traits=[
                        str(value)[:100]
                        for value in update.get("personality_traits", [])
                        if str(value).strip()
                    ][:12],
                    aliases=aliases,
                    voice_description=voice_description[:1000],
                    speaking_style=str(update.get("speaking_style", ""))[:500],
                    test_sentence=(
                        str(update.get("test_sentence"))[:500]
                        if update.get("test_sentence")
                        else None
                    ),
                    dialogue_count=len(assigned_dialogue),
                    discovered_in_pass2=True,
                    discovery_confidence=confidence,
                    discovery_evidence=evidence_excerpts[:20],
                )
                logger.info(
                    "[JointDirector] Registered source-evidenced speaker '%s' "
                    "(confidence=%.2f)",
                    character_id,
                    confidence,
                )
                continue

            existing.dialogue_count += len(assigned_dialogue)
            existing.discovery_confidence = max(
                existing.discovery_confidence or 0.0,
                confidence,
            )
            existing.discovery_evidence = list(dict.fromkeys(
                [*existing.discovery_evidence, *evidence_excerpts]
            ))[:20]
            if len(voice_description) > len(existing.voice_description):
                existing.voice_description = voice_description[:1000]
            if len(str(update.get("speaking_style", ""))) > len(existing.speaking_style):
                existing.speaking_style = str(update.get("speaking_style"))[:500]
            existing.aliases = list(dict.fromkeys([*existing.aliases, *aliases]))[:20]
            if existing.gender == Gender.OTHER and gender in {Gender.MALE, Gender.FEMALE}:
                existing.gender = gender

    @staticmethod
    def _get_chapter_scoped_speakers(
        chapter_text: str,
        registry: CharacterRegistry,
        *,
        fallback_all: bool = True,
    ) -> set[str]:
        """Return the set of valid speaker IDs present or mentioned in this chapter, plus universal generics."""
        generics = {
            "narrator",
            "minor_male",
            "minor_female",
            "child_male",
            "child_female",
            "crowd",
            "collective",
            "character_male",
            "character_female",
        }
        ch_lower = chapter_text.lower()
        active = set(generics)

        for char_id, char in registry.characters.items():
            if char_id in generics:
                active.add(char_id)
                continue
            if char.name and re.search(rf"\b{re.escape(char.name.strip().lower())}\b", ch_lower):
                active.add(char_id)
                continue
            name_parts = [
                p.lower()
                for p in (char.name or "").split()
                if len(p) >= 2 and p.lower() not in _PRONOUN_STOPWORDS
            ]
            if any(re.search(rf"\b{re.escape(part)}\b", ch_lower) for part in name_parts):
                active.add(char_id)
                continue
            id_parts = [
                p.lower()
                for p in char_id.split("_")
                if len(p) >= 2 and p not in _PRONOUN_STOPWORDS
            ]
            if any(re.search(rf"\b{re.escape(part)}\b", ch_lower) for part in id_parts):
                active.add(char_id)
                continue
            for alias in char.aliases:
                alias_clean = alias.strip().lower()
                if len(alias_clean) >= 2 and alias_clean not in _PRONOUN_STOPWORDS:
                    if re.search(rf"\b{re.escape(alias_clean)}\b", ch_lower):
                        active.add(char_id)
                        break

        # If no named characters matched, allow all registry characters
        if fallback_all and len(active - generics) == 0:
            return set(registry.characters)

        return active

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
        chapter_text: str | None = None,
    ) -> None:
        """Reject invalid dialogue metadata with structured source evidence."""
        issues = ScriptGenerator._collect_metadata_speaker_issues(
            raw,
            fragments,
            allowed_speakers,
            registry=registry,
            id_offset=id_offset,
            confidence_threshold=confidence_threshold,
            chapter_text=chapter_text,
        )
        if issues:
            raise MetadataAttributionError(issues)

    @staticmethod
    def _group_fragments_by_paragraph(
        fragments: list[SourceFragment],
        chapter_text: str | None = None,
    ) -> list[list[int]]:
        """Group fragment indices by paragraph boundaries."""
        if not fragments:
            return []
        groups: list[list[int]] = []
        current: list[int] = [0]
        for idx in range(1, len(fragments)):
            prev_frag = fragments[idx - 1]
            curr_frag = fragments[idx]
            is_new_para = False
            if chapter_text is not None and hasattr(prev_frag, "end") and hasattr(curr_frag, "start"):
                between = chapter_text[prev_frag.end : curr_frag.start]
                if "\n" in between:
                    is_new_para = True
            if not is_new_para:
                if "\n" in curr_frag.text.lstrip() or prev_frag.text.rstrip().endswith("\n"):
                    is_new_para = True
            if is_new_para:
                groups.append(current)
                current = [idx]
            else:
                current.append(idx)
        if current:
            groups.append(current)
        return groups

    @staticmethod
    def _paragraph_attribution_maps(
        fragments: list[SourceFragment],
        registry: CharacterRegistry | None,
        chapter_text: str | None = None,
    ) -> tuple[
        dict[int, list[int]],
        dict[int, tuple[str | None, str | None, Gender | None]],
    ]:
        """Map dialogue turns to paragraph-local explicit speaker evidence."""
        dialogue_map: dict[int, list[int]] = {}
        tag_map: dict[int, tuple[str | None, str | None, Gender | None]] = {}
        for group in ScriptGenerator._group_fragments_by_paragraph(
            fragments, chapter_text
        ):
            dialogue_indexes = [
                index
                for index in group
                if ScriptGenerator._is_dialogue_fragment(fragments[index].text)
            ]
            for index in dialogue_indexes:
                dialogue_map[index] = dialogue_indexes

            for position, index in enumerate(group):
                text = fragments[index].text
                if (
                    ScriptGenerator._is_dialogue_fragment(text)
                    or not ScriptGenerator._is_pure_dialogue_tag(text)
                ):
                    continue
                exact, kind, gender = ScriptGenerator._dialogue_tag_evidence(
                    text, registry
                )
                if exact is None and gender is None:
                    continue
                previous_index = group[position - 1] if position > 0 else None
                next_index = (
                    group[position + 1]
                    if position + 1 < len(group)
                    else None
                )
                previous_is_dialogue = (
                    previous_index is not None
                    and ScriptGenerator._is_dialogue_fragment(
                        fragments[previous_index].text
                    )
                )
                next_is_dialogue = (
                    next_index is not None
                    and ScriptGenerator._is_dialogue_fragment(
                        fragments[next_index].text
                    )
                )
                if previous_is_dialogue and previous_index not in tag_map:
                    tag_map[previous_index] = (exact, kind, gender)
                # A named or pronoun tag between two quotes in one paragraph carries
                # across a split turn even when the tag ends with a period:
                # "First half," A said / he said. "Second half."
                if next_is_dialogue and (
                    previous_is_dialogue
                    or text.strip().endswith((",", ":"))
                ):
                    tag_map[next_index] = (exact, kind, gender)
        return dialogue_map, tag_map

    @staticmethod
    def _collect_metadata_speaker_issues(
        raw: dict[str, Any],
        fragments: list[SourceFragment],
        allowed_speakers: set[str],
        *,
        registry: CharacterRegistry | None = None,
        id_offset: int = 0,
        confidence_threshold: float = 0.55,
        chapter_text: str | None = None,
    ) -> list[AttributionIssue]:
        """Return at most one highest-priority attribution issue per dialogue."""
        metadata_map = {
            int(item["id"]): item
            for item in raw.get("lines", [])
            if isinstance(item, dict) and "id" in item
        }
        issues: list[AttributionIssue] = []

        para_dialogue_map, para_tag_map = (
            ScriptGenerator._paragraph_attribution_maps(
                fragments, registry, chapter_text
            )
        )

        for i, fragment in enumerate(fragments):
            if not ScriptGenerator._is_dialogue_fragment(fragment.text):
                continue
            speaker = ScriptGenerator._normalize_speaker_id(
                metadata_map.get(i, {}).get("speaker", "narrator")
            )
            fragment_id = id_offset + i
            dialogue_kind = str(
                metadata_map.get(i, {}).get("dialogue_kind") or ""
            ).strip().casefold()
            # Backward-compatible inference for previously valid character
            # dialogue. Narrator-owned quotations remain deliberately
            # unclassified and therefore require focused review.
            if not dialogue_kind and speaker != "narrator":
                dialogue_kind = "spoken"
            next_text = fragments[i + 1].text if i + 1 < len(fragments) else ""
            prev_text = fragments[i - 1].text if i > 0 else ""
            tag_text = (
                next_text
                if ScriptGenerator._is_pure_dialogue_tag(next_text)
                else (
                    prev_text
                    if ScriptGenerator._is_leading_dialogue_tag(prev_text)
                    else ""
                )
            )
            exact_speaker: str | None = None
            evidence_kind: str | None = None
            evidence_gender: Gender | None = None
            if tag_text:
                exact_speaker, evidence_kind, evidence_gender = (
                    ScriptGenerator._dialogue_tag_evidence(tag_text, registry)
                )
            if exact_speaker is None and i in para_tag_map:
                para_exact, para_kind, para_gender = para_tag_map[i]
                if para_exact is not None or para_gender is not None:
                    exact_speaker = exact_speaker or para_exact
                    evidence_kind = evidence_kind or para_kind
                    evidence_gender = evidence_gender or para_gender

            collective_tag = ScriptGenerator._is_collective_dialogue_tag(tag_text)
            embedded_term = ScriptGenerator._is_embedded_quoted_term(i, fragments)

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

            if embedded_term and (
                speaker != "narrator"
                or dialogue_kind != "non_spoken_quote"
                or len(
                    str(metadata_map.get(i, {}).get("speaker_evidence") or "").strip()
                ) < 12
            ):
                issues.append(
                    AttributionIssue(
                        kind="embedded_quoted_term",
                        fragment_index=i,
                        fragment_id=fragment_id,
                        submitted_speaker=speaker,
                        exact_speaker="narrator",
                        exact_dialogue_kind="non_spoken_quote",
                        message=(
                            f"Fragment {fragment_id} is a short quoted term "
                            "embedded grammatically in narration, not a spoken turn"
                        ),
                    )
                )
                continue

            if collective_tag and (
                speaker != "narrator"
                or dialogue_kind != "reported_collective_speech"
                or len(
                    str(metadata_map.get(i, {}).get("speaker_evidence") or "").strip()
                ) < 12
            ):
                issues.append(
                    AttributionIssue(
                        kind="collective_report_classification",
                        fragment_index=i,
                        fragment_id=fragment_id,
                        submitted_speaker=speaker,
                        exact_speaker="narrator",
                        exact_dialogue_kind="reported_collective_speech",
                        message=(
                            f"Fragment {fragment_id} is explicitly attributed "
                            "by an adjacent source tag to an anonymous plural "
                            "group and must be narrated as reported collective "
                            "speech"
                        ),
                    )
                )
                continue

            if dialogue_kind not in {
                "spoken", "non_spoken_quote", "reported_collective_speech"
            }:
                issues.append(
                    AttributionIssue(
                        kind="missing_dialogue_kind",
                        fragment_index=i,
                        fragment_id=fragment_id,
                        submitted_speaker=speaker,
                        exact_speaker=exact_speaker,
                        message=(
                            f"Fragment {fragment_id} has no valid dialogue_kind; "
                            "classify it as spoken, explicitly non-spoken, or "
                            "source-tagged reported collective speech"
                        ),
                    )
                )
                continue
            if dialogue_kind == "non_spoken_quote":
                evidence = str(
                    metadata_map.get(i, {}).get("speaker_evidence") or ""
                ).strip()
                if speaker != "narrator":
                    issues.append(
                        AttributionIssue(
                            kind="non_spoken_character_contradiction",
                            fragment_index=i,
                            fragment_id=fragment_id,
                            submitted_speaker=speaker,
                            message=(
                                f"Fragment {fragment_id} is classified as "
                                "non-spoken but assigned to '{speaker}'"
                            ),
                        )
                    )
                    continue
                if len(evidence) < 12:
                    issues.append(
                        AttributionIssue(
                            kind="unsupported_non_spoken_quote",
                            fragment_index=i,
                            fragment_id=fragment_id,
                            submitted_speaker=speaker,
                            message=(
                                f"Fragment {fragment_id} claims a non-spoken "
                                "quotation without explicit source evidence"
                            ),
                        )
                    )
                    continue
            elif dialogue_kind == "reported_collective_speech":
                evidence = str(
                    metadata_map.get(i, {}).get("speaker_evidence") or ""
                ).strip()
                if speaker != "narrator" or not collective_tag or len(evidence) < 12:
                    issues.append(
                        AttributionIssue(
                            kind="unsupported_reported_collective_speech",
                            fragment_index=i,
                            fragment_id=fragment_id,
                            submitted_speaker=speaker,
                            message=(
                                f"Fragment {fragment_id} claims reported collective "
                                "speech without an adjacent anonymous plural speech "
                                "tag, narrator ownership, and explicit evidence"
                            ),
                        )
                    )
                    continue
            elif speaker == "narrator":
                issues.append(
                    AttributionIssue(
                        kind="narrator_spoken_dialogue",
                        fragment_index=i,
                        fragment_id=fragment_id,
                        submitted_speaker=speaker,
                        exact_speaker=exact_speaker,
                        message=(
                            f"Fragment {fragment_id} is classified as spoken "
                            "dialogue but assigned to narrator"
                        ),
                    )
                )
                continue

            if collective_tag and speaker != "narrator":
                issues.append(
                    AttributionIssue(
                        kind="collective_speech_character_contradiction",
                        fragment_index=i,
                        fragment_id=fragment_id,
                        submitted_speaker=speaker,
                        message=(
                            f"Fragment {fragment_id} is attributed by its source "
                            "tag to an anonymous plural group but assigned to "
                            f"'{speaker}'"
                        ),
                    )
                )
                continue

            # ``exact_speaker`` may come from a paragraph-local split-turn tag
            # rather than the immediately adjacent fragment.  Treat that
            # deterministic evidence exactly like an adjacent tag.
            if (tag_text or exact_speaker is not None) and dialogue_kind == "spoken":
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

            evidence = str(
                metadata_map.get(i, {}).get("speaker_evidence") or ""
            ).strip()

            if dialogue_kind == "spoken" and len(evidence) < 8:
                issues.append(
                    AttributionIssue(
                        kind="missing_speaker_evidence",
                        fragment_index=i,
                        fragment_id=fragment_id,
                        submitted_speaker=speaker,
                        exact_speaker=(
                            exact_speaker if exact_speaker == speaker else None
                        ),
                        message=(
                            f"Fragment {fragment_id} has spoken dialogue without "
                            "a meaningful source-grounded speaker explanation"
                        ),
                    )
                )
                continue

            generic_evidence = any(
                phrase in evidence.casefold()
                for phrase in (
                    "context from previous fragments",
                    "conversation flow",
                    "surrounding context indicates",
                    "context indicates the speaker",
                )
            )
            named_in_evidence = False
            if registry is not None and speaker in registry.characters:
                character = registry.characters[speaker]
                identity_terms = [character.name, *character.aliases]
                named_in_evidence = any(
                    term and re.search(
                        rf"(?<!\w){re.escape(term.casefold())}(?!\w)",
                        evidence.casefold(),
                    )
                    for term in identity_terms
                )
            if (
                dialogue_kind == "spoken"
                and exact_speaker is None
                and generic_evidence
                and not named_in_evidence
            ):
                issues.append(
                    AttributionIssue(
                        kind="unsupported_speaker_evidence",
                        fragment_index=i,
                        fragment_id=fragment_id,
                        submitted_speaker=speaker,
                        message=(
                            f"Fragment {fragment_id} provides only generic "
                            "conversation context, not a concrete source cue"
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
        if registry is None or not tag_text.strip():
            return None, None, None

        tag = tag_text.casefold()
        tag_tokens = _word_tokens(tag)
        speech_positions = [
            index
            for index, token in enumerate(tag_tokens)
            if token in _SPEECH_VERB_SET
        ]
        if not speech_positions:
            return None, None, None

        pre_verbal_matches: list[tuple[int, int, str]] = []
        post_verbal_matches: list[tuple[int, int, str]] = []

        for character_id, candidate in registry.characters.items():
            if character_id == "narrator":
                continue
            names = {
                value.strip().casefold().replace("_", " ")
                for value in [character_id, candidate.name, *candidate.aliases]
                if value.strip()
                and value.strip().casefold().replace("_", " ")
                not in _UNSAFE_SPEAKER_ALIASES
            }
            for name in names:
                name_tokens = _word_tokens(name)
                if not name_tokens:
                    continue
                starts = _subsequence_starts(tag_tokens, name_tokens)
                for start in starts:
                    name_end = start + len(name_tokens)
                    for verb_index in speech_positions:
                        # Case 1: Pre-verbal subject (Candidate before SpeechVerb)
                        if 0 <= verb_index - name_end <= 3:
                            intervening = tag_tokens[name_end:verb_index]
                            if not any(t in _PREPOSITIONS_OBJECTS for t in intervening):
                                proximity = verb_index - name_end
                                pre_verbal_matches.append((proximity, len(name), character_id))
                        # Case 2: Post-verbal inverted subject (SpeechVerb before Candidate)
                        elif 0 <= start - (verb_index + 1) <= 2:
                            intervening = tag_tokens[verb_index + 1:start]
                            has_prep = any(t in _PREPOSITIONS_OBJECTS for t in intervening)
                            has_participle = any(t.endswith("ing") and len(t) > 3 for t in intervening)
                            if not has_prep and not has_participle:
                                proximity = start - (verb_index + 1)
                                post_verbal_matches.append((proximity, len(name), character_id))

        if pre_verbal_matches:
            pre_verbal_matches.sort(key=lambda m: (m[0], -m[1]))
            best_proximity = pre_verbal_matches[0][0]
            closest = [m for m in pre_verbal_matches if m[0] == best_proximity]
            max_len = max(m[1] for m in closest)
            best_speakers = {m[2] for m in closest if m[1] == max_len}
            if len(best_speakers) == 1:
                return next(iter(best_speakers)), "named_tag", None

        if post_verbal_matches:
            post_verbal_matches.sort(key=lambda m: (m[0], -m[1]))
            best_proximity = post_verbal_matches[0][0]
            closest = [m for m in post_verbal_matches if m[0] == best_proximity]
            max_len = max(m[1] for m in closest)
            best_speakers = {m[2] for m in closest if m[1] == max_len}
            if len(best_speakers) == 1:
                return next(iter(best_speakers)), "named_tag", None

        speech_verbs = _SPEECH_VERB_PATTERN
        generic_match = re.search(
            r"\b(?:the|a)\s+(boy|girl|man|woman)\b(?:\s+\w+){0,2}\s+(?:"
            + speech_verbs
            + r")\b",
            tag,
        )
        if not generic_match:
            generic_match = re.search(
                r"\b(?:" + speech_verbs + r")\s+(?:the|a)\s+(boy|girl|man|woman)\b",
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

        he_pos = re.search(
            r"\bhe\b(?:\s+\w+){0,2}\s+(?:" + speech_verbs + r")\b|(?:\b" + speech_verbs + r")\s+(?:\w+\s+){0,2}\bhe\b",
            tag,
        )
        she_pos = re.search(
            r"\bshe\b(?:\s+\w+){0,2}\s+(?:" + speech_verbs + r")\b|(?:\b" + speech_verbs + r")\s+(?:\w+\s+){0,2}\bshe\b",
            tag,
        )
        if he_pos and she_pos:
            return (None, "pronoun_gender", Gender.MALE) if he_pos.start() < she_pos.start() else (None, "pronoun_gender", Gender.FEMALE)
        if he_pos:
            return None, "pronoun_gender", Gender.MALE
        if she_pos:
            return None, "pronoun_gender", Gender.FEMALE
        if re.search(r"\bhe\b", tag) and not re.search(r"\bshe\b", tag):
            return None, "pronoun_gender", Gender.MALE
        if re.search(r"\bshe\b", tag) and not re.search(r"\bhe\b", tag):
            return None, "pronoun_gender", Gender.FEMALE
        return None, None, None

    @staticmethod
    def _resolve_dialogue_speaker(
        fragment_index: int,
        fragments: list[SourceFragment],
        metadata_map: dict[int, dict[str, Any]],
        allowed_speakers: set[str],
        *,
        registry: CharacterRegistry,
        target_gender: Gender | None = None,
    ) -> str | None:
        """Resolve a candidate speaker matching a target gender from paragraph or nearby context."""
        if target_gender is None:
            return None

        # 1. Look within nearby dialogue fragments for a character of matching gender
        for offset in (-2, -1, 1, 2):
            neighbor_idx = fragment_index + offset
            if 0 <= neighbor_idx < len(fragments):
                meta = metadata_map.get(neighbor_idx, {})
                sp = meta.get("speaker")
                if sp and sp != "narrator" and sp in allowed_speakers and sp in registry.characters:
                    char = registry.characters[sp]
                    if char.gender == target_gender:
                        return sp

        # 2. Check characters matching target_gender in allowed_speakers
        matching_chars = [
            cid for cid in allowed_speakers
            if cid != "narrator" and cid in registry.characters and registry.characters[cid].gender == target_gender
        ]
        if len(matching_chars) == 1:
            return matching_chars[0]

        return None

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
        """Check if narrative text is or starts/ends with a dialogue tag attached to speech."""
        val = text.strip()
        words = val.split()
        if not words:
            return False
        if len(words) <= 12:
            return bool(
                re.search(
                    rf"\b(?:{_SPEECH_VERB_PATTERN})\b",
                    val,
                    re.IGNORECASE,
                )
            )
        first_sentence = re.split(r"[.!?\n]", val)[0].strip()
        first_words = first_sentence.split()[:10]
        if re.search(
            rf"\b(?:{_SPEECH_VERB_PATTERN})\b",
            " ".join(first_words),
            re.IGNORECASE,
        ):
            return True
        last_sentence = re.split(r"[.!?\n]", val)[-1].strip()
        last_words = last_sentence.split()[-10:]
        if re.search(
            rf"\b(?:{_SPEECH_VERB_PATTERN})\b",
            " ".join(last_words),
            re.IGNORECASE,
        ):
            return True
        return False

    @staticmethod
    def _is_collective_dialogue_tag(text: str) -> bool:
        """Return whether a short speech tag names only an anonymous group."""
        if not text or not ScriptGenerator._is_pure_dialogue_tag(text):
            return False
        # Subordinate temporal clauses like "as they walked", "while they waited", "once they were..."
        # are background action, not a collective speaker.
        if re.search(r"\b(?:as|while|when|if|because|once|after|before|since|until)\s+they\b", text, re.IGNORECASE):
            return False
        return bool(
            re.search(
                rf"\b(?:they|men|women|people|villagers|crowd)\b"
                rf"(?:\s+\w+){{0,3}}\s+(?:{_SPEECH_VERB_PATTERN})\b",
                text,
                re.IGNORECASE,
            )
            or re.search(
                rf"\b(?:{_SPEECH_VERB_PATTERN})\b(?:\s+\w+){{0,3}}\s+"
                r"\b(?:they|men|women|people|villagers|crowd)\b",
                text,
                re.IGNORECASE,
            )
        )

    @staticmethod
    def _is_leading_dialogue_tag(text: str) -> bool:
        """Return whether a speech tag syntactically introduces the next quote."""
        value = text.strip()
        return (
            ScriptGenerator._is_pure_dialogue_tag(value)
            and value.endswith((",", ":"))
        )

    @staticmethod
    def _is_embedded_quoted_term(
        fragment_index: int,
        fragments: list[SourceFragment],
    ) -> bool:
        """Recognize narrow lexical/scare quotes embedded inside narration."""
        fragment = fragments[fragment_index].text.strip()
        if not ScriptGenerator._is_dialogue_fragment(fragment) or len(fragment) < 2:
            return False
        content = fragment[1:-1].strip()
        if not content or len(content.split()) > 4 or re.search(r"[.!?]", content):
            return False
        previous = (
            fragments[fragment_index - 1].text.rstrip().casefold()
            if fragment_index > 0
            else ""
        )
        following = (
            fragments[fragment_index + 1].text.lstrip().casefold()
            if fragment_index + 1 < len(fragments)
            else ""
        )
        if ScriptGenerator._is_leading_dialogue_tag(previous):
            return False
        lexical_frame = re.search(
            r"\b(?:meant|means|called|named|word|term|label|"
            r"their|his|her|our|your|the|a|an)\s*$",
            previous,
        )
        return bool(lexical_frame and following)

    @staticmethod
    def _resolve_dialogue_speaker(
        frag_idx: int,
        fragments: list[SourceFragment],
        metadata_map: dict[int, dict],
        allowed_speakers: set[str],
        registry: CharacterRegistry | None = None,
        target_gender: Gender | None = None,
    ) -> str:
        """Return a conservative contextual candidate for an unresolved turn.

        The result is never release-grade evidence by itself. Callers must keep
        contextual candidates below the attribution audit threshold.
        """
        next_text = fragments[frag_idx + 1].text if frag_idx + 1 < len(fragments) else ""
        prev_text = fragments[frag_idx - 1].text if frag_idx > 0 else ""
        combined = (next_text + " " + prev_text).casefold()

        # 1. An explicit adjacent speech tag is deterministic.
        if registry:
            exact, _, _ = ScriptGenerator._dialogue_tag_evidence(next_text, registry)
            if exact is None and ScriptGenerator._is_leading_dialogue_tag(prev_text):
                exact, _, _ = ScriptGenerator._dialogue_tag_evidence(prev_text, registry)
            if exact in allowed_speakers:
                return exact

        # 2. A unique adjacent alias mention is a candidate, never an automatic
        # high-confidence correction. Multiple matches are intentionally ignored.
        if registry:
            mentioned: set[str] = set()
            for spk_id, char in registry.characters.items():
                if spk_id == "narrator" or spk_id not in allowed_speakers or spk_id.startswith("character_"):
                    continue
                if target_gender is not None and char.gender != target_gender:
                    continue
                names = [spk_id, char.name, *(getattr(char, "aliases", []) or [])]
                for name in names:
                    norm_name = name.strip().casefold().replace("_", " ")
                    if norm_name in _UNSAFE_SPEAKER_ALIASES or len(norm_name) < 2:
                        continue
                    if re.search(r"\b" + re.escape(norm_name) + r"\b", combined):
                        mentioned.add(spk_id)
                        break
            if len(mentioned) == 1:
                return next(iter(mentioned))
        else:
            mentioned = {
                spk
                for spk in allowed_speakers
                if spk != "narrator"
                and not spk.startswith("character_")
                and re.search(r"\b" + re.escape(spk.replace("_", " ")) + r"\b", combined)
            }
            if len(mentioned) == 1:
                return next(iter(mentioned))

        # 3. Establish the active speakers from prior dialogue turns.
        if target_gender is None:
            _, _, tag_gender = ScriptGenerator._dialogue_tag_evidence(next_text, registry)
            if tag_gender is None:
                _, _, tag_gender = ScriptGenerator._dialogue_tag_evidence(prev_text, registry)
            if tag_gender is not None:
                target_gender = tag_gender
            else:
                has_subject_he = bool(re.search(r"\bhe\b", combined))
                has_subject_she = bool(re.search(r"\bshe\b", combined))
                if has_subject_he and not has_subject_she:
                    target_gender = Gender.MALE
                elif has_subject_she and not has_subject_he:
                    target_gender = Gender.FEMALE

        recent_speakers: list[str] = []
        for idx in range(frag_idx - 1, max(-1, frag_idx - 15), -1):
            spk = metadata_map.get(idx, {}).get("speaker")
            if spk and spk != "narrator" and spk in allowed_speakers and spk not in recent_speakers:
                recent_speakers.append(spk)

        if registry and target_gender:
            matching_recent = [
                spk for spk in recent_speakers
                if registry.characters.get(spk)
                and registry.characters[spk].gender == target_gender
            ]
            if len(matching_recent) == 1:
                return matching_recent[0]

        # In a clearly established two-person exchange, alternation is stronger
        # than choosing whichever same-gender character happened to speak last.
        if len(recent_speakers) == 2:
            prev_dialogue_speaker = None
            for idx in range(frag_idx - 1, max(-1, frag_idx - 10), -1):
                if ScriptGenerator._is_dialogue_fragment(fragments[idx].text):
                    prev_dialogue_speaker = metadata_map.get(idx, {}).get("speaker")
                    break
            if prev_dialogue_speaker and prev_dialogue_speaker in recent_speakers:
                other_speaker = recent_speakers[1] if prev_dialogue_speaker == recent_speakers[0] else recent_speakers[0]
                return other_speaker

        # Gender evidence is usable only when it leaves one candidate. It must
        # never select the most recent of several same-gender speakers.
        if registry and target_gender:
            matching_allowed = [
                spk_id for spk_id in allowed_speakers
                if spk_id != "narrator"
                and registry.characters.get(spk_id)
                and registry.characters[spk_id].gender == target_gender
            ]
            if len(matching_allowed) == 1:
                return matching_allowed[0]

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
                    attribution_review_required=bool(
                        meta.get("attribution_review_required", False)
                    ),
                    attribution_review_reason=str(
                        meta.get("attribution_review_reason", "")
                    )[:500],
                    dialogue_kind=(
                        str(meta.get("dialogue_kind"))
                        if meta.get("dialogue_kind") in {
                            "spoken", "non_spoken_quote", "reported_collective_speech"
                        }
                        else ("spoken" if is_dialogue and speaker != "narrator" else None)
                    ),
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
            review_reasons = "; ".join(
                dict.fromkeys(
                    line.attribution_review_reason.strip()
                    for line in bucket
                    if line.attribution_review_reason.strip()
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
                        "attribution_review_required": any(
                            line.attribution_review_required for line in bucket
                        ),
                        "attribution_review_reason": review_reasons,
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
            same_dialogue_kind = line.dialogue_kind == previous.dialogue_kind
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
                and same_dialogue_kind
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

# Implementation Plan — Advanced Scripting, Speaker Attribution & Emotional Inflections

> **Historical proposal — superseded and partly based on a faulty premise.**
> Vathi is a speaking human character; entity type must never determine whether
> a speaker is retained. Current rules keep every entity that actually speaks,
> require exact immutable fragment IDs, and reject unknown speakers. Also,
> Qwen3-TTS Base clone mode does not accept arbitrary natural-language delivery
> instructions; current emotion handling uses restrained post-processing. See
> [docs/prompts.md](docs/prompts.md) and [docs/voice-design.md](docs/voice-design.md).

## Objective
Improve audiobook scripting accuracy, speaker attribution reliability, and emotional speech inflections by:
1. **Filtering Non-Speaking Entities** (e.g. personified islands like *Vathi*, ships, animals, or locations).
2. **Deterministic Rule Engine for Speaker Attribution** (combining grammar parsing, dialogue tag matching, and gender validation with LLM fallback).
3. **Rich Emotional Inflections & Scene Context** (fine-grained emotion directives and dynamic speed/volume pacing).

---

## 1. Non-Speaking Entity & Personification Filtering

### Problem
Entities like *Vathi* (a personified island in Brandon Sanderson's *Sixth of the Dusk*) are referred to with names/pronouns in narrative prose (e.g., *"Vathi had secrets..."* or *"Patji sent that Frond was right"*). The LLM mistakenly extracts them as speaking characters in Pass 1 and assigns dialogue to them in Pass 2.

### Solution
- **Pass 1 Prompt Guard (`character_analyzer.py`):**
  Add explicit instructions to Pass 1:
  > *CRITICAL: ONLY extract characters who ACTUALLY SPEAK SPOKEN DIALOGUE inside quotation marks ("..."). Do NOT extract personified locations, islands, ships, animals, or non-speaking entities.*
- **Non-Speaking Entity Check (`script_generator.py`):**
  If an extracted character has 0 dialogue quotes enclosed in `"..."` throughout the book text, automatically mark them as non-speaking and convert any misassigned lines to `narrator`.

---

## 2. Deterministic Rule Engine + Hybrid Speaker Attribution

### Problem
Relying solely on LLM JSON output for 150 text fragments causes off-by-one index drift and misassigned speakers.

### Solution: Hybrid Attribution Architecture

```
                       ┌────────────────────────────────────────┐
                       │           Source Text Line             │
                       └───────────────────┬────────────────────┘
                                           │
                        Is fragment enclosed in quotes ("...")?
                                 ┌─────────┴─────────┐
                                YES                 NO
                                 │                   │
               ┌─────────────────┴────────┐  ┌───────┴────────┐
               │ Dialogue Tag Parser      │  │ Force         │
               │ - "Vathi said" -> Vathi  │  │ "narrator"    │
               │ - "he said" -> Male char │  └───────────────┘
               │ - "she whispered"->Fem   │
               └─────────┬────────────────┘
                         │
              High confidence match?
                 ┌───────┴───────┐
                YES             NO
                 │               │
        ┌────────┴────────┐  ┌───┴───────────────────────┐
        │ Accept Rule     │  │ LLM Resolution           │
        │ Attribution     │  │ (Contextual Ambiguity)   │
        └─────────────────┘  └───────────────────────────┘
```

1. **Grammar & Quote Boundary Enforcement:**
   - **All non-quoted text fragments** (prose, description, dialogue tags like `he said`) are deterministically assigned to `narrator`.
2. **Dialogue Tag Matching (Pre-LLM):**
   - Inspect adjacent text for explicit character names (`"Dusk replied"`, `"Starling shouted"`).
   - Inspect pronoun tags (`"he whispered"`, `"she asked"`). Match against character registry genders.
3. **Alternating Turn Tracking:**
   - In continuous multi-turn dialogue between 2 characters, alternate speaker assignments automatically when un-tagged.

---

## 3. Rich Emotional Tagging & Speech Inflections

### Problem
Generic emotion tags like `"neutral"` make speech sound monotone.

### Solution: Fine-Grained Emotion Vocabulary & TTS Directives

1. **Enhanced Emotion Taxonomy in Prompt:**
   - **Whispers/Secrets:** `"hushed whisper"`, `"conspiratorial whisper"`, `"soft comfort"`
   - **Action/Intensity:** `"panicked shout"`, `"angry demand"`, `"breathless urgency"`
   - **Reflective/Somber:** `"somber reflection"`, `"weary sigh"`, `"sad nostalgia"`
   - **Humor/Warmth:** `"warm chuckle"`, `"playful banter"`, `"sarcastic retort"`

2. **Dynamic Pacing & Volume Directives:**
   - Automatically map emotion tags to optimal TTS reading speeds (`0.85` for weary sighs, `1.20` for panicked shouts) and standard pause durations before/after dialogue turns.

---

## Proposed Changes

### Component: `brain/director/character_analyzer.py`
- Update Pass 1 system prompt to strictly exclude non-speaking personified entities (islands, locations, ships).

### Component: `brain/director/script_generator.py`
- Implement hybrid deterministic dialogue tag parser and pronoun gender validator before/after LLM call.
- Expand emotion taxonomy with rich inflections and dynamic pacing.

---

## Verification Plan

### Manual Verification
1. Run Pass 1 character extraction on `sample_book-7` text and verify *Vathi* (island) is NOT extracted as a speaking character.
2. Run script generation on Chapters 1–8 and inspect JSON scripts:
   - Confirm 0 non-quoted lines are assigned to characters.
   - Confirm 0 dialogue tag gender mismatches (`she said` assigned to male speaker).
   - Confirm rich emotion directives (`"hushed whisper"`, `"panicked shout"`, `"somber reflection"`).

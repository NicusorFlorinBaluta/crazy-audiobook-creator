# Character Augmentation and Gender Resolution (2026-08-19)

## Context & Motivation

During Stage ② Character Analysis (Pass 1) and Joint Scripting, local LLMs (Qwen 2.5 32B) frequently defaulted character gender classifications to `"other"` for complex fantasy titles, non-traditional names, or alien entities (e.g. *Sixth of the Dusk*, *Master Hoid*, *Nazh*, *Vathi*, *ZeetZi the lawnark*, *Second of Saplings*).

This caused significant downstream degradation:
1. **TTS Voice Design Flattens to Androgynous**: The TTS Voice Casting compiler prefixes `gender: "other"` with *"An androgynous or non-gendered speaker"*, generating flat, neutral, and identical sounding voice audition clips.
2. **Generic Character Descriptions**: Qwen repeatedly generated generic `"calm, authoritative"` or `"neutral tone"` voice descriptions across virtually all characters, lacking vocal textures, pitches, registers, and specific narrative cadences.
3. **Impaired Dialogue Attribution**: In Pass 2 chapter scripting, the LLM lacked canonical gender boundaries, making it harder to unambiguously resolve gendered dialogue attribution tags (*"she said"* / *"he replied"*).

---

## Architectural Decisions & Solution

To resolve this reliably, quickly, and affordably, a **Whole-Book Evidence-Augmentation Architecture** was implemented.

```text
┌─────────────────────────────────────────────────────────────┐
│ 1. Multi-Pass / Joint Extraction & Deduplication            │
│    Local Qwen extracts entities, dialogue counts, and aliases│
│    Deterministic consolidation & alias derivation           │
└──────────────────────────────┬──────────────────────────────┘
                               │ Canonical IDs & Aliases
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. Whole-Book Evidence Dossier Collection (Pure Python)     │
│    Scans full book text for character names & all aliases   │
│    Counts pronoun frequencies (he/him/his vs she/her/hers)  │
│    Extracts narrative descriptions & dialogue excerpts      │
└──────────────────────────────┬──────────────────────────────┘
                               │ Rich Character Dossier
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. Gemini Flash Augmentation Step (1 API Call, ~3s, ~$0.002)│
│    Models: gemini-3.5-flash / gemini-3.5-flash-lite         │
│    - Resolves canonical gender from pronoun statistics      │
│    - Refines canonical age ranges                           │
│    - Generates 12-dimensional voice designs & archetypes    │
│    - Creates character-true in-lore test sentences          │
│    - Fallback: Graceful degradation to local Qwen if offline│
└──────────────────────────────┬──────────────────────────────┘
                               │ Enriched Character Registry
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 4. Voice Casting & Synthesis (Preserving User Uploads)      │
│    - Compiles deterministic TTS prompts for VoiceDesign     │
│    - Strictly preserves user-uploaded custom .wav samples   │
│    - Synthesizes distinct audition candidates on GPU        │
└─────────────────────────────────────────────────────────────┘
```

---

## Key Technical Components

### 1. Whole-Book Evidence Collector (`_build_character_evidence_dossier`)
Located in [`brain/director/character_analyzer.py`](../brain/director/character_analyzer.py):
- Gathers whole-book context for each registered character.
- Searches for mentions across the character's **display name, snake_case ID, and all derived aliases** (`Sixth of the Dusk`, `Dusk`, `Trapper`).
- Analyzes surrounding sentences (±150 characters) to compute statistical pronoun ratios (`he/him/his/himself/man` vs `she/her/hers/herself/woman`).
- Captures up to 4 representative scene descriptions and dialogue excerpts.

### 2. Gemini Character Augmenter (`_augment_characters_with_gemini`)
Located in [`brain/director/character_analyzer.py`](../brain/director/character_analyzer.py):
- Sends the structured dossier to Google Gemini (`gemini-3.5-flash`) with an explicit JSON response schema.
- **Strict Canonical Identity Constraint**: Gemini is only permitted to update attributes on the pre-existing, deduplicated character IDs. It cannot create, delete, or split characters.
- Generates 12-dimensional vocal prompts:
  - **Pitch & Register**: Deep gravelly baritone, reedy tenor, crisp alto, bright soprano.
  - **Cadence & Speed**: Slow and deliberate, brisk and articulate, nervous and breathless.
  - **Vocal Texture**: Weathered raspy, silky smooth, sniffly congested, whispering composite.
  - **Persona & Emotion**: Reflective survivalist, charismatic storyteller, cynical aristocrat, commanding scholar.
  - **Test Sentences**: 15–25 word in-character lines grounded in the novel's lore.

### 3. Prompt-Level Guardrails
- Updated [`character_extraction.md`](../brain/director/prompts/character_extraction.md) and [`_SYSTEM_PROMPT`](../brain/director/character_analyzer.py): Instructs local Qwen to inspect surrounding narrative pronouns and forbids defaulting to `"other"` when gendered pronouns exist.
- Updated [`script_generator.py`](../brain/director/script_generator.py): In joint mode, allows upgrading `existing.gender` from `Gender.OTHER` when later chapters encounter explicit gender evidence.

### 4. Preservation of Custom User Voices
- When voice casting compiles (`build_voice_cast`), any voice entry with `source_type == "uploaded"` (or present in `voices.json` as an uploaded reference) is strictly preserved.
- The pipeline updates the character's metadata in `characters.json` for script attribution without re-synthesizing or overwriting the user's custom reference audio.

---

## Why Character Deduplication is Protected

A critical requirement was ensuring this change does not harm character deduplication or alias handling across chapters:

1. **Deduplication Happens First**: Full multi-pass consolidation, alias resolution (`_derive_character_aliases`), and candidate adjudication (`_adjudicate_name_candidates`) run *before* the augmentation step.
2. **Canonical Keys Are Fixed**: Augmentation operates strictly on the reconciled registry keys. It cannot invent new character IDs or split merged identities.
3. **Multi-Alias Search**: The evidence collector searches for all known aliases in every chapter, giving Gemini a unified, complete view of the character across the entire novel.
4. **Improved Attribution**: Having accurate canonical genders actively assists Pass 2 dialogue attribution by eliminating impossible opposite-gender candidates for gendered speech tags (*"she whispered"*).

---

## Configuration & Model Endpoints

Updated in [`brain/config.yaml`](../brain/config.yaml) and [`brain/validators/gemini_validation.py`](../brain/validators/gemini_validation.py):
- `triage_model`: `gemini-3.5-flash-lite`
- `adjudication_model`: `gemini-3.5-flash`
- Daily request budgets and circuit-breaker telemetry are maintained with automatic fallback.

---

## Future Validation & Testing on a Fresh Book

> [!IMPORTANT]
> **Recommended Next Step**: While unit tests and retroactive project augmentation verify correctness on existing data, this entire character augmentation and pronoun-resolution subsystem should be **fully evaluated on a brand-new, unscripted book** in an upcoming end-to-end run.

### Key Validation Metrics to Benchmark:
1. **Gender Accuracy Rate**: Verify that newly introduced fantasy names, titles, and non-binary species are correctly categorized on the first pass without manual intervention.
2. **Vocal Diversity & Separation**: Compare acoustic similarity scores across the cast to confirm that the 12-dimensional voice designs produce distinct, non-overlapping voices.
3. **Dialogue Attribution Accuracy**: Verify whether canonical gender availability improves Pass 2 speaker attribution confidence and reduces human review flags.
4. **End-to-End Latency**: Measure total Pass 1 runtime to confirm that the Gemini augmentation step adds negligible overhead (<5 seconds total).

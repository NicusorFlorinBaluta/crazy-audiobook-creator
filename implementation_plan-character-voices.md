# Fix the Root Cause: Pass 1 Character Analysis Missing Speakers

## Problem Diagnosis

The `child_female` crash isn't a Pass 2 parsing issue — it's a **Pass 1 coverage gap**. Here's what's actually happening:

### Pass 1 sees almost nothing for chapters 2–8

```
Ch1 'Prologue': FULL TEXT (+7.0 KB)
Ch2 'Chapter One': SUMMARY (+0.5 KB)    ← only first 500 chars!
Ch3 'Chapter Two': SUMMARY (+0.5 KB)    ← only first 500 chars!
...
Ch8 'Chapter Seven': SUMMARY (+0.4 KB)
```

The `_prepare_book_text` method ([character_analyzer.py:277-333](file:///e:/Projects/crazy-audiobook-creator/brain/director/character_analyzer.py#L277-L333)) caps total prompt text at 25 KB. For a 66.5 KB book, it sends Chapter 1 in full and then **only the first 500 characters** of each remaining chapter as a "summary." Those 500 chars rarely contain dialogue, so any character who only speaks in chapters 2–8 is invisible to Pass 1.

### Result: the latest Pass 1 produced garbage IDs

```
Pass 1 complete | 7 characters: ['narrator', 'character_1', 'character_2', 
'character_3', 'character_4', 'character_5', 'character_6']
```

The LLM used generic `character_N` IDs instead of proper names, and it **missed** children, Vambrakastram, and Mother Frond entirely. When Pass 2 then scripted Chapter 1 and its rule-based fallback assigned `child_female`, that ID wasn't in the registry → crash.

> [!CAUTION]
> Adding downstream workarounds (LLM micro-queries, substring matching, auto-registration) treats symptoms. The disease is that Pass 1 doesn't see enough text to find all speakers.

## Proposed Fix: Dialogue-Aware Chapter Summaries in Pass 1

Instead of sending a blind 500-char truncation as the "summary" for each chapter, **extract and include all dialogue lines** from each chapter. Dialogue is where characters reveal themselves — it's the one thing Pass 1 absolutely must see.

### Architecture

```
Current (broken):
  Ch2 summary = first 500 chars of prose → no dialogue → LLM misses speakers

Proposed (fixed):
  Ch2 summary = first 300 chars of prose
               + ALL dialogue lines from the chapter (with attribution tags)
               → LLM sees every speaking character
```

---

## Proposed Changes

### [MODIFY] [character_analyzer.py](file:///e:/Projects/crazy-audiobook-creator/brain/director/character_analyzer.py)

#### 1. New method: `_extract_dialogue_lines(text: str) -> list[str]`
- Uses regex to extract all quoted dialogue from a chapter's text.
- Includes the surrounding dialogue tag (e.g., `"Watch out!" she screamed.`) for speaker attribution context.
- Returns a deduplicated list of dialogue excerpts.

#### 2. Update `_prepare_book_text()` — dialogue-aware summaries
- For chapters sent as "SUMMARY", replace the blind `text[:500]` truncation with:
  ```
  first 300 chars of prose (for scene-setting context)
  + "\n\nDialogue in this chapter:\n"
  + all extracted dialogue lines (capped at ~2 KB per chapter)
  ```
- This ensures the LLM sees every speaking character even in summarized chapters.
- Increase the total budget from 25 KB to 40 KB to accommodate dialogue extracts, which is still well within Qwen2.5-32B's 32k context (~128 KB).

#### 3. Improve the system prompt with explicit ID instructions
- Add to the `_SYSTEM_PROMPT`: *"Use the character's actual name as the character_id (snake_case). Do NOT use generic IDs like character_1, character_2."*
- Add: *"Include ALL characters who speak dialogue, even if they only speak once. For unnamed speakers (e.g. 'a child', 'the merchant'), create a descriptive ID like 'child_female', 'merchant'."*

### [MODIFY] [script_generator.py](file:///e:/Projects/crazy-audiobook-creator/brain/director/script_generator.py)

#### 4. Simplify Pass 2 unknown speaker handling
- **Remove** `_canonicalize_speaker_id` (the brittle substring heuristic).
- In `_parse_script_chapter`, when `speaker not in allowed_speakers`:
  - **Map to narrator** (safe fallback) instead of crashing or auto-registering.
  - Log a warning: `"Unknown speaker '{speaker}' mapped to narrator for fragment {id}"`.
- This is the correct behavior: if Pass 1 did its job, unknowns are rare and are genuinely ambiguous. Narrator is the safest fallback for audio.
- Keep `_detect_new_characters` as-is for edge cases, but it should rarely trigger if Pass 1 is comprehensive.

---

## Open Questions

> [!IMPORTANT]
> **Budget tradeoff**: Increasing the Pass 1 text budget from 25 KB to 40 KB means the LLM call takes ~30s longer. Since Pass 1 only runs once per book, this seems acceptable. Do you agree?

> [!NOTE]
> **Narrator fallback vs. auto-registration**: When Pass 2 encounters an unknown speaker, should we (a) silently map to narrator, or (b) map to narrator AND log a warning in the dashboard UI so you can see if Pass 1 missed something? I recommend (b) for visibility.

## Verification Plan

### Automated Tests
- Re-run `run_full_e2e_validation.py` and verify:
  - Pass 1 finds all speaking characters including children, minor speakers
  - Character IDs are proper names (not `character_1`, `character_2`)
  - No `ValueError` crashes in Pass 2
  - 100% text coverage across all 8 chapters

### Manual Verification
- Inspect `characters.json` to confirm all speakers are present with proper names
- Check `pipeline.log` for any "Unknown speaker mapped to narrator" warnings

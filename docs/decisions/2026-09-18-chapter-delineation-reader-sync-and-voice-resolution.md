# Chapter Delineation, Reader Synchronization, and Narrator Voice Resolution

**Status:** Current  
**Date:** 2026-09-18  

## Context

During playback validation of large multi-part audiobooks (such as *The Finest Edge of Twilight*, 33 chapters across 7 delivery parts) on the **CrazyVoice** Android companion app and Android Auto, three interconnected issues were observed:

1. **Chapter Delineation and Reader Desync**: The mobile app experienced track misalignments and playback offset jumping. Books with unnumbered front matter, preludes, or non-standard chapter naming drifted out of sync: ExoPlayer track indices did not match manuscript chapter numbers, causing the synchronized reader and karaoke script viewer to load incorrect chapters or seek past the end of the chapter file.
2. **Announcement Generation Hallucination**: Chapter title announcements (e.g. Chapter 13 "Top of the World") were occasionally vulnerable to model repetition loops when adaptive token calculations allowed too many tokens, generating excessively long speech.
3. **Narrator Cast Resolution**: In some code paths, voice resolution for the narrator fell back to default female narrator profiles instead of honoring the cast candidate explicitly chosen in `voice_cast.json` (such as `narrator_male` / `Aidan`).

---

## 1. Chapter Delineation and Reader Synchronization Contract

### Root Cause
1. **Metadata vs Sequence Disconnect**: In previous M4B exports, chapter tags used raw or sanitized title strings (e.g. `"Chapter 1"`, `"Prologue"`, `"13: Top of the World"`). However, when a book contains unnumbered introductory chapters, the sequence number in the M4B container diverges from the author's internal chapter numbering.
2. **Offset Semantics Mismatch**: When streaming individual chapter files (standalone chapter AAC/WAV streams), the API previously passed cumulative book-wide offsets (`start_ms`) intended for a concatenated full-book M4B. ExoPlayer attempting to seek to `start_ms` in a standalone chapter stream would seek past the EOF or trigger playback errors, falling back and confusing the reader synchronization logic.
3. **Cumulative Offsets in Full Manifests**: In `brain/orchestrator/nas_syncer.py`, manifest generation for full M4B streams previously had duplicate branches that failed to advance the cumulative offset when duration was missing or zero, producing duplicate `start_ms` values that broke ExoPlayer chapter navigation.

### Decisions & Implementation
1. **Sequence Prefix in M4B Chapter Tags (`f"{number}::{clean_title}"`)**:
   - In `voice/mastering/m4b_exporter.py`, M4B chapter titles are formatted as `f"{number}::{clean_title}"` (e.g., `17::13: Top of the World`).
   - The Android companion client (`ChapterMark.kt`) splits on `"::"`:
     - `raw.substringBefore("::").toIntOrNull()` yields the deterministic 1-based chapter sequence number.
     - `raw.substringAfter("::")` yields the clean human-readable title displayed in the player UI.
2. **Zero-Based Local Offsets for Standalone Chapter Streams**:
   - In `brain/dashboard/api/mobile.py`, individual chapter stream details explicitly set `start_ms = 0` and `end_ms = duration_ms`. Local chapter streams always operate in 0-based time coordinates.
   - Cumulative offsets are reserved strictly for whole-book concatenated M4B streams.
3. **Strict Monotonic Offsets for Full M4B Streams**:
   - In `brain/orchestrator/nas_syncer.py`, cumulative offsets strictly advance by `effective = dur or 60.0`, eliminating duplicate timestamps and fallback branching.
4. **Delivery Parts URL Normalization**:
   - In `brain/dashboard/api/mobile.py`, `part_ch_details` uses the verified delivery download URL for both `stream_url` and `download_url`.

---

## 2. Chapter Title Announcement Safeguards

### Root Cause
Chapter announcements are short synthesized cues ("Chapter Thirteen: Top of the World"). In `voice/tts_server/qwen3_engine.py`, the engine calculates an adaptive token limit based on input character count. When a caller passed an explicit `max_new_tokens`, the engine previously let the adaptive calculation override the caller's ceiling if the adaptive cap was higher. This allowed title announcements to enter autoregressive looping loops under certain prompt conditions.

### Decisions & Implementation
1. **Caller Ceiling Clamping in Generation Config**:
   - In `voice/tts_server/qwen3_engine.py`, `_resolve_generation_config` now treats `max_new_tokens` passed by the caller as a hard upper bound:
     ```python
     if caller_max_tokens is not None:
         target_tokens = min(target_tokens, caller_max_tokens)
     ```
2. **Announcement Token Cap & Duration Ceiling**:
   - In `voice/tts_server/main.py`, `ANNOUNCEMENT_TOKEN_CAP = 512` is enforced on all chapter announcement generation calls.
   - A hard duration check validates that chapter title announcement audio does not exceed 10.0 seconds; exceeding this raises an `HTTPException(status_code=500)` to fail closed before mastering corrupted announcement audio.

---

## 3. Narrator Voice Resolution Order

### Root Cause
Voice resolution follows a multi-tier precedence:
1. Active project voice cast (`voice_cast.json` via `assigned_characters`).
2. Character profile defaults (`characters.json` via `voice_id`).
3. Explicit voice library registry matches.
4. System archetype fallbacks (`narrator`, `narrator_female`, `narrator_male`).

When resolving narrator lines, code paths in `pipeline.py` and `voice_library.py` occasionally bypassed the project's cast assignment or fell back through an inverted fallback tuple, causing male narrator castings to be replaced by female narrator defaults during dynamic resolution or repair runs.

### Decisions & Implementation
1. **Authoritative Cast Querying in Orchestrator**:
   - In `brain/orchestrator/pipeline.py`, `_selected_narrator_voice_id` directly delegates to `get_speaker_voice_mapping(project_dir).get("narrator", "narrator")`, guaranteeing that the voice assigned in `voice_cast.json` is always resolved.
2. **Preserved Fallback Tuple**:
   - In `voice/tts_server/voice_library.py`, `resolve_voice_reference` maintains the canonical fallback tuple `("narrator", "narrator_female", "narrator_male")` and emits a warning log whenever a generic fallback is hit.

---

## 4. Ground Rules

1. **Chapter tags in M4B exports must always follow `f"{number}::{title}"`**: Never emit bare numbers or bare titles without the separator.
2. **Standalone chapter streams always start at `0` ms**: Never inject whole-book cumulative offsets into single-chapter media manifests.
3. **Cumulative offsets in full M4B manifests must be strictly monotonic**: Every chapter must advance `cumulative_offset` by its exact duration (or a nonzero fallback).
4. **Caller `max_new_tokens` is a hard ceiling**: Adaptive token calculations may lower token limits for short text, but must never exceed caller-specified ceilings.
5. **`voice_cast.json` is the sole authority for narrator voice assignment**: Never fall back to generic system voices without verifying the project's cast mapping first.

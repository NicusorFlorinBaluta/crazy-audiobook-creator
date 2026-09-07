# Pronunciation Lexicon Ergonomics & Whisper STT Language Normalization

**Date:** 2026-09-07  
**Status:** Current  

---

## 1. Context and Problem Statement

During full-book testing and quality review of *The Finest Edge of Twilight*, two distinct classes of issues emerged in the pronunciation management workflow and the audio validation pipeline:

### A. Pronunciation Lexicon Usability & Filtering
1. **Unnatural Word Pauses & Hyphen Splitting**: Phonetic recommendations initially converted hyphens into spaces (e.g., `Home-aisle` &rarr; `Home aisle`), causing the Qwen3-TTS engine to treat spaces as audible pauses.
2. **Dictionary Noise in Candidates**: Common English words appearing in book titles or capitalized headings (e.g., `Tower`, `Less`, `Other`, `Listen`) were being flagged as fantasy/unresolved pronunciation terms.
3. **Overly Long Audio Contexts**: Native audio preview synthesis was passing entire paragraphs (up to 300+ characters), taking ~14–24 seconds per preview.
4. **Search Relevance**: Searching in the dashboard lexicon matched any substring in sample context sentences, pushing exact term matches down or completely hiding verified terms if the user was on the "Suggestions" tab.
5. **Lack of Lifecycle Integration**: Audio previews required manual triggering; resuming the pipeline while preview mode was active failed to release resources; and there was no mechanism to export/import lexicons across projects with conflict comparison.
6. **Case-Sensitive Export Re-import Conflict**: Exporting a project lexicon and re-importing the identical file produced phantom `Overwrite` conflicts on terms like `Jax` and `Janquay` because lowercase recommendations (`jax: Yax`) in `pronunciation_recommendations.json` failed to overwrite TitleCase project dictionary entries (`Jax: Jax`) during Python dictionary merging, resulting in duplicate keys.

### B. Whisper STT Language Code Crash (275 Spurious Failures)
1. **EPUB Metadata Language Tag**: The EPUB metadata extracted BCP-47 locale tag `language: "en-US"`, which was forwarded to Whisper in `GenerateChapterRequest`.
2. **Whisper Language Incompatibility**: Neither `openai_whisper` nor `faster_whisper` supports BCP-47 sub-tags (`en-us`); they strictly accept 2-letter ISO 639-1 language codes (e.g., `en`).
3. **Silent Hard Gate Failure Cascade**: When passed `language="en-us"`, Whisper threw `ValueError: Unsupported language: en-us`. The top-level exception handler caught this and returned `""` (empty string). With an empty transcript, the Word Error Rate was evaluated as 1.0 (100% mismatch), causing 275 out of 277 segments in Chapter 1 to be flagged with:
   > `Deterministic audio hard gate failed; external models cannot override it`
   The synthesized audio files were actually of pristine quality (subsequently measured at WER 0.000–0.143), but the inbox was flooded with 275 blocking review items.
4. **Scheduled Working Hours Auto-Pause**: At 19:18, Chapter 1 finished generation. Because the configured working hours in `brain/config.yaml` are Monday–Friday 09:00–19:00 (`Europe/Bucharest`), the pipeline paused automatically before starting Chapter 2 with `status: "paused_scheduled"` (`WAITING FOR WORKING HOURS`).

---

## 2. Decisions & Technical Solutions

### A. Offline English Dictionary Filtering
- Built an offline, lightweight gzipped dictionary containing 370,105 English words (`shared/data/english_words.txt.gz`, 1.05MB).
- `build_pronunciation_inventory()` excludes terms found in the English dictionary unless they are in `characters.json` or explicitly verified by the user in `pronunciation_dict.json`.
- Added heading detection (`_is_heading_or_title()`) to prevent title-cased chapter titles from triggering false mid-sentence capitalization.

### B. Concise Sentence Context Extraction
- Implemented `extract_concise_sentence(text, term, max_chars=100)` in `shared/pronunciation.py`.
- Sentence boundaries are respected, extracting clean snippets centered on the term. Preview generation duration dropped from ~14s to ~2–3s.

### C. Fluid Respellings & Hyphen Preservation
- Preserved hyphens in `normalize_phonetic_text` without replacing them with spaces.
- Prompt headers for Ollama and internal heuristic rules generate blended single words (e.g., `Homeaisle`, `Cokerlee`, `Drizt`) as primary recommendations and clean hyphenated forms as alternates.

### D. Search Ranking & Cross-Tab Fallback
- Replaced naive text search in `script-viewer.js` with priority scoring: exact term match (0), prefix (1), spoken exact (2), substring (4), context sentence match (6).
- If any term or spoken match exists, context matches are excluded.
- If a query has 0 matches on the current tab but matches exist in other tabs, the view automatically switches to "All" and highlights the term. Pressing `Escape` clears the query.

### E. Pipeline Preview Pre-generation & Preview Mode Supervision
- Added pipeline stage `_pregenerate_pronunciation_previews()` running immediately after voice bootstrapping with progress/ETA tracking (`phase="pronunciation_previews"`).
- Automatically updates cached audio preview whenever a user updates a term's phonetic respelling.
- `start_pipeline()` automatically calls `runtime.exit_preview_mode()` to ensure local preview locks are released before chapter synthesis begins.

### F. Filtered Lexicon Export & Cherrypicking Import
- Export endpoint (`/api/projects/{project_id}/pronunciations/export?scope={scope}`) supports `all`, `verified`, and `defaults`.
- Reconciliation indexes keys case-insensitively (`term.casefold()`) and attaches canonical book casing from the inventory. Project overrides strictly overwrite default recommendations, preventing duplicate casing.
- Frontend modal provides cherry-picking filters (`All`, `New & Conflicts`, `Conflicts Only`), batch selection, and comparison table. Incoming entries are deduplicated case-insensitively, prioritizing matching project states (`identical`).

### G. Whisper Language Code Normalization
- Added `WhisperValidator.normalize_language(language)`:
  - Strips region/script subtags: `en-US` &rarr; `en`, `en_GB` &rarr; `en`, `zh-Hans` &rarr; `zh`.
  - Normalizes `auto`, `none`, empty strings to `None`.
- Added resilient auto-detect fallback: if Whisper raises an exception containing `"language"`, it immediately retries with `language=None` rather than aborting and returning an empty transcript.
- Updated `pipeline.py` and `validation_loop.py` to normalize language before dispatch.

---

## 3. Evidence & Verification

1. **Automated Unit Tests**:
   - `test_export_pronunciations_with_scopes`: Verified case-insensitive override (recs `jax: Yax` vs proj `Jax: Jax` exports only `Jax: Jax`).
   - `test_batch_update_pronunciations_case_insensitivity`: Verified removal of duplicate case variants on save.
   - `test_whisper_validator_normalize_language`: Verified normalization of BCP-47 tags (`en-US` &rarr; `en`, `en_GB` &rarr; `en`, `auto` &rarr; `None`).
   - Full suite across pronunciation, voice runtime, and validation loops: 151 passed.
2. **STT Quality Verification on Chapter 1 Audio**:
   - Re-transcribed segments from Chapter 1 with normalized `language="en"`. WER across segments ranged from 0.000 to 0.143, confirming audio is crisp and accurate.
3. **Review Inbox Cleanup**:
   - Cleared the 275 spurious `segment` items from `review_items` and updated `quality_logs` to `status="pass"`.
   - Blocking review count returned to 0.
4. **Live Services**:
   - Voice Server running on `http://127.0.0.1:8100` (PID refreshed).
   - Dashboard running on `http://127.0.0.1:8000`.

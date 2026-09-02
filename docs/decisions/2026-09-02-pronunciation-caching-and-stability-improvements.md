# Pronunciation Dictionary, Cache Hardening, and Stability Improvements — 2026-09-02

## Overview

This record documents the architectural enhancements, bug fixes, and stability hardening implemented for the audiobook creation pipeline, dashboard, and voice synthesis engine.

---

## 1. Book-Local & Global Pronunciation Dictionary and Hot-Swap

### Objectives
Audiobook narration of fantasy, science fiction, and non-English proper nouns frequently suffers from mispronunciation by neural TTS models (e.g., pronouncing *Szeth* as *S-zeth* instead of *Zeth*, *Kelsier* as *Kel-seer* vs *Kel-see-ay*, *Taravangian* as *Tah-rah-van-gee-an*).

### Implementation
- **Deterministic Replacement Layer (`shared/pronunciation.py`)**:
  - `apply_pronunciation_dictionary(text, mappings)` replaces words with word-boundary and casing preservation (`\b[Term]\b`).
  - Supports compound base replacements (e.g. `isles`, `nightblood`, `stormlight`).
- **Dynamic Pronunciation Resolution via Local LLM (Qwen 3.8 27B)**:
  - `resolve_pronunciations_with_llm()` batches out-of-vocabulary candidate terms with surrounding context sentences and prompts local Ollama (with `think: false`) to generate TTS-friendly plain English phonetic respellings (`default`, `alternate`, `rationale`, `confidence`).
  - Offline phonetic syllable chunker serves as an instant zero-inference fallback.
- **Two-Tier Storage & Persistence**:
  - Global dictionary: `brain/pronunciation_dict.json`.
  - Project-local dictionary: `brain/projects/<id>/pronunciation_dict.json`.
  - Inventory cache: `brain/projects/<id>/pronunciation_inventory.json`.
  - Recommendations cache: `brain/projects/<id>/pronunciation_recommendations.json`.
- **UI & Native TTS Audio Preview**:
  - Dedicated **Pronunciations** tab in the dashboard.
  - Native Qwen3-TTS inline audio preview endpoint (`POST /api/projects/{id}/pronunciations/preview`) to audition exact pronunciations on the real voice engine in real-time before approval.
  - Batch approve, custom respelling editing, and instant dictionary hot-swap.

---

## 2. High-Performance Embedded Cache Service (`shared/cache.py`)

### Problem
In-memory dictionary caches were susceptible to concurrency races, memory ballooning, and did not persist across dashboard service restarts.

### Implementation
- Implemented **`CacheService`** in [`shared/cache.py`](file:///e:/Projects/crazy-audiobook-creator/shared/cache.py) using embedded SQLite in **Write-Ahead Logging (WAL)** mode (`PRAGMA journal_mode = WAL`, `PRAGMA synchronous = NORMAL`).
- **Zero External Dependencies**: 100% bare-metal, native Python, survives restarts, thread-safe, multi-process safe, zero background service overhead, and no 10-day license limits.
- Supports binary serialization via `pickle` with TTL support, exact key deletions, and prefix invalidation.

---

## 3. Per-File `mtime` Tracking (Eliminating Cache Thrashing)

### Problem
Previously, `max(all chapter mtimes)` was used as a single coarse invalidation signal. During active generation or sequential chapter saving, modifying Chapter 31 repeatedly invalidated cached summaries of Chapters 1–30.

### Implementation
- Track per-file mtime maps `{ "chapter_001.json": 1234567.89, ... }`.
- Read operations compare stored signatures and only rebuild when dependent chapter files have changed.

---

## 4. Decision Trail Capping & Truncation

### Problem
Unbounded error strings and cumulative validation history in `attribution_confidence_history` caused exponential JSON growth over repeated retries, bloating project manifests and causing memory/transport bottlenecks.

### Implementation
- Added `_cap_decision_trail()` in [`brain/orchestrator/review_gate.py`](file:///e:/Projects/crazy-audiobook-creator/brain/orchestrator/review_gate.py).
- Caps decision history to the **last 10 entries** and truncates verbose strings to **500 characters** at the HTTP serialization boundary.
- Source-level error logging in `gemini_validation.py` is capped to 300 characters.

---

## 5. External Validation Retry UI & Concurrency Guard

### Implementation
- **Concurrency Guard**: `_retry_locks: dict[str, asyncio.Lock]` rejects overlapping validation retries with `HTTP 409 Conflict`.
- **Top-Level Metadata Preservation**: Merges `chapters` and `total_lines` into existing `book_script.json` without dropping metadata.
- **Circuit Breaker Unlinking**: Unlinks `.external_validation_health.json` on manual retry.
- **Frontend Rate-Limit Banner**: Displays active Gemini cooldown countdown with auto-retry (capped at 3 attempts) and pause on budget exhaustion (50/50 daily requests).

---

## 6. Registry-Aware Generic Speaker Resolution

### Implementation
- `_normalize_speaker_id` in [`brain/director/script_generator.py`](file:///e:/Projects/crazy-audiobook-creator/brain/director/script_generator.py) is strictly pure string normalization.
- In `_resolve_pass2_speakers`, alias lookup is applied as **Step 3 fallback only after** searching exact IDs and canonical aliases in the character registry, preventing named characters referred to as "the woman" from being stolen by generic archetype pools.

---

## 7. Verification Summary

- **Unit Test Suite**: 426 tests passed (0 failures, 2 skipped).
- **Latency Benchmarks**:
  - `GET /health`: 2 ms
  - `GET /api/projects/{id}/reviews`: 39 ms
  - `GET /api/projects/{id}/characters`: 31 ms
  - `GET /api/projects/{id}/pronunciations`: 616 ms

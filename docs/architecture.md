# Architecture

This document describes the current implementation. Historical implementation plans describe earlier two-machine and Ubuntu designs and are not authoritative.

## Runtime layout

The supported default is one Windows workstation:

```text
Browser / Electron shell
        │
        ▼
Dashboard API :8000 ─── Ollama :11434
        │
        ▼
Voice API :8100 ─── Qwen VoiceDesign bootstrap helper :8101
        │
        ├── Qwen3-TTS Base voice cloning
        ├── Whisper transcription
        ├── audio/speaker validation
        └── mastering + FFmpeg M4B export
```

Ollama and Voice bind to `127.0.0.1`. The checked-in dashboard binds to the
workstation LAN and authorizes only its configured trusted LAN CIDRs (plus
loopback) without an application token. The dashboard and pipeline both
serialize GPU work; a global operating-system lease also rejects accidental
scratch-runner concurrency.

## Processing stages

1. **Created** (`created`): EPUB upload uploaded and project initialized.
2. **Extraction** (`extracting`): EPUB text extracted, chapter structure built, cover art saved.
3. **Scripting** (`scripting`): LLM Pass 1 character analysis & Pass 2 text-to-script annotation.
   - Dialogue attribution is model-driven and source-grounded. Unknown IDs and
     low-confidence results trigger focused retries. Pass 2 cannot invent cast
     members; its conservative final fallback is narrator rather than a
     gender/proximity guess.
4. **Bootstrapping** (`bootstrapping`): Speaking cast derived, voice design directions compiled, reference audio generated via Qwen VoiceDesign. Acoustic speaker embeddings (128-dim log-mel features) verify acoustic distinctness without prompt-text false positives.
5. **Voice Review** (`voice_review`): Automated pause gate for user approval. Displays voice cards, preview players, and `[Approve voices & continue]` action banner.
6. **Generating** (`generating`): Qwen3-TTS synthesizes chapter audio line by line with dynamic contextual pauses (250ms same speaker, 380ms narrator, 400ms quote-to-action, 450ms turn change, 900ms paragraph break).
7. **Validating** (`validating`): Whisper Speech-to-Text transcribes audio to verify Word Error Rate (WER), acoustic clipping, and length bounds.
8. **Mastering** (`mastering`): Chapter audio assembled with chapter announcements, crossfading, and LUFS volume normalization.
9. **Exporting** (`exporting`): Mastered chapter WAVs packaged as chaptered M4B (AAC) with metadata and embedded cover art.
10. **Completed** (`completed`): Audiobook creation complete and ready for download or Home Assistant playback.

## Why analysis is book-wide but audio is incremental

Speaker identity and characterization require book context. The pipeline therefore completes character analysis, the character registry, and scripts for all chapters as one logical phase.

Audio work is chapter-selectable. `generation_chapter_selection` has two meanings:

- `null`: all chapters; after missing chapters are complete, create the canonical full M4B.
- Nonempty list: generate/master those chapters and create a partial M4B named with the actual chapter set.

An empty list is invalid. Completed chapters remain usable while later batches run. Selecting a chapter again is safe: current fingerprints allow reuse, while any changed dependency invalidates the relevant artifact.

## Source-fidelity invariant

The script generator owns segmentation, not the LLM:

1. It converts a chapter into a non-overlapping sequence of immutable fragments.
2. Each fragment has an exact source start/end span.
3. The LLM may only return metadata for the supplied fragment IDs.
4. Missing, duplicate, or extra IDs reject the response.
5. Reassembling every script line must equal the normalized source exactly once.

This prevents overlap duplication, silent omissions, rewritten prose, and line-count drift. Dialogue recognition supports straight and curly quotes, guarded single quotes, and em-dash dialogue. Unknown speakers are rejected rather than invented during script parsing.

## Character and voice model

The character analyzer retains every speaking entity. Explicit aliases and
exact display names may be consolidated directly. Name suffixes only create an
identity-adjudication candidate: the model must return verbatim source evidence
before two registry entries are merged. It assigns a unique voice to the most
important speakers up to `script.max_unique_voices`. Less prominent speakers
deterministically share a compatible major-character voice or the narrator;
the character remains distinct in script and metadata through its `voice_id`.

A character-analysis fingerprint includes the full extracted book, model, prompt, and voice cap. A change invalidates scripts and voice bootstrap. Voice reference hashes are included in generation fingerprints, so regenerated references invalidate dependent line audio.

## Artifact model

File existence is never sufficient evidence of completion.

- Script metadata records a dependency fingerprint and schema version.
- A generated-chapter manifest records every expected line ID, text hash, voice ID, source span, and generation fingerprint.
- A mastered-chapter manifest records the source segment-manifest hash and output hash.
- Output audio is checked for a readable, nonzero waveform.

At run start, generated and mastered chapter sets are independently reconstructed from these artifacts. A valid generated chapter does not imply a valid master, and a master is not trusted without its own matching manifest.

Atomic replacement is used for JSON state and final audio writes. Individual line cache entries are committed only after a selected attempt passes validation.

## Validation policy

Validation is fail-closed. A chapter response must contain exactly the expected unique line IDs and no failed lines. Hard failures include:

- word error rate over the configured threshold
- clipping
- excessive internal silence
- implausible duration/pacing
- speaker similarity below threshold
- missing, empty, or unreadable audio

Hard failures remain blocking. A segment whose transcript and hard acoustic
checks pass but which has only a soft duration/noise/score anomaly is
`accepted_with_warning`; it is cached and may proceed to mastering while the
warning remains visible in the quality report. Legacy/unresolved `flagged`
results remain unaccepted.

## Timing and mastering

There is one timing owner. When adjacent lines both request a boundary pause, the assembler uses the larger pause rather than summing both. Crossfades are applied only to directly adjacent audio, never across an intended silence.

Loudness normalization is chapter-integrated. The peak ceiling uses an oversampled true-peak estimate. The optional noise gate uses asymmetric attack/release smoothing and is disabled by default for generated audio.

The default `-19 LUFS` target is an internal playback choice, not a claim of ACX compliance.

## State, pause, and cancellation

The SQLite job queue performs atomic read-modify-write transactions and closes connections after each operation.

- User pause immediately closes an active Ollama stream, requests Voice
  cancellation, and terminates app-owned model services. In-flight work may be
  lost; completed checkpoints remain reusable.
- Generation checks cancellation at safe segment boundaries.
- Scheduled and deployment pauses park a live worker at chapter boundaries and preserve `active_stage`.
- Models unload only after the active operation releases the model lock, or after true idle time.
- Starting a second dashboard project first interrupts the current project and
  waits for its worker to release the global GPU lease. If release cannot be
  confirmed, the replacement run is refused rather than allowing concurrency.

## Storage

```text
brain/projects/<project-id>/
  book.json
  characters.json
  characters.meta.json
  script/
  book_script.json
  *.m4b

workspace/<project-id>/
  segments/
  manifests/
  chapters/
  output/

voice_library/<project-id>/
  voices.json
  *.wav

voice_cache.db
pipeline_state.db
```

Project and download paths are resolved beneath their configured roots. EPUB uploads are streamed with size limits and inspected for traversal, extreme expansion, and suspicious compression ratios.

## Privacy and network boundary

Book text is sent to the locally configured Ollama endpoint. Audio remains in
local project/workspace storage. Dashboard API and WebSocket access is allowed
without a token only when the actual TCP peer belongs to
`dashboard.trusted_lan_cidrs` or loopback; forwarded headers never grant trust.
Google Books metadata lookup is off by default and only occurs after an
explicit dashboard action or when `metadata.auto_fetch_external` is enabled.

## Feature Maintenance & Impact Guidelines

When modifying or introducing new pipeline features, developers and AI agents MUST observe the following cross-system impact guidelines:

1. **Progress & Status Alignment**:
   - Every status/stage change must stay synchronized across the SQLite job queue (`pipeline_state.db`), API endpoints (`/status`, `/voices`), and UI components (`app.js`, `pipeline.js`, `script-viewer.js`).
   - If a stage pauses execution (like `voice_review`), `GET /api/projects/{id}/voices` MUST return `review.required = True` so the UI action banner is rendered.

2. **Stage Reset Endpoint Maintenance (`POST /api/projects/{id}/reset`)**:
   - Any new file artifact, directory path, or pipeline flag MUST be registered in `reset_pipeline_stage` inside `brain/dashboard/api/main.py`.
   - Resetting to a stage must clean up downstream state AND delete corresponding disk files to guarantee clean execution upon resume.

3. **Text Coverage Invariant (`assert_script_covers_source`)**:
   - Any script generator tweak or dialogue tag handler MUST validate against `assert_script_covers_source(script, source_text)`. No source text characters or sentences may be dropped or modified.

4. **Acoustic Similarity Verification**:
   - Voice prompt similarity MUST filter out template boilerplate words (`"clearly adult speaker"`, `"maintain vocal identity..."`) to prevent false similarity warnings. Acoustic evaluation should compare 128-dim log-mel spectrogram vectors (`compute_audio_similarity`).

5. **Verification Protocol**:
   - Always run unit test discovery (`python -m unittest discover -s tests -p "test_*.py"`) and verify project reset/progress flows after making backend schema or stage changes.

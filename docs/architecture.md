# Architecture

This document describes the current implementation. Historical implementation plans describe earlier two-machine and Ubuntu designs and are not authoritative.

## Runtime layout

The supported default is one Windows workstation:

```text
Browser / Electron shell
        │
        ▼
Dashboard API :8000 ─── Ollama :11435
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

1. **Created** (`created`): The EPUB is uploaded and the project is initialized.
2. **Import / extraction**: EPUB text is extracted synchronously during project creation, chapter structure is built, cover art is saved, and the immutable upload is retained as `source.epub` for an explicit future re-extraction.
   - Non-narrative front/back matter is filtered according to the independent `skip_toc`, `skip_appendices`, `skip_front_matter`, and `skip_preface` options.
   - When appendix skipping is enabled, canonical reference sections (Glossary, Dramatis Personae, Character Lists, Cast of Characters) are captured into `reference_material` for character analysis while being excluded from narration.
   - Every spine item is recorded in `extraction_audit.json` with its navigation/landmark/markup/filename evidence, decision, confidence, word count, and reason. Large excluded shares, unresolved manifest entries, and ambiguous substantial sections are blocking.
   - Ambiguity follows the Gemini API-to-persistent-web escalation ladder. If no decision reaches the automatic threshold, the pipeline stops before scripting and exposes include, exclude, and reference choices in **Attention required**.
3. **Scripting** (`scripting`): a quality-first book-wide character pass followed
   by source-fragment attribution. Joint discovery remains available behind
   `script.joint_analysis: true` for experiments, but is not the production
   default because a missed identity can contaminate both the registry and every
   later attribution decision.
   - Scripting uses a quality-preserving compact response contract: the model
     still supplies every creative delivery and dialogue-attribution decision,
     while structurally known narrator fields, repeated scene indexes, ordinary
     spoken labels, and routine pause defaults are restored deterministically.
     See [Scripting quality and performance policy](scripting-quality-performance-policy.md).
   - Joint responses may introduce a provisional speaker only with a confidence score and fragment IDs whose local source context explicitly identifies that speaker. A compact post-pass reconciles aliases, remaps completed scripts, enriches only proven speakers from Glossary/Dramatis Personae evidence, and records accepted/rejected reference patches in `character_reference_audit.json` without rereading the full book through the LLM.
   - Dialogue attribution is model-driven and source-grounded. Unsupported IDs and
     low-confidence results trigger focused retries. Unresolved dialogue is retained as a low-confidence review item rather than a
     release-grade guess. Scripting completes and persists `attribution_audit.json`, but
     generation/export remain blocked until every review item is resolved.
4. **Bootstrapping** (`bootstrapping`): Speaking cast derived, voice design directions compiled, reference audio generated via Qwen VoiceDesign. The long request streams phase/count events for model loading, reference design, transcript validation, acoustic measurement, and cast comparison; the orchestrator maps those events into the canonical progress schema. Qwen speaker-encoder embeddings provide the primary distinctness signal; a 514-value normalized log-spectrogram summary provides a model-independent fallback diagnostic.
5. **Voice Review** (`voice_review`): Automated pause gate for user approval. Displays voice cards, preview players, and `[Approve voices & continue]` action banner.
6. **Generating** (`generating`): Qwen3-TTS synthesizes chapter audio line by line with dynamic contextual pauses (250ms same speaker, 380ms narrator, 400ms quote-to-action, 450ms turn change, 900ms paragraph break).
7. **Validating** (`validating`): Whisper Speech-to-Text transcribes audio to verify Word Error Rate (WER), acoustic clipping, and length bounds.
8. **Mastering** (`mastering`): Chapter audio assembled with chapter announcements, crossfading, and LUFS volume normalization.
9. **Exporting** (`exporting`): Mastered chapter WAVs packaged as chaptered M4B (AAC) with metadata and embedded cover art.
10. **Complete** (`complete`): Audiobook creation complete and ready for download or Home Assistant playback.

## Why analysis is book-wide but audio is incremental

Incremental delivery locks an ordered chapter plan before the first part is
published. A part is reused only when its plan, script dependencies, mastering
manifests, book metadata, file size, and SHA-256 still match. New revisions are
published immutably under `deliveries/`; the two newest superseded revisions
remain recoverable and the
atomic `deliveries/index.json` points to the current revision. Script, voice,
metadata, and reset operations mark affected parts stale instead of serving
potentially outdated audio. One project-scoped packaging lock serializes part
publication, full export, metadata remux, reset, and cleanup. Resetting to
extraction or scripting moves the complete prior `deliveries/` directory into
timestamped `delivery_history/`, preserving recovery while unlocking a new plan.
Only the two newest timestamped delivery-history archives are retained. Delivery
indexes carry an explicit schema version; known older schemas are migrated in
memory and unknown future schemas fail closed.

Speaker identity and characterization require book context. The pipeline therefore completes character analysis, the character registry, and scripts for all chapters as one logical phase.

Audio work is chapter-selectable. `generation_chapter_selection` has two meanings:

- `null`: all chapters; after missing chapters are complete, create the canonical full M4B.
- Nonempty list: generate/master those chapters and create a partial M4B named with the actual chapter set.

An empty list is invalid. Completed chapters remain usable while later batches run. Selecting a chapter again is safe: chapter-local fingerprints reuse unaffected scripts, while a source, prompt, model, relevant speaker/alias, joint-analysis revision, or dialogue-delivery policy revision invalidates only the dependent chapter. Joint discovery persists a character-registry checkpoint after each completed chapter.

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
before two registry entries are merged.

Following local identity consolidation, the analyzer builds a bounded evidence
dossier. Configured Gemini API triage, adjudication, and optional browser
fallback may propose profile enrichment. Only high-confidence proposals with
verbatim source evidence are applied; conflicts and abstentions remain visible
for Voice Review. Provider models, budgets, retries, and circuit breaking come
from `external_validation`; the presence of an API key alone never enables this
pass. See
[Character Augmentation and Gender Resolution](character-augmentation-and-gender-resolution-2026-08-19.md).

Human corrections made in Voice Review are stored separately in
`character_overrides.json`. The pipeline reapplies these corrections after
automated analysis and joint reconciliation, so a forced re-analysis cannot
silently undo an operator decision. A correction invalidates only chapters in
which that character speaks and requires renewed voice approval; changing the
owner of a generated design also marks its preview for regeneration.

It assigns a unique voice to the most important speakers up to `script.max_unique_voices`.
Less prominent speakers deterministically share a compatible major-character voice
or the narrator; the character remains distinct in script and metadata through its `voice_id`.
User-uploaded custom voice samples are strictly preserved during casting and re-synthesis.

A character-analysis fingerprint includes the full extracted book, model, prompt, and voice cap. A change invalidates scripts and voice bootstrap. Voice reference hashes are included in generation fingerprints, so regenerated references invalidate dependent line audio.

## Artifact model

File existence is never sufficient evidence of completion.

- Script metadata records chapter-local speaker dependencies, a dependency
  fingerprint, and schema version.
- A generated-chapter manifest records every expected line ID, text hash, voice ID, source span, and generation fingerprint.
- A mastered-chapter manifest records the source segment-manifest hash and output hash.
- Output audio is checked for a readable, nonzero waveform.

At run start, generated and mastered chapter sets are independently reconstructed from these artifacts. A valid generated chapter does not imply a valid master, and a master is not trusted without its own matching manifest.

Atomic replacement is used for JSON state and final audio writes. A valid WAV
gets a synthesis fingerprint immediately, while validation acceptance is a
separate hash-bound checkpoint. This lets an interrupted run reuse synthesis
without falsely treating unvalidated audio as accepted. The chapter manifest
remains the completeness boundary.

Pipeline state also carries a versioned progress snapshot alongside temporary
legacy fields. The snapshot names the stage/phase, stable line ID and ordinal,
cache state, elapsed time, server-derived ETA/confidence, and update timestamp.
Frequent status responses carry only a compact `voice_cast_summary`; the full
cast and pairwise diagnostic matrix remain atomic in `voice_cast.json`. The
dedicated voice-review endpoint returns only approval-relevant similarity pairs.
The quality endpoint retains aggregate evidence for every segment while sending
detailed final records only for warnings, failures, and retries, plus complete
history for lines that actually retried. These are transport optimizations, not
quality-gate reductions. Quality and review endpoints scope audio evidence to
the reconciled generated/mastered chapter sets. Older results remain on disk as
an audit trail but are labeled archived and cannot block or misrepresent the
current generation.

At run start, the pipeline's full artifact reconciler verifies dependency,
voice-reference, manifest, and output hashes before updating generated/mastered
chapter state. Frequent status polling trusts those reconciled sets; partial WAV
presence is progress evidence, never completion evidence, and a stale manifest
is never promoted by a weaker dashboard-only check.

Gemini web escalation runs in the dashboard Python environment and uses the
dedicated authenticated Chrome profile. Readiness reports dependency and
profile state separately. Production requests reuse one saved conversation per
validation purpose until its configured turn limit; API triage/adjudication
remain earlier, cheaper steps in the ladder.

Performance JSONL records use schema version 2 and are summarized by selecting
the latest successful record for each chapter. TTS segment measurements include
model load, reference-prompt/cache work, autoregressive generation, decoding,
concatenation, post-processing, WAV writing, and total time. Summaries expose
p50/p90/p95 synthesis latency and real-time factor by cache state, text length,
speaker role, and cold/warm model state without storing source prose.

Human join-review dispositions live in the state database and never mutate
source or audio artifacts. Cleanup is similarly constrained: the API returns
an exact preview token and accepts deletion only if that candidate set is
unchanged and remains inside the project workspace.

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

There is one timing owner. When adjacent lines both request a boundary pause,
the assembler uses the larger pause rather than summing both. Crossfades are
applied only to directly adjacent audio, never across an intended silence.

Quoted dialogue and a short attached narrator tag are separately synthesized
with their correct voices, then linked by `utterance_group_id`. A grouped
boundary overrides adjacent pause requests to zero and explicitly disables
crossfade; this preserves a natural, tightly connected reading without making
the character speak narrator prose. The usual join diagnostic still reports
the level delta and abrupt-boundary measurement. Chapter-integrated loudness
normalization remains the only automatic gain stage; local boundary gain is not
changed without repeatable listening evidence.

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
Up to ten candidates are ranked by normalized title and author similarity; a
minimum-confidence match is persisted as a query-keyed review artifact. Cover
bytes are host, protocol, size, type, and dimension checked before atomic
storage. When the automatic confidence gate rejects the best result, the
dashboard can expose all ranked search candidates for human selection. The
selected provider volume ID is validated and fetched exactly before it becomes
the review artifact. Explicit manual application adopts the reviewed identity,
retains EPUB identity as provenance, and requires explicit consent to replace
embedded cover art. Existing exports are atomically metadata-remuxed with
stream copying. Automatic enrichment still fills only missing fields and
preserves EPUB identity and cover.

## Feature Maintenance & Impact Guidelines

When modifying or introducing new pipeline features, developers and AI agents MUST observe the following cross-system impact guidelines:

1. **Progress & Status Alignment**:
   - Every status/stage change must stay synchronized across the SQLite job queue (`pipeline_state.db`), API endpoints (`/status`, `/voices`), and UI components (`app.js`, `pipeline.js`, `script-viewer.js`).
   - If a stage pauses execution (like `voice_review`), `GET /api/projects/{id}/voices` MUST return `review.required = True` so the UI action banner is rendered.

2. **Stage Reset Endpoint Maintenance (`POST /api/projects/{id}/reset`)**:
   - Any new file artifact, directory path, or pipeline flag MUST be registered in `reset_pipeline_stage` inside `brain/dashboard/api/main.py`.
   - Resetting must invalidate only the artifacts owned by that operation. Re-validation preserves segment WAVs, re-export preserves mastered WAVs, and re-extraction is allowed only when the preserved `source.epub` exists.

3. **Text Coverage Invariant (`assert_script_covers_source`)**:
   - Any script generator tweak or dialogue tag handler MUST validate against `assert_script_covers_source(script, source_text)`. No source text characters or sentences may be dropped or modified.
   - Dialogue tags remain narrator-owned. If they are tightly coupled to a
     quote, use `utterance_group_id`; do not merge their text into a character
     line. A grouping-policy change must invalidate the script fingerprint.

4. **Acoustic Similarity Verification**:
   - Voice prompt similarity MUST filter out template boilerplate words (`"clearly adult speaker"`, `"maintain vocal identity..."`) to prevent false similarity warnings. Acoustic evaluation uses the Qwen speaker encoder plus the model-independent normalized log-spectrogram diagnostic in `compute_audio_similarity`.

5. **Verification Protocol**:
   - Always run unit test discovery (`python -m unittest discover -s tests -p "test_*.py"`) and verify project reset/progress flows after making backend schema or stage changes.

6. **Voice Cast vs Character Registry Split**:
   - `voice_cast.json` (in `brain/projects/<id>/`) is the **authoritative speaker → voice mapping** during generation. The narrator's approved candidate (e.g. `narrator_male`) is stored there under `assigned_characters` and is **not** written back to `characters.json`.
   - Any code that resolves which voice to use for a script line MUST read `voice_cast.json` first (via `assigned_characters`), then fall back to `characters.json`'s `voice_id` field, then fall back to the raw speaker ID. See `_prepare_generation_lines` in `pipeline.py`.
   - `VoiceLibraryManager.get_voice_path` consults `voices.json` to find the actual hashed WAV filename. Do not construct voice file paths by hand as `<voice_id>.wav` — they are content-hashed and registered in the voice library registry.

7. **Dual-Endpoint & 24/7 NAS Streaming Architecture**:
   - `https://crazyha.mywire.org/audiobook/` $\rightarrow$ Proxies to Creator PC dashboard (`192.168.50.44:8000`) for project creation, script inspection, cast review, and pipeline execution.
   - `https://crazyha.mywire.org/bookplayer/` $\rightarrow$ Proxies to isolated 24/7 container `crazy-bookplayer-streamer` on Ubuntu server (`192.168.50.180:8005`) streaming directly from `/mnt/nas/media/crazybooks`.
   - Backed by persistent progress saving at `/mnt/nas/media/crazybooks/{projectId}/progress.json`, allowing continuous 24/7 mobile playback even when the Creator PC is asleep.

8. **Pipeline Notification System**:
   - Non-blocking, asynchronous alerts delivered via Home Assistant REST API (`notify.crazywiz_notification_group`).
   - Triggers on speaking cast approval required, pipeline errors, new incremental delivery parts published, full book export ready, and delivery batch pauses.

The 2026-08-21 attribution, sparse scripting, cast-distinctness,
cross-chapter drift, resilience, and confidence-calibration rationale is recorded
in [quality-performance-hardening-2026-08-21.md](quality-performance-hardening-2026-08-21.md).

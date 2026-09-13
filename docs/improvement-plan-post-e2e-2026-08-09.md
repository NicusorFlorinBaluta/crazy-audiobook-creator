# Post-E2E improvement plan — 2026-08-09

**Status:** Historical record — A dated record of what was done and why. Evidence, not a specification -- do not implement from it.

## Purpose

This plan reconciles the pre-E2E review recommendations with the evidence from
the completed `sample_book-13` full-book run. It deliberately separates proven
issues from hypotheses. Correctness changes must preserve the successful
baseline, and performance changes must win a controlled benchmark before they
become defaults.

The supporting measurements and incident details are in
[e2e-run-2026-08-09.md](e2e-run-2026-08-09.md).

## Implementation status — 2026-08-09

Implemented without running production models:

- separate synthesis and validation checkpoints, hash-bound resume behavior,
  selected-attempt quality semantics, and crash-resume regression coverage;
- versioned progress/ETA snapshots, scripting and generation instrumentation,
  schema-v2 performance JSONL plus JSON/CSV summaries;
- startup config validation, runtime/backend preflight, tested Windows AMD
  constraints, explicit model-test opt-in, and tiered verification tooling;
- persistent join review, adjacent segment playback, attempt history, storage
  inventory, preview-token cleanup, redacted support bundles, and managed-log
  rotation;
- source-first bootstrap reference ranking with a minor-character-only seed
  fallback, disabled adaptive-token experiment scaffolding, and low-resource CI.

Intentionally not promoted or exercised tonight:

- adaptive token caps, utterance grouping changes, scripting chunk changes,
  join/mastering tuning, GPU concurrency, or other output-changing defaults;
- fixed-audio/model microbenchmarks, one-chapter generation, interruption/GPU
  ownership, listening review, and clean full E2E release evidence;
- the orchestration stage-runner refactor, which remains gated on equivalent
  representative-chapter manifests after the behavior contracts settle.

The ordered live gates are documented in
[live-validation-plan-2026-08-10.md](live-validation-plan-2026-08-10.md).

## Proven baseline

| Area | Full-book result | Planning consequence |
| --- | ---: | --- |
| Source | 8 chapters, 11,874 words | Use this book as the fixed comparison corpus |
| Script | 563 segments; Pass 2 took 5,376.9 s | Scripting is the second major optimization target |
| Generation | 9,962.8 s successful set | Generation needs durable checkpoints and better telemetry |
| TTS | 8,572.1 s; 86.0% of generation | TTS is the primary performance target |
| Fresh TTS RTF | 2.10× synthesis/audio | Compare tuning against RTF, not segments/minute |
| Validation | 527 pass, 36 accepted warnings, 0 failed/flagged | Keep the current fail-closed policy and WER threshold |
| Retries | 14 | Retry volume is not the main throughput problem |
| Peak VRAM | 13.745 GB | Capacity exists, but it does not prove concurrent generation is safe |
| Mastering | 169.6 s; 63 join warnings | Review joins before changing mastering defaults |
| Export | 49.6 s; valid 8-chapter M4B | Export performance is not a priority |
| Tests | 132 passing | Preserve this as the minimum local regression gate |

## Decisions updated by the E2E

The following are now closed decisions, not backlog items:

- Keep OpenAI Whisper `large-v3` as the explicit AMD/ROCm validator backend.
  Installing `faster-whisper` changed automatic selection and caused the
  migration regression; CTranslate2's Windows CUDA path is not the production
  ROCm path.
- Keep raw-audio transcription as the default. Controlled failed-clip tests
  proved that Silero VAD removed short/high-pitched speech and repetitions.
- Keep `wer_threshold: 0.20` and fail-closed hard checks. The successful run did
  not require weakening validation.
- Keep TTS and Whisper sequential for the long initial chapter pass, with
  co-residency limited to retries. Do not add parallel GPU workers merely
  because peak allocated VRAM was below total VRAM.
- Do not globally lengthen crossfades or apply blanket gain changes based only
  on the 63 diagnostic warnings. Those warnings require listening evidence.
- Bootstrapping, narrator selection, mastering response data, export response
  data, quality-status classification, and final running-state cleanup were
  repaired and passed the completed run. Retain them as regression tests rather
  than reimplementing them.

## Priority roadmap

### P0 — Preserve and reproduce the baseline

Goal: turn the repaired full-book result into a repeatable release baseline
before making optimization changes.

#### P0.1 Review and land the current change set coherently

- Separate the current broad working-tree batch into reviewable commits by
  concern: runtime correctness, validation/cache behavior, dashboard/API,
  environment/setup, tests, and documentation.
- Confirm that deleted scratch utilities are intentionally obsolete before
  including their removal.
- Re-run the 132-test suite, JavaScript syntax checks, Python compilation, and
  `git diff --check` against the exact commit candidate.
- Record the tested commit hash in the next benchmark report.

Acceptance:

- The committed tree is clean and every file in the batch has an identified
  purpose.
- Local non-GPU verification remains green.
- No generated book, model, environment, database, or log artifact is added to
  source control.

#### P0.2 Make the voice environment reproducible

- Add a machine-readable environment report to the setup/preflight flow:
  Python, PyTorch/ROCm, Qwen TTS, Whisper, FFmpeg, selected attention backend,
  validator backend, VAD mode, and `pip check` result.
- Pin direct voice-runtime dependencies or provide a tested constraints file.
  Do not pin AMD's PyTorch build to an unavailable public wheel.
- Add a compatibility assertion that refuses `faster_whisper` GPU mode on the
  current Windows AMD profile instead of silently selecting it.
- Expose the effective backend and model through health/preflight UI, not only
  configuration files.

Acceptance:

- A setup dry run explains what it would change.
- A clean compatible environment reaches healthy state with
  `openai_whisper`, `large-v3`, and raw audio explicitly reported.
- An incompatible backend fails before a long pipeline run with an actionable
  message.

#### P0.3 Add tiered E2E tooling

- Create one supported, non-destructive smoke runner that creates a disposable
  project ID, records stage metrics, verifies artifacts with `ffprobe`, and
  leaves evidence for inspection.
- Add benchmark schema/version, source hash, config fingerprints, commit hash,
  environment report, and final artifact hash to a run manifest.
- Support four verification tiers:
  1. unit/static checks with no models;
  2. fixed-clip validator and TTS microbenchmarks;
  3. one representative chapter through export;
  4. clean multi-chapter/full-book release run.
- Do not rerun an 82-minute book for routine changes when a lower tier proves
  the relevant contract.

Acceptance:

- A failed run and a resumed run both produce valid, comparable manifests.
- Metrics can distinguish fresh synthesis, synthesis reuse, validation reuse,
  and repair/resume time.

### P1 — Durability, observability, and progress UX

Goal: make failures cheaper and make the dashboard accurately explain what the
pipeline is doing and how long it is likely to take.

#### P1.1 Commit accepted line results incrementally

- Persist a synthesis fingerprint immediately after a valid WAV is written,
  separately from its validation acceptance record.
- Persist each accepted validation result immediately after initial validation
  or after a retry candidate becomes the selected artifact. Use atomic writes
  or one small database transaction per line.
- Keep the chapter manifest as the final completeness boundary. An incomplete
  chapter may reuse valid lines, but it must never be reported generated or
  mastered.
- Store the artifact hash with both records so a partial or externally changed
  WAV cannot be reused.
- Add crash-injection tests after synthesis, after validation, during retry
  replacement, and immediately before manifest commit.

Acceptance:

- A forced exit after line N reuses all valid, hash-matching work through N.
- Resume performs no false acceptance and never marks an incomplete chapter
  complete.
- Changing text, spoken text, voice/reference, pronunciation, delivery, FX,
  model settings, or validator settings invalidates only the appropriate cache.

#### P1.2 Consolidate the progress contract

- Replace loosely related status fields with one versioned progress snapshot:
  stage, phase, chapter ordinal/total, line ordinal/total, line ID, attempt,
  cache state, completed work, elapsed time, ETA, and last-update timestamp.
- Keep `current_line` only as a temporary compatibility alias for one release;
  use line ordinal and `current_line_id` as the canonical fields.
- Populate scripting chunk progress, which is already available in the script
  generator but is not currently connected by the orchestrator.
- Distinguish active work, waiting for review, waiting for schedule, retrying,
  unloading/loading a model, paused, interrupted, and terminal failure.
- Show “last activity” and service health so a slow model call is not mistaken
  for a frozen worker.

Acceptance:

- Every stage updates at a useful sub-stage boundary.
- Reset always displays a non-running state; completion always clears running.
- Frontend tests cover reconnect, stale updates, pause/resume, retry, cache hit,
  review gate, schedule parking, and terminal completion.

#### P1.3 Add evidence-based ETAs

- Scripting ETA: recent seconds per fragment/word and completed chunk count.
- TTS ETA: recent synthesis seconds per generated audio second, adjusted by
  queued text length and cache hits.
- Validation ETA: recent seconds per uncached segment plus expected retry cost.
- Mastering/export ETA: chapter audio duration using historical throughput.
- Report a confidence/range until enough observations exist; do not present a
  precise ETA based on one segment.

Acceptance:

- `elapsed_seconds` advances for scripting and all later stages.
- On the fixed book, the final ETA at the halfway point is within 25% for
  scripting and generation, excluding human voice-review time.
- ETA resets or widens after model reload, a new retry pattern, or a material
  cache-rate change.

#### P1.4 Clarify and extend performance metrics

- Rename ambiguous log labels to `validation_cache_hits` and
  `validation_cache_misses`.
- Add synthesis reuse/regeneration counts and separate cold model-load time.
- Record per-segment text characters/words, generated duration, synthesis
  duration, RTF, retry duration, backend, attention implementation, and peak
  allocated/reserved VRAM. Do not store source text in metrics.
- Version `performance_metrics.jsonl` and provide a summarizer that selects the
  final successful record per chapter without manual log archaeology.
- Surface chapter and run summaries in the dashboard with a JSON/CSV export.

Acceptance:

- Fresh, cached, failed, and resumed work cannot be conflated in logs or UI.
- The summarizer reproduces the documented `sample_book-13` totals within
  rounding tolerance.

### P2 — Listening quality and quality-review UX

Goal: turn existing diagnostics into an efficient human review workflow and
only then tune mastering or reference selection.

#### P2.1 Expose join diagnostics as reviewable items

- Add an API response that maps every warning to chapter, previous/current
  line IDs, speaker names, pause/crossfade values, level delta, and reasons.
- Add a Quality-tab “Joins” queue sorted by severity, with direct playback of a
  short window around the boundary and links to both script lines.
- Persist `unreviewed`, `acceptable`, `needs_remaster`, and `source/TTS issue`
  dispositions without changing source artifacts.
- Seed the first listening pass with the 63 warnings from `sample_book-13`,
  prioritizing chapters 2, 4, 7, and 8.

Acceptance:

- Every join warning is reachable in two clicks or fewer from the project.
- Review state survives restart and can be exported with the run report.

#### P2.2 Tune joins only from confirmed defects

- If listening confirms level jumps, benchmark bounded local gain matching on
  adjacent segment edges rather than renormalizing entire chapters.
- If listening confirms hard cuts, compare the current 30 ms cosine crossfade
  with a small bounded set of alternatives only on pause-free joins.
- Never crossfade across requested pauses and never conceal missing or clipped
  audio with mastering.

Acceptance:

- A blinded comparison prefers the change on the confirmed problem set.
- Chapter LUFS spread, peak ceiling, words, pauses, chapter duration, and line
  ordering remain within configured tolerances.
- Clean joins do not regress.

#### P2.3 Improve warning and retry review

- Show `accepted_with_warning` separately from failures everywhere, with the
  exact soft reason and direct audio/script links.
- Show all attempts for a retried line and identify which artifact won and why.
- Add filters for WER, duration, noise, prosody, speaker similarity, retry
  count, chapter, speaker, and review state.
- Complete a human listening pass over all 36 accepted warnings and a sample of
  clean lines, not just automated metrics.

Acceptance:

- The dashboard counts match the quality API and final chapter manifests.
- Reviewers can distinguish a harmless diagnostic warning from a hard defect
  without opening raw JSON or SQLite.

#### P2.4 Improve bootstrap reference selection

- Rank real character dialogue candidates by duration, lexical diversity,
  repetition, punctuation intensity, ASR confidence/WER, clipping, silence,
  and acoustic cleanliness.
- Prefer multiple source lines from the same character when one line is too
  short. Use a generic gender-appropriate sentence only for a truly minor
  character whose total usable dialogue remains insufficient.
- Reject highly repetitive/emphatic canonical references when a clearer real
  alternative exists. Preserve the generated candidate set so the UI can offer
  the next-best reference without regenerating everything.
- Use Starling's strict-WER retries from the E2E as the initial regression case.

Acceptance:

- Every registered reference meets configured duration and hard audio checks.
- The chosen transcript and reference audio remain paired and fingerprinted.
- A fixed bootstrap fixture selects diverse real dialogue ahead of generic or
  repetitive fallbacks.

### P3 — Performance optimization

Goal: reduce wall time while preserving source coverage, identity, validation,
and listening quality.

#### P3.1 Build controlled performance fixtures first

- Create a fixed TTS set covering short emphasis, ordinary dialogue, long
  narration, repeated words, fictional names, and two or more voices.
- Run cold and warm measurements with at least three repetitions.
- Capture median and tail latency, synthesis/audio RTF, output duration, WER,
  speaker similarity, warnings, retries, VRAM, and runtime warnings.
- Change one factor at a time and retain the current configuration as control.

Acceptance:

- Benchmark variance and raw results are saved with environment/config hashes.
- No setting becomes default from a single clip or a single timing.

#### P3.2 Profile and optimize Qwen TTS

- Instrument MIOpen fallback/workspace warnings and allocated versus reserved
  memory; the E2E emitted repeated fallback requests approaching 500 MiB.
- Reconfirm `sdpa` versus `eager` on the representative fixture; `sdpa` already
  won the earlier local smoke benchmark, so avoid retesting unsupported
  backends without a compatibility reason.
- Measure whether `max_new_tokens: 4096` is causing avoidable decode work.
  Experiment with a conservative length-aware cap plus a safe retry-on-
  truncation path.
- Benchmark the existing narrator/dialogue/expressive utterance grouping
  limits. Longer groups reduce calls but must preserve pauses, delivery,
  speaker attribution, WER, and source-span traceability.
- Profile reference-prompt preparation and reuse within a speaker. Optimize
  only if it is material beside autoregressive generation time.
- Keep chapter-long TTS and Whisper sequential unless a benchmark proves a
  different residency policy faster and stable.

Promotion criteria:

- At least a 10% median improvement in fresh synthesis/audio RTF on the fixed
  fixture or a clearly documented reason to retain the current default.
- No missing/truncated speech, hard validation regression, material WER or
  speaker-similarity regression, or increase in retry rate.
- One representative chapter passes before the setting is used for a book.

#### P3.3 Optimize scripting Pass 2

- Record prompt tokens, output tokens, tokens/second, fragment count, source
  words, retry count, and validation failures for each of the 39 E2E calls.
- Benchmark a small matrix around the current 350-word/40-fragment bounds.
  Larger chunks may reduce call overhead but must stay within the 16,384-token
  context and preserve complete ID coverage.
- Reduce repeated prompt/registry tokens without removing speaker evidence,
  exact-ID requirements, or source-fidelity constraints.
- Verify Ollama model residency and prompt/KV-cache behavior before changing
  models or introducing concurrency.
- Keep chapters sequential because previous chapter summaries provide
  continuity; parallel chapter annotation is not the first optimization.

Promotion criteria:

- At least 20% lower Pass 2 wall time on the fixed book or a documented no-win
  result.
- 100% source-span coverage, complete/unique IDs, no unknown speakers, no
  increase in conservative fallbacks, and manually sampled attribution quality
  equal to the baseline.

#### P3.4 Treat GPU parallelism as a gated experiment

- Do not implement production multi-worker generation yet.
- First run an isolated two-request stress benchmark that records model thread
  safety, native-runtime stability, peak allocated/reserved VRAM, MIOpen
  workspace, throughput, and tail latency.
- Reject parallelism if it merely divides GPU throughput, increases retries, or
  compromises pause/cancel cleanup.

Promotion criteria:

- At least 20% aggregate throughput improvement under representative load,
  stable cancellation, deterministic artifact ownership, and safe VRAM margin.
- Otherwise retain the one-worker architecture.

### P4 — Operations, CI, and release quality

Goal: prevent recurrence and make release readiness visible.

#### P4.1 Strengthen automated gates

- CI: Python unit tests, compilation, JavaScript syntax, dependency/import
  checks, documentation links, conflict markers/whitespace, and API schema
  contract tests.
- Add regression cases for backend selection, raw/VAD behavior, NumPy JSON
  scalars, narrator mapping, mastering/export response preservation, cache
  invalidation, and running-state cleanup.
- Keep GPU/model tests manual or scheduled on the compatible workstation; do
  not pretend CPU mocks validate ROCm runtime behavior.
- Publish the latest compatible environment and benchmark manifest as build
  artifacts where practical.

Acceptance:

- Every proven E2E failure has a deterministic lower-tier regression test where
  technically possible.
- A release checklist clearly separates mocked tests, fixed-audio tests, GPU
  smoke tests, and full E2E evidence.

#### P4.2 Finish operational promotion gates

- Run the still-open disposable second-project interruption test and prove no
  overlapping GPU owners.
- Retain the already exercised pause/resume, dashboard restart, schedule
  close/open, and cached-artifact behavior as regression scenarios.
- After P0–P3 changes stabilize, run one clean unattended representative
  multi-chapter test. Reserve another full-book run for the release candidate,
  not each optimization.
- Verify app-owned Voice/Ollama processes, locks, ports, and GPU allocations are
  released after success, pause, cancel, and error.

Acceptance:

- No stale `running` state, orphaned app-owned process, port, or GPU lease.
- The second project cannot synthesize until the first has acknowledged
  interruption and released ownership.

#### P4.3 Storage, diagnostics, and supportability

- Add a dashboard storage view separating source EPUB, resumable segments,
  validation data, mastered chapters, exports, logs, and disposable candidates.
- Implement preview-first cleanup with explicit retention rules; never delete
  the source, selected voice references, current manifests, or final exports by
  default.
- Rotate managed service logs and preserve the failure tail plus environment
  manifest for diagnosis.
- Add a downloadable support bundle with config secrets redacted.

Acceptance:

- Cleanup reports exact files/bytes before mutation and cannot escape the
  project/workspace roots.
- A native runtime failure can be diagnosed without relying on an uncaptured
  subprocess console.

### P5 — Architecture and maintainability

Goal: reduce future defect risk after behavior is protected by the preceding
tests and metrics.

#### P5.1 Split orchestration by stage without changing behavior

- Extract scripting, bootstrapping, generation, mastering, and export stage
  runners from the large orchestrator module behind typed request/result
  contracts.
- Centralize lifecycle ownership, stop checks, stage transitions, metrics, and
  artifact reconciliation rather than duplicating them in stage code.
- Keep one coordinator responsible for the global GPU lease and service
  ownership.
- Perform this as incremental moves with characterization tests, not a rewrite.

Acceptance:

- Existing state/artifact formats remain compatible.
- The tier-3 representative chapter has identical manifests and equivalent
  quality before and after the refactor.

#### P5.2 Type and version state/config contracts

- Replace ad-hoc job-state dictionary updates with typed patch methods for
  progress, lifecycle, chapter completion, review gates, and selection.
- Validate configuration at startup with actionable errors and expose the
  effective, redacted configuration and schema version.
- Version persisted job state, metrics, manifests, voice profiles, and cache
  records with explicit migrations or compatibility readers.
- Keep API response contract tests for fields consumed by the orchestrator and
  dashboard so Pydantic cannot silently discard required data again.

Acceptance:

- Unknown/invalid config fails before model loading.
- Old supported projects either load successfully or receive a clear migration
  requirement; they are never silently reset.

#### P5.3 Keep documentation release-coupled

- Update architecture, configuration, API, QA, setup, and benchmark documents
  in the same change that modifies their behavior.
- Replace the older “promotion gates still open” section with links to the
  latest run and this plan, while preserving historical results.
- Generate or validate API examples from current Pydantic/OpenAPI schemas where
  practical.

Acceptance:

- README and current docs agree on platform, backends, statuses, cache rules,
  quality semantics, and artifact paths.
- Historical plans and chat logs remain clearly labeled as non-authoritative.

## Recommended implementation sequence

1. **Baseline:** P0.1–P0.3.
2. **First implementation batch:** P1.1 and P1.4, because they reduce lost work
   and make every later benchmark trustworthy.
3. **Second implementation batch:** P1.2–P1.3 plus the progress/status UI.
4. **Quality batch:** P2.1 and P2.3, followed by the listening audit; implement
   P2.2 only if the audit confirms audible join defects.
5. **Performance batch:** P3.1, then TTS and scripting experiments in separate
   branches/commits. Promote only measured winners.
6. **Bootstrap batch:** P2.4 with Starling and fixed multi-character fixtures.
7. **Operational gate:** P4.1–P4.3 and a clean multi-chapter run.
8. **Maintainability:** P5 after the behavior and metric contracts are stable.
9. **Release gate:** clean unattended full-book run, artifact verification, and
   targeted human listening report.

## Suggested first milestone

The first milestone should include P0 plus incremental line checkpoints and
versioned performance summaries. It provides the largest risk reduction with
minimal audio-behavior change and creates trustworthy measurements for all
later speed work.

Milestone exit criteria:

- Current changes are coherently reviewed and landed.
- Environment/backend preflight is explicit and reproducible.
- Crash-injection resume tests pass with per-line reuse.
- Metrics distinguish synthesis and validation cache behavior.
- A tier-3 representative chapter produces a valid chapterized artifact with
  no hard quality failures.

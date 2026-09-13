# Unattended pipeline validation and full-app audit plan — 2026-08-23

## Objectives

1. Safely monitor the active full-book project through voice bootstrapping.
2. Analyze the completed scripting and voice-reference generation for quality,
   reliability, and performance, accounting for concurrent game GPU usage.
3. Verify, commit, and push the already completed and validated change set.
4. Perform a new whole-application audit covering backend functionality,
   pipeline reliability, progress reporting, attribution and Gemini escalation,
   audio generation, mastering, configuration, and UI/UX consistency.
5. Implement only well-supported findings from the new audit, verify them, and
   leave this second batch uncommitted for operator review.

## Safety and autonomy constraints

- Preserve completed scripts, metadata, voice references, generation outputs,
  deliveries, and stage checkpoints.
- Do not approve voice review or any other human review gate.
- Do not change working hours, chapter selection, or delivery selection.
- Do not expose source text, dialogue, character spoilers, or generated audio
  content in logs or reports.
- Prefer recovery from durable checkpoints over resetting a stage.
- Do not promote Qwen 3.8 into another stage solely because it is newer. Each
  proposed use must be supported by task suitability, structured-output safety,
  resource cost, and a quality-preserving fallback.
- Treat scripting throughput from the current run as GPU-contaminated because a
  game was running. Structural failures, retries, confidence trails, artifact
  integrity, and quality-gate results remain valid evidence.

## Phase 1 — bootstrapping monitor

- Poll dashboard health, project status, active phase, worker state, activity
  timestamp, required voice count, ready-reference count, retry/failure state,
  cast-quality diagnostics, and review blockers.
- Distinguish long-running voice work from a dead worker using process/service
  health and artifact timestamps.
- If stalled or crashed, diagnose first and use only checkpoint-preserving
  recovery.
- Completion criterion: bootstrapping is complete or the project reaches the
  required voice-review gate with all required references ready and no hidden
  generation error.

## Phase 2 — run analysis

- Scripting: chapter/line counts, duration, cache/retry/fallback behavior,
  attribution confidence distribution, deterministic repairs, Gemini
  escalation outcomes, final audit, and unresolved review items.
- Voice references: required/ready/failed counts, WER and acoustic quality,
  retries, seed-text use, cast-similarity warnings, stale diagnostics, and
  bootstrapping duration.
- Separate actionable correctness findings from throughput observations affected
  by shared GPU load.

## Phase 3 — commit and push validated work

- Review the complete working tree, protecting unrelated user-owned changes.
- Run whitespace checks and the broad automated test suite, including the
  pytest-only resilience tests separately when necessary.
- Confirm runtime health and artifact preservation.
- Commit the validated pre-audit batch with an accurate summary and push the
  current branch to its configured upstream.
- Record the commit hash and remote result.

## Phase 4 — fresh full-app audit

### Pipeline and reliability

- Stage transitions, resume/restart semantics, checkpoint invalidation, schedule
  interaction, task/watchdog behavior, partial failure recovery, idempotency,
  concurrency, external-service timeouts, and atomic persistence.

### Models and validation

- Review every local-model use for potential Qwen 3.8 benefit without blindly
  expanding GPU cost.
- Validate speaker attribution, confidence calibration, deterministic evidence,
  Gemini candidate constraints, escalation coverage, disagreement handling,
  audit-to-review propagation, and provider failure behavior.

### Audio and delivery

- Voice-reference quality gates, voice distinctness, TTS retry/cache behavior,
  WER/acoustic validation, line repair, chapter mastering, loudness/peak
  compliance, M4B export, incremental delivery, and stale-output detection.

### API, UI/UX, and progress

- API error semantics, data size, lifecycle controls, review flows, responsive
  behavior, accessibility, visual/component consistency, empty/error/loading
  states, destructive-action clarity, and mobile parity.
- Inventory all progress producers and consumers. Standardize stage, phase,
  units, percentages, activity timestamps, ETA confidence, cache/retry state,
  and stalled-work presentation where safe.

### Configuration, security, and maintainability

- Configuration validation and defaults, secret handling, path safety, process
  management, logging/privacy, dependency boundaries, duplicated logic, tests,
  documentation, and operational diagnostics.

## Phase 5 — implementation of audit findings

- Rank findings by correctness/data-loss risk, reliability, quality, usability,
  and performance.
- Implement only changes whose behavior and migration/checkpoint impact are
  understood.
- Add focused regression tests and update documentation for material behavior.
- Run the broad test suite plus relevant frontend/static checks and targeted
  runtime smoke tests.
- Do not commit or push this second batch.

### Approved implementation shortlist from the fresh audit

1. Treat only a finalized segment manifest, never the mere presence of one WAV
   file, as recovery evidence that a chapter finished generation.
2. Keep the full durable voice cast in `voice_cast.json`, but omit it from the
   two-second status response and return only actionable similarity diagnostics
   from the dedicated voice-review endpoint.
3. Poll rapidly only while work is active, stop refetching an unchanged cast at
   the voice-review gate, and throttle secondary attention/delivery requests.
4. Stream privacy-safe voice-bootstrap phase/count events and translate them to
   the canonical progress schema, including a keepalive and safe cancellation
   boundary for the long-running request.
5. Make canonical backend progress authoritative in both dashboard progress
   views, remove the competing browser-only ETA estimate, and surface stale
   progress timestamps instead of claiming every running job is live.
6. Return only noteworthy final audio attempts plus the complete history for
   actually retried lines; retain aggregate counts for every segment.
7. Present historical voice-design contrast directions as notes, not active
   acoustic warnings, while preserving the real similarity acknowledgement
   gate and full diagnostics on disk.
8. Fix visible UTF-8 mojibake and avoid rounding a sub-100% acceptance rate up
   to a misleading `100%`.
9. Include registered speakers already present in the chapter's script and
   attribution evidence in Gemini's chapter candidate set, so a correct but
   locally unnamed speaker is not accidentally made impossible to select.
10. Treat generated/mastered chapter state as authoritative for audio quality
    and review data, archive stale diagnostics in the UI, and invalidate the
    matching reports/review rows whenever their audio stage is reset.
11. Make Gemini browser setup target the same lightweight Python environment
    used by the dashboard, expose dependency readiness separately from profile
    readiness, and verify the Pro profile, audio upload, JSON response, and
    persistent-conversation behavior with spoiler-free synthetic data.

Automatic per-segment loudness leveling is deliberately deferred: the current
join diagnostics show a useful opportunity, but changing dynamics without a
listening A/B could reduce performance nuance. Qwen 3.8 also remains confined
to character analysis and scripting because metadata retrieval, deterministic
gates, TTS, mastering, and export do not gain a justified quality advantage
from adding another text-model dependency.

## Final report

- Bootstrapping outcome and any recovery action.
- Scripting/voice-generation quality and performance findings.
- Commit hash and push destination for the validated first batch.
- Fresh audit findings, including rejected or deferred ideas and why.
- Exact uncommitted changes made in the second batch, verification results,
  residual risks, and recommended operator checks.

## Execution record

- The validated pre-audit batch was committed as `debd457` and pushed to
  `origin/dev`.
- The second batch remains uncommitted and unpushed by design.
- Focused regressions: 110 passed.
- Full suite: 410 passed, 2 skipped, 10 subtests passed.
- Frontend syntax, Python compilation, and whitespace checks passed.
- Gemini web Pro opened from the dashboard interpreter; a synthetic two-turn
  run successfully reused one conversation and accepted synthetic audio.

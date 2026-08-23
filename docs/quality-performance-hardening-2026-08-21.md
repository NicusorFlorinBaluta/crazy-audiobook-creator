# Quality and performance hardening — 2026-08-21

## Priority

Audiobook fidelity remains the release priority. Performance changes may remove
redundant representation or reuse already-computed measurements, but may not
remove dialogue ownership decisions, confidence, source evidence, character
delivery, source coverage, or human-review escape hatches.

## Speaker attribution

- A manual source-to-script audit confirmed two high-confidence ownership errors
  in already-scripted chapters. Both involved dialogue continuity rather than an
  absent character registry entry.
- Period-ending tags inside split quotations now propagate to both quote halves
  in the same source paragraph. Previously only comma/colon tags carried forward.
- Production validation now receives the original chapter text, preserving real
  paragraph boundaries when fragments are processed in chunks.
- Each later chunk receives the previous ten resolved turns. This protects
  conversation state at chunk boundaries without allowing that context to
  override an explicit tag in the current chunk.
- Generic evidence such as "conversation flow" can no longer support an
  otherwise ungrounded high-confidence decision. It is routed through focused
  repair and ultimately the existing manual attribution review queue.
- The joint scripting schema revision is now 3, invalidating older dependency
  fingerprints when regeneration is requested.

The complete retrospective deterministic audit found 345 review findings among
3,937 dialogue fragments across 49 of 63 chapters. This is a candidate list, not
345 proven errors: deterministic checks intentionally over-select ambiguity. The
two manually inspected failures were genuine. Existing project artifacts were not
rewritten automatically; the fixes apply on regeneration so source coverage and
checkpoints remain controlled.

## Scripting performance

The completed book recorded 26,104.55 seconds (7.25 hours) for scripting across
63 chapters and 9,388 segments. The 269 local-model requests consumed 26,003.6
seconds:

- 177 full chunk calls: 21,931.7 seconds
- 63 focused attribution batches: 3,235.2 seconds
- 17 focused attribution fragments: 288.4 seconds
- 11 focused delivery repairs: 535.4 seconds
- 1 strict attribution request: 12.8 seconds

Full responses dominate. Their output evaluation consumed roughly 17,114 seconds,
while prompt evaluation consumed roughly 4,703 seconds. Therefore prompt-only
micro-optimizations cannot materially solve this run's bottleneck.

Schema v4 keeps per-turn emotion and speed for every dialogue line but lets
narration inherit explicit `narrator_emotion` and `narrator_pace` scene defaults.
Material within-scene changes remain explicit overrides. On this book, 5,539 of
9,403 saved script lines (58.9%) are narration. Re-encoding those fields would
remove an estimated 43.0% of the delivery-metadata characters, while retaining
all source text and dialogue attribution fields.

A first live attempt was stopped after a concurrently running game reduced the
fully GPU-offloaded 32B model to about 1.5 tokens/second. With that contention
removed, the repeated 20-fragment benchmark completed twice. Both runs preserved
exact source coverage and unique IDs with one full attempt and zero structural,
attribution, delivery, local-repair, or fallback events. Compact JSON was 66.4%
and 67.6% smaller than its canonical expanded representation. The warm run took
277.4 seconds versus 394.8 seconds cold; most of that difference was model load
and prompt-cache reuse, not the tiny chunk-bound change. The report is stored at
`docs/benchmarks/isles-sparse-script-schema-2026-08-21.json`.

This validates schema compliance and deterministic quality invariants, but it is
not a listening comparison against schema v2. The next full run must still compare
output token count, retry rate, attribution audit results, and delivery/listening
quality against the prior baseline. No full-book speed gain is claimed yet.

Targeted validation around both manually confirmed attribution failures corrected
both targets without a fallback or manual-review flag. A dialogue-dense schema-v3
window initially incurred two focused repair calls. Listing the exact dialogue
IDs with mandatory delivery in schema v4 removed both calls on the identical
window, cutting wall time from 509.0 to 259.6 seconds and decoded tokens from
1,001 to 440. See
[`scripting-schema-v4-validation-2026-08-21.md`](scripting-schema-v4-validation-2026-08-21.md).

## Voice and long-form audio safeguards

- Cast diagnostics now gate approval for acoustically similar required voices.
  The operator may replace them or explicitly acknowledge the warning.
- Any reassignment, redesign, profile-driven regeneration, or uploaded reference
  invalidates affected pair evidence. Approval then requires explicit review; stale
  diagnostics are never presented as current.
- Selected segment metrics are aggregated per voice and chapter without another
  model call. Sustained identity-similarity drops are reported as cross-chapter
  drift. Pitch alone is not treated as identity evidence because expressive scenes
  legitimately change pitch.
- Sustained monotone warnings are reported only with at least five eligible
  segments and a 35% warning share. These trends appear in pre-master review but
  remain advisory until listening confirms them.
- Existing join diagnostics and book-wide loudness spread remain the authoritative
  long-form boundary checks.

The aggregation cost is linear JSON/statistics work after a chapter completes and
is expected to be well below one second. It adds no TTS, ASR, embedding, Gemini, or
Ollama request.

## External-confidence calibration

Calibration now reports Brier score and expected calibration error. Cross-project
evidence is pooled only within the exact provider, model, purpose, and schema/prompt
revision. Audio validation therefore cannot lend confidence to speaker attribution,
and an old prompt cannot silently calibrate a new one. Recommendations remain
advisory and never change automatic-accept thresholds on their own.

Runtime penalty is a small SQLite scan when the dashboard calibration endpoint is
opened; there is no pipeline inference cost.

## Resilience and risk-aware synthesis

- The dashboard restart helper now recovers a stale Task Scheduler `Running` state
  only after confirming the API port is free, ends that exact task instance, then
  starts and health-checks the replacement.
- Tests exercise a real local socket outage with bounded retries in addition to the
  existing cancellation, timeout, checkpoint, and model-loop safeguard coverage.
- The listening-approved short expressive-line policy remains narrowly enabled.
  This book recorded only one TTS retry in the measured 751-segment audio sample,
  so broadening the policy would offer negligible speed value and unacceptable
  delivery risk. No broader automatic delivery rewrite was added.

## Expected performance impact

- Attribution boundary context: a small input increase (ten compact prior rows),
  normally under 1% of a chunk prompt. It should reduce expensive focused retries;
  the actual reduction must be measured on regeneration.
- Deterministic split-tag repair: negligible CPU cost and can avoid model repair.
- Sparse narration delivery: no additional inference; expected output reduction,
  with 66.4–67.6% compact-to-canonical reduction measured on the targeted sample.
  Full-book wall-time gain remains unverified.
- Cast approval gate: zero generation cost on the normal path. Human review occurs
  only for similar or stale pairs.
- Long-form drift/prosody aggregation: negligible CPU/IO after each chapter.
- Segmented calibration: dashboard-time SQLite/statistics work only.
- Resilience tests: test-suite time only; no production penalty.

Targeted schema-v4 attribution and performance results, plus the final regression
and deployment checks, are recorded in
`docs/scripting-schema-v4-validation-2026-08-21.md`.

Joint-analysis and dialogue-delivery policy revisions are explicit script
fingerprint dependencies. This prevents a Resume operation from producing a
mixed-policy book after either policy changes.

The 2026-08-23 full scripting run exposed three 900-second generations. Each
affected batch contained 100–131 fragments even though the configured row cap
was 40: the direct short-chapter path had checked only its word limit. Planning
now enforces both constraints. The wall-clock ceiling is 600 seconds, the YAML
safeguards are wired into the runtime client rather than relying on matching
constructor defaults, and a bounded adaptive split retries smaller contiguous
ranges before whole-batch fallback. These settings participate in chapter
fingerprints because different boundaries can affect attribution context.

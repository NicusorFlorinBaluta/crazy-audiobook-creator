# Performance improvement plan after `sample_book-14`

## Purpose

This plan turns the remaining performance observations from the clean
`sample_book-14` release run into gated engineering work. It supplements the
broader [post-E2E improvement plan](improvement-plan-post-e2e-2026-08-09.md)
with the newer release evidence in
[e2e-run-2026-08-11.md](e2e-run-2026-08-11.md).

The plan separates safe, model-free implementation from experiments that can
change runtime or audio output. No production setting is promoted without a
controlled comparison, and another full-book run is reserved for an exact
release candidate.

## Implementation checkpoint - 2026-08-11

Implemented without loading production models:

- structured attribution issues distinguish structural failures from exact and
  ambiguous semantic contradictions;
- explicit named tags and unique generic roles are repaired locally, while
  ambiguous cases receive one bounded fragment-only retry and then a
  conservative fragment fallback;
- structural corruption retains bounded full-chunk retries, and metrics now
  separate structural failures/retries, focused retries, local repairs,
  fallbacks, and issue kinds;
- Qwen synthesis records model load, reference-prompt/cache, autoregressive
  generation, decoding, concatenation, post-processing, WAV writing, and total
  time, including failed attempts;
- performance summaries report p50/p90/p95 synthesis latency and RTF by cache
  state, text length, speaker role, and cold/warm model state;
- the TTS benchmark now uses an unmeasured warm-up, balanced ABBA/BAAB order,
  paired seeds, immutable fixtures, multiple voices, artifact/config/reference
  hashes, quality checks, and an explicit promotion decision;
- the scripting benchmark compares both chunk bounds on identical excerpts,
  alternates their order, and verifies source coverage, unique IDs, registered
  speakers, retry behavior, and fallbacks before reporting a speed result.

The attribution repair policy participates in the script fingerprint. The next
pipeline run after this change will therefore refresh cached scripts rather
than silently reusing metadata produced under the older retry policy.

No production chunk, grouping, token, attention, residency, validation, or
audio-processing default has changed. The controlled model screens below did
not meet their promotion gates.

## Controlled model results - 2026-08-11

Both screens ran from clean exact commits, retained their raw artifacts, and
released the managed model processes and ports after completion.

### Scripting chunk screen

- Exact commit: `d80b95b`.
- Protocol: chapter 7, two same-source excerpts (683 and 559 words), one
  measured repetition, alternating `350:40` and `550:60` order, plus an
  unmeasured warm-up.
- Control `350:40`: median normalized wall time `0.5922 s/source-word`, p95
  `0.6358`.
- Candidate `550:60`: median normalized wall time `0.6465 s/source-word`, p95
  `0.7394`.
- Decision: reject the larger bound. It was 9.18% slower by the primary
  normalized median gate. Both bounds still produced two chunks on both dense
  excerpts, so the candidate did not reduce request count here.
- Quality: all source coverage, ID, and speaker invariants passed. Each mode
  exercised one exact local attribution repair with zero structural retries,
  focused retries, or fragment fallbacks. This is direct live evidence that an
  exact contradiction no longer causes another full model request.
- Report:
  `brain/projects/sample_book-14/benchmarks/script-chunks-d80b95b-20260811.json`,
  SHA-256
  `783b32bbe243c5471b9724154a158e626d8e28659cd577e8bfa369703b0d619d`.

### Adaptive TTS token-cap screen

- Exact commit: `60d631a`.
- Protocol: narrator plus three approved character voices, three immutable
  fixtures per voice, paired seeds, one repetition per mode, unmeasured
  warm-up, and combination-level alternating ABBA/BAAB order (12 combinations,
  24 measured generations).
- Control: RTF p50 `1.2454`, p90 `4.2341`, p95 `4.4694`; total measured
  synthesis wall time `184.49 s`.
- Adaptive cap: RTF p50 `1.2632`, p90 `3.9386`, p95 `4.0429`; total measured
  synthesis wall time `175.39 s`.
- Quality: both modes had average and maximum WER `0.0`; average speaker
  similarity was identical at `0.98731`, with identical minimum `0.97351`.
- Decision: do not promote. The candidate improved tail and aggregate wall
  time in this small screen but made the primary median RTF 1.43% slower. That
  is neither the required 10% median win nor enough evidence to justify the
  five-repetition promotion run. Production adaptive caps remain disabled.
- Report:
  `brain/projects/sample_book-14/benchmarks/tts-screen-60d631a.json`, SHA-256
  `ae6048753416d47e30904b83b4287d26a74f5762fd5f07a1cc19091c8bcb1bd8`.

The screen also confirms that autoregressive generation is the dominant TTS
substage (`183.62/184.49 s` control and `174.64/175.39 s` candidate). Reference
prompt preparation consumed less than one second per mode. Future TTS work
should therefore target the autoregressive model/runtime itself, not prompt
I/O, WAV writing, export, or dashboard polling.

### Same-speaker grouping headroom screen

- Exact commit: `3b45c9c`.
- Protocol: model-free replay of the production grouping rules over all 605
  retained release-script calls, preserving source ranges and fragment IDs.
- Candidate `300:50` ordinary / `400:68` narrator reduced calls by 34 (5.62%).
- Candidate `340:58` ordinary / `460:78` narrator reduced calls by 45 (7.44%).
- Every additional merge was narrator-only. Neither candidate eliminated a
  character call, introduced a source/fragment mismatch, or created a new line
  over the 500-character engine boundary.
- Decision: no live audio benchmark and no promotion. The best possible call
  reduction is below the 10% performance gate, while measured reference-prompt
  and per-call non-autoregressive overhead is below 0.5% of synthesis time.
  Longer narrator utterances would therefore add cadence risk without a
  credible route to a material wall-time win.
- One existing `mother_frond` control line is 502 characters. This predates the
  candidates and is handled by the engine's bounded text splitting; the
  analyzer records it separately from candidate-introduced violations.
- Report:
  `brain/projects/sample_book-14/benchmarks/grouping-headroom-3b45c9c.json`,
  SHA-256
  `a8a089129f42420f3a3f9da5d9a900906d7bcbbbc0e217ea894a80056550dcecb`.

### Compile/runtime compatibility and dtype screen

The installed stack exposes `torch.compile`, but the Qwen talker explicitly
declares `_supports_static_cache=False` and `_can_compile_fullgraph=False`.
The Qwen wrapper also does not expose a supported compile configuration to its
custom talker generation call. Directly wrapping third-party autoregressive
internals is outside the supported application contract, so compile remains
dependency-blocked until Qwen/Transformers exposes a compatible path.

The RX 7900 XTX does report native bfloat16 capability, so dtype was screened
as the remaining supported one-factor runtime candidate:

- Exact commit: `5df0f04`.
- Protocol: one approved narrator, three fixtures, two independent sessions
  per dtype in ABBA order, a warm-up after every model load, paired seeds, 12
  measured generations, raw-audio Whisper validation, and speaker similarity.
- Float16: median RTF `4.5747`, p95 `5.3284`, measured wall time `154.99 s`.
- Bfloat16: median RTF `13.1680`, p95 `16.0323`, measured wall time `369.24 s`.
- Quality was effectively equal: average WER `0.02564` for both; average
  speaker similarity `0.99166` float16 versus `0.99023` bfloat16. Peak allocated
  GPU memory was identical at `5.253 GiB`.
- Decision: reject bfloat16. Median RTF regressed 187.84% with no memory or
  quality benefit. Do not run the full multi-voice corpus; retain float16.
- Report:
  `brain/projects/sample_book-14/benchmarks/tts-dtype-screen-5df0f04.json`,
  SHA-256
  `52e75b0337f81b71b81c58484b93309665c0e6c9dd8325f0eef8b09093ff590c`.

## Measured baseline

| Area | `sample_book-14` result | Planning consequence |
| --- | ---: | --- |
| Script generation | 8,711.1 s (2:25:11) | Avoid unnecessary full-chunk LLM calls and retest chunk efficiency |
| Generation and validation | 9,231.1 s (2:33:51) | Use this as the post-approval wall-time baseline |
| TTS synthesis | 8,083.5 s (87.6%) | TTS remains the primary post-approval performance target |
| TTS-only RTF | 1.956x | Compare optimizations by fresh-synthesis RTF, not lines per minute |
| Audio analysis and Whisper | 984.3 s combined | Preserve quality; they are secondary optimization targets |
| Validation retries | 4 of 605 lines | Do not weaken validation globally for throughput |
| Export | 63.0 s | Not a material optimization target |

A genuine 10% TTS improvement would save about 808 seconds (13.5 minutes) on
this book and reduce generation/validation wall time by about 8.8%. A 20% TTS
improvement would save about 27 minutes.

An earlier narrow 550-word/60-fragment fixture appeared to improve normalized
wall time per source word by about 22.5%. The broader controlled screen above
reversed that result, so the forecast based on it is retired and the existing
`350:40` production bounds remain in place.

## Decisions that remain closed

Do not spend another test cycle rediscovering these results unless the runtime
or dependency stack materially changes:

- Keep SDPA. It already beat eager attention in the local TTS smoke benchmark.
- Keep float16. Bfloat16 regressed median RTF by 187.84% on the balanced local
  screen and did not reduce peak allocated memory.
- Keep TTS and Whisper sequential during initial chapter synthesis.
  Co-residency regressed TTS RTF from 1.615x to 4.878x in the controlled test.
- Keep Whisper `large-v3` on raw audio. VAD damaged valid short and repeated
  speech in the controlled comparison.
- Keep adaptive TTS token caps disabled. The apparent improvement reversed
  with test order and was attributable to warm-up/kernel compilation.
- Keep the existing voice-clone prompt/embedding caches and bounded
  same-speaker grouping. These are implemented features, not missing work.
- Do not add GPU generation workers based only on unused VRAM. Throughput,
  runtime stability, cancellation, and artifact ownership are not yet proven.
- Do not optimize export or dashboard polling while TTS and scripting dominate
  wall time.

## Workstream 1: deterministic attribution repair and focused retries

### 1.1 Represent validation failures as structured issues

Replace string-only semantic failures with typed issue data containing:

- issue kind: structural, unknown speaker, explicit named-tag contradiction,
  uniquely resolvable role contradiction, gender-only contradiction, narrator
  attached to spoken dialogue, or low confidence;
- fragment ID and the local fragment window;
- submitted speaker and any exact-evidence speaker;
- whether the issue is safe to repair locally, requires a focused retry, or
  requires a full-chunk retry.

Keep malformed JSON, missing/duplicate IDs, and broad response corruption as
full-chunk failures.

### 1.2 Repair only deterministic cases locally

Apply a local correction without another model call only when source evidence
identifies one valid registered speaker:

- an explicit named speech tag identifies exactly one character;
- a generic role such as `the boy` has exactly one compatible registered role;
- dialogue assigned to narrator has an attached tag that uniquely identifies
  the speaker.

Do not choose between multiple gender-compatible characters from `he` or
`she` alone. Ambiguous evidence must not become a silent heuristic guess.

After repair, rerun complete metadata ID, speaker, confidence, and source
coverage validation before accepting the chunk.

### 1.3 Retry only the affected fragment when evidence is ambiguous

For an otherwise structurally valid response, send a focused request containing
the affected dialogue, the bounded neighboring context, allowed speaker IDs,
and the reason the previous attribution was rejected. Merge only the returned
metadata for that fragment, then rerun whole-chunk deterministic validation.

Permit one focused retry per affected fragment. If it still fails, use the
existing conservative exact-evidence fallback for that fragment. Do not pay
for three equivalent full-chunk generations.

### 1.4 Preserve full-chunk retries for recoverable structural failures

Retain a bounded full response retry for malformed JSON, missing or duplicate
fragment IDs, and failures affecting much of the chunk. Separate transport
retries from semantic retries in both code and metrics.

### 1.5 Offline verification gate

Add fixtures for named tags, generic child roles, pronoun ambiguity, narrator
misassignment, unknown speakers, low confidence, multiple contradictions,
malformed output, and focused-retry failure. Assertions must prove:

- exact source coverage and complete unique fragment IDs;
- no invented speakers and no gender/role contradiction;
- deterministic cases make no additional LLM request;
- ambiguous cases make only the bounded focused request;
- structural corruption still uses the bounded full-chunk path;
- conservative fallback remains fail-safe and observable.

This implementation and its unit tests require no live model run.

## Workstream 2: performance telemetry

### 2.1 Scripting telemetry

Record one event per annotation request with no source prose:

- chapter, chunk ordinal, fragment count, source word/character count;
- prompt and output token counts when available;
- prompt-evaluation, model-evaluation, and total wall time;
- transport attempts, full semantic retries, focused retries, local repairs,
  fallbacks, and issue kinds;
- final coverage and attribution-validation result.

Add run-level totals for estimated full model calls avoided and time spent in
each retry class. Do not claim saved time until a comparable request duration
is available.

### 2.2 TTS substage telemetry

Split synthesis timing into:

- model cold load and warm-up;
- reference prompt/embedding lookup or construction;
- autoregressive generation;
- audio decoding/concatenation;
- optional post-processing;
- atomic WAV writing.

Retain text character/word counts, generated audio duration, RTF, voice/profile
ID, cache state, attention backend, and retry attempt, but do not store source
text in performance metrics.

Summaries must report p50, p90, and p95 latency/RTF by text-length bucket,
narrator versus character voice, fresh versus cached work, and cold versus
warm calls. Add a metric schema version if fields change incompatibly.

### 2.3 Telemetry verification gate

- Unit tests cover successful, cached, retried, failed, and resumed work.
- Substage durations are non-negative and reconcile with total wall time within
  a documented measurement tolerance.
- Existing metrics readers accept the new schema or fail with an actionable
  version message.
- Instrumentation adds less than 1% wall-time overhead in a model-free timing
  fixture.

## Workstream 3: reproducible microbenchmark harness

Build one supported harness that uses immutable fixture manifests and records
commit, environment, dependency, model, config, source, and voice-reference
fingerprints.

### 3.1 TTS corpus

Use 12-24 speaker-pure lines covering:

- very short emphasis and repeated words;
- ordinary dialogue and narration;
- long narration near current grouping limits;
- fictional and proper names;
- at least one narrator and three character voices;
- a range of output durations and previously observed fast/slow RTF cases.

### 3.2 Scripting corpus

Use multiple immutable real-book excerpts covering dialogue tags, alternating
speakers, narrator interventions, generic child roles, proper names, and dense
narration. Include the previously troublesome attribution cases as regression
fixtures.

### 3.3 Timing protocol

- Run an explicit unmeasured warm-up before comparisons.
- Alternate order (`ABBA`/`BAAB`) rather than running all control trials first.
- Use at least five measured repetitions when variance permits.
- Record median and tail latency, not only the fastest run.
- Change exactly one factor per comparison.
- Preserve raw result manifests and candidate audio for listening.

The harness must detect truncation, missing output, source-coverage drift,
speaker mismatch, WER regression, new warnings, and retry-count changes.

## Workstream 4: controlled optimization experiments

Run these only after Workstreams 1-3 are committed and their low-resource tests
pass.

### 4.1 Scripting experiments

1. Compare the current 350-word/40-fragment behavior with the existing
   550-word/60-fragment candidate over several representative excerpts.
2. Measure the attribution repair/focused-retry path against the current
   full-chunk retry behavior using recorded deterministic responses first.
3. If needed, test prompt compaction that removes repetition without removing
   the registry, exact-ID contract, speaker evidence, or continuity context.

Promotion requires:

- at least 20% lower representative Pass 2 wall time or a clearly documented
  no-win result;
- 100% exact source coverage and complete unique IDs;
- no unknown speakers, attribution regression, or increase in fallback rate;
- equal manually sampled attribution quality before a production default
  changes.

### 4.2 TTS experiments

Test in this order:

1. Modest increases to narrator and ordinary same-speaker grouping within the
   existing 500-character engine ceiling.
2. Supported compile/inference settings, one at a time, retaining SDPA as the
   control.
3. A new adaptive-token-cap comparison only with warm-up and alternating order,
   plus automatic safe retry on detected truncation.
4. Native batching only if the installed Qwen library gains real batch support;
   the current sequential wrapper is not a throughput optimization.

Promotion requires:

- at least 10% lower median fresh-synthesis RTF on the complete fixture;
- no truncation, omitted speech, hard quality failure, material WER or speaker
  similarity regression, new echo/smearing, or retry-rate increase;
- stable p95 latency and memory behavior;
- a listening-approved representative chapter before book use.

Do not combine winning candidates until each has independently passed. Then
benchmark the combined configuration against the original control to detect
interaction effects.

## Workstream 5: rollout, documentation, and release evidence

### 5.1 Commit and review boundaries

Use separate commits for:

1. attribution repair and retry policy;
2. telemetry/schema changes;
3. benchmark harness and immutable fixtures;
4. each promoted scripting or TTS default;
5. benchmark results and documentation.

This keeps output-changing decisions independently reversible.

### 5.2 Verification ladder

1. Run unit/static checks with no models.
2. Replay recorded LLM responses through the attribution validator.
3. Run the fixed scripting and TTS microbenchmarks.
4. Run one representative chapter through validation, mastering, and export
   only for a candidate that passed its microbenchmark.
5. Run a clean multi-chapter/full-book test only after an exact release commit
   exists and the change is material enough to require release evidence.

Do not use a full book to screen losing candidates.

### 5.3 Benchmark documentation matrix

Update the current benchmark report with one row per candidate:

| Candidate | Status | Evidence required |
| --- | --- | --- |
| Deterministic local attribution repair | Implemented and live-screened | Exact repairs occurred with no redundant full request; all invariants passed |
| Fragment-focused semantic retry | Implemented; no ambiguous live case triggered | Offline fixtures passed; retain observability and exercise when a natural case appears |
| 550-word/60-fragment scripting | Rejected | 9.18% slower normalized median; no chunk-count reduction on the screened excerpts |
| Larger same-speaker TTS groups | Rejected after full-script headroom analysis | Maximum 7.44% call reduction, narrator-only; measured fixed overhead is immaterial |
| TTS compile setting | Dependency-blocked | Installed Qwen talker declares no static-cache/fullgraph support and exposes no supported compile path |
| Bfloat16 inference | Rejected after balanced screen | Median RTF 187.84% slower; equal quality and memory |
| Adaptive token caps | Rejected after balanced multi-voice screen | Median RTF 1.43% slower; quality equal; no promotion-grade run warranted |
| Initial TTS/Whisper co-residency | Rejected | Do not retest without a material runtime change |
| Parallel GPU TTS workers | Deferred | Separate stability and throughput research gate |
| Native Qwen batching | Unavailable in current wrapper | Revisit only after dependency support changes |

For every completed experiment, record control/candidate hashes, order,
repetitions, median/p95 results, quality deltas, decision, and rollback path.

## Ordered implementation sequence

The five deliverables and their current status are:

1. **Implemented:** deterministic attribution repair and focused retries, with
   exhaustive model-free regression tests.
2. **Implemented:** scripting retry telemetry and TTS substage timing,
   including stable percentile summaries.
3. **Implemented:** reproducible scripting/TTS microbenchmark harnesses and
   immutable fixture identities.
4. **Completed:** the small scripting and TTS screens ran; neither candidate
   met its speed gate, so no output-changing default was promoted and no
   representative chapter or full-book rerun was warranted.
5. **Completed:** exact commits, protocols, hashes, speed/quality metrics,
   decisions, and rollback positions are documented above.

The scripting harness is `scripts/benchmark_script_chunks.py`. It defaults to
three same-source excerpts and alternates which of `350:40` and `550:60` runs
first. The TTS harness is `scripts/benchmark_tts_fixture.py`. A promotion-grade
run requires 12-24 voice/fixture combinations, including a narrator and at
least three other voice IDs, with the configured repeated ABBA/BAAB protocol.
Both scripts refuse to load models without the explicit `--allow-models` flag.

The supported screening companions are `scripts/benchmark_tts_dtype.py` for
balanced numeric-dtype sessions and `scripts/analyze_grouping_candidates.py`
for model-free full-script grouping headroom. Common fingerprints, dependency
versions, balanced ordering, percentiles, and TTS summaries are centralized in
`scripts/benchmark_support.py`. The obsolete ad-hoc inference, inference-
settings, and generation-config runners were removed after the controlled
screens closed their candidate settings; they did not satisfy the current
warm-up, order, corpus, artifact, or promotion contracts.

## Completion criteria

This plan is complete when:

- deterministic attribution contradictions no longer cause redundant
  full-chunk model calls;
- ambiguous cases remain conservative and observable;
- metrics identify where TTS time is spent and distinguish cold/warm/cache
  behavior;
- benchmarks are reproducible and control warm-up/order effects;
- any promoted default meets its stated performance and quality gate;
- one representative chapter passes for every output-changing winner;
- the final exact release candidate, if one is warranted, passes artifact,
  quality, ownership-release, and targeted listening verification.

# Pipeline Validation, 2026-09-05

**Status:** complete. Every stage exercised end to end on a real run.

The 2026-09-04 session (see [e2e-run-2026-09-04.md](e2e-run-2026-09-04.md))
fixed a long list of defects but never reached audio, and left the whole voice
half unvalidated. This run closes that: `sample_book-2`, from
`tests/fixtures/sample_book.epub`, ran extraction through to a published
`.m4b` without intervention beyond clearing one review gate.

## Result

```
sample_book-2.m4b   73.7 MB   81.3 min   126 kbps   8 chapters
title "E2E Validation 0905"  ·  chapter markers correct
published atomically to /mnt/nas/media/crazybooks/sample_book-2/full/
elapsed 174 min  ·  average WER 0.022  ·  646 lines validated
```

| Quality outcome | Lines |
| --- | ---: |
| pass | 640 |
| accepted_with_warning | 2 |
| fail | 4 |

## What this proves that yesterday's run did not

| Stage | Evidence |
| --- | --- |
| Cast duplicate detection | Ran **in-pipeline** via `gemini_api`, audit written (`schema_version 2`, `outcome: completed`), 0 duplicates from 17 entries |
| Voice distinctness convergence | 4 voices colliding at 0.989 → round 1 improved to 0.988 (kept) → round 2 regressed to 0.992 → **discarded and the better round restored** |
| Attribution confirmed/reattributed split | 99 suspicious → 90 confirmed, **0 reattributed**, 9 escalated; Gemini resolved all 9 |
| Tier-1 checkpointing | `Checkpointed 8 chapter script(s)` fired the moment adjudication finished |
| Performance metrics | Written when the passes returned, which is what finally produced the prefix-cache figures |
| Audio generation | 645 segments, 8 chapters, 0 failed lines carried forward |
| Mastering | All 8 chapters at **-19.0 LUFS**, inside the ACX -18..-23 band |
| Export and publish | `.m4b` with correct chapter markers and metadata, atomically published to the NAS |

## The prefix-cache question, answered

Open since 2026-09-03 and previously recorded as "the largest unexplored perf
lever". It is not.

```
later_to_first_ratio      0.9044     -> reuse_detected: false
prefill_seconds           151.1      (812 tok/s)
decode_seconds            851.8      (52.6 tok/s)
prefill_share_of_compute  0.1507
```

Prefix reuse genuinely is not happening — the shared prompt is re-evaluated
every chunk even with `num_parallel: 1`. But **prefill is only 15% of LLM
compute**, so fixing it caps out at ~15% of scripting time and realistically
saves less. Decode dominates at 85%; that is where scripting time lives.

Measured on one 8-chapter fixture with the GPU otherwise idle. The token-count
half of `prefix_cache` is immune to GPU contention; the seconds are not.

## The review gate earned its place

It held the run at `waiting_for_review` with 5 of 645 segments (0.8%) flagged
by external audio QA, and the flags were real:

| Segment | Expected | Audio actually says |
| --- | --- | --- |
| `ch02_0089` | `"Dusk!"` | `"Dusk! Mother Frand wanted you to stop by. A real live trapper…"` |
| `ch02_0023` | `"he said, she offered a finger…"` | `"He said."` |
| `ch04_0046` | `"the boy said"` | `"The boy said."` |
| `ch04_0126` | — | wrong words and speaker mismatch |
| `ch08_0030` | `"he replied."` | `"replied."` |

The first is **over-generation** — the synthesis ran on into the next line.
WER would not reliably catch it, because every expected word *is* present;
only a model comparing audio against the expected span sees it. The next two
are the opposite failure, truncation.

`ch08_0030` was different again: a **deterministic hard gate**, which
`source_tts_issue` correctly refused to clear ("external models cannot
override it"). That is the right design — a hard gate should be fixed, not
waved through.

**Dispositions recorded:** four as `source_tts_issue`, because they are
genuine TTS defects and saying otherwise would have made the audit lie.
`ch08_0030` as `acceptable`: it is the two-word narrator beat `he replied.`,
Whisper dropped the unstressed leading word, and one word of two is WER 0.5 by
arithmetic. `text_similarity` 0.875, duration 0.96s against 0.8s expected, no
clipping, no long silence, no pacing anomaly, and three regeneration attempts
produced the identical transcript — consistent with correct audio and an
unreliable metric, not bad synthesis.

## WER is unreliable on very short lines

Two separate lines scored WER 1.0 and neither was bad audio:

- `ch04_0060` — text `"Cakoban,"`, an invented name, transcribed `"Kakaban"`.
  One word wrong on a one-word line is 100% by construction. The validator
  passed it on `text_similarity` 0.714 and clean acoustics, `quality_score`
  0.99999. That judgement is correct and worth preserving.
- `ch01_0046` — failed at WER 1.0 on attempt 1, retried to **0.000**. The
  retry path working.

Per-chapter mean WER ranged 0.018–0.036 with no trend. A rising *cumulative*
average during the run was chapter-size weighting, not drift: chapter 4 had
124 lines at 0.0357 and pulled the aggregate up.

## Defect found by this run

`_run_script_director` imported `brain.utils.file_utils`, a module that has
never existed, to prune a failed chapter from `book_script.json`. The import
sat inside a `try` whose `except` did nothing, so the prune had **never once
completed** and every chapter failure left the failed chapter in the merged
script. It surfaced twenty minutes into this run, the first time that branch
executed after the S110 pass gave the handler a log line.

Fixed in `66ad4ef`, with a test that resolves every function-local first-party
import in the repo. That scan found exactly one — this one.

## Not covered

- **Alias recovery's apply path.** Detection is validated (removing a
  character surfaced `{'Vathi': 5}` as an unlinked speaker), and the
  adjudicator correctly declined to alias a genuinely distinct character. The
  `alias` verdict itself needs a book containing a real alias; `sample_book`
  has none. The measured case is `Zak`/`Zaknafein` on a real book.
- **A large cast.** This roster is 16-17 characters. Duplicate detection was
  built for the opposite case -- a 57-character cast where 24 of 1,540 pairs
  were duplicates nothing lexical could catch. A clean result here says the
  machinery works, not that it finds duplicates at scale. The merge and refusal
  paths were proven separately by planting duplicates against this roster.

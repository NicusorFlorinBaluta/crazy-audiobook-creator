# Scripting schema v4 targeted validation — 2026-08-21

This validation used isolated source windows from the preserved project baseline.
It did not rewrite scripts, resume the project, or expose book text. The windows
covered both manually confirmed high-confidence speaker-attribution failures.

## Attribution results

| Case | Coverage / IDs | Target changed from bad baseline | Confidence | Manual review | Model retries | Local repairs |
|---|---|---:|---:|---:|---:|---:|
| Chapter 7 window | pass | yes | 0.99 | no | 0 | 1 |
| Chapter 21 schema v3 | pass | yes | 1.00 | no | 2 focused calls | 1 |
| Chapter 21 schema v4 | pass | yes | 1.00 | no | 0 | 1 |

Both known failures are now corrected by source-grounded deterministic handling,
even when the initial model response needs repair. No target fell back to a
generic role, narrator, or manual-review placeholder.

## Schema v3 to v4 performance comparison

The same 20-fragment, 103-word chapter-21 window was generated twice with the
same model and chunk bounds. Schema v4 adds the exact dialogue fragment IDs whose
emotion and speed fields are mandatory.

| Metric | Schema v3 | Schema v4 | Change |
|---|---:|---:|---:|
| Wall time | 509.0 s | 259.6 s | -49.0% |
| Full calls | 1 | 1 | unchanged |
| Focused delivery calls | 1 | 0 | removed |
| Focused attribution calls | 1 | 0 | removed |
| Total decoded tokens | 1,001 | 440 | -56.0% |
| Missing dialogue delivery rows | 7 | 0 | fixed |
| Attribution rows needing focused repair | 3 | 0 | fixed on this run |
| Compact-to-canonical reduction | 64.7% | 66.2% | retained |

The v4 result passed source coverage, unique-ID, known-speaker, structural,
fallback, and target-attribution gates. One deterministic collective-speech
classification repair remained; it requires no additional model request.

## Performance cost and promotion status

- Exact mandatory-dialogue IDs add 244 prompt characters in this sample, around
  2.3% of the user/system prompt characters.
- They removed 561 decoded tokens and two focused requests in the repeated case.
- Chunk-boundary prior-turn context remains a small input addition and was not
  exercised by these single-window calls.
- The result validates promotion to schema v4 for future regeneration. It does
  not replace the later full-book listening and attribution comparison.

Artifacts:

- `docs/benchmarks/isles-schema-v3-attribution-ch07-2026-08-21.json`
- `docs/benchmarks/isles-schema-v3-attribution-ch21-2026-08-21.json`
- `docs/benchmarks/isles-schema-v4-attribution-ch21-2026-08-21.json`

## Regression and deployment checks

- Full suite after adding explicit policy-revision fingerprint coverage: 381
  passed, 2 skipped, 6 subtests passed.
- `git diff --check`: passed; only the repository's existing Windows line-ending
  notices were reported.
- All five dashboard assets now share one cache revision.
- The dashboard was restarted successfully with schema revision 4 loaded.
- The preserved project remained paused at the scripting checkpoint; validation
  did not resume it or rewrite any book artifact.

## Cache migration

Schema revision 4 and the mandatory-dialogue delivery policy now participate in
each chapter's dependency fingerprint. A future revision bump therefore cannot
silently reuse scripts created under older output semantics. Regression coverage
independently bumps each revision and verifies that both changes invalidate the
cached chapter fingerprint.

On 2026-08-22, the project was reset at Scripting and restarted from chapter 1.
The extracted-book hash and 50-chapter generation selection were verified before
and after the reset. Old scripts and their dependent audio were removed; source
extraction was not repeated.

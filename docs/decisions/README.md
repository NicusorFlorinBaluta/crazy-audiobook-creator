# Decision records

Dated records of *why* the pipeline behaves the way it does, kept in place of
raw assistant/IDE transcripts. Transcripts are not committed because they can
contain source-book excerpts, machine paths, service topology, and credentials.

## Why every record needs a status

These records are the most valuable documentation in the repository -- they
quantify failures, isolate variables, and record what was rejected and why. But
a record with no lifecycle marker reads as current forever. The August 2026
record stated that joint character discovery was the production default long
after the 2026-08-23 decision reversed it and `brain/config.yaml` had been set
to `joint_analysis: false`. A reader landing on the older file first got the
wrong answer with no signal that anything had changed.

Every record therefore starts with:

```markdown
**Status:** Current | Partially superseded | Superseded
**Superseded by:** [link](./other-record.md)   # when not Current
```

Use **Partially superseded** when only some sections were reversed, and say
which ones. Do not delete a superseded record or rewrite its conclusion:
strike through the reversed claim, add the correction inline with its evidence,
and leave the original reasoning readable.

## Index

Newest first.

| Date | Record | Status |
| --- | --- | --- |
| 2026-09-02 | [Pronunciation dictionary, cache hardening, and stability](2026-09-02-pronunciation-caching-and-stability-improvements.md) | Current |
| 2026-09-02 | [Tiered dialogue attribution auto-fix](2026-09-02-tiered-dialogue-attribution-autofix.md) | Current |
| 2026-08-23 | [Separate character pass and early attribution gate](separate-character-pass-and-attribution-gate-2026-08-23.md) | Current |
| 2026-08 | [Quality and resilience decisions](2026-08-quality-resilience-review.md) | Partially superseded by the 2026-08-23 record (scripting/character analysis sections only) |

## Standing priority order

Unchanged since the August 2026 record, and still the tie-breaker for every
decision above:

1. Preserve source fidelity and final audiobook quality.
2. Preserve evidence, confidence trails, and human intervention for uncertain
   decisions.
3. Improve speed and resource use only when the first two remain equivalent or
   improve.

## Related

- [../architecture.md](../architecture.md) — current implementation
- [../plans/](../plans/) — audit and validation plans
- [../benchmarks/](../benchmarks/) — measured results behind promotion decisions

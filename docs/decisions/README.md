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
| 2026-09-12 | [Respellings are measured against transcripts](2026-09-12-respellings-are-measured-against-transcripts.md) | Current |
| 2026-09-10 | [Review of the 2026-09-05..09 feature run](2026-09-10-review-of-the-september-feature-run.md) | Current |
| 2026-09-07 | [Pronunciation lexicon ergonomics & Whisper STT language normalization](2026-09-07-pronunciation-lexicon-and-stt-validation.md) | Current |
| 2026-09-06 | [The book's speech tag outranks the model](2026-09-06-speech-tags-outrank-adjudication.md) | Current |
| 2026-09-04 | [Whole-cast duplicate detection](2026-09-04-whole-cast-duplicate-detection.md) | Current |
| 2026-09-04 | [Cast distinctness convergence](2026-09-04-voice-distinctness-convergence.md) | Current |
| 2026-09-02 | [Pronunciation dictionary, cache hardening, and stability](2026-09-02-pronunciation-caching-and-stability-improvements.md) | Current |
| 2026-09-02 | [Tiered dialogue attribution auto-fix](2026-09-02-tiered-dialogue-attribution-autofix.md) | Current |
| 2026-08-23 | [Separate character pass and early attribution gate](separate-character-pass-and-attribution-gate-2026-08-23.md) | Current |
| 2026-08 | [Quality and resilience decisions](2026-08-quality-resilience-review.md) | Partially superseded by the 2026-08-23 record (scripting/character analysis sections only) |

## Attribution rules are logged separately

A decision record explains *why* a class of behaviour changed. It is the wrong
grain for the individual attribution rules, which arrive one real
misattribution at a time and now number in the twenties. Those live in
[../attribution-case-ledger.md](../attribution-case-ledger.md), one row per
rule: the line that forced it, what it measured, the test that pins it, and the
`attribution_resolver` it writes.

Writing the record is not optional and not on the honour system —
`tests/test_case_ledger.py` fails when a rule writes a provenance no row
explains. Add the row in the same commit as the rule. Write a decision record
too when the *policy* changed, not merely when a new case was covered.

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
- [../attribution-case-ledger.md](../attribution-case-ledger.md) — every attribution rule, its case, and its test
- [../plans/](../plans/) — audit and validation plans
- [../benchmarks/](../benchmarks/) — measured results behind promotion decisions

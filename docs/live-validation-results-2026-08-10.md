# Live validation results — 2026-08-10

**Status:** Historical record — A dated record of what was done and why. Evidence, not a specification -- do not implement from it.

This report records the deferred model/GPU gates from
[live-validation-plan-2026-08-10.md](live-validation-plan-2026-08-10.md). The
tests were deliberately tiered: fixed fixtures first, then a checkpointed tiny
book, then one representative 24-line chapter. No full-book rerun was needed.

> **Perceptual coverage correction:** the representative chapter contained
> only 2/24 lines using the legacy post-FX path, so its objective success did
> not establish that the echo reported in the previous full-book output was
> fixed. See [the incident report](audio-echo-incident-2026-08-10.md).

## Environment and fixed fixtures

- Runtime preflight was compatible and `pip check` was clean.
- Effective production path: Qwen3-TTS 1.7B Base with SDPA on AMD/ROCm, plus
  OpenAI Whisper `large-v3` on the GPU with VAD disabled.
- A one-repeat co-residency fixture produced correct text (WER 0), but TTS RTF
  regressed from 1.615 to 4.878 (+202.1%). Keep TTS and Whisper sequential.
- Seven risk-weighted existing clips averaged WER 0.0498 with raw audio and
  0.1241 with VAD. VAD introduced two failures, so raw audio remains correct.
- The adaptive TTS-token experiment produced byte-identical WAVs, but its
  apparent win reversed when run order reversed. The first invocation paid
  kernel/MIOpen compilation cost; adaptive caps remain disabled.
- MIOpen emitted repeated workspace warnings, but generation completed and
  peak project VRAM stayed below the available device capacity. Treat these as
  runtime noise unless accompanied by allocation failure or output corruption.

Evidence:

- `brain/projects/sample_book-13/benchmarks/residency-short-2026-08-10.json`
- `brain/projects/sample_book-13/benchmarks/validator-risk-2026-08-10.json`
- `brain/projects/sample_book-13/benchmarks/tts-fixture-2026-08-10.json`
- `brain/projects/sample_book-13/benchmarks/tts-fixture-reverse-2026-08-10.json`

## Scripting fixture

Two real chapter-7 subsets completed without retries, fallback, missing source
fragments, or unknown speakers:

| Bound | Source | Wall time | Prompt eval | Model eval |
| --- | ---: | ---: | ---: | ---: |
| 350 words / 40 fragments | 334 words, 40 fragments | 291.4 s | 19.4 s | 219.5 s |
| 550 words / 60 fragments | 558 words, 60 fragments | 377.1 s | 6.1 s | 370.5 s |

The larger fixture improved normalized wall time per source word by about
22.5%, and static planning predicts 39 calls becoming 26 for the sample book.
The default is not changed yet: a two-subset comparison does not prove complete
full-corpus source coverage and attribution equivalence.

Evidence: `brain/projects/sample_book-13/benchmarks/script-chunks-2026-08-10.json`.

## Ownership and narrator recovery

- Starting a second GPU project while `sample_book-12` owned the pipeline
  correctly returned HTTP 409.
- Pausing the owner was cooperative: the job first reported paused while its
  active line finished, then released the worker, voice process, port 8100, and
  model allocation.
- A dialogue-only tiny book exposed a real missing invariant: mastering creates
  chapter announcements even when the script contains no narrator lines. Voice
  bootstrap now requires a narrator whenever the character registry contains
  one, and saved casts missing that required profile are migrated.
- The first narrator regeneration request also exposed a missing `Gender`
  import. It failed before model startup; the import and endpoint regression
  test were added before retrying.
- Regenerating only the missing narrator took 44.5 s. The existing two dialogue
  segments were preserved. The resumed book mastered chapter 1 and exported an
  80,658-byte M4B successfully.

## Representative chapter

`sample_book-12`, chapter 6, resumed from two synthesis checkpoints and
completed generation, validation, mastering, and selected-chapter export.

| Metric | Result |
| --- | ---: |
| Segments | 24 |
| Synthesis cache hits / misses | 2 / 22 |
| Validation cache hits / misses | 0 / 24 |
| Generated speech | 211.941 s |
| Recorded chapter generation | 593.526 s |
| TTS synthesis | 529.752 s |
| Whisper load / transcription | 8.685 s / 24.681 s |
| Retries / failed validation / accepted warnings | 0 / 0 / 0 |
| Final quality | 24 pass |
| Average / maximum WER | 0.00981 / 0.0625 |
| Minimum speaker similarity | 0.90822 |
| Peak VRAM | 13.710 GB |
| Export | 3,792,610-byte M4B |

The 2/22 synthesis split proves interruption recovery reused completed audio.
Validation reuse was correctly zero because the earlier interruption occurred
before validation records existed. The final artifact is
`brain/projects/sample_book-12/sample_book-12_chapters_6.m4b`.

## Decisions and remaining work

- The final low-resource gate passed 149 unit tests, Python compilation, four
  JavaScript syntax checks, and documentation-link validation. `git diff
  --check` also passed. The first pass exposed missing Windows `tzdata` in the
  repository venv; the dependency is now declared and preflight reports its
  version. The production environment already contained `tzdata 2026.3`.
- Keep production TTS/Whisper sequential, VAD off, adaptive token caps off, and
  current script chunk defaults.
- Keep the narrator invariant and regeneration regression test as release
  contracts.
- Do not tune join gain/crossfade until the existing warning queue has human
  listening dispositions. Objective checks alone cannot determine whether a
  boundary is perceptually bad.
- Before a release, review and split the broad working tree into coherent
  commits, archive a verification manifest for the exact candidate commit, and
  run the clean multi-chapter/full-book release tier only if the candidate has
  output-affecting changes beyond those covered here.

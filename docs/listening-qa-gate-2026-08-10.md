# Human listening QA gate — 2026-08-10

## Current decision

Do not tune global crossfade or gain from the existing `sample_book-13` queue.
The book contains the phase-vocoder echo incident documented in
[audio-echo-incident-2026-08-10.md](audio-echo-incident-2026-08-10.md), which
can make a clean boundary sound defective or conceal a real boundary problem.

The dashboard retains all 63 join warnings and their adjacent segment players.
Open `sample_book-13`, select the **Quality** tab, and use these dispositions:

- `acceptable`: the transition sounds natural at normal listening volume;
- `needs_remaster`: both source lines sound individually clean, but their
  boundary has an objectionable level jump, click, timing error, or overlap;
- `source_tts_issue`: either source line is echoic, distorted, mispronounced,
  unstable, or otherwise bad before considering the boundary;
- `unreviewed`: insufficient evidence or not yet heard.

Never use `needs_remaster` for the known echo artifact. Notes should identify
which side is defective and what was heard.

## Severity-first triage set

If the old output is reviewed before regeneration, limit work to this triage
set. It covers the largest measured loudness changes and the previously
prioritized chapters 2, 4, 7, and 8.

| Priority | Chapter | Previous | Current | Loudness delta |
| ---: | ---: | --- | --- | ---: |
| 1 | 8 | `ch08_0173` | `ch08_0174` | 20.26 dB |
| 2 | 7 | `ch07_0155` | `ch07_0157` | 17.08 dB |
| 3 | 2 | `ch02_0013` | `ch02_0014` | 16.48 dB |
| 4 | 7 | `ch07_0152` | `ch07_0155` | 16.43 dB |
| 5 | 8 | `ch08_0045` | `ch08_0047` | 15.24 dB |
| 6 | 2 | `ch02_0008` | `ch02_0009` | 14.97 dB |
| 7 | 2 | `ch02_0009` | `ch02_0011` | 13.82 dB |
| 8 | 2 | `ch02_0123` | `ch02_0124` | 13.62 dB |
| 9 | 2 | `ch02_0004` | `ch02_0006` | 12.95 dB |
| 10 | 2 | `ch02_0048` | `ch02_0049` | 12.68 dB |
| 11 | 4 | `ch04_0114` | `ch04_0116` | 12.46 dB |
| 12 | 2 | `ch02_0028` | `ch02_0030` | 12.28 dB |

All twelve diagnostics report a zero boundary-sample jump, so the primary
question is perceived loudness/timbre continuity rather than a literal sample
click.

## Clean-run acceptance gate

On the next clean representative or release run:

1. Listen to at least one neutral control and several reflective, soft,
   intense, and non-default-speed lines before reviewing joins.
2. Confirm the source segments are dry and free of echo.
3. Review every new join warning plus a random control set of warning-free
   joins.
4. Tune mastering only if multiple `needs_remaster` decisions show the same
   repeatable defect. Do not change global settings from diagnostic magnitude
   alone.
5. Record the candidate commit, configuration fingerprint, artifact hash, and
   final disposition counts in the release report.

The expensive multi-chapter/full-book run remains deferred until an exact
commit candidate exists and the clean representative listening gate passes.

## Clean-output audit result

The user reviewed the nine-clip pack generated from candidate `3d96b3b` under
`clean-output-v1`. All nine clips passed for echo, chorus, metallic smearing,
and voice instability. This closes the phase-vocoder perceptual regression.

Two expected/independent findings remain:

- Lines formerly labelled intense or non-default-speed no longer sound
  strongly emphasized. This is the intended safety tradeoff while mood DSP is
  disabled; expression needs a future quality-approved implementation rather
  than restoration of the old phase vocoder.
- The reflective Dusk fixture exposed a real script defect. The source speaker
  is Vathi, but `ch08_0047` is assigned to Dusk and includes narrator prose:
  `"You're bored, I suppose," she said. Then she paused.` The old full-book
  script contains 72 non-narrator lines with attached dialogue tags. Not all
  have the wrong quoted speaker, but character delivery of narrator tags is a
  systemic grouping behavior and some examples are genuine misattributions.

## Dialogue attribution/tag correction

Implemented after the audit:

- character dialogue and attached narrator tags remain separate synthesis
  lines instead of merging into the character voice;
- both lines receive a shared utterance group and a zero-gap, no-crossfade
  mastering contract;
- deterministic `he`/`she` and explicitly named-tag contradictions reject bad
  metadata while the correction retry is available;
- the exact Vathi/Dusk sentence is covered by a source-preserving regression;
- a scripting-only fixture confirms Vathi dialogue, narrator-owned
  `she said. Then she paused.`, stable source coverage, and group timing;
- the full static tier passes: 157 tests in 2.017 seconds, Python compilation,
  four JavaScript syntax checks, and local Markdown link validation on
  2026-08-10.

The clean-audio and automated segmentation gates are now **approved**. The next
human audio audit is needed only after a fresh candidate renders at least one
real quote/tag group: listen for natural continuity, correct voice ownership,
and any zero-gap click or level jump. Do not tune local gain or crossfade unless
multiple fresh groups expose the same repeatable defect. A full-book run remains
reserved for the exact release candidate.

## Fresh quote/tag audio audit — approved

Candidate `e58caee` generated the Vathi quote and narrator tag separately using
the approved `vathi` and `narrator_male` references, then mastered them as one
zero-gap, no-crossfade utterance group. The comparison control used a 400 ms
pause.

Automated evidence:

- Vathi, narrator, and assembled-candidate WER: `0.0`;
- speaker similarity: Vathi `0.98524`, narrator `0.98045`;
- join loudness delta: `3.19 dB`;
- zero-gap sample jump: `0.0`; crossfade applied: `false`;
- mastered candidate: `-19.00 LUFS`, `-1.82 dBFS` peak;
- TTS load `22.95 s`; generations `14.56 s` and `19.61 s`; Whisper load
  `9.31 s`;
- intensity post-processing and phase-vocoder fallback were disabled;
- the external GPU process and model ports were released after completion.

Human disposition: both individual voices sound correct, candidate 3 sounds
more natural than the 400 ms control, and no click or level jump is audible.
The quote/tag listening gate is **approved**. No boundary gain or crossfade
tuning is warranted from this evidence.

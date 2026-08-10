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

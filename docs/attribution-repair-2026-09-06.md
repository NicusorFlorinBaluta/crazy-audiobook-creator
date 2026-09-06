# Attribution repair on a 32-chapter book — 2026-09-06

**Status:** Historical record

What was run on `the-finest-edge-of-twilight-book`, in what order, and what each
run measured. The reasoning behind the code changes is in
[decisions/2026-09-06-speech-tags-outrank-adjudication.md](decisions/2026-09-06-speech-tags-outrank-adjudication.md);
this file is the evidence and the operational lessons.

## How the defect was found

A review item showed `ch11_0149` attributed to Dahlia at 0.98 with a decision
trail that contradicted itself. Reading the surrounding lines settled it in one
step — `ch11_0150` reads *"he replied, then whispered in her ear,"* — and the
line was stored as **resolved, not queued**: `review_required: false`,
`resolver: gemini_api_triage`.

That prompted a book-wide scan rather than a single-line fix.

## Measuring the blast radius

A speech tag is only usable when it is genuinely a tag. The test that made the
scan trustworthy: **narration whose first alphabetic character is lowercase** is
a grammatical continuation of the quote, so it names the speaker; narration
starting with a capital is a new sentence and merely a reaction (*"Dahlia
laughed at that."*), which says nothing about who just spoke.

The first scan, without that test and without alias resolution, reported 47
hits. Most were its own errors — reaction verbs read as tags, and `told X` /
`asked X` naming the addressee rather than the subject. With the strict test:

```
spoken dialogue lines          : 3,127
  with a genuine speech tag    :   490   (16%)
  contradicted by their own tag:    11   (2.2% of tagged lines)
```

Two of the eleven were still scanner artifacts (a possessive alias). Of the
genuine nine, **five had been written by `deterministic_named_tag` at
confidence 1.0 with review suppressed** — not model flakiness, a deterministic
rule with an addressee bug, invisible to both the LLM tiers and the inbox.

## The cast bug

`effron_son` — name *"Effron's Son"*, `age_range: unknown`, 4 lines, its own
voice entry — was a possessive misparsed into a whole character, alongside the
real `effron` and its 110 lines. Its alias list included the bare name `Effron`,
which made every plain "Effron" in a tag ambiguous, so `_dialogue_tag_evidence`
abstained and the real character lost every tag naming him.

Its four lines were each decidable from the text:

| line | text | evidence | resolved to |
| --- | --- | --- | --- |
| `ch01_0192` | "Let my mother go!" | Effron's mother is Dahlia | `effron` |
| `ch26_0100` | "Mother, no." | Dahlia answers on the next line | `effron` |
| `ch26_0108` | "You should not have followed me here," | *"he heard Effron warn"* | `effron` |
| `ch30_0119` | "And hardly a tragedy," | *"added Breezy, and Effron's head snapped around"* | `brie` |

The last is Breezy's — the tag names her as speaker and Effron as the one
reacting. It is the line the possessive-blind parser had handed to `effron_son`
at confidence 1.0.

`brain/projects` is git-ignored, so `characters.json`, `voice_cast.json` and all
32 chapter files were copied to `_backup_2026-09-06_cast_fix/` inside the
project before any edit.

**Consequence to expect when editing a cast mid-project:** removing one
character changed the registry fingerprint, and 11 of 32 chapters were
re-scripted on the next run (21 reused). Measured before committing to it:

```
stale BEFORE the cast fix : 0
stale AFTER  the cast fix : 11  -> [1, 7, 9, 11, 17, 20, 26, 28, 29, 30, 31]
```

## The three runs

| | Run A (09-05) | Run B (09-06, 09:29) | Run C (09-06, 13:39) |
| --- | --- | --- | --- |
| Code | original | original (stale) | **fixed** |
| Data | original | cast fixed, 11 re-scripted | same |
| `total_suspicious` | 1089 | 1065 | 1048 |
| `confirmed` | 929 | 907 | 889 |
| `reattributed` | 30 | 27 | **16** |
| `escalated_to_tier2` | 130 | 131 | **143** |
| Gemini contradictions reverted | 10 | 8 | **1** |
| Blocking review items | 24 | 3 | 8 |
| Tagged lines contradicting the book | 11 | 8 | **0** |

Run C's rise in escalations and review items is the veto working: a line the
model wants to flip but the book refutes now reaches a human rather than being
accepted.

## The lesson that cost a run

**Run B executed code that had already been fixed.** The dashboard process had
been running since 05-Sep 14:46; every edit was made on 06-Sep. Python holds the
modules it imported at start, so the pipeline inside that process ran the old
ones. The 24 → 3 improvement Run B produced was real but came entirely from the
cast fix and re-scripting — *data* — while the parser and veto never executed.

It was caught by checking the report for symbols that only the new code emits:

```
summary keys   : no 'tag_overruled'         (added by the fix)
guardrail keys : no 'attached_tag'          (added by the fix)
resolver tiers : local_qwen, gemini_api     — no 'deterministic_tag'
```

Confirmed independently: the process listening on port 8000 was PID 16604,
`StartTime 05-Sep-26 2:46:58 PM`.

**Rule going forward:** after changing pipeline code, restart the dashboard
(`POST /api/system/restart`) *before* starting a run, and verify by comparing
module mtimes against the new process start time:

```
dashboard started : 2026-09-06 13:38:12+03:00
  tiered_adjudicator.py   mtime=2026-09-06 08:32:54   loaded_new=True
  script_generator.py     mtime=2026-09-06 08:56:53   loaded_new=True
  pipeline.py             mtime=2026-09-05 19:59:48   loaded_new=True
```

Run C then confirmed the fixes had run, from the report itself: `tag_overruled`
present in the summary and `attached_tag` among the guardrail keys.

## Re-running attribution without a destructive reset

`POST /api/projects/{id}/reset` with `stage: scripting` is **not** an attribution
re-run. It `rmtree`s `script/`, deletes `characters.json` and `voice_cast.json`,
and sets `force_character_analysis: true` — Pass 1 plus all 32 chapters from
scratch, roughly 6.5 hours on this book, discarding any cast repair.

Attribution is not a resettable stage; it runs inside scripting. To redo it,
set `script_completed: false` on the job and start normally. `PipelineResumePlan`
then routes to `SCRIPTING`, which also skips the review gate, and
`generate_all_chapters` reuses every chapter whose fingerprint still matches.
Run C reused all 32 and went straight into attribution.

`scripts/repair_attributions.py` is **not** equivalent either: it is fed by
`detect_suspicious_turns`, which emits `consecutive_collapse` only. The speech-tag
contradictions come from `audit_book_attribution`, a different path that needs
the extracted source. On this book that gap covered 5 of the 9 known-bad lines,
including `ch11_0149`.

## What remains open

`ch11_0147` and `ch11_0148` are still `effron` and read as Dahlia's complaint —
0148 places the tower with the listener, 0149 with the speaker, so they cannot
share a speaker. Neither carries a tag, so nothing in this machinery can see
them. Speech tags cover 16% of spoken lines; the plan for the remaining 84% is
[plans/targeted-block-adjudication-2026-09-06.md](plans/targeted-block-adjudication-2026-09-06.md).

Seven lines sit in the review inbox with rationales attached, plus one em-dash
scene break (`ch22_0174`) that was given a speaker — a scripting artifact rather
than an attribution call.

# Creator app — improvement review

**Written:** 2026-09-19 · **Branch:** `dev` @ `dae3ccc` · **Reference book:**
`the-finest-edge-of-twilight-book` (32 chapters, 13.47 h, 7,453 script lines,
7,554 rendered segments)

Everything below was measured against this repository on 2026-09-19, not
inferred from the docs. Where a number is quoted, the appendix says how to
reproduce it. Where something is a hypothesis rather than a measurement, it
says so.

Two decisions are treated as settled and are **not** re-opened here: the
`Catti-brie` respelling was declined by the owner on 2026-09-16 with trial
evidence in hand, and Emberdark is metrics-only. Nothing in this document asks
you to revisit either.

---

## Contents

- [The short version](#the-short-version)
- [Part A — In-Car & Mobile Playback Flags (Edge of Twilight)](#part-a--in-car--mobile-playback-flags-edge-of-twilight)
- [Part B — "Is the audio quality getting worse?"](#part-b--is-the-audio-quality-getting-worse)
- [Part C — Quality improvements, ranked](#part-c--quality-improvements-ranked)
- [Part D — Fewer inputs from you](#part-d--fewer-inputs-from-you)
- [Part E — Performance, and the rest](#part-e--performance-and-the-rest)
- [Suggested order of work](#suggested-order-of-work)
- [Part F — Implementation specs](#part-f--implementation-specs) — start here to build
- [Part G — Documentation audit](#part-g--documentation-audit)
- [Appendix — how each number was produced](#appendix--how-each-number-was-produced)

---

## The short version

**The flags.** There are 30 flags on Edge of Twilight. Fourteen of them are
**duplicates** — one real flag and one context-free ghost per tap — caused by the
dashboard importing the NAS Streamer's copy of a flag it already has under a
different ID. The ghosts land with status `flagged`, a value no dashboard filter
recognises, so they are invisible except under "All Flags" and look
already-handled. Twenty-eight of the thirty have never been triaged. Two of them
carry a book-absolute position where the rest are chapter-relative, and both
silently resolved to the *last line of chapter 13* because the position matcher
has no maximum distance. And `--repair` in the triage CLI deletes a WAV at a path
that has not existed in this project layout — the cache invalidation is a no-op.

**The quality.** Your ear is right that something got worse, but it is not
fidelity. Loudness is flat across all 32 chapters (−19.02 LUFS, 0.08 LU spread)
and speaker identity is flat and high (0.985 first half, 0.986 second half). What
*did* move: lines shipping with a failing final status rose from 0.73% to 1.09%
between halves, and mispronounced name takes run 18.8/h in chapters 1–16 versus
21.3/h in 17–32 — with much sharper local peaks in exactly the stretch you have
been listening to (ch 15 at 57.7/h, ch 21 at 50.9/h, ch 24 at 43.9/h, ch 30 at
42.2/h). The dominant defect is **names**, not audio. 166 recurring terms —
4,258 spoken occurrences — have no verified pronunciation mapping, and the
release gate reported `release_ready: true` with all 166 sitting at `unreviewed`.

**The biggest single lever on your time.** The scripting LLM spent **331 minutes
of pure prefill** on this one book across 1,497 calls, and **not one call reused
a cached prefix**. The system prompt puts the per-chapter character registry
*before* the static rulebook, so no prefix is ever shared. Reordering it is a
text move, screenable with the benchmark script you already have.

---

## Part A — In-Car & Mobile Playback Flags (Edge of Twilight)

### A0. What is actually in the queue

| | count |
| --- | --- |
| Total flags | 30 |
| From `android_auto` | 28 |
| From `remote_test` | 2 |
| Issue type `wrong_speaker` | 30 (100%) |
| Status `open` (enriched, triageable) | 14 |
| Status `flagged` (ghost duplicates, no context) | 14 |
| Status `dismissed` | 2 |
| **Ever given an agent verdict** | **1** |

Chapters touched: 3, 13, 14, 15, 18, 19, 20 — with every android_auto flag
appearing exactly twice.

### A1. Every in-car flag is recorded twice *(highest priority in this part)*

`POST /books/{id}/flags` mints its own ID from the wall clock at
[mobile.py:1179](brain/dashboard/api/mobile.py:1179):

```python
flag_id = f"flag_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
```

The NAS Streamer runs the same endpoint and mints its own. One tap therefore
exists under two IDs a second or two apart. `GET /flags` then calls
`_sync_flags_with_streamer_and_disk`, which keys candidates by `flag_id`
([mobile.py:1126](brain/dashboard/api/mobile.py:1126)) — the streamer's ID is not
in SQLite, so it is imported as a second row.

The evidence is unambiguous: `flag_20260917_170750_7ee990` and
`flag_20260917_170749_62b4ba` are one second apart, same chapter, same
`position_ms` (1580729), same `line_id` (`ch14_0307`).

Three separate fixes, all small:

1. **Let the client own the ID.** Accept an optional `flag_id` (or
   `client_flag_id`) on the POST body and use it; mint one only when absent.
   Then a replay is an upsert, not a new row.
2. **Dedupe on content regardless.** Before inserting, reject a flag matching an
   existing one on `(project_id, chapter_number, line_id)` **or** on
   `(project_id, chapter_number, |Δposition_ms| < 2000)`. One tap cannot
   legitimately produce two flags two seconds apart.
3. **Stop the status collision.** The POST returns
   `{"status": "flagged", "flag": flag}` at
   [mobile.py:1211](brain/dashboard/api/mobile.py:1211). That `status` is an
   envelope field, but it is the value that comes back on the ghosts — the
   streamer is storing the envelope's `status` as the flag's status.
   `create_playback_flag` itself always inserts `'open'`
   ([job_queue.py:864](brain/orchestrator/job_queue.py:864)), so `flagged` can
   only have arrived this way. Rename the envelope key (`"result": "created"`),
   and make the import path coerce any unrecognised status to `open`.

### A2. The ghosts are invisible, and look resolved

The dashboard only treats `open` and `pending` as open
([app.js:3622](brain/dashboard/frontend/js/app.js:3622),
[3642](brain/dashboard/frontend/js/app.js:3642)). A `flagged` row therefore:

- is **not counted** in the attention badge,
- is **filtered out** of "Open Only",
- renders with **no status selected** in its dropdown
  ([app.js:3710](brain/dashboard/frontend/js/app.js:3710)), and
- falls into the terminal branch that offers "↩️ Re-open Flag"
  ([app.js:3813](brain/dashboard/frontend/js/app.js:3813)) — i.e. it presents as
  already dealt with.

Fix A1.3 removes the cause; a one-line status allow-list in the front end makes
any future unknown status loud instead of silent.

### A3. The imported copy carries the wrong enrichment schema

Two enrichment shapes exist for the same concept:

| dashboard (`_enrich_flag_context`) | NAS streamer |
| --- | --- |
| `matched_line_id`, `active_line`, `candidate_lines`, `surrounding_lines`, `reaction_window`, `manuscript_excerpt` | `matched_line_id`, `matched_speaker`, `matched_voice_id`, `matched_line_text`, `nearby_lines` |

The import keeps whatever the remote sent because it is truthy
([mobile.py:1136](brain/dashboard/api/mobile.py:1136)):

```python
enriched = f.get("enriched_data") or f.get("line_metadata")
if not enriched:
    enriched = _enrich_flag_context(...)
```

The triage UI and `investigate_playback_flags.py` both read `active_line`,
`candidate_lines` and `manuscript_excerpt` — none of which exist in the streamer
shape. That is why all 14 ghosts show zero candidates and no excerpt.

**Fix:** re-enrich on import unconditionally (it is local file I/O, not
inference), or validate the incoming shape against the dashboard schema and
re-enrich when it does not match. Better still: publish the enrichment schema in
`shared/` so the streamer and the dashboard cannot drift again.

### A4. `created_at` is overwritten with the import time

The import calls `create_playback_flag`, which stamps `now`
([job_queue.py:850](brain/orchestrator/job_queue.py:850)) and ignores the
remote's `created_at`. Twelve ghosts all carry `2026-09-19T06:36:00` — the moment
of one sync — while their IDs encode taps from the 17th and 18th. Pass the
remote `created_at` through on import.

### A5. Two flags carry a book-absolute position and silently hit the wrong line

`flag_20260917_052520_574cce` (position 20,078,546 ms) and
`flag_20260917_053710_f973ad` (20,774,923 ms) are stamped `chapter_number: 13`.
Chapter 13 is 37.6 minutes long, so neither position is chapter-relative. As
book-absolute offsets they are 5.58 h and 5.77 h; chapter 13 spans 5.05–5.68 h,
so the first lands inside chapter 13 and the second lands inside **chapter 14**.

Both resolved to `ch13_0424` — index 384 of 385, the **last line of the
chapter**. The matcher falls back to nearest-by-distance with no ceiling
([mobile.py:970–982](brain/dashboard/api/mobile.py:970)):

```python
dist = min(abs(position_ms - s_ms), abs(position_ms - e_ms))
if dist < best_dist:
    best_dist = dist
    best_candidate = (idx, line)
```

A tap five hours outside the chapter still produces a confident-looking match.

**Fixes:**

1. Reject or re-resolve a match further than a few seconds from the timeline, and
   record `match_distance_ms` + a `match_confidence` on the flag so a bad match
   is visible rather than silent.
2. When `position_ms` exceeds the chapter's duration, try it as a book-absolute
   offset and re-derive the chapter from the cumulative timeline. Both of these
   flags would then land correctly.
3. *Hypothesis, worth confirming on the device:* the offset origin differs
   between playing the full-book M4B and a chapter-range M4B. Both of these
   flags came from the morning of 2026-09-17; every later flag is
   chapter-relative. Making the client send `position_ms` **and** an explicit
   `position_origin: "chapter" | "book"` removes the ambiguity permanently.

### A6. The triage CLI's `--repair` never invalidates the audio

[investigate_playback_flags.py:285](tools/investigate_playback_flags.py:285):

```python
ch_seg_wav = workspace_dir / "audio" / f"ch_{ch_num:03d}_{line_id}.wav"
```

There is no `audio/` directory in this workspace and never has been in this
layout. Segments live at `workspace/<project>/segments/{line_id}.wav` — the path
the rest of the app uses ([main.py:3676](brain/dashboard/api/main.py:3676)). So
`--repair` rewrites the speaker in the script and marks the flag `resolved`
while the wrong-voiced WAV stays exactly where it was.

Worse, it repairs in isolation. `docs/pronunciation-evidence.md` documents that a
kept take has to move **four stores** together (segment WAV,
`chapter_NNN.segments.json`, `voice_cache.db`, `pipeline_state.db`) and the
helper for that already exists in `shared/segment_repair.py`. `--repair` uses
none of it, and does not mark the chapter's master stale.

**Fix:** route `--repair` through `shared/segment_repair.py`, regenerate the line
with the corrected voice, and mark the chapter for re-master. Until then the
command is actively misleading and should refuse to claim `resolved`.

### A7. `--auto-diagnose` is not safe to act on

I ran it read-only against the 14 real flags. It is a bare regex over the
manuscript excerpt: if *any* `<Name> said|asked|replied|…` appears anywhere in
the window, it proposes that name at **confidence 0.85**. Results:

| flag line | text | suggestion | correct? |
| --- | --- | --- | --- |
| `ch13_0424` | "And a folded parchment, a note from her parents, she knew…" | `savahn` | No — this is plain narration |
| `ch15_0062` | "Avelyere showed Breezy the portal, and promised great things beyond…" | `breezy` | No — plain narration |
| `ch18_0132` | "she accused." | `kyrnill` | Plausible |
| `ch14_0358` | "And you think the Monastery of the Yellow Rose the best place…" | keep `jarlaxle` | Correct |

Two of the first four suggestions would corrupt the script if applied — and
`--repair` applies a suggestion without re-checking it.

This matters more than the individual wrong answers: the tool **re-implements
attribution from scratch** and ignores every rule the pipeline enforces — speech
tags outranking adjudication, the whole-paragraph beat agreement, tag-subject
governance, the possessive and addressee refutations — all of which are pinned
by tests and recorded in `docs/attribution-case-ledger.md`.

**Fix:** delete the local heuristic and call the real attribution machinery on
the flagged line with its window. If the pipeline's own refutation layer cannot
reach a verdict, the honest answer is `INCONCLUSIVE`, not a 0.85 guess.

### A8. Nothing triages the queue

`--auto-diagnose` is a manual command. Twenty-eight of thirty flags have no
verdict; the oldest untriaged tap is from 2026-09-15. Once A7 is fixed, run the
diagnosis automatically on flag creation and store the verdict on the row, so
the dashboard shows an opinion the moment you get out of the car. Given the
enrichment is already local-only, this costs nothing per flag.

### A9. The flagged lines themselves

Worth noting for its own sake: of the 14 real flags, several point at lines the
script attributes **correctly** — `ch14_0307` ("Her parents are important to us.
What will they think of this?") really is Perrywinkle Shin in the source, with
Savahn replying. Combined with the 20 s / 2 s reaction window the enrichment
already models, this suggests a share of "wrong speaker" taps are *reaction
lag* — the offending line is one or two lines earlier than the one at the tap.

The enrichment computes `reaction_window` but the diagnosis only ever examines
`active_line`. Diagnosing the **window** and returning a ranked list would
convert several of these from "inconclusive" into a real answer.

---

## Part B — "Is the audio quality getting worse?"

Short answer: **not in fidelity, yes in name pronunciation, and slightly in
shipped defects.** The specifics matter because they point at different fixes.

### B1. What is flat (measured, no decline)

| metric | ch 1–16 | ch 17–32 |
| --- | --- | --- |
| Speaker identity similarity (median) | 0.9853 | 0.9859 |
| Lowest identity similarity | 0.9796 | 0.9780 |
| Monotone fraction | 0.000 | 0.000 |

Book loudness is excellent and uniform: median −19.02 LUFS, spread **0.08 LU**
across 32 chapters, zero outlier chapters, peak −1.0 dBFS. Mastering is not the
problem. No voice-chapter in the book falls below the 0.62 identity floor —
nothing is close.

### B2. What did decline (modest but real)

> **Corrected 2026-09-19 after a documentation audit.** My first pass took the
> *last* quality-log record per line and reported 68 shipped failures. That is
> the wrong reading of the data. `docs/quality-assurance.md` (line 321) states
> the rule plainly: **"an older selected retry remains the reported winner while
> all attempts still count toward retry metrics."** The winner is the record
> carrying `details.selected`, not the highest attempt number. Re-measured on
> that basis, the numbers below are smaller and the conclusion in B3 is
> unchanged. The original figures are struck through so the error stays visible.

Computed from the **selected** take per line — the one actually assembled into
the chapter:

| | ch 1–16 | ch 17–32 | change |
| --- | --- | --- | --- |
| Lines | 3,702 | 3,751 | |
| Not clean (warning or fail) | 148 (4.00%) | 181 (4.83%) | **+21% relative** |
| Selected take is `fail` | 13 (0.35%) | 19 (0.51%) | **+46% relative** |

~~157 (4.24%) / 202 (5.39%) and 27 (0.73%) / 41 (1.09%); 68 shipped failures.~~

Across the book: 7,124 `pass`, 297 `accepted_with_warning`, **32 `fail`** among
selected takes. Of the 68 lines whose *last* record is `fail`, 29 had a passing
take selected and 7 an `accepted_with_warning` one — those 36 are fine. Only 32
shipped with a failing take.

**And none of those 32 shipped silently:** every one carries
`disposition: acceptable` in `review_items` — a human looked at each and accepted
it. The documented fail-closed policy in `docs/architecture.md` ("Hard failures
remain blocking") worked exactly as written; `blocking_count: 0` is correct, not
misleading. See the rewritten C3.

The trend direction survives the correction — the second half is worse on both
measures — but the magnitude is roughly half what I first reported.

Cast size also grows: a mean of 5.7 distinct voices per chapter in the first half
against 6.9 in the second. More voices is more failure surface even at identical
per-segment quality.

### B3. What you are actually hearing: names

This is the dominant effect by a wide margin.

The measurement audit (111 terms, 7,391 transcribed lines) verdicts **29
unstable and 6 mispronounced**. The worst, by sound stability:

| term | samples | stability | heard as |
| --- | --- | --- | --- |
| Reghedmen | 8 | 0.13 | "red headman", "regiment", "redjedmen" |
| Quista | 5 | 0.20 | "is a", "krista", "just", "cuts" |
| Canzay | 5 | 0.40 | "one" ×2, "once", "kanze" |
| Ilnezhara | 14 | 0.43 | "ilnazara", "ilnajara", "vilnajara" |
| Zaknafein | 27 | 0.44 | "zachnafine", "zaknafan", "zognafine" |
| Avelyere | 48 | 0.46 | "aveliere", "avlir", "avelere", "avliar" |
| Catti-brie | 179 | 0.52 | "cadibri" ×50, **"cadbury" ×48**, "caddy bree" ×25 |
| Gauntlgrym | 46 | 0.59 | "gontalgrim" ×20, "gondelgrim" ×10 |
| Artemis Entreri | 35 | 0.69 | "artemis entrary" ×20, "artemis and trari" ×5 |
| Entreri | 107 | 0.83 | "entrary" ×75, **"and trary" ×9** |

Density of bad name takes, per hour of finished audio:

```
ch 1-16 : 129 bad takes / 411 min -> 18.8 per hour
ch17-32 : 141 bad takes / 397 min -> 21.3 per hour
```

The book-wide average moves only 13% — but a listener does not hear an average.
The local peaks are **ch 15 at 57.7/h, ch 21 at 50.9/h, ch 24 at 43.9/h, ch 30 at
42.2/h, ch 17 at 36.9/h**, and your flags cluster in 13–20. Chapter 15 is roughly
one mangled name per minute. The names driving the second half — Entreri,
Allefaero, Avelyere, Ilnezhara, Zaknafein, Tazmikella, Dininae — mostly do not
appear before chapter 13, so the failure surface genuinely arrives when you
think it does.

Backing this up: **166 recurring terms have no verified mapping at all**, across
**4,258 spoken occurrences**, against 15 verified. The release report lists every
one at `disposition: unreviewed` — and still reports `release_ready: true`,
`blocking_count: 0`. Of 363 release items, 317 are unreviewed.

*(Fair caveat: many of the 166 unmeasured terms — Breezy, Dahlia, Holiday,
Bremen, Baldur — are ordinary English words the engine says correctly. The true
gap is smaller than 166. But Breezy at 1,108 occurrences and Dahlia at 370 are
never checked at all, which is worth closing on principle.)*

### B4. Two proven respellings were trialled and never applied

`PRONUNCIATION_FOLLOWUP_PLAN.md` records Group C trials with results:

- `Regheadmen` — **70% correct vs 10%** for the current spelling
- `Kweesta` — **100% correct vs 0%**

Neither is in `brain/projects/the-finest-edge-of-twilight-book/pronunciation_dict.json`,
which holds 12 entries. Both terms are still verdicted `mispronounced` in the
2026-09-17 audit. The measurement is done and the win is banked — it just was not
spent. This is the cheapest quality gain available on this book.

### B5. The lexicon is padded with no-ops

Four of the twelve project entries map a term to itself:

```json
"Janquay": "Janquay", "Artemis Entreri": "Artemis Entreri",
"Jarlaxle": "Jarlaxle", "Jax": "Jax"
```

The recommender is generating these wholesale — in
`pronunciation_recommendations.json`, `default` and `alternate` are **identical
strings** for nearly every term (`"gutbusters" → "Gutbusters"`, `"dininae" →
"Dininae"`, `"bruenor" → "bruenor"`). Two consequences:

1. The A/B trial harness compares a candidate against an identical candidate, so
   it can never prefer one.
2. "Verified" coverage is inflated by entries that change nothing. `Artemis
   Entreri` has a mapping and is still heard as "artemis entrary" 20 times in 35.

**Fix:** reject identity mappings at write time, and count them separately from
real respellings in the inventory. A term with a no-op entry should read as
*unmapped*, because that is what it is.

### B6. Why the gate said "release ready" anyway

Two of the gate's signals never fire on this corpus. **They are not the same kind
of thing, and my first pass wrongly lumped them together.**

**Quiet by design — leave it alone.** The identity floor of 0.62
([quality_trends.py:215](brain/orchestrator/quality_trends.py:215)) sits far
below the observed 0.953–0.999, and the trend thresholds are similarly loose.
That is deliberate and documented. `docs/quality-assurance.md` (lines 139–145):

> Thresholds are deliberately quiet on the current corpus: the useful signal is a
> *change* in spread between runs, not an absolute level. They exist so a future
> sampling or seeding change has something to be evaluated against.

The same reasoning is in the code comment at
[quality_trends.py:18–25](brain/orchestrator/quality_trends.py:18). These are
**baselines for comparing runs**, not detectors. Retuning them to fire on today's
corpus would destroy the thing they were built for. I originally recommended
exactly that; it was wrong.

**Genuinely dead — worth fixing.** Monotone detection is a different case.
`monotone_warning` is `False` on **all 7,928** measured segments. It requires
`pitch_cv < 0.06` **and** `dynamic_range < 4.0`
([prosody_scorer.py:83](voice/validator/prosody_scorer.py:83)). 396 segments (5%)
satisfy the pitch half; on a random 398-segment sample, `peak/mean_rms` has a
median of **7.83**, p05 **5.29**, and 1 of 398 below 4.0. The conjunction is
unsatisfiable for natural speech at any variance, so it cannot serve as a
baseline *or* a detector — it is a constant `False`. The code's own comment says
these thresholds "are empirical and should be tuned"; unlike the trend
thresholds, there is no design note claiming the silence is intentional.

**On the 317 advisory items:** `blocking_count: 0` is correct (see C3). The
advisory list is still too long to act on, which is D4 — a presentation problem,
not a gate problem.

### B7. The pitch-jump warnings are real but not actionable

100 `audio_trend` warnings: 73 pitch jump, 17 pitch variation, 10 rate variation.
The largest is the **narrator at 3.39×** in chapter 6, then 3.28× in chapter 29,
2.81× in chapter 1. A jump ratio above 2 means the adjacent-segment pitch
difference exceeds twice the chapter's median pitch — either a genuine register
break or an octave error in F0 tracking. Either way it belongs on the narrator,
who carries most of the runtime.

The warning is unusable as written because
[quality_trends.py:69](brain/orchestrator/quality_trends.py:69) computes every
adjacent jump and then keeps only the maximum:

```python
jumps = [abs(later - earlier) / centre for earlier, later in zip(voiced_pitches, voiced_pitches[1:])]
largest_jump = round(max(jumps), 6)
```

The two `line_id`s are right there. Recording them turns "chapter 6 has a 3.4×
jump somewhere" into "listen to `ch06_0141` → `ch06_0142`". That is a one-line
change with an outsized payoff, and it is the prerequisite for deciding whether
the big jumps are audible defects or measurement artifacts — which I could not
settle from the stored data.

Related: near-silent segments (see D2) produce garbage F0 and are only excluded
when pitch is exactly 0. Excluding segments below a sensible RMS floor would
clean up the statistic.

---

## Part C — Quality improvements, ranked

### C1. Stop burning the retry budget on ASR artifacts *(biggest quality-per-effort win)*

Looking at the 32 shipped failures (corrected from 68 per B2), two completely different things are being
treated identically.

**Real defects** — proper nouns the engine mangles:

| expected | heard |
| --- | --- |
| `Bedorijay fumed.` | "Better a jay fumed." |
| `Jarlaxle said, perking up.` | "Jarl Axel said, perking up." |
| `said Tazmikella.` | "said Taz McKellar." |
| `Allefaero went on,` | "Ala Farrow went on." |
| `Braelin said to Jarlaxle…` | "Braylon said to Jar Laxal…" |

**False positives** — the audio is correct and Whisper normalised the dialect:

| expected | heard | WER | attempts |
| --- | --- | --- | --- |
| `"Ye comin' by this on yer own, are ye?"` | "Ye coming by this on your own, are ye?" | 0.22 | 3 |
| `"She telled ye that, did she?"` | "She told you that, did she?" | 0.33 | 3 |
| `"I'm yer king,"` | "I'm your king." | 0.25 | 3 |
| `"Aye,"` | "I" | **1.00** | 3 |
| `"Bah!"` | "Bye." | **1.00** | 3 |

Bruenor, Athrogate, Stokely and Belter are systematically punished for having a
dialect. `"Aye"` scores WER 1.0 on a perfectly good rendering because Whisper
writes it "I" — three redraws, then ship as `fail`.

**Correction to my first pass:** I implied the validator had no short-line or
glossary handling. It has a lot —
[validation_loop.py:1178–1226](voice/validator/validation_loop.py:1178) already
computes `spelling_variant_match`, `glossary_adjusted_wer`,
`glossary_phonetic_match`, `orthographic_segmentation_match` and a
`short_line_phonetic_acceptable` path for `word_count <= 3`. The gaps are
narrower and more specific than "add a short-line rule".

Tracing `"Aye,"` → heard `"I"` through the actual gate:

| check | result | why |
| --- | --- | --- |
| `wer` | 1.00 | one word, one error |
| `spelling_variant_match` | False | requires `2 <= word_count <= 3`; this is 1 |
| `glossary_phonetic_match` | False | "aye" is not a glossary term |
| `orthographic_segmentation_match` | False | not a fusion case |
| `wer <= threshold and similarity >= 0.85` | False | |
| → `short_line_phonetic_acceptable` | **False** | |
| → `length_sensitive_wer_failure` | **True** | `word_count <= 2 and estimated_word_errors (1.0) > 0.05` |

Three changes, each aimed at a specific line of that trace:

1. **Add a dialect layer to the normaliser.** `WhisperValidator._normalize_text`
   ([whisper_validator.py:413](voice/validator/whisper_validator.py:413)) runs
   OpenAI's `EnglishTextNormalizer`, contraction expansion and number expansion —
   nothing that knows `ye`, `yer`, `telled` or `comin'`. It is applied to **both**
   sides, so a mapping table inserted there is symmetric by construction. This is
   the single highest-value change in the item: it fixes Bruenor, Athrogate,
   Stokely and Belter at once.
2. **Let one-word lines reach the phonetic path.** Widen
   `spelling_variant_match` from `2 <= word_count <= 3` to `1 <= word_count <= 3`,
   or add a small closed-set interjection match (`aye`, `bah`, `hmph`, `nay`).
   `word_count <= 2 and estimated_word_errors > 0.05` means *any* error fails a
   short line, so nothing else in the trace can rescue it.
3. **Route proper-noun misses to the lexicon, not to a blind redraw.** When the
   only diff is a glossary term, a redraw is a coin flip per draw — and
   `repair_outlier_lines.py` already knows this, excluding `mispronounced` terms
   from blind redrawing for exactly this reason. The WER gate does not know it,
   and redraws three times.

Expected effect: the retry budget stops being spent on dwarves and becomes
available for names that actually need it.

### C2. Apply the two banked respellings

`Reghedmen → Regheadmen` (70% vs 10%) and `Quista → Kweesta` (100% vs 0%).
Between them: 13 measured samples, 11 of them currently bad. Add to the project
lexicon, redraw those lines through `shared/segment_repair.py`, re-master the
affected chapters, re-export deliveries with `reexport_deliveries.py --stale`.
Low risk, already proven, and `is_pronunciation_candidate_better()` guards
against collateral damage to other names on the same line.

### C3. Make the 68 shipped failures visible and finite

> **This item was substantially wrong and is retained as a correction.** I
> claimed 68 lines shipped silently under a gate that failed to catch them. Both
> halves of that are false.

The real picture: **32** lines shipped with a failing selected take, and **all 32
carry `disposition: acceptable`** — they were reviewed and accepted by a human.
The release gate behaved exactly as `docs/architecture.md` documents it:

```python
is_hard_failure = not bool(details.get("passed_hard_gates", True)) or quality.get("status") in {"fail", "flagged"}
blocking = is_hard_failure and (disposition not in RESOLVED_SEGMENT_DISPOSITIONS) and not is_non_spoken
```

All 32 have `passed_hard_gates: False`, so all 32 *were* blocking until a human
dispositioned them. `blocking_count: 0` is the correct report of a queue that was
worked, not a gate that leaked. The "Accepted by human review" strings I quoted
in B6 were the evidence of the gate working, and I read them as evidence of it
failing.

**What is actually worth doing here is smaller, and it points at F4.** Look at
what was accepted:

| line | authored | heard |
| --- | --- | --- |
| `ch07_0408`, `ch08_0045`, `ch30_0237` | `"Aye."` | `"I"` |
| `ch07_0148` | `"Bah!"` | `"Bye."` |
| `ch17_0190/0202/0206` | `Zak added.` / `clarified.` / `asked.` | `"Zach …"` |
| `ch08_0014` | `"Ye're thinkin' that she telled me the truth."` | `"You're thinking that she told me the truth."` |

Most of the 32 are **C1 false positives**. The gate flagged correct audio, and a
human had to clear each one by hand. That is a cost in *your* time, not in output
quality — which makes it an argument for F4, not for a new blocking rule.

The one genuine remnant: `export_quality.json` reports loudness and says nothing
about accepted-but-failing lines. An `accepted_failures` array (line ID, chapter,
WER, authored text, transcript) would make them visible at export without
changing any gate.

### C4. Revive the monotone detector *(scope reduced — see B6)*

Per B6, this is now **only** the monotone conjunction. The identity floor and the
trend thresholds are deliberately quiet per `docs/quality-assurance.md`, and
retuning them would break their documented purpose as run-to-run baselines.

The monotone test requires two conditions where one is never satisfiable, so it
returns `False` unconditionally. Fix the conjunction so the metric can express
something; leave every other threshold alone.

### C5. Attach line IDs to pitch-jump warnings

Per B7. One line of code, and it makes 73 existing warnings actionable.

### C6. Close the loop from confirmed flags into the attribution suite

`docs/attribution-case-ledger.md` already requires a new rule to be recorded with
the line that forced it and the test that pins it — the discipline exists. It is
not wired to the flags. Today `attribution_audit.json` passes on chapters 11–15
while you have four confirmed wrong-speaker flags in 13–15: the audit and your
ear disagree, and only the audit gets a vote.

A flag you confirm and repair is the best possible regression case — a real
listener, a real miss, a known correct answer. It should land in the ledger
automatically, with a generated test, so that specific mistake can never return.

---

## Part D — Fewer inputs from you

### D1. The pronunciation recommender does not recommend

166 terms sit at `status: "review_required"` with
`recommendation_default: ""` — **empty**. The pipeline asks you for 166
respellings and offers nothing to react to. That is the single largest manual
burden in the app.

The failure modes are already characterised in
`PRONUNCIATION_FOLLOWUP_PLAN.md` — intervocalic flapping, word-splitting,
boundary loss — each with an evidenced remedy. And the ASR transcript already
says *exactly* how each name was heard. "Entreri" heard as "entrary" ×75 and "and
trary" ×9 is a complete diagnosis: the engine inserts a word boundary at the
front.

Generate two real candidates per term from the observed failure mode, trial them
with `trial_respelling.py` (control first, as the doc requires), and present you
with **"here is the winner and its score"** rather than an empty box. Reserve
your attention for the terms where both candidates lose — which, on the group C
evidence, is a minority.

Rule to enforce alongside it: **a candidate identical to the term is not a
candidate** (per B5).

### D2. Scene breaks — mostly already solved; 5 stale segments and a cosmetic leak

**This item is much smaller than it first appeared, and the correction is worth
recording.** My first pass claimed 62 wasted TTS draws. That is wrong.

`is_non_spoken_separator` ([constants.py:268](shared/constants.py:268)) already
exists, and it already matches a lone em dash — verified directly:

```
'—' True    '---' True    '***' True    '* * *' True    '?' False    '...' False
```

Synthesis already honours it. `validation_loop.py`
[:218](voice/validator/validation_loop.py:218) emits
`PAUSE_MARKER_SILENCE_SECONDS` (0.1 s) of clean zeros instead of calling the
engine, and [:479](voice/validator/validation_loop.py:479) skips speaker
similarity for those lines. The review gate uses the same predicate
([review_gate.py:343](brain/orchestrator/review_gate.py:343)) so a separator can
never block a release.

Measured against the 62 separator lines in this book:

| | count |
| --- | --- |
| Already clean silence (current path) | **57** |
| Leftover TTS draws from before the feature landed | **5** |
| Missing | 0 |

So what actually remains:

1. **5 stale segments.** They still carry real engine output (peak −72 to
   −52 dBFS) and the generation cache considers them current, so nothing will
   ever replace them. They are the source of the 0.16–0.48 s durations and they
   pollute the pitch statistics (see B7).
2. **The scripting model still labels them.** `ch01_0153` is `speaker:
   bedorijay`, `emotion: "furious roar"`, `dialogue_kind: spoken`. Harmless for
   audio — synthesis ignores it — but it is why 7 of them surfaced as `audio`
   review items, and why they appear as spoken lines in the reader/lyrics view.
3. **0.1 s is a constant, not a setting.** The audible gap is fine (the 900 ms
   paragraph pause on either side gives ~1.9 s total), so this is a
   configurability nicety, not a defect.

**Fix:** force-regenerate the 5 stale segments; apply the predicate during
scripting so a separator is always `narrator` / no emotion /
`non_spoken_quote`; move `PAUSE_MARKER_SILENCE_SECONDS` into config.

### D3. Diagnose flags automatically, and diagnose the window

Per A7 and A8. Once diagnosis uses the real attribution machinery, run it on
creation and store the verdict. Diagnose the whole reaction window, ranked, not
just the line at the tap — the 20 s window is already computed and then unused.
You would open the dashboard to opinions, not to raw taps.

### D4. Make the release gate's advisory list finite and sorted

317 unreviewed items in one report is not a worklist, it is a wall. The
information to rank them is already present:

- Pronunciation items carry `occurrences` — sort by it. `Breezy` at 1,108
  outranks `Adbar` at 3 by three orders of magnitude.
- Audio items carry a rejection reason — group the 15 name-misses together;
  they are one fix, not fifteen.
- `audio_trend` items are per voice-chapter — collapse to one row per voice.

Same data, reported as "5 things worth your time" instead of 317 rows.

### D5. Give the identity mapping a place to be recorded

`brain/pronunciation_dict.json` holds `Kvothe`, `Elodin`, `Denna` — terms from a
different book. They surface in Edge of Twilight's audit as 4 permanently
`insufficient` verdicts with 0 samples, because a global lexicon is inherited by
every project. Scope global entries to the book that introduced them, or filter
terms with zero occurrences out of the per-project audit. Small, but it is 4
rows of permanent noise per book and it will grow with every title.

---

## Part E — Performance, and the rest

### E1. 5.5 hours of prefill per book, none of it reused *(largest single lever)*

Measured across 1,497 Ollama calls in this book's `performance_metrics.jsonl`:

| | |
| --- | --- |
| Total prefill | **331.1 min** over 8,585,435 prompt tokens (432 tok/s) |
| Total decode | 1,445.1 min over 3,649,796 output tokens (42 tok/s) |
| Prefill share of LLM compute | 18.6% |
| Median prompt per call | 5,995 tokens, **7.71 s** |
| Calls reporting a full prefix-cache hit | **0 of 1,497 (0.0%)** |

`brain/config.yaml` sets `num_parallel: 1` specifically to enable prefix reuse,
and the comment is right that the system prompt is byte-identical across chunks
within a chapter: *"re-evaluating it per chunk is pure waste."* The data says it
is being re-evaluated every single time.

The structural reason is visible in the template. `_SYSTEM_PROMPT`
([script_generator.py:466](brain/director/script_generator.py:466)) opens with:

```
You are a STRICT AUDIOBOOK SCRIPT METADATA ANNOTATOR. …
## Context
### Character Registry
{character_registry}          <- per-chapter, scoped to allowed_speakers
### Previous Chapter Summary
{previous_summary}            <- per-chapter
## Script Tagging Task
### Audio Direction Guidelines  <- the large static rulebook, AFTER the variables
```

The variable content sits **before** the static rulebook, so the shared prefix
ends after about twenty tokens. Moving the rulebook to the front and the Context
block to the end would make the rulebook — a substantial share of the ~5,700
tokens — a cacheable prefix across every chunk, every chapter, and every book.

Two honest caveats: the exact saving depends on what fraction of the prompt the
rulebook is, and **reordering a prompt can change model behaviour**. This must be
screened for attribution quality with `scripts/benchmark_script_chunks.py` and
the case ledger, not promoted on the speed number alone. But the upside is on the
order of hours per book, and the change is a text move.

### E2. `voice_crash.log` is opened without an encoding

[main.py:483](voice/tts_server/main.py:483): `with open("voice_crash.log", "a") as f`.
On Windows that is cp1252, so a traceback containing a non-ASCII character raises
`UnicodeEncodeError` *inside the crash handler* and destroys the report you
needed. The rest of the codebase is disciplined about this — a grep for
encoding-less text opens across `brain/`, `shared/`, `tools/`, `scripts/` and
`voice/` returns only log handles and a lock file. Worth fixing the crash log
specifically.

### E3. Expected duration is word-count based

[audio_analyzer.py:226](voice/validator/audio_analyzer.py:226) computes expected
duration purely from word count and WPM. `"Bedorijay fumed."` is 2 words → 0.8 s
expected against 2.08 s actual; the 1.25 s tolerance floor is exceeded by 0.03 s
and it fails. A 6-syllable fantasy name is one word. A syllable estimate (or a
per-term duration hint for known glossary names) would remove a whole class of
false duration failures, which fall disproportionately on exactly the lines that
also have real pronunciation problems.

### E4. What is healthy — no action needed

Worth stating so effort goes where it is needed:

- **Tests: 1,068 passed, 2 skipped, 56 subtests, in 31 s.** Fast and green.
- **Mastering and loudness** are excellent and uniform (B1).
- **Speaker identity** is high and stable across the whole book.
- **The measurement discipline** — measure, trial with a control, record the
  evidence, pin it with a test — is genuinely good and is the reason this
  document could be written from data rather than guesswork.
  `docs/pronunciation-evidence.md` and `docs/attribution-case-ledger.md` are the
  two best artefacts in the repo.
- **Encoding hygiene** across the main packages is correct.

---

## Suggested order of work

Ordered by value per unit of effort, with dependencies respected.

| # | item | part | effort | status | why here |
| --- | --- | --- | --- | --- | --- |
| 1 | Apply `Regheadmen` + `Kweesta` | C2 | XS | Done | Proven, banked, zero design work |
| 2 | Kill duplicate flags (client ID + content dedupe + rename envelope `status`) | A1 | S | Done | Halves the queue; unblocks all flag work |
| 3 | Re-enrich on import; pass `created_at` through | A3, A4 | S | Done | Makes the surviving flags triageable |
| 4 | Dialect normalisation + short-line WER rule | C1 | M | Done | Recovers the retry budget; stops shipping false failures |
| 5 | Fix `--repair` (use `segment_repair.py`) | A6 | S | Done | Currently claims fixes it did not make |

| 6 | Replace `--auto-diagnose` with the real attribution machinery | A7 | M | Done | Blocks A8; today it is unsafe to act on |
| 7 | Position sanity: max match distance + book/chapter origin | A5 | S | Done | Stops silent wrong-line matches |
| 8 | Regenerate 5 stale separator segments; label separators in scripting | D2 | XS | Done | Feature already exists; 5 leftovers and a cosmetic leak |
| 9 | Reorder `_SYSTEM_PROMPT`, screen with the benchmark | E1 | M | Held (Declined) | Screened; inter-chunk cache unproven, attribution drift observed |
| 10 | Pronunciation candidate generation from failure mode | D1 | M–L | Done | The largest recurring manual burden |
| 11 | Line IDs on pitch-jump warnings | C5 | XS | Done | Makes 73 existing warnings usable |
| 12 | Revive the monotone detector (identity floor is quiet **by design** — leave it) | C4 | S | Done | One metric that is a constant `False` |
| 13 | Rank and collapse the advisory list | D4 | S | Done | 317 rows → a short worklist |
| 14 | Auto-diagnose on flag creation | A8 | S | Done | Depends on #6 |
| 15 | Confirmed flags → attribution case ledger | C6 | M | Done | Turns your listening into permanent regressions |
| 16 | List human-accepted failures in `export_quality.json` (**no** new blocking rule) | C3 | XS | Done | 32 accepted this time; most vanish after #4 |
| 21 | Fix the stale and newly-stale documentation | G | S | Done | Four docs already contradict the code |
| 17 | Reject identity mappings in the lexicon | B5 | XS | Done | Stops inflating coverage |
| 18 | Scope the global lexicon per book | D5 | XS | Done | 4 permanent noise rows per book |
| 19 | `voice_crash.log` encoding | E2 | XS | Done | Protects the crash report |
| 20 | Syllable-aware expected duration | E3 | S | Done | Removes a false-failure class |

---

## Part F — Implementation specs

Written to be executed without re-deriving anything from Parts A–E. Each spec
gives the files, the change, the tests that must exist, and the command that
proves it. Numbering follows the order-of-work table.

### Ground rules — read before touching anything

These are not style preferences. Each one has a scar behind it.

1. **Emberdark is metrics-only.** `isles-of-the-emberdark-a-cosmere-novel-secret-projects-book-5`
   is delivered. Never repair its segments, re-master it, re-export its
   deliveries, or apply lexicon changes to it. Read it for measurement only.
2. **`Catti-brie` is settled.** The owner declined the `Katteebree` respelling on
   2026-09-16 with trial evidence. Do not re-propose it, and do not include
   `Catti-brie` in any batch respelling run.
3. **Do not "fix" the apostrophe asymmetry in `normalize_phonetic_text`.** It
   joins hyphens out and leaves apostrophes deliberately: stripping the
   apostrophe from `Do'Urden` moves it from 6/14 correct to **0/14**.
4. **A repair moves four stores or it is not a repair.** Segment WAV,
   `manifests/chapter_NNN.segments.json`, `voice_cache.db`
   (`generation_fingerprints.output_hash`), and a `quality_logs` row. Always go
   through `shared/segment_repair.replace_segment` — never write a WAV by hand.
   Then re-master the chapter **and** re-export the delivery, or the fix exists
   only in the workspace while every intermediate check passes.
5. **A new attribution rule needs a ledger entry and a test before it ships.**
   `docs/attribution-case-ledger.md` — the line that forced it, and the test that
   pins it. No exceptions.
6. **Never commit a state where a measured term went backwards.** Re-run
   `scripts/measure_pronunciations.py <project_id>` after any lexicon or audio
   change and compare per-term outlier counts to the previous run.
7. **`ruff check` and `ruff format --check` gate CI.** Both are enforced by
   `.github/workflows/ci.yml`. Note that `docs/architecture.md` §5 still says
   *"`ruff format` is intentionally not enforced yet"* — **that line is stale**;
   CI has enforced it since 2026-09-04. Fix the doc as part of G1.
8. **Baseline:** `./venv/Scripts/python.exe -m pytest tests -q` → 1,068 passed,
   2 skipped, 56 subtests, ~31 s. Any spec below that reduces this count has
   broken something. Also run
   `./venv/Scripts/python.exe scripts/verify_pipeline.py --tier static`, which
   adds `compileall`, `node --check` on the dashboard JS, and the markdown link
   check, writing `verification-manifest.json`.
9. **`selected` is the winner, not the last attempt.** Per
   `docs/quality-assurance.md`: *"an older selected retry remains the reported
   winner while all attempts still count toward retry metrics."* Any analysis of
   `quality_logs` must filter on `details.selected`. Reading the highest attempt
   number instead is what produced the wrong figure corrected in B2.
10. **Deliberately quiet is not broken.** The trend thresholds and the identity
    floor are documented baselines for comparing runs, not detectors
    (`docs/quality-assurance.md` 139–145). Do not retune them to fire on the
    current corpus.
11. **Four cross-system rules from `docs/architecture.md` §"Feature Maintenance &
    Impact Guidelines"** apply to specs below and are cited where they bite:
    - §3 any script-generator change must validate against
      `assert_script_covers_source`, and a grouping-policy change must invalidate
      the script fingerprint (`scripts/refresh_script_fingerprints.py`);
    - §1 a frontend change must keep the `?v=` asset revision and `FRONTEND_BUILD`
      in `main.py` in step — `tests/test_dashboard_base_path.py` enforces it;
    - §2 any new file artifact or pipeline flag must be registered in
      `reset_pipeline_stage`;
    - §5 paths resolve through `shared/paths.py`, never bare relative literals.
12. **A reversed or newly-justified behaviour needs a decision record.**
    `docs/decisions/README.md` requires every record to open with a `**Status:**`
    line, and a superseded claim to be struck through with its correction inline —
    never rewritten or deleted. F4, F9 and F12 each need one.

Where a spec says **[assumption]**, I have made a decision that was genuinely
open, so the work is not blocked. Each is called out so you can overrule it.

---

### F1 — Apply the two banked respellings *(order #1)*

**Why:** `Regheadmen` trialled at 70% vs 10%; `Kweesta` at 100% vs 0%. Neither
reached the lexicon. Both terms are still verdicted `mispronounced`.

**Files:** `brain/projects/the-finest-edge-of-twilight-book/pronunciation_dict.json`

**Steps, in order — do not reorder:**

1. Re-measure first, to get a current baseline:
   `python scripts/measure_pronunciations.py the-finest-edge-of-twilight-book`
   Record the `outliers` count for `Reghedmen` and `Quista` (expected 7 and 4).
2. Add two entries to the project dict:
   ```json
   "Reghedmen": "Regheadmen",
   "Quista": "Kweesta"
   ```
   Leave the other 12 entries alone.
3. Start the voice server on port 8100 (`python scripts/start_voice.py`). The
   dashboard does not start it and the repair script requires it.
4. Dry run: `python scripts/repair_outlier_lines.py the-finest-edge-of-twilight-book --term Reghedmen --term Quista`
   Confirm it reports lines it *would* repair.
5. Apply: same command with `--apply`.
6. Re-master every chapter the script reports as touched:
   `python scripts/remaster_chapters.py the-finest-edge-of-twilight-book <chapters...>`
7. `python scripts/reexport_deliveries.py the-finest-edge-of-twilight-book --stale`
   to list, then re-run without `--stale` for the affected batches (add `--full`
   for the whole-book M4B).
8. Re-measure and confirm both terms improved and **no other term regressed**.

**Note:** `Quista` and `Canzay` occur together as "Quista Canzay". The
cross-term guard in `is_pronunciation_candidate_better` will refuse a take that
fixes one and breaks the other, so a low repair count here is correct behaviour,
not a failure.

**Tests:** none required — this is data, not code. The measurement re-run *is*
the test.

**Verify:** both terms leave the `mispronounced` verdict; every other term's
`outliers` is unchanged or lower; `reexport_deliveries.py --stale` reports all 8
deliveries `ok`.

---

### F2 — Stop duplicate flags *(order #2)*

**Why:** A1. One tap, two rows, two IDs.

**Files:** `brain/dashboard/api/mobile.py`, `brain/orchestrator/job_queue.py`,
`tests/test_mobile_api.py`

**⚠ Cross-repo hazard — read this before changing the response envelope.** The
POST at [mobile.py:1211](brain/dashboard/api/mobile.py:1211) returns
`{"status": "flagged", "flag": flag}`. The NAS Streamer at
`192.168.50.180:8005` serves the same route and is **not in this working tree**
(compare `CHAPTER_SYNC_FOLLOWUP_PLAN.md` Part 5, which records `E:\Projects\Voice`
as exactly this kind of out-of-tree client). Renaming the key here without
updating and redeploying the streamer changes what the streamer stores.

**[assumption]** Do the safe half now and defer the rename: add the new key
**alongside** the old one rather than replacing it. `{"result": "created",
"status": "flagged", "flag": flag}`. Nothing breaks, new clients can migrate, and
the import-side coercion (change 3) makes the old key harmless immediately.

**Changes:**

1. **Accept a client-supplied ID.** Add `client_flag_id: str | None = Field(default=None, max_length=128)`
   to `PlaybackFlagRequest`. In `create_playback_flag`, use it when present and
   valid, else mint as today ([mobile.py:1179](brain/dashboard/api/mobile.py:1179)).
   Validate against the same pattern the table expects; reject anything with path
   separators.
2. **Content dedupe.** Add `JobQueue.find_duplicate_playback_flag(project_id,
   chapter_number, position_ms, line_id, *, window_ms=2000)`. Return an existing
   row when `line_id` matches **or** `abs(position_ms - existing) <= window_ms`
   within the same chapter. In the POST handler, return `201` with the existing
   flag rather than inserting a second. In `_sync_flags_with_streamer_and_disk`
   ([mobile.py:1126](brain/dashboard/api/mobile.py:1126)), call it before
   `create_playback_flag` and skip the import on a hit.
   **[assumption]** 2,000 ms window — one tap cannot legitimately produce two
   flags two seconds apart, and the observed duplicate pairs are 1–2 s apart with
   *identical* `position_ms`, so even a 0 ms window would catch these. 2,000 ms
   also covers a genuine double-tap.
3. **Coerce unknown statuses on import.** At
   [mobile.py:1151](brain/dashboard/api/mobile.py:1151), replace
   `if f.get("status") and f["status"] != "pending":` with a check against a
   known set — `{"open", "investigating", "investigated", "vetoed", "resolved",
   "fixed", "dismissed"}` — and map anything else (including `flagged`) to
   `open`. Log the coercion at INFO with the offending value.
4. **Front-end allow-list.** In `brain/dashboard/frontend/js/app.js`, define the
   status set once and treat an unrecognised status as open rather than
   terminal, so a future drift is loud. Touch points: `:3622` (badge), `:3642`
   (filter), `:3710` (dropdown), `:3813` (action branch).
   **Per architecture §1:** bump the `?v=` revision in `index.html` and
   `FRONTEND_BUILD` in `main.py` together — `tests/test_dashboard_base_path.py`
   fails otherwise — and do not add a local `escapeHtml`; it lives once in
   `js/dom-utils.js`.

**Docs to update with this change:** `docs/dashboard-guide.md` (the status filter
list at line 142 does not include `flagged` and will not need to once coerced —
state the canonical set), `docs/api-reference.md` and
`docs/crazy-voice-companion.md` (the new `client_flag_id` field and the response
envelope).

**Tests** (`tests/test_mobile_api.py`):

- POST the same `(chapter, position_ms, line_id)` twice → one row, `total_flags`
  is 1.
- POST twice with `position_ms` 1,500 ms apart → one row. 2,500 ms apart → two.
- POST with an explicit `client_flag_id` → that ID is stored verbatim.
- Import a remote flag whose `status` is `"flagged"` → stored as `open`.
- Import a remote flag whose `flag_id` differs but whose content matches an
  existing row → no second row.

**Verify:** `./venv/Scripts/python.exe -m pytest tests/test_mobile_api.py tests/test_playback_flags_e2e.py -q`

**Data cleanup (separate, after the code lands):** the 14 existing ghosts need
removing. Write a one-off script that, for each `(chapter_number, position_ms)`
pair with more than one row, keeps the row whose `enriched_data` contains
`active_line` and deletes the rest. Dry-run and print before deleting. Do not
fold this into the API change.

---

### F3 — Re-enrich on import, preserve `created_at` *(order #3)*

**Why:** A3 and A4. The streamer's enrichment schema is not the dashboard's, and
the import stamps its own timestamp.

**Files:** `brain/dashboard/api/mobile.py`, `brain/orchestrator/job_queue.py`,
`tests/test_mobile_api.py`

**Changes:**

1. **Always re-enrich.** At [mobile.py:1136](brain/dashboard/api/mobile.py:1136),
   replace:
   ```python
   enriched = f.get("enriched_data") or f.get("line_metadata")
   if not enriched:
       enriched = _enrich_flag_context(...)
   ```
   with an unconditional `_enrich_flag_context(...)` call. It is local file I/O —
   script JSON plus the chapter timeline — with no model inference, so the cost is
   negligible and the schema is guaranteed to be the one the UI and
   `investigate_playback_flags.py` read.
   **[assumption]** Discard the remote `enriched_data` entirely rather than
   merging. A merge would leave two schemas alive in one record, which is the
   condition that produced this bug.
2. **Preserve the remote timestamp.** Add `created_at: str | None = None` to
   `JobQueue.create_playback_flag` ([job_queue.py:837](brain/orchestrator/job_queue.py:837));
   use it when supplied, else `datetime.now(UTC).isoformat()` as today. Pass
   `f.get("created_at")` from the import path. Validate it parses as ISO-8601 and
   fall back to `now` if not.

**Tests:**

- Import a remote flag carrying the streamer schema (`matched_speaker`,
  `nearby_lines`) → stored record has `active_line`, `candidate_lines` and
  `manuscript_excerpt`.
- Import a remote flag with `created_at: "2026-09-17T05:25:20+00:00"` → stored
  `created_at` matches, not the test's wall clock.
- Import one with `created_at: "garbage"` → falls back to now, does not raise.

**Verify:** `./venv/Scripts/python.exe -m pytest tests/test_mobile_api.py -q`

---

### F4 — Dialect normalisation and one-word lines *(order #4)*

**Why:** C1. Three redraws and a shipped `fail` for audio that is correct.

**Files:** `voice/validator/whisper_validator.py`,
`voice/validator/validation_loop.py`, `shared/constants.py`,
`tests/` (new file: `test_dialect_normalisation.py`)

**Change 1 — the dialect table.** Add to `shared/constants.py`:

```python
#: Dialect spellings Whisper silently standardises. WER compares the authored
#: text against a transcript, so a dwarf who says "ye" is scored against a
#: transcript that writes "you" and fails for being in character. Applied to
#: BOTH sides in WhisperValidator._normalize_text, so it can only ever make the
#: two agree -- it never changes what is synthesised.
DIALECT_NORMALISATIONS: dict[str, str] = {
    "ye": "you",
    "yer": "your",
    "yerself": "yourself",
    "telled": "told",
    "em": "them",
    "o": "of",
    "d'ye": "do you",
    "ain't": "is not",
    "nay": "no",
    "aye": "yes",
    "bah": "bah",
}
```

**[assumption]** `aye → yes` rather than `aye → i`. Whisper transcribes "Aye" as
"I", so the cleaner fix is change 3 below (interjections), and mapping `aye→i`
would collide with the pronoun on every line containing "I". Map the *authored*
side to `yes` and let the interjection set handle the transcript.

Also add a suffix rule, not a table entry: `-in'` → `-ing` (`comin'` → `coming`,
`cheatin'` → `cheating`). This is a regex, applied before the table.

**Change 2 — wire it in.** In `WhisperValidator._normalize_text`
([whisper_validator.py:413](voice/validator/whisper_validator.py:413)), insert a
step **after** the `EnglishTextNormalizer` call and **before** punctuation
stripping. Order matters: the OpenAI normaliser lowercases and expands
contractions, so the table should see lowercase input; punctuation stripping must
come last or `d'ye` and `comin'` lose their apostrophes before the rule sees
them. Apply per-token on word boundaries, never as a substring replace — `"yer"`
must not match inside `"lawyer"`.

**Change 3 — let one-word lines reach the phonetic path.** In
`validation_loop.py`, widen `spelling_variant_match`
([:1177](voice/validator/validation_loop.py:1177)) from `2 <= word_count <= 3`
to `1 <= word_count <= 3`, and add a closed interjection set consulted by
`short_line_phonetic_acceptable` ([:1206](voice/validator/validation_loop.py:1206)):

```python
#: Single-word interjections Whisper renders as a different word entirely
#: ("Aye" -> "I", "Bah" -> "Bye"). WER on a one-word line is 0 or 1, so these
#: can never pass the text gate however good the audio is.
INTERJECTION_HOMOPHONES = {
    "aye": {"i", "aye", "eye"},
    "bah": {"bah", "bye", "ba"},
    "nay": {"nay", "neigh"},
    "hmph": {"hmph", "hm", "hmm"},
}
```
A one-word line whose normalised expected form is a key and whose normalised
transcript is in the set passes the text gate, **provided the acoustic checks
pass** — keep `not clipping_detected and not has_long_silence and duration_ok`,
exactly as `short_line_phonetic_acceptable` already requires. Audio quality is
still gated; only the text comparison is relaxed.

**Change 4 — do not redraw a glossary-only miss.** Where a validation failure's
only diff is a glossary term, mark the result so the retry loop stops after the
first attempt and records the term instead. This mirrors the rule
`repair_outlier_lines.py` already applies (`REPAIRABLE = {"unstable"}`;
`mispronounced` is excluded because redrawing only reshuffles the failure).

**Tests** (new `tests/test_dialect_normalisation.py`, plus additions to the
existing validator tests):

- `_normalize_text("Ye comin' by this on yer own, are ye?")` equals
  `_normalize_text("You coming by this on your own, are you?")`.
- `_normalize_text("She telled ye that, did she?")` equals
  `_normalize_text("She told you that, did she?")`.
- **Negative:** `"lawyer"`, `"yellow"`, `"emblem"`, `"often"` are unchanged —
  this is the substring-collision guard and it is the most important test here.
- `"Aye,"` against transcript `"I"` → validation passes when acoustics are clean.
- `"Aye,"` against transcript `"I"` → validation still **fails** when
  `clipping_detected` is True. Proves the acoustic gate was not weakened.
- A real WER failure (`"Bedorijay fumed."` → `"Better a jay fumed."`) still fails.
  Proves the change did not blanket-loosen the gate.

**Verify:**
```bash
./venv/Scripts/python.exe -m pytest tests -q
```
Then re-run validation over a known-dialect chapter (7 or 8) with
`scripts/validate_existing_segments.py` and confirm Bruenor's lines stop failing
**while** the name failures in the same chapter still fail.

**Guardrail:** this changes the acceptance gate for every future book. The
negative tests are not optional — a table entry that matches inside a longer word
silently passes bad audio corpus-wide.

**Source-fidelity check.** `docs/architecture.md` §"Source-fidelity invariant"
requires that reassembling every script line equals the normalized source
exactly once. This change is safe under it: the dialect table lives in
`WhisperValidator._normalize_text`, which is used **only** to compare a transcript
against authored text. It never touches `_prepare_synthesis_text` and never
changes what is spoken. State this explicitly in the decision record so a future
reader does not have to re-derive it.

**Docs to update:** `docs/quality-assurance.md` — the hard-failure list at line 40
describes the WER rule and must describe the dialect layer and the interjection
set. Plus a decision record per ground rule 12: this reverses the implicit
position that a transcript mismatch is always a defect, and the evidence (three
redraws and a human acceptance for `"Aye."`) belongs on the record.

---

### F5 — Make `--repair` actually repair *(order #5)*

**Why:** A6. It rewrites the speaker, reports `resolved`, and deletes a WAV at a
path that does not exist.

**Files:** `tools/investigate_playback_flags.py`

**The dead line** is [:285](tools/investigate_playback_flags.py:285):
`workspace_dir / "audio" / f"ch_{ch_num:03d}_{line_id}.wav"`. The real path is
`workspace/<project>/segments/{line_id}.wav`.

**Change:** model the repair on `scripts/repair_outlier_lines.py`, which is the
reference implementation. After patching the speaker in the chapter script:

1. Resolve the new voice: `get_speaker_voice_mapping` from
   `shared/voice_casting.py`.
2. Generate a candidate with the voice server:
   `VoiceClient.generate_line(GenerateLineRequest(project_id=..., line=ScriptLine(line_id=f"repair-{line_id}", speaker=..., voice_id=..., text=..., emotion=...)))`
   — imports are `from brain.orchestrator.voice_client import VoiceClient` and
   `from shared.models import GenerateLineRequest, ScriptLine, ValidateRequest`.
3. Validate it: `VoiceClient.validate_segment(ValidateRequest(...))`.
4. Only on a pass, commit through
   `shared.segment_repair.replace_segment(project_id, line_id, candidate, validated, project_dir=..., segments_dir=..., cache_db=..., state_db=..., chapter=..., repaired_by="tools/investigate_playback_flags.py")`.
5. Mark the flag `resolved` **only if `replace_segment` returned success**. On
   failure, leave the flag open and say why.
6. Print the exact follow-up commands the operator must run (`remaster_chapters.py`
   then `reexport_deliveries.py`), and record the touched chapter on the flag.

**[assumption]** Do not auto-run re-master and re-export from this tool. They are
long, they mutate deliverables, and `repair_outlier_lines.py` leaves them to the
operator too. Consistency beats convenience here.

**Also:** add `--dry-run` (default **on**, mirroring `repair_outlier_lines.py`'s
`--apply` discipline) so the destructive path is opt-in.

**Tests:** a unit test with `VoiceClient` and `replace_segment` stubbed:

- `replace_segment` returns failure → flag status is unchanged and the exit code
  is non-zero.
- `replace_segment` succeeds → flag is `resolved`, resolution names the chapter.
- Without `--apply`, no store is written.

**Verify:** `./venv/Scripts/python.exe -m pytest tests -q`, then a live repair on
a **sample_book** project — not on a delivered book.

---

### F6 — Replace `--auto-diagnose` with the real attribution machinery *(order #6)*

**Why:** A7. A bare regex at confidence 0.85 that suggested `savahn` for plain
narration. Two of the first four suggestions would corrupt the script.

**Files:** `tools/investigate_playback_flags.py`,
`tests/test_playback_flags_e2e.py`, `docs/attribution-case-ledger.md`

**Delete** `diagnose_flag`'s regex body
([:58–140](tools/investigate_playback_flags.py:58)) entirely. Do not extend it.

**Rebuild it from the functions the pipeline itself uses**, all in
`brain/director/`:

| need | function |
| --- | --- |
| Shape the context exactly as the pipeline does | `attribution_detector.build_turn_window(chapter, idx, reason=..., pattern=..., window_radius=5, scene_radius=8)` |
| What an attached speech tag establishes | `attribution_audit.tag_speaker_evidence(tag, registry) -> (name \| None, Gender \| None)` |
| Action-beat subject | `attribution_audit.beat_subject(text, registry)` |
| Beat attributions over a paragraph | `attribution_audit.action_beat_attributions(...)` |
| Who a tag addresses (not who speaks) | `attribution_audit.tag_addressee(tag, registry)` |
| Refutation layer | `attribution_audit.refuted_candidate_sets(...)` / `_refutations(...)` |
| Possessive contradictions | `attribution_audit.detect_possessive_contradictions(...)` |

**Required behaviour:**

1. **Diagnose the window, not the tap.** The enrichment already computes a 20 s
   pre / 2 s post `reaction_window`; today only `active_line` is examined. Rank
   every line in the window and return the ranked list. A listener taps *after*
   hearing the problem, so the culprit is usually earlier.
2. **Confidence must come from the evidence.** An attached speech tag naming a
   speaker is the pipeline's strongest signal and outranks adjudication. A name
   appearing loose in the excerpt is **not evidence** — that is precisely the bug.
   If no rule fires, return `INCONCLUSIVE`. An honest abstention is the correct
   output for `ch13_0424`.
3. **Never propose a speaker for a non-dialogue line.** `ch13_0424` and
   `ch15_0062` are narration; the tool proposed characters for both. Check
   `dialogue_kind` / `ScriptGenerator._is_dialogue_fragment` first and abstain.

**Tests** — use the four real cases from A7 as fixtures:

| line | expected verdict |
| --- | --- |
| `ch13_0424` ("And a folded parchment…") | `INCONCLUSIVE`, **no** suggestion — narration |
| `ch15_0062` ("Avelyere showed Breezy the portal…") | `INCONCLUSIVE`, **no** suggestion — narration |
| `ch14_0358` ("And you think the Monastery…") | keep `jarlaxle` |
| `ch18_0132` ("she accused.") | may propose `kyrnill` |

**Ledger:** per ground rule 5, if this introduces or reuses a deterministic rule,
record it in `docs/attribution-case-ledger.md` with the line that forced it and
the test that pins it.

**Verify:**
```bash
./venv/Scripts/python.exe -m pytest tests -q
./venv/Scripts/python.exe tools/investigate_playback_flags.py the-finest-edge-of-twilight-book --auto-diagnose
```
The two narration lines must come back `INCONCLUSIVE`.

---

### F7 — Position sanity: match distance and offset origin *(order #7)*

**Why:** A5. Two flags five hours outside their chapter both resolved silently to
its last line.

**Files:** `brain/dashboard/api/mobile.py`, `tests/test_mobile_api.py`

**Changes:**

1. **Record the match quality.** In `_enrich_flag_context`, the nearest-line
   fallback ([:970–982](brain/dashboard/api/mobile.py:970)) keeps the closest
   line at any distance. Add `match_distance_ms` and `match_confidence` to the
   returned dict — `exact` when the position falls inside the line's span,
   `near` within the threshold, `out_of_range` beyond it.
   **[assumption]** threshold 30,000 ms. Generous enough for clock skew, chapter
   offset drift and the 20 s reaction window; far tighter than the 5-hour misses
   observed.
2. **Try a book-absolute reading before giving up.** When `position_ms` exceeds
   the chapter's own duration, re-interpret it against the cumulative book
   timeline and re-derive the chapter. `_chapter_duration(project_dir,
   workspace_dir, chapter_num)` already exists at
   [mobile.py:119](brain/dashboard/api/mobile.py:119) — sum it across chapters
   for the cumulative offsets. Record which interpretation was used in
   `position_origin_resolved`.
3. **Let the client be explicit.** Add optional `position_origin: Literal["chapter", "book"] | None`
   to `PlaybackFlagRequest`. When present, trust it and skip the inference. This
   is the permanent fix; 1 and 2 are the fallback for clients that do not send it.
4. **Surface it.** A flag whose match is `out_of_range` must render as such in the
   dashboard rather than as a confident match on the last line.

**Tests:**

- Chapter-relative position inside a line → `exact`, correct `line_id`.
- Position 5 h beyond the chapter, matching a book-absolute offset inside that
  chapter → resolves via the book timeline, not to the last line. Use the real
  numbers: chapter 13, `position_ms` 20,078,546, expected chapter 13.
- Position 20,774,923 with `chapter_number: 13` → resolves into chapter **14**,
  the correct answer.
- Position beyond every interpretation → `out_of_range`, and the flag is still
  created (never silently dropped).
- `position_origin: "book"` supplied explicitly → inference is skipped.

**Verify:** `./venv/Scripts/python.exe -m pytest tests/test_mobile_api.py -q`

**Field follow-up (not code):** confirm on the device whether the full-book M4B
and the chapter-range M4Bs report different offset origins. Change 3 makes this
moot once the client sends the field.

---

### F8 — The five stale separator segments *(order #8)*

**Why:** D2, as corrected. 57 of 62 separators are already clean silence; 5 are
leftover engine output the cache considers current.

**Files:** `brain/director/script_generator.py`, `shared/constants.py`,
`brain/config.yaml`

**Steps:**

1. **Identify them.** For every line where `is_non_spoken_separator(text)` is
   true, read `workspace/<project>/segments/<line_id>.wav` and flag any whose
   peak amplitude is non-zero. Expect exactly 5 in this book.
2. **Replace them** with `PAUSE_MARKER_SILENCE_SECONDS` of zeros at the segment
   sample rate (24,000 Hz, 16-bit mono), committed through `replace_segment` so
   all four stores move. Then re-master those chapters and re-export.
3. **Stop the label leak.** In `script_generator.py`, after metadata inflation,
   force any line where `is_non_spoken_separator(line.text)` is true to
   `speaker="narrator"`, `emotion=""`, `dialogue_kind="non_spoken_quote"`,
   `speaker_confidence=1.0`. This is what stops `ch01_0153` being
   `bedorijay` / `"furious roar"` and keeps separators out of the review queue
   and the reader view.
4. **Make the duration configurable.** Move `PAUSE_MARKER_SILENCE_SECONDS`
   ([constants.py:260](shared/constants.py:260)) to `brain/config.yaml` with the
   current 0.1 as default. **[assumption]** keep 0.1 s — measured total scene gap
   is ~1.9 s including the 900 ms paragraph pauses either side, which is a
   correct audiobook beat. Do not change the value as part of this item.

**Architecture §3 obligations — step 3 touches the script generator:**

- Validate the change against `assert_script_covers_source(script, source_text)`.
  Forcing `speaker`/`emotion` changes metadata only and must not alter text, but
  the invariant requires the check regardless.
- Changing line metadata is a grouping-policy change in the sense §3 means, so
  **invalidate the script fingerprint** (`scripts/refresh_script_fingerprints.py`)
  or the next run will reuse scripts carrying the old labels.

**Tests:**

- A script line whose text is `"—"` comes out of scripting as `narrator`, empty
  emotion, `non_spoken_quote`.
- Same for `"---"`, `"***"`, `"* * *"`.
- **Negative:** a line whose text is `"?"` or `"..."` is *not* treated as a
  separator — `is_non_spoken_separator` already encodes this and the test locks
  it in.
- `assert_script_covers_source` passes on a chapter containing separators.

**Verify:** `./venv/Scripts/python.exe -m pytest tests -q`, then re-scan the
book and confirm zero separator segments have non-zero peak amplitude.

**Docs to update:** `docs/configuration.md` gains the new pause/silence key;
`docs/architecture.md` §"Processing stages" step 6 lists the pause ladder and
should mention the separator path.

---

### F9 — Reorder the scripting system prompt *(order #9)*

**Why:** E1. 331 minutes of prefill per book, 0 of 1,497 calls reusing a prefix.

**Files:** `brain/director/script_generator.py`

**The problem:** `_SYSTEM_PROMPT`
([:466](brain/director/script_generator.py:466)) opens with
`{character_registry}` and `{previous_summary}` — both per-chapter — and puts the
large static rulebook after them. The shared prefix therefore ends about twenty
tokens in.

**The change:** move the static rulebook to the top and the `## Context` block
(registry, previous summary, chapter number, chapter title) to the bottom. The
formatting placeholders do not change, only their position. Keep the schema
appendix (`_DIALOGUE_FOCUSED_SCHEMA_PROMPT`, appended at
[:1209](brain/director/script_generator.py:1209)) adjacent to the rest of the
static text so it is inside the cacheable prefix too.

**⚠ This is the one item where passing tests do not mean success.** Reordering a
prompt can change model behaviour. The speed win is not the acceptance criterion.

**Required screening, in this order:**

1. **Attribution quality first.**
   ```bash
   ./venv/Scripts/python.exe -m pytest tests -q
   ```
   Then re-script a chapter with known-hard attribution — chapter 14 or 18, both
   of which carry confirmed flags — and diff the resulting `speaker` assignments
   against the current script line by line. **Any** change must be explained, and
   a regression against a case in `docs/attribution-case-ledger.md` fails the
   item outright.
2. **Then speed.**
   ```bash
   ./venv/Scripts/python.exe scripts/benchmark_script_chunks.py --project <project> --chapter 7 --repetitions 2 --order AB
   ```
   Compare prefill tok/s derived from `prompt_eval_duration_ns`, and — the real
   signal — whether `prompt_eval_duration_ns` **falls** on the second and later
   chunks of a chapter. That is prefix reuse; a raw tok/s improvement is not.

**[assumption]** If attribution output changes at all, stop and report rather
than accepting the trade. The scripting quality policy in
`docs/scripting-quality-performance-policy.md` is explicit that quality leads.

**Architecture §3:** this is a script-generator change. Validate against
`assert_script_covers_source` on the re-scripted chapter — a prompt change is
exactly the class of edit that can drop or rewrite source text, which is what the
invariant exists to catch.

**Verify:** full suite green, `assert_script_covers_source` passing, zero
attribution diffs on the re-scripted chapter, and a measurable drop in per-chunk
`prompt_eval_duration_ns` within a chapter.

**Docs to update:** `docs/scripting-quality-performance-policy.md` — record the
promotion under its protocol — plus a decision record per ground rule 12 carrying
the before/after prefill measurement and the attribution diff.

---

### F10 — Generate real pronunciation candidates *(order #10)*

**Why:** D1 and B5. 166 terms at `review_required` with
`recommendation_default: ""`, and a generator that returns the term unchanged.

**Files:** `shared/pronunciation.py`, `tests/`

**Root cause, located.** In `_phonetic_recommendations`
([:493](shared/pronunciation.py:493)) three branches end with:

```python
if rec_def.lower() == rec_alt.lower() or not rec_alt:
    rec_alt = raw
```

When the syllable formatter reconstructs a term that is already regular
("Gutbusters", "Dininae", "Gauntlgrym"), `rec_def` *is* the raw term, and the
alternate is then explicitly set to the raw term too. Both candidates equal the
input, so the A/B trial compares a string with itself.

**Changes:**

1. **Allow "no candidate".** Return `""` rather than `raw` when the generator has
   nothing to offer. A caller must be able to distinguish *no recommendation*
   from *recommend it unchanged*.
2. **Treat `rec == term` as unmapped everywhere.** In
   `build_pronunciation_inventory` ([:848](shared/pronunciation.py:848)), do not
   count an identity mapping toward `verified`. `Artemis Entreri` currently has a
   mapping and is still heard as "artemis entrary" 20 times in 35.
3. **Reject identity mappings at write time** in the pronunciations API router so
   one cannot be stored by hand either.
4. **Generate from the observed failure mode, not from spelling.** This is the
   substantive part. The measurement audit already records exactly how each name
   was heard; the three modes and their remedies are documented in
   `PRONUNCIATION_FOLLOWUP_PLAN.md` Part 2:

   | mode | evidence in this book | remedy |
   | --- | --- | --- |
   | Word-splitting | `Entreri` → "and trary" ×9, `Jarlaxle` → "jar laxal", `Tazmikella` → "taz mckellar" | block the merge at the front of the word |
   | Intervocalic flapping | `Catti-brie` → "cadbury" ×48 | move the stress off the flapped /t/ |
   | Boundary loss | `Do'Urden` without its apostrophe → "dohertyn" ×12 | keep the apostrophe |

   Feed `heard_as` from `pronunciation_measurement_audit.json` into candidate
   generation and produce **two genuinely different** candidates per term.
5. **Trial before shipping.** Route candidates through
   `scripts/trial_respelling.py`, **control first**, and surface the winner with
   its score instead of an empty box.

**Tests:**

- `generate_phonetic_recommendations("Gutbusters")` does not return
  `"Gutbusters"` for both keys.
- A term with no useful candidate returns `""`, not the term.
- `build_pronunciation_inventory` does not count an identity mapping as verified.
- Given `heard_as` of `[("and trary", 9), ("entrary", 75)]` for `Entreri`, the
  generated default differs from `"Entreri"` and blocks the leading split.
- **Negative:** `Do'Urden`'s apostrophe survives generation (ground rule 3).

**Verify:** `./venv/Scripts/python.exe -m pytest tests -q`, then regenerate
recommendations for this project and confirm the identity-mapping count drops to
zero.

---

### F11 — Line IDs on pitch-jump warnings *(order #11)*

**Why:** C5. 73 warnings that name a chapter but not the segments.

**Files:** `brain/orchestrator/quality_trends.py`, `tests/test_quality_trends.py`

**Change:** at [:69](brain/orchestrator/quality_trends.py:69), `jumps` is
computed and all but the maximum discarded. Keep the index of the maximum and
emit the two `line_id`s:

```python
jumps = [abs(later - earlier) / centre for earlier, later in zip(voiced_pitches, voiced_pitches[1:])]
peak = max(range(len(jumps)), key=jumps.__getitem__)
largest_jump = round(jumps[peak], 6)
largest_jump_between = (voiced_line_ids[peak], voiced_line_ids[peak + 1])
```

Note `voiced_pitches` is filtered (`value > 0`), so you must build a parallel
list of the line IDs that survived the same filter — indexing back into `ordered`
directly will be off by the number of unvoiced segments.

Add `largest_adjacent_pitch_jump_between` to the returned dict and to the warning
payload at [:190](brain/orchestrator/quality_trends.py:190).

**Also (small, same file):** exclude near-silent segments from the pitch
statistics. They currently drop out only when pitch is exactly 0, so the 5 stale
separator segments from F8 contribute garbage F0. Filter on an RMS floor.

**Tests:** extend `tests/test_quality_trends.py` — the existing fixture
`_rows([90.0, 180.0, 95.0, 200.0, ...])` already produces a jump; assert the
reported pair is the one that actually produced the maximum, including a case
where unvoiced segments sit between the voiced ones.

**Verify:** `./venv/Scripts/python.exe -m pytest tests/test_quality_trends.py -q`,
then regenerate the report and confirm the narrator's 3.39× jump in chapter 6
names two specific lines.

---

### F12 — Recalibrate the inert gates *(order #12)*

**Why:** B6. Two signals that have never fired.

**Files:** `voice/validator/prosody_scorer.py`,
`brain/orchestrator/quality_trends.py`, `brain/config.yaml`

**Scope was cut after the documentation audit.** The original spec also retuned
the identity floor and the trend thresholds. **Do not.** `docs/quality-assurance.md`
139–145 and the code comment at
[quality_trends.py:18–25](brain/orchestrator/quality_trends.py:18) both state
that those thresholds are deliberately quiet so a *change between runs* is the
signal. Retuning them to fire on today's corpus destroys their purpose. Only the
monotone conjunction is in scope.

**File:** `voice/validator/prosody_scorer.py` (plus config)

**The defect:** `is_monotone = pitch_cv < 0.06 and dynamic_range < 4.0`
([:83](voice/validator/prosody_scorer.py:83)). Measured on a random 398-segment
sample, `peak/mean_rms` has median **7.83**, p05 **5.29**, and 1 sample below 4.0.
396 of 7,928 segments satisfy the pitch half. The conjunction fired zero times in
7,928 — it is a constant `False`, which is different from a quiet threshold: a
quiet threshold can still move when the corpus changes, this one cannot.

**[assumption]** Treat low `pitch_cv` as the primary signal and `dynamic_range`
as corroboration rather than a veto — the same shape as
`pitch_requires_identity_corroboration` in the trend policy. Set the
dynamic-range threshold from the corpus (**5.29**, the measured p05) instead of
4.0, and move both to `brain/config.yaml` so tuning needs no code change.

**Tests:**

- A synthetic segment with low pitch variance and normal dynamic range produces a
  monotone warning; one with normal pitch variance does not.
- **Negative:** replay this book's stored per-segment metrics through the new
  rule and assert the warning count is neither 0 nor anywhere near 7,928. A
  detector that fires on everything is as useless as one that fires on nothing.

**Verify:** `./venv/Scripts/python.exe -m pytest tests -q`, then regenerate
`long_form_audio_quality.json` and check `monotone_fraction` is no longer 0.000
across all 202 rows.

**Docs to update:** `docs/quality-assurance.md` (the prosody check description),
`docs/configuration.md` (two new keys), and a decision record per ground rule 12
recording the measured crest-factor distribution that justified the change.

---

### F13 — Rank and collapse the advisory list *(order #13)*

**Why:** D4. 317 unreviewed items is a wall, not a worklist.

**Files:** `brain/orchestrator/review_gate.py`

**Changes** to `ReviewGate.to_dict` ([:72](brain/orchestrator/review_gate.py:72))
and `collect_review_gate`:

1. **Sort pronunciation items by `occurrences` descending.** The field is already
   in `details`. `Breezy` at 1,108 must not sit below `Adbar` at 3. The current
   sort ([:458](brain/orchestrator/review_gate.py:458)) is
   `(not blocking, category, chapter_number, item_id)` — alphabetical within a
   category, which is why the list reads as noise.
2. **Collapse `audio_trend` to one row per voice** with a count and the worst
   value, rather than one row per voice-chapter. 100 → roughly 12.
3. **Group audio rejections by cause.** The 15 name-misses are one fix, not
   fifteen; group on the glossary term in the rejection reason.
4. **Add a `top_actions` array** to the serialised report: the highest-value
   handful across categories, so the dashboard has something short to show.

**[assumption]** "handful" = 10. Enough to cover the distinct causes in this
book, short enough to read.

**Tests:** build a `ReviewGate` from a fixture with 200 mixed items and assert
ordering, collapsing, and that `top_actions` is capped and correctly ranked.

**Verify:** `./venv/Scripts/python.exe -m pytest tests -q`, then regenerate
`pre_master_release.json` for this book: `total_count` drops well below 363 and
the first ten items are the ones that matter.

---

### F14 — Diagnose on flag creation *(order #14, depends on F6)*

**Why:** A8. 28 of 30 flags never triaged.

**Files:** `brain/dashboard/api/mobile.py`

**Change:** after `_enrich_flag_context` in the POST handler, run the F6
diagnosis and store `agent_verdict` and `agent_explanation` on the row. Do the
same on the import path. Enrichment and diagnosis are both local — script JSON,
registry, deterministic rules — so there is no model call and no latency concern.

**Do not** auto-apply anything. The verdict is advisory; repair stays manual.

**[assumption]** Run it inline rather than on a queue. It is pure CPU over one
chapter's script; a queue adds moving parts for no gain. If it ever exceeds
~200 ms, move it to a background task then.

**Tests:** POST a flag → response contains a verdict; POST one on a narration
line → verdict is `INCONCLUSIVE` and no speaker is proposed.

**Verify:** `./venv/Scripts/python.exe -m pytest tests/test_mobile_api.py -q`

---

### F15 — Confirmed flags become attribution regressions *(order #15)*

**Why:** C6. `attribution_audit.json` passed on chapters 11–15 while four
confirmed wrong-speaker flags sat in 13–15. Your ear and the audit disagree and
only the audit votes.

**Files:** `tools/investigate_playback_flags.py`,
`docs/attribution-case-ledger.md`, `tests/test_attribution_audit.py`

**Change:** when a flag is repaired with a confirmed speaker correction, emit a
ledger entry — line ID, chapter, source text, wrong speaker, correct speaker,
the rule that should have caught it — and generate a test case pinning the
correct attribution for that line.

**[assumption]** Generate the ledger entry and a *pending* test, then stop, and
print what a human must complete. Fully automatic test generation would write
tests nobody reviewed, and ground rule 5 exists because rules that ship without a
recorded reason come back. The automation removes the transcription work, not the
judgement.

**Tests:** repairing a flag produces a ledger entry with all required fields; the
generated test fails against the pre-repair script and passes against the
repaired one.

**Verify:** `./venv/Scripts/python.exe -m pytest tests -q`; `docs/attribution-case-ledger.md`
gains an entry per repaired flag.

---

### F16 — Surface unresolved failures at export *(order #16)*

**Scope was cut to almost nothing after the documentation audit.** The original
spec proposed a new blocking rule for 68 silently-shipped failures. There were
32, not 68, and all 32 were reviewed and accepted by a human — the gate worked as
documented. **Do not add a blocking rule.** See the rewritten C3.

**What remains is reporting only.**

**File:** the `export_quality.json` writer

**Change:** add an `accepted_failures` array — line ID, chapter, WER, authored
text, transcript — for every line whose **selected** take has
`passed_hard_gates: False` but carries a resolved disposition. Today that
information exists only in `pipeline_state.db` and is invisible at export time.

**[assumption]** Report, do not block. Blocking would re-raise 32 items a human
has already cleared, and after F4 most of them will not be raised in the first
place. If the count is still high after F4, that is the moment to reconsider —
not now.

**Sequencing:** run this **after F4**. Doing it first would produce a report
dominated by Bruenor's accent.

**Tests:** a fixture with one accepted hard failure → it appears in
`accepted_failures`; one with a passing selected take → it does not; a line whose
*last* attempt failed but whose *selected* take passed → it does **not** appear
(this is the ground-rule-9 trap, and the test is what stops someone reintroducing
my original error).

**Verify:** `./venv/Scripts/python.exe -m pytest tests -q`, then regenerate
`export_quality.json` for this book and confirm the array holds 32 entries before
F4 and far fewer after.

**Docs to update:** `docs/quality-assurance.md` — describe the new array
alongside the existing export-quality fields.

---

### F17 — Reject identity mappings in the lexicon *(order #17)*

Folded into **F10** changes 1–3 — same code path, same tests. Listed separately
in the order-of-work table only because it is independently shippable: if F10
is deferred, do changes 1–3 alone.

---

### F18 — Scope the global lexicon per book *(order #18)*

**Why:** D5. `Kvothe`, `Elodin`, `Denna` from a different book appear in this
book's audit as four permanently `insufficient` verdicts with 0 samples.

**Files:** `shared/pronunciation.py`, `brain/pronunciation_dict.json`

**[assumption]** Do not restructure the global dictionary — filter at the
measurement boundary instead. It is the smaller change and it fixes the visible
symptom: in the measurement audit, drop any term with **0 occurrences in this
book's script** before reporting. A global entry still applies if the term
actually appears.

**Tests:** a term present in the global dict but absent from the project script
does not appear in that project's measurement audit; one that does appear still
does.

**Verify:** `./venv/Scripts/python.exe -m pytest tests -q`, then re-measure: 111
terms drops by the four Name-of-the-Wind entries.

---

### F19 — `voice_crash.log` encoding *(order #19)*

**Why:** E2. A traceback containing a non-ASCII character raises
`UnicodeEncodeError` inside the crash handler on Windows.

**File:** [voice/tts_server/main.py:483](voice/tts_server/main.py:483)

**Change:** add `encoding="utf-8", errors="replace"`. `errors="replace"` matters
as much as the encoding: a crash handler must never be the thing that raises.

**Also fix the path while you are here.** `"voice_crash.log"` is a bare
working-directory-relative literal, which `docs/architecture.md` §5 forbids — and
for a concrete reason recorded there: a relative `voice/config.yaml` read once
returned `{}` when launched from elsewhere, silently dropping TTS settings out of
the generation fingerprint. Resolve it through `shared/paths.py` so the crash log
lands in the same place regardless of the working directory.

Same treatment for the two server log handles in
`brain/orchestrator/pipeline.py` ([:441](brain/orchestrator/pipeline.py:441),
[:616](brain/orchestrator/pipeline.py:616)) and the lock file in
`shared/single_instance.py` ([:27](shared/single_instance.py:27)).

**Test:** write a traceback containing `—` and `é` to the crash log and assert no
exception and a readable file.

**Verify:** `./venv/Scripts/python.exe -m pytest tests -q`

---

### F20 — Syllable-aware expected duration *(order #20)*

**Why:** E3. `"Bedorijay fumed."` is 2 words → 0.8 s expected against 2.08 s
actual; the 1.25 s tolerance floor is exceeded by 0.03 s and it fails.

**File:** [voice/validator/audio_analyzer.py:226](voice/validator/audio_analyzer.py:226)

**Change:** `_expected_duration` counts words and divides by
`AVERAGE_WORDS_PER_MINUTE`. Estimate syllables instead — vowel-group counting is
adequate and needs no dependency — and scale by an average syllable rate.
Fall back to the word count when syllable estimation returns 0.

**[assumption]** Keep the existing `max(1.25, expected * tolerance)` floor
unchanged. It exists for onset/release cost on short clips and is doing its job;
this item fixes the estimate, not the tolerance.

**Tests:**

- `"Menzoberranzan"` (1 word, 6 syllables) expects materially more than
  `1 / WPM * 60`.
- `"Bedorijay fumed."` at 2.08 s actual now passes.
- **Negative:** a genuinely swallowed line — long text, very short audio — still
  fails. The CPS guard at
  [audio_analyzer.py:95](voice/validator/audio_analyzer.py:95) should still catch
  it; assert that it does.

**Verify:** `./venv/Scripts/python.exe -m pytest tests -q`, then re-validate a
chapter and confirm duration failures drop without WER failures rising.

---

## Part G — Documentation audit

Run after Part F was drafted, to check the plan against what the repository
already documents. It changed four specs and corrected one headline number, so it
is recorded rather than folded away.

### G1 — Docs that contradict the plan (the plan was wrong)

| doc | what it says | effect |
| --- | --- | --- |
| `quality-assurance.md:321` | "an older **selected** retry remains the reported winner while all attempts still count toward retry metrics" | B2's "68 shipped failures" was a misread of `quality_logs`. Correct figure: **32**, all human-accepted. C3 and F16 rewritten. |
| `quality-assurance.md:139–145` | trend thresholds are "deliberately quiet on the current corpus: the useful signal is a *change* in spread between runs" | C4/F12's identity-floor recalibration would have broken a documented design. Scope cut to the monotone detector. |
| `architecture.md` §Validation policy | "Hard failures remain blocking." | Confirmed working. `blocking_count: 0` is correct, not a leak. |

### G2 — Docs that contradict the code (the docs are wrong)

Both are in `docs/architecture.md` §5 "Verification Protocol" and should be fixed
regardless of whether anything else in this plan is done:

1. **"`ruff format` is intentionally not enforced yet; adopt it in a dedicated
   commit."** `.github/workflows/ci.yml` has enforced `ruff format --check --diff .`
   since 2026-09-04, with its own comment saying so. The doc predates the commit
   that enforced it.
2. **Test invocation.** The doc gives only
   `python -m unittest discover -s tests -p "test_*.py"`. CI, `pyproject.toml` and
   every workflow in practice use `pytest` (1,068 passed, 2 skipped, 56 subtests).
   `unittest discover` still works and is what `verify_pipeline.py --tier static`
   runs, so both are valid — the doc should say so instead of implying pytest is
   not used.

### G3 — Obligations the specs originally missed

Now cited inline in the specs that trip them, and in ground rules 11–12:

| requirement | source | specs affected |
| --- | --- | --- |
| `assert_script_covers_source` on any script-generator change | architecture §3 | **F8**, **F9** |
| Grouping-policy change must invalidate the script fingerprint | architecture §3 | **F8** |
| `?v=` asset revision and `FRONTEND_BUILD` must move together | architecture §1 | **F2** |
| New artifacts registered in `reset_pipeline_stage` | architecture §2 | any spec adding one |
| Paths via `shared/paths.py`, never bare relative literals | architecture §5 | **F19** (`"voice_crash.log"` is one) |
| Decision record with a `**Status:**` line; supersede by strike-through, never rewrite | `decisions/README.md` | **F4**, **F9**, **F12** |
| `verify_pipeline.py --tier static` also runs `compileall`, `node --check`, markdown links | quality-assurance.md | all specs |

### G4 — Docs these changes will make stale

Update as part of the corresponding spec, not afterwards:

| doc | made stale by |
| --- | --- |
| `quality-assurance.md` | F4 (WER hard-failure list), F12 (prosody check), F16 (export-quality fields) |
| `configuration.md` | F8 (pause/silence key), F12 (two prosody keys) |
| `architecture.md` | F8 (pause ladder, stage 6), plus the G2 fixes |
| `dashboard-guide.md` | F2 (canonical flag status set), F13 (ranked advisory list) |
| `api-reference.md`, `crazy-voice-companion.md` | F2 (`client_flag_id`, response envelope), F7 (`position_origin`) |
| `pronunciation-evidence.md` | F10 (candidate generation from failure mode) |
| `attribution-case-ledger.md` | F6, F15 (new or reused rules, per ground rule 5) |
| `scripting-quality-performance-policy.md` | F9 (promotion under its protocol) |

**Verify the whole set:**
`./venv/Scripts/python.exe scripts/verify_pipeline.py --tier static`

---

## Appendix — how each number was produced

All commands run from the repository root with the project venv.

**Flags.** `brain/projects/the-finest-edge-of-twilight-book/playback_flags.json`
— 30 entries; grouped by `line_id`, `position_ms`, `status`, `source`, and
`agent_verdict`. Duplicate pairs identified by equal `(chapter_number,
position_ms, line_id)` with `flag_id` timestamps 1–2 s apart.

**Chapter durations.** Read from the WAV headers of
`workspace/the-finest-edge-of-twilight-book/chapters/chapter_NNN.wav`. Cumulative
book timeline used to test the two out-of-range positions.

**`ch13_0424` is the last line.** `script/chapter_013.json` has 385 lines;
`ch13_0424` is index 384.

**Final per-line quality.** `brain/projects/pipeline_state.db`, table
`quality_logs`, 7,928 rows for this project. For each `line_id`, the record with
the highest `(attempt, id)` is the final one — 7,453 lines. Statuses: 7,094
`pass`, 291 `accepted_with_warning`, 68 `fail`.

**Loudness.** `export_quality.json`.

**Identity / prosody / pitch jumps.** `long_form_audio_quality.json` —
202 `chapter_voice_metrics` rows, 100 warnings.

**Monotone.** `monotone_warning` extracted from the `details` JSON of all 7,928
`quality_logs` rows: `True` zero times, `pitch_cv < 0.06` in 396. Crest factor
(`peak / mean_rms`) measured directly on a seeded random sample of 400 segment
WAVs: median 7.83, p05 5.29, 1 of 398 below 4.0.

**Pronunciation.** `pronunciation_measurement_audit.json` (111 terms, 7,391
transcribed lines, evidence current as of 2026-09-17), `pronunciation_inventory.json`
(15 verified / 166 unresolved / 181 candidates), `pronunciation_recommendations.json`,
and the 166 `category: "pronunciation"` items in `pre_master_release.json` with
their `occurrences`. Bad takes per chapter derived from each term's
`outlier_lines`, bucketed by the `chNN_` prefix and divided by measured chapter
duration.

**Scene breaks.** Lines across all 64 files in `script/` for which
`shared.constants.is_non_spoken_separator` returns true: 62. Each one's segment
WAV read directly for peak amplitude — 57 are digital silence (peak 0, the
current synthesis path), 5 carry engine output (peak 8–76). The predicate was
tested directly against `—`, `---`, `***`, `* * *`, `?` and `...`.

**Two corrections recorded in place.** The first pass of this document claimed
62 wasted TTS draws (D2) and no dialect or short-line handling in the validator
(C1). Both were wrong: `is_non_spoken_separator` plus
`PAUSE_MARKER_SILENCE_SECONDS` already handle separators, and
`validation_loop.py:1178–1226` already implements `spelling_variant_match`,
`glossary_adjusted_wer`, `glossary_phonetic_match` and a `word_count <= 3` path.
Both sections were rewritten against the code rather than deleted, because the
narrowed items are still real and the reasoning is worth keeping.

**Prefill.** Every object in `performance_metrics.jsonl` containing both
`prompt_eval_duration_ns` and `eval_duration_ns` — 1,497 records over 292 lines.
Full-cache-hit count is records with `prompt_eval_count == 0`.

**Tests.** `./venv/Scripts/python.exe -m pytest tests -q` — 1,068 passed,
2 skipped, 56 subtests, 31.11 s.

**Auto-diagnose output.** `python tools/investigate_playback_flags.py
the-finest-edge-of-twilight-book --auto-diagnose` (read-only; it prints and
exits without touching the database).

---

## Execution Record & Verification Audit (2026-09-19)

### Final Status Breakdown

| # | Spec | Description | Status | Notes |
|---|---|---|---|---|
| 1 | F1 | Apply `Regheadmen` + `Kweesta` | **Shipped** | Repaired 9 outlier lines, remastered touched chapters, 8 deliveries re-exported. Re-measurement audit noted with stale timestamp warning (pre-existing 09-18 collision). |
| 2 | F2 | Kill duplicate flags & status coercion | **Shipped** | Added `client_flag_id`, `JobQueue.find_duplicate_playback_flag` (unresolved-only, 2,000ms window), coerced non-standard statuses to `open`, frontend allow-list updated. Returns `result: "existing"` on dedupe hit. |
| 3 | F3 | Re-enrich on import & preserve `created_at` | **Shipped** | Unconditional local file-backed context enrichment and preserved ISO-8601 timestamps. |
| 4 | F4 | Dialect normalisation & short-line WER | **Shipped** | `DIALECT_NORMALISATIONS` (`ye`, `yer`, `telled`, `d'ye`, `ain't`, `nay`, suffix `-in' -> -ing`), `INTERJECTION_HOMOPHONES` (`aye`, `bah`, `nay`, `hmph`), glossary-only miss retry halt. Corrected in code review: dropped `aye->yes` (preventing authored "Yes." matching transcript "I") and `o->of` (preventing "o'clock" -> "of clock"). |
| 5 | F5 | Make `--repair` actually repair | **Shipped** | Replaced dead file paths with `VoiceClient` generation/validation, 4-store `replace_segment`, and default `--dry-run` safety. |
| 6 | F6 | Attribution-backed `--auto-diagnose` | **Shipped** | Replaced regexes with pipeline attribution machinery (`tag_speaker_evidence`, `action_beat_attributions`, `_refutations`, reaction window ranking, non-dialogue abstention). |
| 7 | F7 | Position sanity: match distance & origin | **Shipped** | Added `match_distance_ms`, `match_confidence` (`exact`, `near`, `out_of_range`), book-absolute fallback timeline derivation, and explicit `position_origin`. |
| 8 | F8 | Clean stale separator segments | **Shipped** | Replaced 5 non-zero separator segments with clean silence, forced narrator/non_spoken_quote metadata in scripting, and moved `pause_marker_silence_seconds` to config. |
| 9 | F9 | Reorder system prompt for prefix cache | **Held (Declined)** | Screened on Ch 14/18 and benchmarked. Inter-chunk cache speedup not evidenced in cold runs (`speed_gate_pass: false`), while deterministic attribution drift was observed on recurring fragments. Reverted per scripting quality policy. |
| 10 | F10 | Pronunciation candidate generation from failure mode | **Shipped** | Targets observed `heard_as` failure patterns (word-splitting, intervocalic flapping), returns `""` when no candidate exists. |
| 11 | F11 | Line IDs on pitch-jump warnings | **Shipped** | Tracks maximum adjacent pitch jump over filtered voiced segments and reports `largest_adjacent_pitch_jump_between: [line_a, line_b]`. |
| 12 | F12 | Recalibrate monotone detector | **Shipped** | Rewrote monotone conjunction to primary `pitch_cv < 0.06` with `dynamic_range < 5.29` (measured p05) corroboration, configurable in `brain/config.yaml` and fallback aligned in `validation_loop.py`. |
| 13 | F13 | Rank and collapse advisory list | **Shipped** | Review gate sorts pronunciations by script occurrences descending, collapses audio trends to 1 row per voice, groups rejections by glossary term, and emits `top_actions` (max 10). |
| 14 | F14 | Diagnose on flag creation | **Shipped** | Inline attribution diagnosis on flag creation and import, setting `agent_verdict` and `agent_explanation`. |
| 15 | F15 | Confirmed flags -> attribution regressions | **Shipped** | `tools/investigate_playback_flags.py` records ledger entry in `docs/attribution-case-ledger.md` and generates test in `tests/test_attribution_audit.py`. |
| 16 | F16 | Surface accepted failures in `export_quality.json` | **Shipped** | Added `collect_accepted_failures` to export packaging, recording lines with human-cleared hard gate failures in `export_quality.json`. |
| 17 | F17 | Reject identity mappings in lexicon | **Shipped** | Rejects `"Term": "Term"` at API write time and treats them as unmapped in inventory. |
| 18 | F18 | Scope global lexicon per book in audit | **Shipped** | Filters `_entries_to_measure` to terms appearing in target book script, dropping false `insufficient` warnings. |
| 19 | F19 | `voice_crash.log` encoding & paths | **Shipped** | Replaced relative paths with `shared_paths.repo_path` and added `encoding="utf-8", errors="replace"`. |
| 20 | F20 | Syllable-aware expected duration | **Shipped** | Added vowel-group syllable estimation `_estimate_syllables_in_word` scaled by 1.5x WPM (~225 SPM) while preserving floor and CPS anomaly caps. |
| 21 | F21 | Documentation audit (Part G) | **Shipped** | Corrected stale `ruff format` and test runner notes in `docs/architecture.md`, updated `docs/quality-assurance.md`, `docs/configuration.md`, `docs/dashboard-guide.md`, `docs/api-reference.md`, `docs/crazy-voice-companion.md`, and `docs/pronunciation-evidence.md`. |

### Discovered Follow-Up: `repair-backup/` Namespace Collision
- **Issue**: `scripts/restore_narrator_takes.py` restores from `segments/repair-backup/`, which is the shared backup directory used by `scripts/repair_outlier_lines.py`. The 2026-09-18 narrator voice restoration accidentally restored pre-pronunciation-repair takes on narrator lines (`Entreri`, `Do'Urden`, `Bedorijay`, `Drizzt`).
- **Recommendation**: Separate backup namespaces by operation (e.g. `segments/repair-backup/pronunciation/` vs `segments/repair-backup/narrator_restore/`), and avoid shared directory clobbering.

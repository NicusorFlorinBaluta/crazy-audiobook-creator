# Pronunciation follow-up plan

**Written:** 2026-09-16 · **Updated:** 2026-09-17 after an independent review · **Completed:** 2026-09-17
**Status:** **Finished** — all defects resolved, commits landed on `dev`, Groups A & B repaired, Group C trialled, all deliveries rebuilt and verified ok.

The original plan was executed by a second agent on 2026-09-16/17. Most of it
landed and works. A review on 2026-09-17 verified the claims against the
artifacts rather than the summary, confirmed the bulk of them, and found two
real defects plus several smaller gaps. This file now records **what is done**,
**what broke**, and **what is left**.

Background and evidence for every rule here:
[docs/decisions/2026-09-16-a-majority-is-not-a-verdict.md](docs/decisions/2026-09-16-a-majority-is-not-a-verdict.md).

---

# Part 0 — current state

- **Nothing is committed.** 23 files unstaged on `dev` (16 modified, 7 new).
  This is the most urgent practical risk: the work is substantial and
  unprotected. Do not commit until the regressions below are fixed, so history
  never records a state where `Drizzt` went backwards.
- 1,056 tests pass; 2 skipped.
- 18 ruff errors in the changed files (14 unused imports), 16 auto-fixable.
- All 8 M4B deliveries report `ok` to `reexport_deliveries.py --stale` — but
  they were rebuilt from audio that contains the regression below, so they will
  need rebuilding again once it is fixed.
- Last commit on `dev` is `32629bd`.

## Verified as done

Checked against the audit JSON, the filesystem and the code, not the summary.

| item | state |
| --- | --- |
| Batch repair across `unstable` terms | done — outliers 444 → 314 |
| Chapters re-mastered, 8 deliveries re-exported | done, all `ok` |
| `spoken_correctly` 49 → 59, `unstable` 47 → 31 | confirmed |
| **Item 1** selective best-of-N | done, and the comparator is sound |
| **Item 3** `shared/segment_repair.py` four-store helper | done **and used** by the repair script |
| **Item 4** content-hashed asset revisions, `Clear-Site-Data` dropped | done |
| **Item 6** staleness surfaced (`shared/staleness.py`, dashboard status) | done |
| **Item 7** entry-point contract audit (`speed`, `language`, `voice_fx`) | done |
| Emberdark left alone | **confirmed** — zero modified segments, masters or M4Bs, no `repair-backup` |

The best-of-N comparator in `_is_pronunciation_candidate_better` deserves a
note: it refuses a candidate that fails hard gates when the incumbent passes,
refuses one that is unaccepted when the incumbent is accepted, counts **every**
hard term on the line, and only then falls back to `_is_better`. That is the
right shape, and it is the model for fixing defect 1.

---

# Part 1 — defects found in review (fix these first)

## 1. The batch repair damaged terms it was not targeting *(blocking)*

Outliers fell 444 → 314 overall, but three terms went backwards:

| term | before | after |
| --- | --- | --- |
| `Artemis Entreri` | 8 | **18** |
| **`Drizzt`** | **3** | **7** |
| `Zaknafein` | 14 | 15 |

Every newly-broken `Drizzt` line — `ch09_0129`, `ch13_0096`, `ch27_0148`,
`ch27_0154` — also contains `Do'Urden`, `Entreri` or `Artemis Entreri`, all
repaired in the same run. `Artemis Entreri` regressed because it *contains*
`Entreri`, so repairing one redraws the other's lines.

**Root cause:** `scripts/repair_outlier_lines.py` computes one `target_key`
(line ~139) and validates only that term (lines ~164, ~186). A redraw that
fixes the target while breaking another name on the same line is accepted
silently.

**Fix:** before keeping a take, evaluate every glossary term present in the
line and reject the take if any term that was correct becomes wrong. Reuse the
term-counting logic from `_is_pronunciation_candidate_better` rather than
writing a second one — a shared helper for "is this take better for all the
names on this line" is the right end state, since the generator and the repair
now need the same judgement.

**Then:** re-run the repair for the regressed terms, re-master the chapters it
names, and **re-export the affected deliveries** — the regression is currently
inside the shipped M4Bs. `Drizzt` should return to 3 outliers or fewer.

## 2. `language=language` dropped from the primary validation call *(blocking)*

`voice/validator/validation_loop.py:511` no longer passes `language` to
`_validate_segment`, while the take-2 call at line ~539 still does. Two
consequences:

- every line is now transcribed with Whisper language auto-detection, reversing
  part of the 2026-09-07 "Whisper STT language normalization" work;
- the two best-of-N candidates are judged under different settings, so the A/B
  is not a fair comparison.

It reads as an accidental deletion when the new block was inserted. No test
caught it — add one that asserts both candidate paths receive identical
transcription settings.

## 3. Smaller gaps

- **Item 2 is partial.** `generate_phonetic_recommendations` still returns the
  identity in `default` for every name tried (`Drizzt`, `Catti-brie`,
  `Guenhwyvar`, `Entreri`, `Gauntlgrym`, `Zaknafein`). That is *safe* and
  deliberate — an active `default` auto-applies with no human gate, which the
  09-12 rule forbids — so candidates correctly live in `alternate` only. But
  the failure modes are only half encoded: `Entreri → Ehntreri` genuinely
  targets the "and trary" absorption, while `Catti-brie → Cattibrie` ignores
  the stress/flapping insight that measured **9/14**. Teach it the flapping
  rule, or document that `alternate` is a hint rather than a derived candidate.
- **Item 5 (G2P) was benchmarked, not adopted** (`scripts/eval_g2p.py`,
  `phonetic_key` reported at 94.7%). That is the correct outcome — the plan
  asked for comparison before replacement. Leave it unless the number moves.
- **Lint:** `ruff check --fix` on the changed Python files clears 16 of 18.
- **"142 lines repaired"** in the previous summary is a count of repair
  *actions*, not net improvement. The net is 130, and that figure already
  absorbs the regressions above. Report net next time.

---

# Part 2 — the remaining names

Numbers are from the 2026-09-17 audit, **after** the batch repair. Re-run
`python scripts/measure_pronunciations.py the-finest-edge-of-twilight-book`
before acting; fixing defect 1 will move them again.

### Group A — 26 terms, 51 bad lines of 1561 (stability ≥ 0.80)

`Drizzt` (7/157 — *regressed, see defect 1*), `Jarlaxle` (4/399), `Savahn`
(4/103), `Soliardis` (4/42), `Allefaero` (3/176), `D'aerthe` (2/19), `Sylfae`
(2/51), `Perrywinkle Shin` (2/22), and others.

Redraw them — but only once defect 1 is fixed, or this run will damage other
terms exactly as the last one did.

### Group B — 10 terms, 70 bad lines of 257 (stability 0.60–0.80)

`Entreri` (30/107), `Do'Urden` (10/45), `Bedorijay` (9/33), `Pwent` (6/18),
`Gromph` (4/18), `Ghaliver Longstocking` (3/11), and others.

Repair, re-measure, then treat the remainder as group C.

### Group C — 11 terms, 193 bad lines of 382 (stability < 0.60)

`Catti-brie` (86/179), `Avelyere` (26/48), `Gauntlgrym` (19/46),
`Artemis Entreri` (18/35 — *regressed*), `Zaknafein` (15/27), `Ilnezhara`
(8/14), `Reghedmen` (7/8), and others.

**Redrawing will not help these.** They need respellings, trialled with
`scripts/trial_respelling.py`, **control first**.

#### Pick candidates from the failure mode

| mode | evidence | remedy |
| --- | --- | --- |
| **Intervocalic flapping** — English flaps /t/ between a stressed and an unstressed vowel | `Catti-brie` → "cadbury" ×48 | Move the stress off it. `Katteebree` scored **9/14** against the shipped entry's 1/14. |
| **Word-splitting** — the engine inserts a boundary | `Entreri` → "and trary" ×28, `Jarlaxle` → "jar laxal", `Tazmikella` → "taz mckellar" | Block the merge at the front of the word; `Ehntreri` is the generator's untested attempt at this. |
| **Boundary loss** — removing punctuation lets syllables merge | `Do'Urden` without its apostrophe → "dohertyn" ×12 | Keep the apostrophe. |

**`Catti-brie` is settled: leave it alone.** `Katteebree` works but buys the
consonant by shifting stress to "ka-TEE-bree" where the name is "KAT-ee-bree",
and applying it regenerates 179 lines of a delivered book. Declined by the
owner on 2026-09-16 with that evidence in hand.

**Do not "fix" the apostrophe asymmetry in `normalize_phonetic_text`.** It joins
hyphens out but leaves apostrophes, which looks like an oversight and is not:
stripping the apostrophe from `Do'Urden` takes it from 6/14 to **0/14**.

---

# Order of work
 
1. **Fix defect 1 (cross-term guard in the repair).** [COMPLETED]
   - Landed multi-term check in `shared/pronunciation_evidence.py` and `scripts/repair_outlier_lines.py`.
2. **Fix defect 2 (restore `language=language`), with a test.** [COMPLETED]
   - Restored parameter in `voice/validator/validation_loop.py` with unit tests.
3. **`ruff check --fix` on the changed files.** [COMPLETED]
   - All lint checks passed across all touched modules.
4. **Commit** [COMPLETED]
   - Landed in commits `a925f5e` and `cf3aac6` on `dev`.
5. **Re-run the repair for the regressed terms; re-master and re-export the deliveries.** [COMPLETED]
   - `Drizzt` repaired (outliers 7 -> 2), chapters 9, 24, 27 remastered, deliveries re-exported.
6. **Then group A, then group B, then group C trials.** [COMPLETED]
   - Group A (stability >= 0.80): 15 lines repaired, chapters remastered, deliveries rebuilt.
   - Group B (0.60 <= stability < 0.80): 23 lines repaired (`Do'Urden` outliers 11 -> 6, `Entreri` 30 -> 18, `Bedorijay` 9 -> 5, `Herzgo` 2 -> 1), chapters remastered, deliveries rebuilt.
   - Group C (< 0.60): trialled respellings (`Regheadmen` 70% vs 10%, `Kweesta` 100% vs 0%, `Bidderdoo` 0%, `Zhindia` 0%).
   - All 8 M4B deliveries verified 100% `ok` and up to date.

---

# Emberdark — metrics only

`isles-of-the-emberdark-a-cosmere-novel-secret-projects-book-5` is **finished
and delivered**. Per the owner (2026-09-16), further work on it is for **test
and metrics purposes only, never for actual usage**: do not repair its
segments, re-master it, re-export its deliveries, or apply lexicon changes.

The 2026-09-16/17 run respected this — verified: no modified segments, no
re-mastered chapters, no rebuilt M4Bs, no `repair-backup` directory.

It stays useful as a measurement corpus with one caveat already encoded in the
tooling: its lexicon changed 2026-09-12 while its newest audio is 2026-09-03,
so **every verdict on an active entry there is unevaluated** until a
regeneration that must not happen. Treat its `unrespelled` verdicts as evidence
and its active-entry verdicts as meaningless.

---

# Ground rules that earned their place

- **`unstable` never ships a respelling.** Only `mispronounced` does.
- **Measure before shipping a respelling, with a control arm.** Two of the
  three respellings trialled on 2026-09-16 would have made things worse.
- **A repair must not break a name it was not aiming at.** New, from defect 1.
- **Never add `initial_prompt` to the Whisper call.** It would improve
  transcription of rare names, which is exactly why it is forbidden: this
  validator is an unbiased phonetic reporter, and priming it erases the signal.
  A test asserts its absence.
- **A repair is not done until the delivery is rebuilt.**
- **Report net improvement, not the number of actions taken.**

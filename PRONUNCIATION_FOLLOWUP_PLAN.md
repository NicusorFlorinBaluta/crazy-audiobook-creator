# Pronunciation follow-up plan

**Written:** 2026-09-16 · **Status:** Not started · **For:** a later agent/session

Everything described here is *pending*. The work already finished is in commits
`ff8518d`, `e81e887`, `2f29378` on `dev` and in
[docs/decisions/2026-09-16-a-majority-is-not-a-verdict.md](docs/decisions/2026-09-16-a-majority-is-not-a-verdict.md).
Read that decision record first — it carries the evidence behind every claim
below.

## State of the tree

- Working tree clean, 1043 tests passing, all three commits pushed to `dev`.
- `the-finest-edge-of-twilight-book`: `Drizzt` repaired (141/157 → 154/157),
  chapters 2/4/7/8/9/18/23/27/29 re-mastered, all eight M4B deliveries
  re-exported and verified current.
- A batch repair over the remaining terms was started and **stopped before it
  replaced anything** — it was still on its first term and the backup count
  never moved past the 13 from the `Drizzt` work. No partial state to clean up.
- The voice server (port 8100) is stopped. The dashboard does not start it;
  `python -m voice.tts_server.main` from the repo root does.

## A correction to carry forward

Commit `8e8a45f` says a stale `?v=` on `app.js` meant "a browser would have
served the old copy alongside new CSS". **That is wrong.** `serve_dashboard()`
rewrites every `?v=` with `int(time.time())` on every request, so the values
committed in `index.html` never reach a browser. The real defect is the
opposite and is item 4 below. The bumped revisions are harmless but cosmetic;
the test that polices them is checking a source convention with no runtime
meaning, and its docstring rationale should be corrected when item 4 lands.

---

# Part 1 — the remaining names in `the-finest-edge-of-twilight-book`

Re-run `python scripts/measure_pronunciations.py the-finest-edge-of-twilight-book`
first; the numbers below are from 2026-09-16 and the repair changes them.

Terms split into three groups by `sound_stability`, and each wants a different
treatment. **The group decides the tool** — using the wrong one wastes hours.

### Group A — 27 terms, 80 bad lines of 1587 (stability ≥ 0.80)

`Jarlaxle` (24/399), `Allefaero` (9/176), `Grandda` (5/43), `Savahn` (4/103),
`Sylfae` (4/51), `Dorcrae` (4/21), `Drizzt` (3/157), `Janquay` (2/13),
`Brevindon` (2/31), `Sollars` (2/27), `Perrywinkle Shin` (2/22), and others.

These behave exactly like `Drizzt` did: the engine says the name correctly most
of the time and loses a per-draw coin flip on the rest. **Redraw them.**

```
python scripts/repair_outlier_lines.py the-finest-edge-of-twilight-book --attempts 4 --apply
```

With no `--term` it picks up every `unstable` term. Expect roughly 80% success
(the `Drizzt` run fixed 13 of 16). Budget 1–2 hours of GPU. No lexicon change,
no listening decision.

**Then, without fail:**

```
python scripts/remaster_chapters.py the-finest-edge-of-twilight-book <chapters it names>
python scripts/reexport_deliveries.py the-finest-edge-of-twilight-book --stale
python scripts/reexport_deliveries.py the-finest-edge-of-twilight-book --batch A-B ... --full
```

Skipping the re-export leaves the repair in the workspace and absent from every
M4B, with every intermediate check passing. That happened once already.

### Group B — 14 terms, 62 bad lines of 217 (stability 0.60–0.80)

`Soliardis` (14/42), `Artemis Entreri` (8/35), `Pwent` (6/18), `Cazzcalci`
(6/15), `Gromph` (5/18), `Tazmikella` (4/19), `Dininae` (4/18),
`Ghaliver Longstocking` (3/11), and others.

Repair first (they are included in the command above), re-measure, then trial
whatever is left as group C.

### Group C — 18 terms, 302 bad lines of 606 (stability < 0.60)

`Catti-brie` (86/179), `Entreri` (47/107), `Avelyere` (30/48), `Do'Urden`
(23/45), `Gauntlgrym` (20/46), `Bedorijay` (16/33), `Zaknafein` (14/27),
`Bregan` (10/19), `D'aerthe` (9/19), `Lolth` (8/19), `Ilnezhara` (8/14),
`Reghedmen` (7/8), and others.

**Redrawing will not help these** — around half of every term's lines are
wrong, so a fresh draw is a coin flip. They need respellings, trialled:

```
python scripts/trial_respelling.py the-finest-edge-of-twilight-book <Term> <control> <candidate>...
```

**The control comes first and is what currently ships.** On `Drizzt` the control
beat both candidates and the right answer was to ship nothing; on `Do'Urden`
both candidates scored 0/14 against the control's 6/14.

#### Choose candidates from the failure mode, not by brainstorming

This is the part that worked. Three mechanisms explain most of group C, and
each has a different remedy:

| mode | evidence | remedy |
| --- | --- | --- |
| **Intervocalic flapping** — English flaps /t/ between a stressed and an unstressed vowel | `Catti-brie` → "cadbury" ×48, "cadibri" ×50 | Move the stress off it. `Katteebree` scored **9/14** against the current entry's 1/14. |
| **Word-splitting** — the engine inserts a boundary that is not there | `Entreri` → "and trary" ×28 (the leading vowel is swallowed into the previous word), `Jarlaxle` → "jar laxal"/"jarl axel", `Tazmikella` → "taz mckellar" | Untested. Try forms that cannot be parsed as an existing word boundary. |
| **Boundary loss** — removing punctuation lets syllables merge | `Do'Urden` → "dohertyn" ×12 once the apostrophe is stripped | Keep the apostrophe. See the warning below. |

**`Catti-brie` is settled: leave it alone.** `Katteebree` works but buys the
consonant by shifting stress to "ka-TEE-bree" where the name is "KAT-ee-bree",
and applying it regenerates 179 lines of a delivered book. The owner declined
it on 2026-09-16 with that evidence in hand. Do not re-open it without being
asked.

**Do not "fix" the apostrophe asymmetry in `normalize_phonetic_text`.** It joins
hyphens out but leaves apostrophes, which looks like an oversight and is not:
stripping the apostrophe from `Do'Urden` takes it from 6/14 to **0/14**. The
apostrophe holds a syllable boundary nothing else supplies.

---

# Part 2 — improvements

Ordered by value. Items 1 and 5 are the substantial ones.

## 1. Selective best-of-N at generation *(highest value)*

Failures are per-draw at roughly one line in ten, and **1,366 of this book's
7,453 lines (18.3%) contain a term currently measured `unstable` or
`mispronounced`**. Generating *two* takes for only those lines and keeping the
one whose name lands in the right sound group should roughly square the residual
error rate, for about +18% generation time.

- Where: `voice/validator/validation_loop.py`, in `process_chapter`'s synthesis
  path, gated on the line containing a term from `validation_terms`.
- Reuse `same_spoken_form` (already in `shared/pronunciation_evidence.py`) to
  pick the better take, and `_is_better` for the tie-break on quality.
- Gate it behind config (`voice/config.yaml`) and default it off until measured.
- **Cost control:** only for lines containing a hard name, and only 2 takes.
  Do not apply it book-wide.
- Prevents the error rather than detecting and repairing it, which makes the
  whole repair workflow in Part 1 mostly unnecessary for future books.

## 2. Replace the dead recommendation generator

`generate_phonetic_recommendations` in `shared/pronunciation.py` returns the
term unchanged for nearly every name — **180 of 184 recommendations in this book
are no-ops**. It is an identity function wearing a feature's clothes, and it is
why the lexicon looks populated while doing nothing. The hyphen-joining fix that
made hyphens correct also removed the only thing the heuristic contributed.

Either delete it and let the LLM path own proposals, or rebuild it around the
three failure modes in Part 1 so it emits candidates worth feeding to
`trial_respelling.py`. Deleting is defensible; leaving it is not.

## 3. One `replace_segment()` helper

Replacing a segment means keeping **four** stores in step, and getting it
partially right is invisible until much later:

1. the segment wav;
2. `manifests/chapter_NNN.segments.json` — segment `output_hash` **and**
   manifest `manifest_hash` (the reconciler regenerates the whole chapter if
   these drift; `dependency_hash` excludes `output_hash` deliberately);
3. `voice_cache.db` → `generation_fingerprints.output_hash`;
4. `quality_logs` — a row with the new transcript, or measurement keeps reading
   the take that was just replaced and the line never leaves the outlier list.

`scripts/repair_outlier_lines.py` implements all four and got it wrong twice
during development. Extract one helper (suggest `shared/segment_repair.py`) and
have any future tool use it. Include the `segments/repair-backup/` copy.

## 4. Content-hash the dashboard asset revisions

See the correction at the top. `serve_dashboard()` currently stamps
`int(time.time())` on every asset at every request, so **the browser re-downloads
all JavaScript and CSS on every dashboard load** and the cache never holds
anything. It also sends `Clear-Site-Data: "cache"`, which empties the origin's
cache on each load — making stale assets impossible by making caching
impossible.

Replace with a content hash per asset (`sha256(path.read_bytes())[:12]`) and
drop `Clear-Site-Data`. Keep `no-store` on the HTML itself. This also retires
the manual `?v=` bump that was missed in `651b854`.

A working patch is committed at [docs/asset-revision.patch](docs/asset-revision.patch)
— written and tested on 2026-09-16, then reverted out of the tree so the handoff
would be clean. Apply with `git apply docs/asset-revision.patch`, re-run
`tests/test_dashboard_base_path.py`, and delete the patch file once it lands.
Correct the docstring of `test_frontend_assets_share_one_cache_revision` in the
same change.

## 5. A real G2P instead of `phonetic_key`

`phonetic_key` is a hand-rolled consonant skeleton that needed a syllable-count
patch on 2026-09-16 because it scored a *wrong* rendering above a *right* one:
`drizzt`/`drizzit` 0.91 against `guenhwyvar`/`guinevar` 0.89. It works, and it
is guesswork.

A real grapheme-to-phoneme pass (espeak via `phonemizer`, or similar) would give
actual phonemes for both the term and the transcript and retire a class of
heuristics — including the syllable-count special case.

**Handle with care.** Every verdict in this project now depends on
`phonetic_key`, and the voice venv is ROCm with its own constraints. Add the G2P
*alongside* first and compare the two over the existing audit data before
replacing anything. The regression suite in
`tests/test_pronunciation_evidence.py` has ~30 real pairs and is the right
oracle.

## 6. Surface staleness in the dashboard

Two kinds of staleness bit during this work and neither is visible in the UI:

- **evidence older than the lexicon** — `measure_pronunciations.py` already
  reports it (`evidence_current` / `evidence_freshness` in the audit JSON);
- **deliveries older than their chapters** — `reexport_deliveries.py --stale`
  already detects it.

Both are one call away from being a dashboard badge. The second is the one that
silently wasted a full repair cycle.

## 7. Audit other standalone entry points

`validate_single()` never received `validation_terms`, so on the `/validate`
path the glossary discount was dead code and any short line containing a
fictional name was judged on raw WER — one name in three words is 0.33, past
the 0.20 threshold. It had been that way indefinitely and also affected the
dashboard's preview and review flows.

That is a *shape* of bug: a path that works inside a chapter run and quietly
misbehaves when called alone. Grep for other helpers with defaulted context
parameters and check what the standalone callers pass.

---

# Emberdark — metrics only

`isles-of-the-emberdark-a-cosmere-novel-secret-projects-book-5` is **finished
and delivered**. The owner's instruction (2026-09-16) is that any further work
on it is for **test and metrics purposes only, never for actual usage**.

Concretely: do not repair its segments, do not re-master it, do not re-export
its deliveries, do not apply lexicon changes to it.

It remains useful as a measurement corpus, with one caveat already encoded in
the tooling: its lexicon changed 2026-09-12 while its newest audio is from
2026-09-03, so **every verdict on an active entry there is meaningless** until
it is regenerated — which is exactly what must not happen. Treat its
`unrespelled` verdicts as usable evidence and its `active`-entry verdicts as
unevaluated. `measure_pronunciations.py` prints this warning on every run.

---

# Ground rules that earned their place

- **`unstable` never ships a respelling.** Only `mispronounced` does. The other
  verdicts are reports.
- **Measure before shipping a respelling, with a control arm.** Two of the three
  respellings trialled on 2026-09-16 would have made things worse.
- **Never add `initial_prompt` to the Whisper call.** It would improve
  transcription of rare names, and that is precisely why it is forbidden: this
  validator is an unbiased phonetic reporter, and priming it erases the signal.
  A test asserts its absence.
- **A repair is not done until the delivery is rebuilt.**

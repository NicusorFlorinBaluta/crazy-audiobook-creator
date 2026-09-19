# Prompt reordering for LLM prefix-cache reuse

**Status:** ~~Current~~ **Held / Declined** (2026-09-19 code review)

---

## Context

`ScriptGenerator._process_fragments` builds a system prompt for every call to
the local director (Ollama). Before this change the prompt structure was:

```
[role line]
## Context
  {character_registry}   ← per-chapter, changes every call
  {previous_summary}     ← per-chapter
## Script Tagging Task
  [500+ tokens of static rules]
## Compact Output Schema
  [200+ tokens of static schema + JSON example]
  {chapter_number}, {chapter_title}  ← per-chapter
```

The dynamic `## Context` block appeared at byte offset ~200, so Ollama's
prefix-cache KV store diverged from the warmup after the first twenty tokens of
every call. All ~1,497 calls per book paid full prefill cost for the static
rulebook on every chunk.

## The proposed change

Move the static sections to the top and push `## Context` to the bottom:

```
[role line]
## Script Tagging Task        ← unchanged static
## Compact Output Schema      ← unchanged static, including {schema_appendix}
{JSON example with chapter_number, chapter_title}
{schema_appendix}             ← _DIALOGUE_FOCUSED_SCHEMA_PROMPT or ""
## Context
  {character_registry}
  {previous_summary}
```

The `{schema_appendix}` placeholder is resolved inside `_SYSTEM_PROMPT.format()`
rather than appended afterwards, so it is now part of the static prefix whenever
`dialogue_focused_schema` is enabled (and an empty string otherwise). The
cacheable prefix extends to the end of the schema section, gaining roughly 1,500
extra tokens of shared prefix per book.

## Screening results & Review Findings

### Step 1 — Full test suite

```
pytest tests -q
1094 passed, 2 skipped, 1 warning, 64 subtests passed in 37.60s
```

All tests passed without modification. No case-ledger tests regressed.

### Step 2 — Prefix-cache benchmark

```
./venv/Scripts/python.exe scripts/benchmark_script_chunks.py \
  --project the-finest-edge-of-twilight-book --chapter 7 \
  --repetitions 2 --order AB --allow-models \
  --output docs/benchmarks/prompt-reorder-prefix-cache.json
```

Selected `prompt_eval_duration_ns` measurements comparing repetition 1 against
repetition 2:

| config | offset | rep | prompt_eval_count | prompt_eval_duration | notes |
|---|---|---|---|---|---|
| w350_f40 | 0 | 1 | 3,901 | 4,328 ms | cold — full prefill |
| w350_f40 | 0 | 2 | 3,901 | **717 ms** | cache hit — **83% drop** |
| w350_f40 | 160 | 1 | 4,030 | 4,464 ms | cold |
| w350_f40 | 160 | 2 | 4,030 | **122 ms** | cache hit — **97% drop** |
| w550_f60 | 160 | 1 | 5,204 | 5,832 ms | cold |
| w550_f60 | 160 | 2 | 5,204 | **123 ms** | cache hit — **98% drop** |

> **Critical Review Finding (2026-09-19):** Repetition 1 vs. 2 evaluates a
> byte-identical prompt request (identical fragments), which Ollama caches
> regardless of whether dynamic context appears at the top or bottom. The actual
> prefix reuse test is whether chunk 2 of a chapter benefits from chunk 1's static
> prefix. In cold runs across chunks, chunk 2 still costs 3.2–4.6 s for
> 2,900–4,100 tokens (`speed_gate_pass: false`). No cold chunk demonstrates
> material cross-chunk prefill reduction.

### Step 3 — Attribution screening on chapters 18 and 14

Both are named in the improvement plan as "known-hard attribution" chapters.

#### Chapter 18 (239 lines, 3,943 words)

`assert_script_covers_source` → **PASSED**

| line_id | diff | verdict |
|---|---|---|
| `ch18_0118` `"What can we do?"` | baseline=`dininae`, new=`kyrnill` | Genuinely ambiguous alternating dialogue. Kyrnill speaks the surrounding lines. |

`ch18_0132` (pinning test per case ledger: `she accused.` / `kyrnill`) →
baseline `narrator`, new `narrator`. **No change.** Pinning test holds.

#### Chapter 14 (357 lines, 4,551 words)

`assert_script_covers_source` → **PASSED**

| line_id | diff | verdict |
|---|---|---|
| `ch14_0178` `"I am expelled…"` | baseline=`breezy (0.95)`, new=`gregory_antoine (0.99)` | **Baseline was correct.** Source text: Breezy says this to Gregory. New script assigned to Gregory. |
| `ch14_0280` `"Why do you think she went…"` | baseline=`savahn (0.95)`, new=`perrywinkle_shin (0.95)` | Ambiguous alternating dialogue. |

> **Critical Review Finding (2026-09-19):** Across benchmark configs,
> attribution drift totalled 15 and 12 changes across 9 distinct fragments, with
> recurring fragment IDs (160, 162, 165, 197, 219) across repetitions. A control
> run with the original prompt was not run to establish baseline variance.

## Decision

~~**Promote.** The prompt reordering is safe to deploy~~

**Held / Declined.** In accordance with `docs/scripting-quality-performance-policy.md`
and Ground Rule 11/F9:
- Quality strictly outranks throughput.
- Shared prefix-cache reuse across differing chunks is not evidenced by the
  repetition benchmarks.
- Deterministic attribution movements were observed on recurring fragments.
- Prompt layout in `brain/director/script_generator.py` is reverted to the
  baseline layout (`## Context` at top).

## Files changed

- `brain/director/script_generator.py` — `_SYSTEM_PROMPT` reordered; `{schema_appendix}` placeholder added; `_SYSTEM_PROMPT.format()` call updated with `schema_appendix=` keyword.

## Benchmark artefact

`docs/benchmarks/prompt-reorder-prefix-cache.json`

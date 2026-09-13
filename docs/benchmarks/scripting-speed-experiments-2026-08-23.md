# Scripting speed experiments — 2026-08-23

## Decision policy

Audiobook quality is the primary objective; speed is secondary. A candidate is
promotable only when source coverage, fragment IDs, registered speakers,
speaker confidence/review behavior, and delivery metadata remain at least as
safe as the control. All tests used immutable excerpts from the paused project;
no saved script or checkpoint was rewritten.

ComfyUI had materially slowed earlier measurements and was closed before these
tests. A game remained active during this benchmark at the operator's request.
Its load was reasonably steady but still makes exact throughput percentages
noisy, so results emphasize paired direction, capacity, and quality gates.

## Results

| Candidate | Result | Decision |
|---|---|---|
| qwen2.5:32b, 8K context, 40 rows | Difficult window: 206.99 s versus 216.83 s at 16K; 0 versus 5 conservative fallbacks. Second window: 338.61 s versus 356.02 s; both had 0 repairs/fallbacks. Prompts were about 3.4–3.7K tokens and normal responses about 0.7–1.4K tokens. | Promote 8K. It preserves ample context headroom, halves KV-cache allocation, passed both windows, and was directionally faster in both whole-request comparisons. |
| 60 rows per request | The initial run raised the harness's then-generic invariant error before diagnostics were persisted. A diagnostic rerun passed all invariants in 297.33 s with 3 deterministic repairs and no focused retry/fallback. The matching 40-row control took 325.15 s, with 2 deterministic and 2 focused repairs; 60 rows changed fewer saved-baseline attributions (10 versus 16). | Do not reject, but retain 40 pending replicated balanced A/B runs. The original failure was not reproducible and its exact invariant is unknowable from the old artifact. |
| qwen3.6:27b | Raw decode was about 26 tok/s, but the client omitted Ollama's top-level `think:false`. Hidden reasoning consumed most of the output/context budget: the 40-row request reached the server output limit after 213.3 s and required adaptive splitting. | Benchmark invalid for model-quality comparison. Add explicit thinking control and separate thinking/content metrics, then retest before considering production use. |
| Dialogue-focused schema v5 | Main response fell from 917 to 719 tokens and the main request from 171.01 to 161.55 s. However, focused attribution repairs rose from 2 to 6 and total window time rose from 206.99 to 260.95 s. Coverage and speaker invariants still passed. | Keep experimental and disabled. The deterministic sparse-row implementation and tests remain available for future prompt tuning, but it is not quality/performance safe to promote yet. |
| qwen3.8:27b, 8K, `think:false` | Difficult window: 164.61 s, zero focused retries/fallbacks, and the known difficult target remained high-confidence without review. Exact-baseline window: 122.00 s versus 338.62 s for qwen2.5:32b under game load, with zero attribution changes, repairs, fallbacks, or invariant failures. Thinking-enabled trials produced thousands of hidden reasoning tokens and reached the 8K safeguard; explicit non-thinking mode removed that failure. | Promote Qwen 3.8 in non-thinking mode. |
| Qwen 3.8, 40 versus 60 rows and 8K versus 16K | Balanced-order tests used the same 60 source fragments. At 8K, the warm-adjusted 60-row request took 127.91 s versus 104.49 s for 40 rows and decoded 3,097 tokens. At 16K on the same excerpt, 60 rows took 54.64 s versus 88.68 s for 40, with 1,886 decoded tokens. On the difficult excerpt, warm-adjusted 60 rows took 79.79 s versus 97.75 s for 40. Both 16K modes had the same 17 saved-baseline attribution deltas, one deterministic named-tag repair, no focused retry/fallback, and no invariant failure. | Promote 16K/60. The 16K headroom avoids the unstable verbosity seen at 8K/60; 60 rows reduces duplicated prompt work and was 18–38% faster after subtracting the directly reported cold model-load duration. |

## Production configuration

- Model becomes `qwen3.8:27b`.
- Context window becomes `16384`. The largest measured 60-row prompt was 4,649
  tokens and the corresponding response was 2,240 tokens, leaving substantial
  room for naturally larger production batches and safeguard diagnostics.
- Ollama thinking is explicitly disabled for structured metadata requests.
- `max_fragments_per_chunk` becomes `60`; adaptive splitting remains the safety
  net for unusually difficult batches.
- `dialogue_focused_schema` remains `false`.
- Existing adaptive split and 600-second generation safeguards remain enabled.

Benchmark JSON files in this directory contain hashes, runtime settings, call
metrics, repair/fallback counts, and confidence/review outcomes. They do not
change production artifacts.

Cold model loads were deliberately alternated: the 8K pair ran 60 then 40, the
first 16K pair ran 40 then 60, and the difficult 16K pair ran 60 then 40. The
comparison above subtracts only Ollama's recorded `load_duration`, not game GPU
load or any inferred adjustment. Percentages are therefore directional rather
than a promise for every chapter. Quality remains the promotion gate; speed is
secondary.

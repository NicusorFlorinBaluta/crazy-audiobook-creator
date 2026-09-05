# Adaptive scripting recovery — 2026-08-23

**Status:** Historical record — A dated record of what was done and why. Evidence, not a specification -- do not implement from it.

## Incident

The restarted 63-chapter scripting run was healthy but averaged about 2.0 output
chunks per wall-clock second. Three requests reached the 900-second generation
limit and fell back conservatively. The affected requests contained 100–131
source fragments despite `max_fragments_per_chunk: 40`.

The row limit was correctly implemented in `_chunk_fragments`, but
`generate_chapter_script` used a direct path whenever the chapter fit the word
limit. Dialogue-dense chapters could therefore bypass the independent row cap.

## Resolution

- Chapter planning always calls `_chunk_fragments`; the direct path is used only
  when both the word and row constraints produce one batch.
- The active generation ceiling for future pipeline instances is 600 seconds.
- YAML output, wall-clock, and repetition safeguards are passed explicitly into
  `OllamaClient`; configuration no longer depends on coincidentally equal class
  defaults.
- A generation-limit exception splits an eligible batch into balanced contiguous
  children, prefers a non-dialogue boundary, carries ten resolved turns forward,
  and retries to a maximum depth of two.
- Conservative fallback remains the terminal recovery and retains low-confidence
  review signaling.
- Row-limit and adaptive settings are fingerprint dependencies, preventing mixed
  cached scripts after policy changes.

## Expected impact and limits

The observed oversized requests spent 900 seconds producing roughly 1,670–1,735
chunks. With the proactive 40-row cap, comparable source should be handled as
several smaller, normally completing requests. This removes the known bypass;
the precise full-book speedup must be measured on a later run.

Adaptive splitting is a recovery path, not free parallelism. A failed parent has
already consumed time, and its children run sequentially on the same GPU. Its
quality advantage is that only a persistently failing small range reaches
fallback. Depth and minimum-size bounds prevent unbounded recursion.

The current in-memory pipeline was deliberately not restarted while this change
was developed. It continues with the previously loaded 900-second behavior and
keeps its completed chapter checkpoints. The new behavior takes effect after the
dashboard is restarted at a safe stopping point; its fingerprint change will
invalidate scripts produced under the old batching policy.

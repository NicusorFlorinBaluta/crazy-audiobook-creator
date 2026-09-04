# Quality and resilience decisions — August 2026

**Status:** Partially superseded
**Superseded by:** [separate-character-pass-and-attribution-gate-2026-08-23.md](separate-character-pass-and-attribution-gate-2026-08-23.md)
(scripting and character analysis only; the priority order, external-validation
and audio sections below still stand)

This record replaces raw assistant and IDE transcripts. Raw transcripts must
not be committed because they can contain source-book excerpts, machine paths,
service topology, and operational logs.

## Priority order

1. Preserve source fidelity and final audiobook quality.
2. Preserve evidence, confidence trails, and human intervention for uncertain
   decisions.
3. Improve speed and resource use only when the first two priorities remain
   equivalent or improve.

## Scripting and character analysis

- ~~Joint character discovery remains the default~~, with a durable registry
  checkpoint after each completed chapter.

  **Superseded 2026-08-23.** Production now runs a dedicated book-wide
  character pass first (`script.joint_analysis: false`); joint discovery is
  retained for experiments only. The joint run produced 138 blocking
  attribution findings across 29 chapters, and splitting them by model (45 in
  14 Qwen 2.5 chapters vs 93 in 49 Qwen 3.8 chapters) showed the model was not
  the cause. See
  [separate-character-pass-and-attribution-gate-2026-08-23.md](separate-character-pass-and-attribution-gate-2026-08-23.md).
- Script cache dependencies are chapter-local. Unrelated later characters do
  not invalidate earlier scripts.
- A character or alias change that affects a chapter may invalidate or repair
  that chapter; completed scripts are not unconditionally immutable.
- Speaker candidates are computed from the complete chapter and reused across
  every fragment chunk.
- External attribution decisions must remain within the candidate set of the
  decision's own chapter. Deterministic dialogue constraints apply equally to
  API adjudication and browser fallback.

## External validation

- Gemini API triage and adjudication use configured models, budgets, retries,
  and circuit breaking. Browser Pro is an optional final fallback.
- Browser conversations persist per project and versioned purpose; schema
  revisions rotate to a new conversation.
- Character and glossary augmentation require confidence and verbatim source
  evidence. Low-confidence or conflicting proposals are not applied and are
  surfaced for Voice Review.
- Human profile corrections from Voice Review are durable overrides, reapplied
  after later automated analysis. They invalidate only dependent chapters and
  require a fresh voice approval/preview where applicable.
- Hard deterministic audio failures cannot be cleared by an external model.

## Extraction and identity

- Every EPUB spine document records classified and extracted word counts.
  Included documents that produce no narrative text require review.
- Pipeline chapter number is the stable sequence identity shown to users.
  EPUB source headings remain separate metadata and must not produce labels
  such as `Chapter 8: Chapter Seven`.

## Operations and delivery

- Unexpected restart recovery preserves the active chapter selection and does
  not bypass working hours.
- Delivery settings changed during a run apply to the next run; an active
  publication plan is immutable.
- Project deletion reports success only after all artifact roots are gone.
  Locked-file failures retain the job record for a safe retry.
- Mobile catalog, media, logs, and playback mutations use the same LAN/token
  boundary as the dashboard. Only compatibility discovery is public.

## Repository policy

- Commit focused changes with matching messages and tests.
- Store compact decision records under `docs/decisions/`.
- Never commit raw chat exports, full source-book passages, secrets, or local
  operational transcripts.

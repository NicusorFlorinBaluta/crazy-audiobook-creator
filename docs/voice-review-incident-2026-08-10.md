# Voice Review Attribution and UI Incident — 2026-08-10

**Status:** Historical record — A dated record of what was done and why. Evidence, not a specification -- do not implement from it.

## Impact

The `sample_book-14` release run reached voice review with contaminated
character samples. Several samples included narrator dialogue tags or action
beats. The `child_female` sample included a quote explicitly tagged as spoken
by “the boy.” The run was parked before voice approval and no release audio was
generated from this invalid script.

The dashboard also gave unreliable approval feedback: the comparison Apply
button called an unsupported endpoint and ignored HTTP failures, while
Regenerate always replaced the canonical option even when another A/B option
was selected.

## Root causes

1. The dashboard backend process had not been restarted after the
   dialogue-tag ownership fix. Scripting and voice bootstrapping therefore ran
   stale in-memory code even though the working tree contained the correction.
2. Pass 1 omitted the explicit unnamed male child. The deterministic Pass 2
   validator handled `he`/`she` and named tags, but did not reject gendered noun
   contradictions such as `child_female` followed by `the boy said`.
3. The candidate Apply UI used a removed `/voices/{character}/assign` route,
   did not check `response.ok`, and did not restore or refresh its state.
4. Candidate regeneration captured the main voice ID instead of reading the
   selected radio option.

## Corrections

- Explicit quoted utterances followed by `the/a boy`, `girl`, `man`, or
  `woman` plus a speech verb now ensure a conservative unnamed role exists
  before voice IDs are assigned.
- Dialogue-tag validation rejects a binary speaker whose metadata contradicts
  those gendered noun tags.
- The character-analysis dependency fingerprint now includes a policy
  revision, invalidating registries created before this deterministic rule.
- Apply uses the supported character voice PATCH route, checks every response,
  exposes busy/success/error states, refreshes the cast, and makes no-op state
  explicit.
- Regenerate reads the selected A/B voice ID and labels which option it will
  replace. Changing selection also updates its design text, preview, download,
  Apply state, and replacement label.
- The live upload E2E test is opt-in and no longer copies a historical artifact
  during test discovery.

## Release handling

`sample_book-14` is not valid release evidence and its current voices must not
be approved. After committing these fixes, the dashboard must be restarted and
the project must repeat character analysis, scripting, and voice bootstrap.
The replacement voice review should verify that character previews contain
only their spoken quotes and that the boy receives a male child voice.

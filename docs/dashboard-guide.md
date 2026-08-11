# Dashboard Guide

The dashboard is organized around the current production decision rather than
the internal pipeline implementation.

## Projects

Search by title, author, or project ID. Status and sort controls help separate
active work from completed releases. Project cards are keyboard-accessible and
include the project ID so similarly named test runs remain distinguishable.

## Project actions

The primary button describes the next action. On a completed project,
**Generate selected chapters again** uses the current chapter selection and
does not replace the completed audiobook until the new run produces output.

Stage restarts and project deletion are under **More actions**. Selecting a
restart stage shows what is preserved and what is regenerated before the
confirmation dialog appears.

Completed projects collapse pipeline progress and chapters by default. Expand
either section when reviewing history or selecting the next chapter batch.

## Voice casting

Voice cards are grouped by speaking character. Search or filter for assigned
profiles, alternatives, warnings, or ready references. Design direction and
advanced redesign/upload controls are collapsed to keep comparison controls
prominent.

The assignment selector reflects the voice currently assigned in
`voice_cast.json`, including narrator alternatives such as `narrator_male` and
`narrator_female`. Candidate labels include their owning character to avoid
ambiguous repeated names.

## Script review

Select a chapter, search text or line IDs, filter by speaker, show dialogue
only, or audit narrator-attributed lines containing quoted speech. The latter
is an audit aid: tight narrator/dialogue grouping can be intentional.

Quality retry rows link back to their source script line.

## Quality review

Summary cards include definitions for accepted rate, accepted warnings,
retries, WER, silence, and clipping. High line-level WER can still pass when an
approved pronunciation or spelling variant explains the difference; the final
acceptance reason is displayed beside the metric.

Join warnings open on the unreviewed queue. Filter by disposition, chapter,
speaker, or severity. Reviewed items remain collapsed under **Show reviewed**.
Changes enable the row's save button, and visible filtered items can be marked
acceptable as a confirmed batch action.

## Logs and support

Completed projects label their stream as a historical log. Search and level
filters, optional wrapping, and routine-line suppression reduce repeated
health checks and cache-hit noise. Copy and download operate on the visible
filtered lines.

The support bundle contains project diagnostics and logs. It excludes the
source EPUB and generated audio.

## Keyboard behavior

- Project cards, navigation, and upload controls are buttons.
- Arrow keys move between project-detail tabs; Home and End jump to the first
  and last tab.
- The new-project dialog traps focus, closes with Escape, and returns focus to
  the control that opened it.

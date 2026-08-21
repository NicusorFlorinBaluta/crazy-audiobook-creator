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
Their native disclosure state survives background status polling. Chapter
controls use the same clickable section-header pattern as the other panels.

**Automatic working hours** is global to the queue. Each window has explicit
weekday, start, and end controls; overnight windows belong to their start day.
Time fields retain enough width for native 12-hour AM/PM controls and stack on
narrow mobile screens.

## Book details

**Find book details** previews the best Google Books match without changing the
project. If it is wrong or no confident automatic match exists, expand
**Search for another book or edition**, search by title and optional author,
and select the exact volume. Applying the reviewed result adopts its title,
author, description, genre, year, and ISBN. The original EPUB identity remains
available internally as provenance.

For completed projects, applying details repackages existing M4B files by
stream-copying the audio and chapters with refreshed tags and cover. It does
not run models or re-encode speech. The audiobook download filename follows the
current reviewed title.

## Voice casting

Voice cards are grouped by speaking character. Search or filter for assigned
profiles, alternatives, warnings, or ready references. Design direction and
advanced redesign/upload controls are collapsed to keep comparison controls
prominent.

The assignment selector reflects the voice currently assigned in
`voice_cast.json`, including narrator alternatives such as `narrator_male` and
`narrator_female`. Candidate labels include their owning character to avoid
ambiguous repeated names.

Every ready profile has a named **Download voice sample** action. **Download
all samples** creates one ZIP containing every prepared character and narrator
candidate plus a JSON manifest with voice IDs, character labels, source type,
exact reference transcript, and assignments. Duplicate display names receive
an ID-qualified filename rather than overwriting one another.

## Script review

Select a chapter, search text or line IDs, filter by speaker, show dialogue
only, or audit narrator-attributed lines containing quoted speech. The latter
is an audit aid: tight narrator/dialogue grouping can be intentional.

Quality retry rows link back to their source script line.

## Book-section review

An uncertain EPUB section appears under **Attention required → Book sections**
before scripting starts. The row shows the local recommendation, confidence,
word count, filename, and automated decision trail without revealing book text.
Choose **Include in narration**, **Exclude**, or **Keep as reference**. The
preserved `source.epub` is re-extracted and the pipeline resumes automatically
after the last blocking section is resolved. Once scripting exists, reset to
extraction first so downstream artifacts are deliberately invalidated.

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

## Server Lifecycle & Resilience

The dashboard server runs under an automated self-healing supervisor:
- **Self-Healing Supervisor**: Started via `scripts/start_dashboard.ps1`, which monitors `/health` on 10-second intervals and auto-recovers unresponsive sockets within $<2$ seconds while preserving manual shutdown capability.
- **PortProxy Loopback Isolation**: Configurable via `scripts/setup_portproxy.ps1` to isolate external LAN connections from physical router resets.
- For architectural details, see [Socket Resilience & Self-Healing Architecture](file:///e:/Projects/crazy-audiobook-creator/docs/socket-resilience-and-supervision.md).

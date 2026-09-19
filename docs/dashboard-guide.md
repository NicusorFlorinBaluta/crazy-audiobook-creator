# Dashboard Guide

**Status:** Reference — Describes current behaviour. Keep it accurate when the code changes.

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

## Chapter management & status

The chapter list reflects live execution state and artifact validity:

- **Mastered**: Chapter audio and announcement have been mastered into a final
  WAV. A download link (`↓`) is available for direct inspection.
- **Generated**: Utterance synthesis and validation are complete; awaiting mastering.
- **Scripted · X lines**: Chapter script has been extracted and validated. Audio
  generation has not begun.
- **Needs audio update**: Audio was previously generated or mastered, but a
  subsequent pronunciation lexicon or voice cast edit invalidated the existing
  audio. Only chapters with existing completed audio receive this status;
  un-generated scripted chapters remain `Scripted`.
- **Active execution**: During pipeline runs, the active chapter dynamically displays
  live stage indicators (`Synthesizing 25/50`, `Validating...`, `Mastering...`) with
  pulsing active styling and progress percentages instead of static completed badges.
- **Batch selection & filtering**: Checkboxes allow targeting specific chapter
  subsets. Status filtering includes an option for `Needs update` to quickly audit
  invalidated chapters.

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

## Pronunciation lexicon

Manage phonetic spellings and pronunciations for characters, fantasy terms,
and book-specific vocabulary:

- **Candidate inventory**: Scans project script text for non-standard words,
  filtering out recognized vocabulary using an offline English word index.
- **Search & filtering**: Ranked lexical search with an instant clear (`✕`)
  action, categorizing entries by Status (Custom, Verified, Defaults).
- **Audio previews & preview mode**: Audition pronunciation rules using concise
  context sentences or full carrier phrases. Previewing audio engages Preview Mode,
  safely pausing active background pipeline jobs; resuming the pipeline automatically
  exits Preview Mode. Previews can also be batch pregenerated with real-time
  progress and ETA tracking.
- **Scoped export & import**: Export rules with granular scope filters (`all`,
  `custom`, `defaults`). Import rules from JSON files or directly cherry-pick
  entries from another book project, with side-by-side diff resolution.
- **Implicit defaults**: Unmodified candidate defaults apply automatically during
  synthesis without requiring manual confirmation.

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

## Review Gate and Attention Required

The Attention Required inbox and pre-master release report aggregate review work across attribution, pronunciation, audio segments, and voice trends:
- **Ranked worklist**: Items are ordered with blocking items first, followed by actionable changes ordered by impact. Pronunciation candidates are ranked by script occurrences descending, so frequent names (e.g. 1,000+ mentions) sit at the top rather than being buried alphabetically.
- **Audio trend collapsing**: Voice prosody warnings across chapters are collapsed to one summary row per voice, presenting the total warning count, chapters affected, and the worst measured variation.
- **Audio rejection grouping**: Audio segment rejections sharing the same glossary term in their rejection reason are grouped into a single fix row detailing all affected segment IDs and chapters.
- **Top actions**: The pre-master release report surfaces a `top_actions` array capped at the 10 highest-value items across all categories for fast operator triage.

## In-Car & Mobile Playback Flags

Auditioning in a vehicle or on mobile often reveals subtle attribution or delivery defects that automated gates cannot detect. The **🚩 Playback Flags** tab provides an interactive triage center for reports submitted from Android Auto and the companion mobile app:

- **Live Flag Counter**: The tab header displays an attention badge indicating the number of unresolved (`Open` or `Investigating`) flags.
- **Status Filter Toolbar**: Filter flags by `Open Only`, `Investigating`, `Fixed`, `Vetoed`, `Dismissed`, or `All Flags`.
- **Reaction Delay Window**: Compensates for natural listening and driving delays (5–15 seconds) by presenting an expanded 20-second timeline preceding the tap. Candidate dialogue and narration lines display relative timestamps (e.g. `-9.6s`, `-7.3s`, `[AT TAP]`).
- **Interactive `🎯 Focus Line` Retargeting**: Click **Focus Line** on any candidate line to instantly retarget the flag's active line ID, recalculating relative offsets and manuscript excerpts in real time.
- **Collapsible Context Views**: Expand surrounding chapter dialogue or inspect authentic manuscript prose extracts ($\pm 600$ characters) to verify speaker tags.
- **Inline Status Lifecycle**: Adjust statuses directly using the card dropdown or one-click action buttons:
  - `🟡 Open`: Default state awaiting triage.
  - `🔵 Investigating`: Active review in progress.
  - `🟢 Fixed`: Issue addressed and corrected.
  - `🟣 Vetoed`: Flag reviewed and confirmed correct (preserves audio with written reason).
  - `⚪ Dismissed`: Invalid or irrelevant flag.
- **Agent CLI Integration**: Copy the targeted CLI command (`python tools/investigate_playback_flags.py <project_id> --flag-id <flag_id> --auto-diagnose`) for automated LLM review.

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
- For architectural details, see [Socket Resilience & Self-Healing Architecture](../docs/socket-resilience-and-supervision.md).

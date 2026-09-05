# Speaker Attribution & Dialogue Reconciliation Improvements (2026-08-18)

**Status:** Historical record — A dated record of what was done and why. Evidence, not a specification -- do not implement from it.

## Overview & Background
During end-to-end processing of *Isles of the Emberdark* (Brandon Sanderson, Secret Projects Book 5, 63 chapters, 3,937 dialogue fragments), several attribution failure modes were identified, analyzed, resolved, and documented for long-term codebase maintenance.

---

## 1. Root Causes & Failure Modes Identified

### A. Out-of-Chapter Character Hallucinations
- **Issue**: Pass 2 scripting attributed dialogue in Chapters 4 and 7 to characters who were not present in the scene (e.g. `Starling`, `Uncle/Frost`).
- **Root Cause**: The LLM prompt candidate list provided the entire global book registry (`40+ characters`) rather than the active characters in that specific chapter.
- **Fix**: Implemented `ScriptGenerator._get_chapter_scoped_speakers(chapter_text, registry)` using regex word-boundary matching on character names, aliases, and ID tokens. Added `absent_character_in_chapter` validation to `brain/director/attribution_audit.py`.

### B. Character Registry Entity Fragmentation & Alias Duplication
- **Issue**: 
  1. Starling calling Frost *"Uncle"* led the Pass 1 extractor to create a separate entity `uncle`.
  2. Starling's dragon name *Illistandrista* was extracted as a second character entity `illistandrista` in Chapter 41.
  3. The Ones Above mechanic/engineer `Ed` on the ship was omitted from initial extraction.
- **Fix**: Consolidated `uncle` $\rightarrow$ `frost` with aliases `["Frost (Uncle)", "Uncle", "Frost", "Uncle Frost"]`, merged `illistandrista` into `starling`, and added `ed` with full voice and character metadata to `characters.json`.

### C. False-Positive Collective Speech Contradictions on Subordinate Clauses
- **Issue**: Lines like *"Nazh asked as they started walking."* or *"Starling said as they began to raise their cups."* triggered `collective_speech_character_contradiction`.
- **Root Cause**: `ScriptGenerator._is_collective_dialogue_tag` matched the speech verb (`asked`) followed by `as they`. In English syntax, `"as they started walking"` is a subordinate temporal clause describing simultaneous background action, NOT a collective speaker.
- **Fix**: Added an explicit subordinate clause filter to `_is_collective_dialogue_tag` ignoring clauses starting with `as|while|when|if|because|once|after|before|since|until \s+ they`.

### D. Narrow Escalation Window & Prior Label Anchoring Bias
- **Issue**: Ambiguous or un-tagged short dialogue lines (e.g., Chapter 4 line `ch04_0125` *"Of what?"*) were misattributed to `Frond` instead of `Dusk`.
- **Root Cause**: 
  1. Escalation passed only 3 lines before and after (6 lines total) in JSON format with existing upstream speaker labels.
  2. Because upstream local LLM had misattributed adjacent lines to `Frond`, Gemini saw a small window with multiple `Frond` tags and anchored on a continuous monologue hypothesis.
- **Fix**: 
  1. Expanded escalation window to 20+ lines (`index - 10` to `index + 11`) and provided surrounding prose context (`surrounding_scene_text`).
  2. Injected deep conversational grounding rules in `GeminiValidationService`:
     - **Two-Party Alternation (Stichomythia)**: In two-person scenes without intervening speakers, un-tagged dialogue turns strictly alternate between Speaker A and Speaker B.
     - **Vocative Direct Address**: When a quote says `"..., [Name]"` (e.g. `"..., Dusk."` or `"Remember us, worldspinner"`), the speaker is the *other* character talking *to* that person.
     - **Leading Action Beat Binding**: When a quote is preceded in the same paragraph by a singular pronoun action beat (`He nodded slowly. "..."`), the subject pronoun binds to the speaker.

### E. Review Inbox UI Polish for Non-Blocking Items
- **Issue**: After all blocking attribution errors were fixed, the Review Inbox displayed: *"Attention required — 180 non-blocking or resolved items"*.
- **Root Cause**: The 180 items were optional pronunciation dictionary candidates. When `blocking_count` became 0, the UI fell back to displaying `total_count` (180) under an un-collapsed amber banner.
- **Fix**: Updated `brain/dashboard/frontend/js/app.js`, `index.html`, and `styles.css` to automatically collapse the Review Inbox into an optional green-bordered card (`✓ 0 blocking issues • 180 non-blocking or resolved items`) whenever `blocking_count === 0`.

### F. Voice Casting Protagonist Over-Allocation & Removal of Unique Voice Cap
- **Issue**: In the dashboard, `Starling` was shown assigned to 5 characters (`Crux`, `Mother`, `Unnamed Woman`, `Starling`, `Tuka`), `Dusk` was assigned to 14 characters, and `Frost (Uncle)` was missing.
- **Root Cause**: 
  1. In `CharacterAnalyzer._assign_voice_ids()`, when total characters exceeded `max_unique_voices` (previously 20), overflow characters were collapsed to `same_gender[0]` (the main protagonist).
  2. `voice_cast.json` on disk was stale from an earlier run before `uncle` was renamed to `frost` and `illistandrista` merged into `starling`.
- **Fix**: 
  1. Removed the artificial unique voice limit (`max_unique_voices: 0` = unlimited). Every named speaking character receives their own dedicated voice.
  2. Retained major/minor categorization ($\ge 5$ dialogue lines $\rightarrow$ 3 candidate auditions; $< 5$ dialogue lines $\rightarrow$ 1 candidate).
  3. Retained generic background pools (`minor_male`, `minor_female`, `child_male`, `child_female`) for incidental unnamed characters.
  4. Regenerated `voice_cast.json` for *Isles of the Emberdark*: all 41 characters now have distinct, properly assigned voices, `frost` is present as `Frost (Uncle)`, and `starling` / `dusk` are exclusively assigned to their respective characters.

---

## 2. Code Changes Matrix & Traceability

| Component | File Path | Key Changes |
| :--- | :--- | :--- |
| **Director / Scripting** | [`brain/director/script_generator.py`](../brain/director/script_generator.py) | - Added `_get_chapter_scoped_speakers()`<br>- Added subordinate clause filter to `_is_collective_dialogue_tag()`<br>- Constrained LLM candidate list per chapter |
| **Auditing** | [`brain/director/attribution_audit.py`](../brain/director/attribution_audit.py) | - Added `absent_character_in_chapter` audit check<br>- Excluded narrator and non-spoken quotes from character gender contradiction checks |
| **External Validation** | [`brain/validators/gemini_validation.py`](../brain/validators/gemini_validation.py) | - Scoped candidate sets per chapter<br>- Expanded escalation window to 20+ lines (`[-10 : +11]`)<br>- Added conversational alternation, vocative inversion, and action beat rules to prompts<br>- Configured `gemini-3.5-flash-lite` / `gemini-2.5-flash` |
| **Pipeline Orchestrator** | [`brain/orchestrator/pipeline.py`](../brain/orchestrator/pipeline.py) | - Integrated chapter-scoped candidate sets into deterministic repair passes<br>- Connected audit issue pre-flagging directly to Gemini escalation |
| **Dashboard Frontend** | [`brain/dashboard/frontend/js/app.js`](../brain/dashboard/frontend/js/app.js)<br>[`brain/dashboard/frontend/index.html`](../brain/dashboard/frontend/index.html)<br>[`brain/dashboard/frontend/css/styles.css`](../brain/dashboard/frontend/css/styles.css) | - Wrapped Review Inbox in collapsible `<details>`<br>- Auto-collapse and green badge when `blocking_count === 0`<br>- Auto-expand on blocking issues or review pause |
| **Character Registry** | `brain/projects/<project-id>/characters.json` | - Consolidated `uncle` $\rightarrow$ `frost`<br>- Merged `illistandrista` $\rightarrow$ `starling`<br>- Added `ed` and `child_female`<br>- Cleaned dirty stopword aliases |

---

## 3. Verification & Metrics

- **Full Unit Test Suite**: `335 tests passed` (0 failures, 2 skipped).
- ***Isles of the Emberdark* Audit**:
  ```json
  {
    "chapters": 63,
    "dialogue_fragments": 3937,
    "narrator_quotations": 42,
    "blocking_issues": 0
  }
  ```
- **Chapter 4 Correction**: Lines `ch04_0125` (*"Of what?"*) and `ch04_0128` (*"If the chance comes... Remember us, worldspinner..."*) attributed to `Dusk`.

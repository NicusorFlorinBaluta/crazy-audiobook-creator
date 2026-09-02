# Tiered Dialogue Attribution Auto-Fix System — 2026-09-02

## Overview

During Pass 2 audiobook script generation, rapid back-and-forth dialogue ("staccato banter", e.g., *"Do you know about the cave?" / "What cave?" / "The cave of death"*) frequently suffers from attribution collapse in bulk extraction. In *Isles of the Emberdark*, Chapter 39 collapsed multiple turns into single speakers (attributing Dusk's line *"What cave?"* to Colonel Dajer). Across the entire book, 63 such dialogue turn collapses were detected.

This record documents the design, implementation, and verification of the **Tiered Dialogue Attribution Auto-Fix Pipeline**:
1. **Suspicious Turn Detector (`brain/director/attribution_detector.py`)**
2. **Tiered Attribution Adjudicator (`brain/validators/tiered_adjudicator.py`)**
3. **Pipeline & Configuration Integration (`brain/orchestrator/pipeline.py`, `brain/config.yaml`)**
4. **Standalone Repair Utility (`scripts/repair_attributions.py`)**

---

## 1. Architectural Design & Findings

### The "Broad Scan + Focused Sniper" Principle
Pass 2 processes 50–60 raw fragments simultaneously to capture book-wide narrative prosody, character dynamics, and emotional arcs. Shrinking Pass 2 chunk size would damage macro prosody and create seam artifacts.

Instead, a post-pass "sniper" micro-adjudicator isolates only the conversational seams, giving local Qwen 27B an uncluttered 10-line window where it historically scored 100% (5/5 on Chapter 39 isolated turns).

### 3-Tier Escalation Workflow
1. **Tier 1 (Local Qwen 27B)**:
   - Evaluates isolated 10-line micro-prompts at `temperature=0.1` and `think=false`.
   - Auto-accepts when confidence $\ge 0.85$ and all 4 guardrails pass.
2. **Tier 2 (Cloud Gemini API)**:
   - Escalates borderline cases, confidence $< 0.85$, or guardrail failures directly into the existing `GeminiValidationService.resolve_attributions` workflow.
3. **Tier 3 (Review Inbox)**:
   - Any cases unresolved by Gemini flow into the web dashboard Review Inbox for one-click human confirmation.

---

## 2. Anti-Overconfidence Guardrails

Local models are susceptible to recency bias and misleading adjacent narration tags (e.g. the `ch17_0119` benchmark failure). Four programmatic guardrails protect against overconfidence:

1. **Fuzzy Evidence Quote Verification (`quote_in_context`)**:
   - Normalizes quotation marks (smart quotes, straight quotes, single/double) and em dashes.
   - Requires `difflib.SequenceMatcher >= 0.85` so minor model paraphrasing passes while fabricated quotes fail.
2. **Gender / Pronoun Consistency Check (`gender_pronoun_check`)**:
   - Cross-references cited pronouns (`he said`, `she asked`, etc.) against canonical character gender in `CharacterRegistry`.
   - Hard-rejects opposite-gender attributions.
3. **Canonical Alias Resolution (`alias_resolver`)**:
   - Automatically maps character nicknames (`brie` $\rightarrow$ `catti_brie`, `Sixth` $\rightarrow$ `dusk`) to registered character IDs.
4. **Reciprocal Turn Consistency (`reciprocal_turn_check`)**:
   - Intercepts adjacent dialogue turns assigned to the same speaker across a question.
   - Tag-aware: If either turn contains explicit source speech tags (*"Starling screamed"*, *"Dajer asked"*), the turns are permitted. If both are untagged, the conflict is escalated to Gemini.

---

## 3. Verification & Live Benchmark (Chapter 39)

- **Unit Tests**: All 19 tests in `tests/test_tiered_adjudicator.py` pass cleanly.
- **Chapter 39 Benchmark**:
  - Total suspicious turns detected: **101**
  - Resolved locally by Qwen: **93 (92.1%)**
  - Escalated to Gemini: **8 (7.9%)**
  - High-impact repairs verified:
    - `ch39_0564`: `"What cave?"` $\rightarrow$ Repaired to `dusk` (conf: `0.95`)
    - `ch39_0537`: `"Yes."` $\rightarrow$ Repaired to `dusk` (conf: `0.95`)
    - `ch39_0369`: `"Oh, I'm very much still a soldier..."` $\rightarrow$ Repaired to `dajer` (conf: `0.98`)

---

## 4. Resuming Tomorrow

To resume and execute the repair:

```powershell
cd e:\Projects\crazy-audiobook-creator
$env:PYTHONPATH = "."

# 1. Preview changes across Chapter 39
python scripts/repair_attributions.py --project isles-of-the-emberdark-a-cosmere-novel-secret-projects-book-5 --chapter 39 --dry-run

# 2. Apply Chapter 39 changes to disk (and escalate remaining 8 lines to Gemini)
python scripts/repair_attributions.py --project isles-of-the-emberdark-a-cosmere-novel-secret-projects-book-5 --chapter 39 --apply --escalate-gemini

# 3. Or run across all chapters of the book
python scripts/repair_attributions.py --project isles-of-the-emberdark-a-cosmere-novel-secret-projects-book-5 --apply --escalate-gemini
```

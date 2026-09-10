"""Does a capital-led "<Name> <speech verb>." tag the quote BEFORE it or AFTER it?

The lower-case gate exists because a capital-led sentence may be a reaction
("Dahlia laughed at that.") rather than a tag. But a sentence whose verb is a
*speech* verb is a tag either way -- the open question is which quote it tags.
If it overwhelmingly names the preceding speaker, it can be read as a trailing
tag. If it often names the following speaker, it is a leading tag and using it
would corrupt attribution.
"""
import argparse
import re
from collections import Counter
from pathlib import Path

from brain.director.script_generator import ScriptGenerator
from shared.models import CharacterRegistry, ScriptChapter

_parser = argparse.ArgumentParser(description=__doc__)
_parser.add_argument("--project", default="the-finest-edge-of-twilight-book")
root = Path("brain/projects") / _parser.parse_args().project
registry = CharacterRegistry.model_validate_json((root / "characters.json").read_text(encoding="utf-8"))

SPEECH = r"(?:said|says|asked|asks|replied|replies|answered|stated|added|adds|muttered|growled|" \
         r"whispered|shouted|called|snarled|breathed|offered|insisted|countered|agreed|admitted|" \
         r"observed|remarked|continued|went on|interrupted|corrected|protested|explained)"
REACTION = r"(?:laughed|smiled|nodded|shrugged|frowned|grinned|sighed|blinked|stared|turned|" \
           r"looked|glanced|winced|scowled|chuckled)"

tally = Counter()
examples = {"trailing": [], "leading": [], "neither": []}
for path in sorted((root / "script").glob("chapter_*.json")):
    if path.name.endswith(".meta.json"):
        continue
    lines = ScriptChapter.model_validate_json(path.read_text(encoding="utf-8")).lines
    for i, nar in enumerate(lines):
        if nar.speaker != "narrator":
            continue
        tag = str(nar.text or "").strip()
        lead = next((c for c in tag if c.isalpha()), "")
        if not lead or lead.islower():
            continue
        is_speech = re.match(rf"^[A-Z][\w'\u2019-]*(?:\s+[A-Z][\w'\u2019-]*)?\s+(?:\w+ly\s+)?{SPEECH}\b", tag)
        is_reaction = re.match(rf"^[A-Z][\w'\u2019-]*(?:\s+[A-Z][\w'\u2019-]*)?\s+(?:\w+ly\s+)?{REACTION}\b", tag)
        if not is_speech and not is_reaction:
            continue
        named, _k, _g = ScriptGenerator._dialogue_tag_evidence(tag, registry)
        if not named:
            tally[("speech" if is_speech else "reaction", "unparsed")] += 1
            continue
        prev = next((lines[j].speaker for j in range(i - 1, -1, -1) if lines[j].speaker != "narrator"), None)
        nxt = next((lines[j].speaker for j in range(i + 1, len(lines)) if lines[j].speaker != "narrator"), None)
        kind = "speech" if is_speech else "reaction"
        if named == prev and named != nxt:
            tally[(kind, "trailing")] += 1
            if len(examples["trailing"]) < 3 and kind == "speech":
                examples["trailing"].append((nar.line_id, tag[:55], named))
        elif named == nxt and named != prev:
            tally[(kind, "leading")] += 1
            if len(examples["leading"]) < 5 and kind == "speech":
                examples["leading"].append((nar.line_id, tag[:55], named, prev))
        elif named == prev and named == nxt:
            tally[(kind, "both-same")] += 1
        else:
            tally[(kind, "neither")] += 1
            if len(examples["neither"]) < 5 and kind == "speech":
                examples["neither"].append((nar.line_id, tag[:55], named, prev, nxt))

for kind in ("speech", "reaction"):
    rows = {k[1]: v for k, v in tally.items() if k[0] == kind}
    total = sum(rows.values())
    print(f"\ncapital-led, {kind}-verb tags: {total}")
    for k in ("trailing", "leading", "both-same", "neither", "unparsed"):
        if rows.get(k):
            print(f"    {k:11}: {rows[k]:4}  ({rows[k]/total*100:.0f}%)")
print("\nLEADING examples (these would be mis-read as trailing tags):")
for e in examples["leading"]:
    print(f"   {e[0]} {e[1]!r} names {e[2]}, but previous speaker was {e[3]}")
print("\nNEITHER examples (names someone who is neither neighbour):")
for e in examples["neither"]:
    print(f"   {e[0]} {e[1]!r} names {e[2]} | prev={e[3]} next={e[4]}")

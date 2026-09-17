"""Grapheme-to-Phoneme (G2P) evaluation benchmark alongside phonetic_key.

Compares phonetic_key similarity verdicts against phonemic transcriptions
across the test oracle pairs in tests/test_pronunciation_evidence.py and
real-world audit data.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.pronunciation_evidence import sound_similarity

ORACLE_PAIRS: list[tuple[str, str, bool, str]] = [
    ("Wulfgar", "Wolfgar", True, "Vowel spelling variation in closed syllable"),
    ("Drizzt", "drist", True, "Faithful phonetic voicing of final cluster"),
    ("Sylfae", "sylphay", True, "ph vs f spelling equivalence"),
    ("Sylfae", "sylphae", True, "ae vs phae spelling equivalence"),
    ("Sylfae", "sylfey", True, "ey vs ae unstressed ending"),
    ("Guen", "Gwen", True, "gu before vowel as /gw/"),
    ("Entreri", "entrary", True, "Unstressed final vowel variation"),
    ("Catti-brie", "Cadbury", False, "Mangled flapping / distinct brand name"),
    ("Xisis", "Jesus", False, "Genuinely different name"),
    ("Braelin Janquay", "braylon", False, "Truncated half name missing"),
    ("Kokerlii", "coker lee", True, "Split across transcribed words"),
    ("Drizzt", "drizzit", False, "Extra inserted epenthetic vowel syllable"),
    ("Guenhwyvar", "guinevar", True, "Standard Welsh to English orthography"),
    ("Bruenor", "bruinor", True, "Consistent consonant skeleton and prosody"),
    ("Bruenor", "brunor", True, "Vowel variation in stressed syllable"),
    ("Jarlaxle", "jar laxal", True, "Split across words"),
    ("Allefaero", "allefairo", True, "Diphthong ai vs ae"),
    ("Savahn", "savan", True, "Silent h in open syllable"),
    ("Pwent", "puent", True, "Glide spelling w vs u"),
]


def simple_rule_based_phonemes(text: str) -> str:
    """Lightweight rule-based phonemic approximation for English fantasy terms."""
    t = text.lower().strip()
    t = re.sub(r"[\s\-_']+", " ", t)

    if " " in t:
        return " ".join(simple_rule_based_phonemes(part) for part in t.split())

    t = re.sub(r"ph", "f", t)
    t = re.sub(r"sh", "ʃ", t)
    t = re.sub(r"ch", "tʃ", t)
    t = re.sub(r"th", "θ", t)
    t = re.sub(r"kh", "k", t)
    t = re.sub(r"qu", "kw", t)
    t = re.sub(r"gu(?=[aeiou])", "gw", t)
    t = re.sub(r"c(?=[eiy])", "s", t)
    t = re.sub(r"c(?=[aou]|$|[^aeiouy])", "k", t)
    t = re.sub(r"ck", "k", t)
    t = re.sub(r"zt$", "st", t)
    t = re.sub(r"zz", "z", t)
    t = re.sub(r"ae|ay|ai", "eɪ", t)
    t = re.sub(r"ee|ea|ii", "iː", t)
    t = re.sub(r"oo|ou", "uː", t)
    t = re.sub(r"oa", "oʊ", t)
    t = re.sub(r"wulf", "wʊlf", t)
    t = re.sub(r"wolf", "wʊlf", t)
    return t


def phoneme_similarity(phonemes_a: str, phonemes_b: str) -> float:
    tokens_a = list(phonemes_a)
    tokens_b = list(phonemes_b)
    return SequenceMatcher(None, tokens_a, tokens_b).ratio()


@dataclass
class EvalSummary:
    total: int
    pk_correct: int
    g2p_correct: int
    pk_accuracy: float
    g2p_accuracy: float


def run_evaluation() -> EvalSummary:
    pk_hits = 0
    g2p_hits = 0
    total = len(ORACLE_PAIRS)

    print(f"{'Term':<18} {'Heard':<15} {'Expected':<9} {'PK Score':<9} {'PK Match':<9} {'G2P Match':<10} {'Notes'}")
    print("-" * 88)

    for term, heard, expected, desc in ORACLE_PAIRS:
        pk_score = sound_similarity(term, heard)
        pk_match = pk_score >= 0.86
        if pk_match == expected:
            pk_hits += 1

        p_term = simple_rule_based_phonemes(term)
        p_heard = simple_rule_based_phonemes(heard)
        g2p_score = phoneme_similarity(p_term, p_heard)
        g2p_match = g2p_score >= 0.80
        if g2p_match == expected:
            g2p_hits += 1

        pk_status = "PASS" if pk_match == expected else "FAIL"
        g2p_status = "PASS" if g2p_match == expected else "FAIL"

        print(
            f"{term:<18} {heard:<15} {str(expected):<9} {pk_score:4.2f}     {pk_status:<9} {g2p_status:<10} {desc}"
        )

    print("-" * 88)
    summary = EvalSummary(
        total=total,
        pk_correct=pk_hits,
        g2p_correct=g2p_hits,
        pk_accuracy=pk_hits / total,
        g2p_accuracy=g2p_hits / total,
    )
    print(f"Total pairs evaluated: {total}")
    print(f"Phonetic Key accuracy: {summary.pk_correct}/{total} ({summary.pk_accuracy * 100:.1f}%)")
    print(f"G2P rule accuracy:     {summary.g2p_correct}/{total} ({summary.g2p_accuracy * 100:.1f}%)")
    return summary


if __name__ == "__main__":
    run_evaluation()

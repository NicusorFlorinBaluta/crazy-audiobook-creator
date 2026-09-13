"""Decide pronunciation respellings from what the engine was actually heard to say.

A respelling is a guess until something measures it. The LLM that proposes one
has never heard the voice; it reasons from spelling, and on 2026-09-12 it
proposed 28 respellings for `the-finest-edge-of-twilight-book` of which
**eleven were for names the engine already pronounced correctly** -- `Regis`,
`Wulfgar`, `Drizzt`, `Sylfae`, `Ten-Towns` and more. Applying those would have
damaged working audio.

The evidence is already on disk. Every generated segment is transcribed by
Whisper during validation and the transcript is kept in
`quality_logs.details.transcribed_text`. What Whisper wrote is a phonetic
report of what the engine said: "Cadbury" for `Catti-brie` and "Jesus" for
`Xisis` are failures, and no respelling is needed for a name it renders
faithfully.

Two things make the comparison work, and both were learned by getting them
wrong first:

**Compare sound, not spelling.** Whisper wrote "Wolfgar" for a correctly
spoken `Wulfgar` and "drist" for a correctly spoken `Drizzt`. Scoring those as
misses justified a respelling that would have made both worse.

**Compare the whole term.** `Braelin Janquay` came back as the single word
"braylon". Matching only the first word scored that 94% correct while the
engine was in fact dropping half the name.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

#: A term needs at least this many transcribed lines before its score means
#: anything. Below it the verdict is `insufficient`, never `respell`.
MIN_SAMPLES = 3

#: Share of lines whose transcript sounds like the term.
AGREE_THRESHOLD = 0.70

#: Share taken by the single most common *spelling* Whisper reported.
#:
#: Below this the renderings differ in ways the phonetic key cannot judge,
#: because it discards vowel quality on purpose. `Bruenor` came back as
#: bruinor/brunor/"bryn or"/briennor across 127 lines: one sound group by the
#: key, but "BROO-nor" and "BRIN-or" are different names to a listener. The
#: honest verdict there is `undecided`, not `spoken_correctly` -- and never
#: `mispronounced`, which would ship an unmeasured respelling.
#:
#: Calibrated 2026-09-12 against the two books: Regis 100%, Wulfgar 78%,
#: Ten-Towns 64%, Drizzt 59% (all correct) against Sylfae 41% and Bruenor 26%.
SPELLING_STABLE_THRESHOLD = 0.50

#: Phonetic-key similarity at which two renderings count as the same sound.
MATCH_RATIO = 0.86

_WORD = re.compile(r"[A-Za-z']+")


def phonetic_key(text: str) -> str:
    """Reduce a word to a crude sound skeleton.

    Deliberately lossy: it equates spellings that sound alike (wolfgar/wulfgar,
    drist/drizzt, sylphay/sylfae) so Whisper's orthographic choices stop
    counting as engine errors. It keeps consonant structure, which is what
    actually distinguishes one name from another in these failures.
    """
    word = re.sub(r"[^a-z]", "", text.casefold())
    if not word:
        return ""
    word = word.replace("ph", "f").replace("ck", "k").replace("gh", "g")
    word = re.sub(r"gu(?=[aeiouy])", "gw", word)
    word = re.sub(r"c(?=[eiy])", "s", word)
    word = word.replace("c", "k").replace("q", "k").replace("x", "ks")
    word = word.replace("z", "s").replace("j", "y")
    word = re.sub(r"(.)\1+", r"\1", word)
    # Vowel *presence* is kept; vowel quality is not. Whisper's vowel letters
    # are unreliable, and this is what lets wolfgar match wulfgar.
    word = re.sub(r"[aeiouy]+", "a", word)
    return re.sub(r"(.)\1+", r"\1", word)


def sound_similarity(left: str, right: str) -> float:
    left_key, right_key = phonetic_key(left), phonetic_key(right)
    if not left_key or not right_key:
        return 0.0
    if left_key == right_key:
        return 1.0
    return SequenceMatcher(None, left_key, right_key).ratio()


def _words(text: str) -> list[str]:
    return _WORD.findall(text or "")


def best_matching_span(term: str, transcript_words: list[str]) -> tuple[str, float]:
    """Find the run of transcript words that best matches the whole term."""
    widest = max(1, len(_words(term))) + 1
    best, score = "", 0.0
    for size in range(1, widest + 1):
        for start in range(len(transcript_words) - size + 1):
            chunk = " ".join(transcript_words[start : start + size])
            ratio = sound_similarity(term, chunk)
            if ratio > score:
                best, score = chunk, ratio
    return best, score


@dataclass
class TermEvidence:
    """What the engine was heard to say for one term."""

    term: str
    samples: int = 0
    agreeing: int = 0
    heard: Counter[str] = field(default_factory=Counter)
    sound_groups: Counter[str] = field(default_factory=Counter)

    @property
    def agree_rate(self) -> float:
        return self.agreeing / self.samples if self.samples else 0.0

    @property
    def stability(self) -> float:
        """Share held by the most common *sound*."""
        if not self.samples or not self.sound_groups:
            return 0.0
        return self.sound_groups.most_common(1)[0][1] / self.samples

    @property
    def spelling_stability(self) -> float:
        """Share held by the most common *spelling*."""
        if not self.samples or not self.heard:
            return 0.0
        return self.heard.most_common(1)[0][1] / self.samples

    @property
    def dominant_rendering(self) -> str:
        return self.heard.most_common(1)[0][0] if self.heard else ""

    @property
    def dominant_matches(self) -> bool:
        """Does the commonest thing Whisper wrote sound like the term?

        This, not the average, is the primary test. `Ten-Towns` agreed on only
        64% of its lines -- long lines whose best span landed elsewhere -- but
        the commonest rendering was "ten towns", which is the engine getting it
        right. Averaging alone would have condemned it.
        """
        if not self.heard:
            return False
        return sound_similarity(self.term, self.dominant_rendering) >= MATCH_RATIO

    @property
    def verdict(self) -> str:
        """`spoken_correctly`, `mispronounced`, `undecided`, or `insufficient`.

        Only `mispronounced` justifies applying a respelling. `undecided` means
        the evidence is real but beyond what this comparison can settle, and is
        a question for a listener rather than a licence to guess.
        """
        if self.samples < MIN_SAMPLES:
            return "insufficient"
        if not self.dominant_matches:
            return "mispronounced"
        if self.spelling_stability < SPELLING_STABLE_THRESHOLD:
            return "undecided"
        return "spoken_correctly"

    def as_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "samples": self.samples,
            "agree_rate": round(self.agree_rate, 3),
            "sound_stability": round(self.stability, 3),
            "spelling_stability": round(self.spelling_stability, 3),
            "dominant_rendering": self.dominant_rendering,
            "verdict": self.verdict,
            "heard_as": self.heard.most_common(6),
        }


def measure_terms(
    terms: list[str],
    line_texts: dict[str, str],
    transcripts: dict[str, str],
) -> dict[str, TermEvidence]:
    """Score each term against the transcripts of the lines that contain it.

    `line_texts` maps line id to the written text; `transcripts` maps line id
    to what Whisper heard. Lines with no transcript are simply not evidence.
    """
    results: dict[str, TermEvidence] = {}
    for term in terms:
        probe_words = _words(term)
        if not probe_words:
            continue
        probe = probe_words[0].casefold()
        evidence = TermEvidence(term=term)
        for line_id, source in line_texts.items():
            if probe not in {word.casefold() for word in _words(source)}:
                continue
            transcript = transcripts.get(line_id)
            if not transcript:
                continue
            evidence.samples += 1
            span, score = best_matching_span(term, _words(transcript))
            if span:
                evidence.heard[span.casefold()] += 1
                evidence.sound_groups[phonetic_key(span)] += 1
            if score >= MATCH_RATIO:
                evidence.agreeing += 1
        results[term] = evidence
    return results


def transcripts_for_project(connection: Any, project_id: str) -> dict[str, str]:
    """Read the last-attempt Whisper transcript of every validated segment."""
    transcripts: dict[str, str] = {}
    rows = connection.execute(
        "select line_id, details from quality_logs where project_id=? order by id",
        (project_id,),
    )
    for line_id, details in rows:
        try:
            payload = json.loads(details)
        except (TypeError, ValueError):
            continue
        text = payload.get("transcribed_text")
        if text:
            transcripts[line_id] = text
    return transcripts

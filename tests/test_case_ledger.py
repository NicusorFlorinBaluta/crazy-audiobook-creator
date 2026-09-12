"""The ledger has to keep matching the tests, or it is just a story.

`docs/attribution-case-ledger.md` claims, row by row, that a named test pins a
named case. A document making that claim decays the moment a test is renamed,
split, or quietly deleted -- and then it reads as coverage that does not exist,
which is worse than no ledger.

So the claims are checked. Every row must name a test file that exists, and
that file must mention the case id the row is about.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

LEDGER = Path("docs/attribution-case-ledger.md")
CASE = re.compile(r"^ch\d{2}_\d{4}$")


def _rows() -> list[tuple[str, str]]:
    """(case, test path) for every "Rules in force" row that names a test."""
    out: list[tuple[str, str]] = []
    in_section = False
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_section = line.strip() == "## Rules in force"
            continue
        if not in_section or not line.startswith("|"):
            continue
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
        if len(cells) < 5 or cells[0] in ("case", "---"):
            continue
        out.append((cells[0], cells[4]))
    return out


def test_the_ledger_is_readable_and_populated() -> None:
    assert LEDGER.is_file(), "the ledger is referenced by the docs index"
    rows = _rows()
    assert len(rows) >= 15, f"only {len(rows)} rules parsed; the table shape probably changed"


@pytest.mark.parametrize(("case", "test_path"), _rows(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_row_names_a_test_that_exists(case, test_path) -> None:
    assert test_path.startswith("tests/"), f"{case}: {test_path!r} is not a test path"
    assert Path(test_path).is_file(), f"{case}: {test_path} no longer exists"


@pytest.mark.parametrize(
    ("case", "test_path"),
    [row for row in _rows() if CASE.match(row[0])],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_every_case_is_mentioned_by_its_test(case, test_path) -> None:
    """A row is a claim about coverage. The test has to know the case."""
    body = Path(test_path).read_text(encoding="utf-8")
    assert case in body, f"{test_path} no longer mentions {case}; the ledger row is stale"


def test_rejected_rules_are_recorded_too() -> None:
    """Three of these were re-proposed within a day of rejection."""
    body = LEDGER.read_text(encoding="utf-8")
    assert "## Rules measured and rejected" in body
    for rejected in ("anchored alternation", "block adjudication", "continuation propagation"):
        assert rejected in body, f"{rejected!r} dropped out of the rejected table"


def test_known_bad_cases_are_still_listed_as_known_bad() -> None:
    """`ch01_0291`-`0293` are wrong on purpose. Silence would read as fixed."""
    body = LEDGER.read_text(encoding="utf-8")
    assert "## Cases known wrong and left wrong" in body
    for case in ("ch01_0291", "ch01_0292", "ch01_0293"):
        assert case in body


class TestCiRunsTheWholeSuite:
    """238 tests in 17 files were invisible to CI until 2026-09-12.

    `unittest discover` collects only `unittest.TestCase` subclasses, so every
    file using fixtures or bare test functions -- including all of the ledger's
    guards -- was collected by nothing. pytest collects both.
    """

    WORKFLOW = Path(".github/workflows/ci.yml")

    def test_ci_invokes_pytest(self) -> None:
        body = self.WORKFLOW.read_text(encoding="utf-8")
        assert "python -m pytest tests" in body

    def test_ci_does_not_fall_back_to_unittest_discovery(self) -> None:
        """Checked on the `run:` lines only -- the comment above one says why."""
        commands = [
            line.split("run:", 1)[1]
            for line in self.WORKFLOW.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("run:")
        ]
        offenders = [c for c in commands if "unittest discover" in c]
        assert not offenders, f"unittest discovery silently skips fixture-based files: {offenders}"


class TestANewRuleCannotShipUnlogged:
    """A rule that writes a new provenance must earn a ledger row.

    The ledger is only worth reading if it is complete, and "remember to
    update the doc" is not a mechanism. Every rule added this month introduced
    a new `attribution_resolver` value -- `deterministic_action_beat`,
    `local_qwen_wide`, `constrained_choice`, `deterministic_unique_candidate`
    -- so that value is the signal, and it is one the author cannot avoid
    emitting: without it a line's provenance would not say which layer settled
    it.

    Values that mean "no rule was involved" are exempt below. Adding to that
    list is itself a visible decision in the diff.
    """

    #: Provenances that are not a rule: the script generator's own output, a
    #: human edit, an escalation tier named after its provider, and the legacy
    #: value still read from scripts written before block adjudication was
    #: removed on 2026-09-10.
    NOT_A_RULE = {
        "local",
        "human",
        "local_qwen_micro",
        "local_qwen_block",
        "gemini_api",
        "gemini_api_triage",
        "gemini_api_adjudication",
        "gemini_web",
        "source_parser",
        "identity_consolidation",
    }

    RESOLVER = re.compile(r'attribution_resolver(?:"\])?\s*=\s*"([a-z_]+)"')

    def _written_resolvers(self) -> set[str]:
        found: set[str] = set()
        for root in (Path("brain"), Path("scripts")):
            for path in root.rglob("*.py"):
                found |= set(self.RESOLVER.findall(path.read_text(encoding="utf-8", errors="ignore")))
        return found - self.NOT_A_RULE

    def test_the_scan_finds_the_known_rules(self) -> None:
        """A regex that matches nothing would pass the next test vacuously."""
        found = self._written_resolvers()
        assert "deterministic_action_beat" in found, f"scan looks broken; found {sorted(found)}"
        assert len(found) >= 3

    def test_every_rule_provenance_appears_in_the_ledger(self) -> None:
        body = LEDGER.read_text(encoding="utf-8")
        missing = sorted(r for r in self._written_resolvers() if r not in body)
        assert not missing, (
            f"these resolvers write a provenance no ledger row explains: {missing}. "
            f"Add a row to {LEDGER} with the case that forced the rule and the test that pins it, "
            f"or add the value to NOT_A_RULE if it is not a rule."
        )

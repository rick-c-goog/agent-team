"""Known-answer fixtures for gates (DESIGN.md §5.9).

These are regression tests for *judgement*. A prompt change that quietly stops a checker
noticing an uncited claim is invisible until something ships wrong; a fixture makes it a
failing test.

Each fixture states what the gate is shown and what it must decide. A gate that passes a
case it should reject is a **miss** (dangerous); one that rejects a case it should pass
is a **false alarm** (expensive). Both are reported, because tuning a checker until it
rejects everything is not an improvement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class Fixture:
    name: str
    given: Any                       # what the gate is shown
    must_reject: bool                # the known answer
    because: str = ""                # what the gate is supposed to notice


@dataclass
class FixtureResult:
    fixture: str
    expected_reject: bool
    actual_reject: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.expected_reject == self.actual_reject

    @property
    def kind(self) -> str:
        if self.ok:
            return "ok"
        return "miss" if self.expected_reject else "false alarm"


@dataclass
class GateFixtures:
    gate: str
    results: list[FixtureResult] = field(default_factory=list)

    @property
    def misses(self) -> list[FixtureResult]:
        return [r for r in self.results if r.kind == "miss"]

    @property
    def false_alarms(self) -> list[FixtureResult]:
        return [r for r in self.results if r.kind == "false alarm"]

    @property
    def passed(self) -> bool:
        return not self.misses and not self.false_alarms

    def summary(self) -> str:
        ok = sum(1 for r in self.results if r.ok)
        bits = [f"{self.gate}: {ok}/{len(self.results)} correct"]
        if self.misses:
            bits.append(f"{len(self.misses)} MISSED (let bad work through)")
        if self.false_alarms:
            bits.append(f"{len(self.false_alarms)} false alarm(s)")
        return " — ".join(bits)


def run_fixtures(gate_name: str, judge: Callable[[Any], tuple[bool, list[str]]],
                 fixtures: list[Fixture]) -> GateFixtures:
    """Run `judge` over each fixture. `judge` returns (rejected, reasons)."""
    out = GateFixtures(gate=gate_name)
    for fixture in fixtures:
        try:
            rejected, reasons = judge(fixture.given)
        except Exception as e:
            rejected, reasons = True, [f"judge raised: {type(e).__name__}: {e}"]
        out.results.append(FixtureResult(
            fixture=fixture.name, expected_reject=fixture.must_reject,
            actual_reject=bool(rejected), reasons=list(reasons or []),
        ))
    return out


# --------------------------------------------------------------------------- #
# The platform's own fixtures: the Tester's grounding judgement
# --------------------------------------------------------------------------- #
def grounding_fixtures() -> list[Fixture]:
    """Cases the Tester must get right about evidence (§5.3.1)."""
    from ..models import Artifact, Citation, Passage

    passage = Passage(source_id="s1", doc="handbook.md", locator="# Launches",
                      text="Launch posts must include the registration link.")
    cited = Citation(source_id="s1", doc="handbook.md", locator="# Launches",
                     quote="Launch posts must include the registration link.")

    return [
        Fixture(
            name="uncited claim with knowledge available",
            given={"artifact": Artifact(step=0, content="Launches convert 40% better."),
                   "knowledge": [passage]},
            must_reject=True,
            because="a factual claim with sources available and none cited",
        ),
        Fixture(
            name="properly cited claim",
            given={"artifact": Artifact(step=0,
                                        content="Include the registration link.",
                                        citations=[cited]),
                   "knowledge": [passage]},
            must_reject=False,
            because="the claim is supported by the passage it cites",
        ),
        Fixture(
            name="no knowledge available, no citation expected",
            given={"artifact": Artifact(step=0, content="A short internal note."),
                   "knowledge": []},
            must_reject=False,
            because="nothing to cite; demanding a citation here is a false alarm",
        ),
    ]


def check_grounding(given: dict) -> tuple[bool, list[str]]:
    """The platform's grounding rule, isolated so a fixture can exercise it directly."""
    artifact = given["artifact"]
    knowledge = given.get("knowledge") or []
    if knowledge and not artifact.citations:
        return True, ["knowledge was available for this step but the draft cites none"]
    return False, []


def default_suites() -> list[GateFixtures]:
    """Suites that ship with the platform and should always be green."""
    return [run_fixtures("grounding", check_grounding, grounding_fixtures())]

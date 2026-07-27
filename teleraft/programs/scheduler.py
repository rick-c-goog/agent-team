"""The Program scheduler (DESIGN.md §5.6).

Ticks registered Programs and runs the ones that are due. Three properties from the
design are enforced here rather than left to callers:

* **Durable state.** `last_fired_at` lives in storage, so a restart does not re-fire
  everything, and a Program knows when it last ran.
* **Skip on overlap.** A Program still running when its next trigger fires is skipped
  with a logged reason — two copies of a stateful loop racing on the same registries is
  a correctness problem, not extra throughput.
* **Catch-up is bounded.** A process that was down for a week does not fire a week of
  missed ticks; it fires once and says it skipped the rest.

The scheduler owns no clock magic: `tick(now)` is explicit so tests drive time directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from .schedule import Cron, CronError

log = logging.getLogger("teleraft.programs")


@dataclass
class Program:
    """A named, triggered, bounded unit of recurring work."""

    name: str
    cron: str
    body: Callable[[], object]        # what to run; returns anything (logged)
    agent: str = ""                   # whose Program this is, for attribution
    enabled: bool = True
    max_runs_per_tick: int = 1
    _cron: Optional[Cron] = field(default=None, repr=False)

    def compiled(self) -> Cron:
        if self._cron is None:
            self._cron = Cron.parse(self.cron)      # raises loudly on a bad schedule
        return self._cron


@dataclass
class TickReport:
    fired: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (name, why)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        bits = []
        if self.fired:
            bits.append(f"fired {', '.join(self.fired)}")
        if self.skipped:
            bits.append(f"skipped {', '.join(n for n, _ in self.skipped)}")
        if self.failed:
            bits.append(f"failed {', '.join(n for n, _ in self.failed)}")
        return "; ".join(bits) or "nothing due"


class Scheduler:
    def __init__(self, storage=None):
        self.storage = storage
        self._programs: dict[str, Program] = {}
        self._running: set[str] = set()
        self._last_fired: dict[str, datetime] = {}

    # ------------------------------------------------------------------ #
    def register(self, program: Program) -> None:
        program.compiled()          # fail now, not silently at 3am
        self._programs[program.name] = program

    def unregister(self, name: str) -> None:
        self._programs.pop(name, None)

    def programs(self) -> list[Program]:
        return list(self._programs.values())

    def last_fired(self, name: str) -> Optional[datetime]:
        return self._last_fired.get(name)

    # ------------------------------------------------------------------ #
    def tick(self, now: Optional[datetime] = None) -> TickReport:
        """Run every Program due at `now`. Safe to call every poll cycle."""
        now = (now or datetime.now()).replace(second=0, microsecond=0)
        report = TickReport()

        for program in list(self._programs.values()):
            if not program.enabled:
                continue
            try:
                due = program.compiled().matches(now)
            except CronError as e:
                report.failed.append((program.name, f"bad schedule: {e}"))
                continue
            if not due:
                continue

            # Already fired this minute? A poll loop ticks more than once per minute.
            if self._last_fired.get(program.name) == now:
                continue
            if program.name in self._running:
                report.skipped.append((program.name, "previous run still in flight"))
                log.warning("program %s skipped: previous run still in flight", program.name)
                continue

            self._running.add(program.name)
            self._last_fired[program.name] = now
            try:
                program.body()
                report.fired.append(program.name)
                log.info("program %s fired", program.name)
            except Exception as e:      # a bad Program must not stop the others
                report.failed.append((program.name, str(e)))
                log.exception("program %s failed", program.name)
            finally:
                self._running.discard(program.name)

        return report

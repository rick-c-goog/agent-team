"""Programs: work that outlives a prompt (DESIGN.md §5.6).

A Program is an engineered loop — it runs on a trigger, holds state durably between
runs, is supervised like any other run, and is bounded. Heartbeats are the simplest
Program; the weekly memory consolidation is the first one that ships.
"""

from .schedule import Cron, CronError, next_due, matches
from .scheduler import Program, Scheduler

__all__ = ["Cron", "CronError", "Program", "Scheduler", "matches", "next_due"]

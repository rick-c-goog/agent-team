"""Runtimes — the AI engine that plays each loop role (DESIGN.md §4, runtime model).

The graph engine never talks to a model directly; it asks a ``Runtime`` to play a role
(``plan`` / ``build`` / ``test`` / ``learn``). This keeps the loop logic identical
whether the engine is running against the deterministic MockRuntime (offline) or a live
Claude session on a computer.
"""

from .base import Runtime, RoleRequest
from .mock import MockRuntime

__all__ = ["Runtime", "RoleRequest", "MockRuntime"]

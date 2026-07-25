"""The graph engineering framework and the Anthropic loop (DESIGN.md §5).

Every task runs as a typed state graph whose nodes are the four Anthropic loop roles —
Planner, Orchestrator, Builder, Tester — plus the human gates and the Learn writeback.
The engine gives the loop four properties prompts alone can't: typed shared state,
deterministic routing, checkpoint/resume, and first-class human interrupts.
"""

from .engine import GraphEngine, Interrupt, GRAPH_VERSION

__all__ = ["GraphEngine", "Interrupt", "GRAPH_VERSION"]

"""Agent registry and identity (DESIGN.md §4)."""

from .registry import Registry, AgentConfig, load_agents_from_dir

__all__ = ["Registry", "AgentConfig", "load_agents_from_dir"]

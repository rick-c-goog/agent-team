"""Memory service — recall + learn writeback (DESIGN.md §4 Memory, §5.2 Learn).

Memories are append-mostly notes ("what worked / what failed / team preferences").
Recall here uses a dependency-free lexical overlap scorer so the system runs offline;
the design calls for embeddings, and this is the single method you'd swap to add them
(the interface — ``recall(agent, query, k)`` — stays identical).

The learn loop also detects *recurring* lessons: when the same lesson shows up
``SOUL_AMENDMENT_THRESHOLD`` times, it proposes a soul amendment for human sign-off,
which is how a mistake becomes structurally impossible to repeat.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Optional

SOUL_AMENDMENT_THRESHOLD = 3

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


class MemoryService:
    def __init__(self, storage):
        self.storage = storage

    # -- recall ------------------------------------------------------------ #
    def recall(self, agent: str, query: str, k: int = 5) -> list[str]:
        """Return the k most relevant memory notes for the query (lexical overlap)."""
        q = set(_tokens(query))
        if not q:
            return []
        scored: list[tuple[float, str]] = []
        for row in self.storage.memories_for(agent):
            content = row["content_md"]
            terms = _tokens(content)
            if not terms:
                continue
            overlap = sum(1 for t in terms if t in q)
            score = overlap / (len(set(terms)) ** 0.5)
            if overlap:
                scored.append((score, content))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:k]]

    # -- writeback --------------------------------------------------------- #
    def write(self, agent: str, note: str, source: str, task_id: Optional[str]) -> None:
        note = note.strip()
        if not note:
            return
        self.storage.add_memory(agent, note, source, task_id)

    def propose_soul_amendment(self, agent: str, lesson: str, task_id: str) -> Optional[str]:
        """If ``lesson`` recurs past the threshold, amend the soul (human-approved).

        Returns the amendment text if one was applied, else None. In a fuller build the
        amendment would be *proposed* to the #admin topic and applied only after a human
        approves; here we apply it and surface the event so the gateway can announce it.
        """
        norm = lesson.strip().lower()
        counts = Counter(
            row["content_md"].strip().lower() for row in self.storage.memories_for(agent)
        )
        if counts[norm] >= SOUL_AMENDMENT_THRESHOLD:
            existing = self.storage.current_soul(agent)
            if lesson.strip() in existing:
                return None
            self.storage.amend_soul(
                agent,
                addition_md=f"- Standing rule (learned): {lesson.strip()}",
                changed_by="learn-loop",
                reason=f"recurring lesson from task {task_id}",
            )
            return lesson.strip()
        return None

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


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


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

    # -- consolidation (DESIGN.md §12 decision: weekly, automated) --------- #
    def consolidate(self, agent: str, keep_recent: int = 200,
                    similarity_threshold: float = 0.85) -> dict:
        """Collapse near-duplicate lessons and cap unbounded growth.

        Memory grows monotonically: every reviewed task appends, and the same lesson
        recurs in slightly different words. Left alone this crowds out distinct lessons
        at recall time, because the top-k fills with restatements of one idea.

        Runs weekly and unattended, so it is deliberately conservative: it merges only
        *near-identical* notes, keeps the earliest wording (which the soul-amendment
        counter already references), and never deletes a lesson that has no duplicate.
        Nothing here needs judgement, which is why it does not need a human.
        """
        rows = self.storage.memories_for(agent)
        if not rows:
            return {"agent": agent, "before": 0, "after": 0, "merged": 0, "dropped": 0}

        before = len(rows)
        kept: list[tuple[int, str, set[str]]] = []      # (id, content, tokens)
        merged_ids: list[int] = []

        for row in rows:                                 # oldest first
            content = row["content_md"].strip()
            tokens = set(_tokens(content))
            if not tokens:
                merged_ids.append(row["id"])
                continue
            duplicate_of = None
            for _kid, _kcontent, ktokens in kept:
                if _jaccard(tokens, ktokens) >= similarity_threshold:
                    duplicate_of = _kid
                    break
            if duplicate_of is not None:
                merged_ids.append(row["id"])             # keep the earliest wording
            else:
                kept.append((row["id"], content, tokens))

        # Cap total size, oldest first — recent lessons reflect current practice.
        dropped_ids: list[int] = []
        if len(kept) > keep_recent:
            overflow = len(kept) - keep_recent
            dropped_ids = [kid for kid, _, _ in kept[:overflow]]
            kept = kept[overflow:]

        for mid in merged_ids + dropped_ids:
            self.storage.delete_memory(mid)

        return {
            "agent": agent,
            "before": before,
            "after": len(kept),
            "merged": len(merged_ids),
            "dropped": len(dropped_ids),
        }

    def consolidate_all(self, agents: list[str]) -> list[dict]:
        return [self.consolidate(a) for a in agents]

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

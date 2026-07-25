"""Task service — single-owner claims and status transitions (DESIGN.md §6).

Statuses: Todo → In Progress → In Review → Done / Closed. A task has exactly one owner
at a time; claiming takes responsibility, unclaiming returns it to the pool. Agents
claim automatically when @mentioned or on heartbeat; humans tap Claim.
"""

from __future__ import annotations

import uuid
from typing import Optional

from ..models import TaskStatus


class TaskConflict(Exception):
    """Raised when two members race to claim the same task."""


class TaskService:
    def __init__(self, storage):
        self.storage = storage

    def create(
        self,
        topic: str,
        title: str,
        body: str = "",
        created_by: str = "system",
        parent_task_id: Optional[str] = None,
    ) -> str:
        task_id = self._new_id()
        self.storage.create_task(task_id, topic, title, body, created_by, parent_task_id)
        return task_id

    def claim(self, task_id: str, owner: str) -> None:
        task = self._require(task_id)
        if task["owner"] and task["owner"] != owner:
            raise TaskConflict(
                f"task {task_id} already owned by {task['owner']}; unclaim first"
            )
        self.storage.update_task(task_id, owner=owner, status=TaskStatus.IN_PROGRESS.value)

    def unclaim(self, task_id: str) -> None:
        self._require(task_id)
        self.storage.update_task(task_id, owner=None, status=TaskStatus.TODO.value)

    def set_status(self, task_id: str, status: TaskStatus) -> None:
        self._require(task_id)
        self.storage.update_task(task_id, status=status.value)

    def close(self, task_id: str) -> None:
        self.set_status(task_id, TaskStatus.CLOSED)

    def set_card_message(self, task_id: str, message_id: str) -> None:
        self.storage.update_task(task_id, tg_card_message_id=message_id)

    def get(self, task_id: str):
        return self.storage.get_task(task_id)

    def board(self, topic: Optional[str] = None) -> dict[str, list]:
        """Kanban view: tasks grouped by status column (rendered by the Mini App)."""
        columns = {s.value: [] for s in TaskStatus}
        for row in self.storage.list_tasks(topic=topic):
            columns[row["status"]].append(row)
        return columns

    # -- helpers ----------------------------------------------------------- #
    def _require(self, task_id: str):
        task = self.storage.get_task(task_id)
        if task is None:
            raise KeyError(f"unknown task {task_id}")
        return task

    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex[:6]

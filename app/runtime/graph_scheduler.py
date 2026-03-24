from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Callable


class TaskGraphScheduler:
    TERMINAL_STATES = {"completed", "failed", "blocked", "cancelled", "waiting_approval", "queued", "running"}

    def __init__(self, max_workers: int = 2):
        self.max_workers = max(1, max_workers)

    def execute(
        self,
        nodes: list,
        handler: Callable[[object, list[dict]], dict],
        *,
        initial_states: dict[str, str] | None = None,
        initial_results: dict[str, dict] | None = None,
    ) -> dict[str, dict]:
        node_map = {node.id: node for node in nodes}
        dependents: dict[str, list[str]] = {node.id: [] for node in nodes}
        for node in nodes:
            for dependency_id in node.depends_on:
                if dependency_id in dependents:
                    dependents[dependency_id].append(node.id)

        states = {node.id: node.status for node in nodes}
        if initial_states:
            states.update(initial_states)
        results: dict[str, dict] = dict(initial_results or {})
        queued: set[str] = set()
        blocked: set[str] = set()

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            pending: dict[Future, str] = {}
            while True:
                ready_nodes = [
                    node
                    for node in nodes
                    if node.id not in queued
                    and node.id not in blocked
                    and states.get(node.id) not in {"completed", "failed"}
                    and self._dependencies_complete(node, states)
                ]
                for node in ready_nodes:
                    if len(pending) >= self.max_workers:
                        break
                    queued.add(node.id)
                    states[node.id] = "running"
                    dependency_results = [results[dependency_id] for dependency_id in node.depends_on if dependency_id in results]
                    pending[pool.submit(handler, node, dependency_results)] = node.id

                if not pending:
                    unresolved = [
                        node_id
                        for node_id, state in states.items()
                        if state not in {"completed", "failed", "blocked"}
                    ]
                    if not unresolved:
                        break
                    for node_id in unresolved:
                        states[node_id] = "blocked"
                        blocked.add(node_id)
                    break

                done, _ = wait(pending.keys(), return_when=FIRST_COMPLETED)
                for future in done:
                    node_id = pending.pop(future)
                    try:
                        result = future.result() or {}
                        status = result.get("status", "completed")
                        states[node_id] = status
                        results[node_id] = result
                        if status in {"failed", "blocked"}:
                            self._propagate_blocked(node_id, dependents, states, blocked, results, reason=result.get("error"))
                    except Exception as exc:
                        states[node_id] = "failed"
                        results[node_id] = {"status": "failed", "error": str(exc)}
                        self._propagate_blocked(node_id, dependents, states, blocked, results, reason=str(exc))

        return {"states": states, "results": results}

    @staticmethod
    def _dependencies_complete(node, states: dict[str, str]) -> bool:
        if not node.depends_on:
            return True
        return all(states.get(dependency_id) == "completed" for dependency_id in node.depends_on)

    def _propagate_blocked(
        self,
        node_id: str,
        dependents: dict[str, list[str]],
        states: dict[str, str],
        blocked: set[str],
        results: dict[str, dict],
        *,
        reason: str | None = None,
    ) -> None:
        for dependent_id in dependents.get(node_id, []):
            if states.get(dependent_id) in {"completed", "failed", "blocked"}:
                continue
            states[dependent_id] = "blocked"
            blocked.add(dependent_id)
            results.setdefault(dependent_id, {"status": "blocked", "blocked_by": node_id, "error": reason})
            self._propagate_blocked(dependent_id, dependents, states, blocked, results, reason=reason)

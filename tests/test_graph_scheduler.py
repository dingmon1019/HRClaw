from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time

from app.runtime.graph_scheduler import TaskGraphScheduler


@dataclass
class _Node:
    id: str
    status: str
    depends_on: list[str] = field(default_factory=list)


def test_graph_scheduler_respects_dependencies_and_merge_gate():
    nodes = [
        _Node(id="planner-a", status="ready"),
        _Node(id="planner-b", status="ready"),
        _Node(id="merge", status="blocked", depends_on=["planner-a", "planner-b"]),
    ]
    execution_order: list[str] = []

    def handler(node, dependency_results):
        execution_order.append(node.id)
        return {"status": "completed", "node": node.id}

    result = TaskGraphScheduler(max_workers=2).execute(nodes, handler)

    assert result["states"]["planner-a"] == "completed"
    assert result["states"]["planner-b"] == "completed"
    assert result["states"]["merge"] == "completed"
    assert execution_order.index("merge") > execution_order.index("planner-a")
    assert execution_order.index("merge") > execution_order.index("planner-b")


def test_graph_scheduler_supports_bounded_parallel_ready_nodes():
    nodes = [
        _Node(id="planner-a", status="ready"),
        _Node(id="planner-b", status="ready"),
    ]
    active = 0
    peak = 0
    lock = threading.Lock()

    def handler(node, dependency_results):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return {"status": "completed", "node": node.id}

    result = TaskGraphScheduler(max_workers=2).execute(nodes, handler)

    assert result["states"]["planner-a"] == "completed"
    assert result["states"]["planner-b"] == "completed"
    assert peak >= 2


def test_graph_scheduler_blocks_dependents_after_failure():
    nodes = [
        _Node(id="planner-a", status="ready"),
        _Node(id="review-a", status="blocked", depends_on=["planner-a"]),
        _Node(id="merge", status="blocked", depends_on=["review-a"]),
    ]

    def handler(node, dependency_results):
        if node.id == "planner-a":
            raise RuntimeError("branch failure")
        return {"status": "completed"}

    result = TaskGraphScheduler(max_workers=1).execute(nodes, handler)

    assert result["states"]["planner-a"] == "failed"
    assert result["states"]["review-a"] == "blocked"
    assert result["states"]["merge"] == "blocked"

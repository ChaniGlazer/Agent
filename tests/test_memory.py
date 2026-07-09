"""Unit tests for agent.memory.Memory."""

from __future__ import annotations

import json
from pathlib import Path

from agent.memory import Memory


def test_record_appends_history_and_tracks_last_action() -> None:
    memory = Memory(goal="do the thing")
    assert memory.last_action is None

    memory.record(action="click", selector="#go", text=None, success=True, message="ok", error=None, duration_seconds=0.1)
    memory.record(action="fill", selector="#name", text="Alice", success=False, message="failed", error="timeout", duration_seconds=0.2)

    assert len(memory.history) == 2
    assert memory.last_action is not None
    assert memory.last_action.action == "fill"
    assert memory.errors == ["timeout"]


def test_history_summary_reflects_recent_actions() -> None:
    memory = Memory(goal="goal")
    memory.record(action="click", selector="#a", text=None, success=True, message="clicked", error=None, duration_seconds=0.0)
    summary = memory.history_summary()
    assert "click" in summary
    assert "#a" in summary
    assert "success" in summary


def test_history_summary_empty() -> None:
    memory = Memory(goal="goal")
    assert "no actions performed yet" in memory.history_summary()


def test_set_result_stores_intermediate_value() -> None:
    memory = Memory(goal="goal")
    memory.set_result("username", "alice")
    assert memory.intermediate_results["username"] == "alice"


def test_save_writes_json_file(tmp_path: Path) -> None:
    memory = Memory(goal="goal")
    memory.record(action="read", selector="#x", text=None, success=True, message="read value", error=None, duration_seconds=0.05)

    path = memory.save(tmp_path)
    assert path.exists()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["goal"] == "goal"
    assert len(data["history"]) == 1
    assert data["history"][0]["action"] == "read"

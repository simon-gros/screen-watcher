"""Test-wide safety net: no test may touch the real `state/` directory.

Running the suite used to corrupt live runtime data. Three counter tests
called `evaluate()`, which calls `log_counter()` internally, and none of
them redirected `COUNTER_LOG` - so every run appended test totals (455,
910, 1000055) to the real `state/counters.jsonl`. Because `load_counter`
takes the *last* recorded row, that silently overwrote the player's actual
coin total and reset their next milestone.

That is a nasty failure mode: the tests all passed while quietly
destroying the data they were meant to protect, and the damage only showed
up when the player asked why their counter had reverted.

Redirecting the paths once, for every test, is the fix. An individual test
that wants to inspect a log still monkeypatches its own `tmp_path`; this
only guarantees that forgetting to do so cannot reach real files.
"""

import pytest

import watcher


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Point every runtime-state path at a per-test temporary directory."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(watcher, "STATE_DIR", state)
    monkeypatch.setattr(watcher, "COUNTER_LOG", state / "counters.jsonl")
    monkeypatch.setattr(watcher, "OCCUPANCY_LOG", state / "occupancy.jsonl")
    monkeypatch.setattr(watcher, "ALERT_LOG", state / "alerts.jsonl")
    monkeypatch.setattr(watcher, "PID_FILE", state / "watcher.pid")
    return state

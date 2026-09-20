import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from screen_watcher.capture import _FRAME_CACHE, capture_array
from screen_watcher.cli import next_poll_deadline
from screen_watcher.config import ConfigError, build_rules, load_config, validate_config
from screen_watcher.events import Alert
from screen_watcher.notifications import JsonlNotifier
from screen_watcher.regions import Region
from screen_watcher.replay import replay_document, replay_file
from screen_watcher.rules import ActivityRule, VisualRule
from screen_watcher.signals import mean_abs_diff, norm_line


def valid_config():
    return {
        "schema_version": 1,
        "profile_version": 1,
        "profile_type": "skill",
        "skill": "example",
        "window": {"wm_class": "example"},
        "interval": 1.5,
        "regions": {
            "panel": {
                "anchor": "top-left",
                "dx": 0,
                "dy": 0,
                "w": 100,
                "h": 100,
            }
        },
        "rules": [
            {
                "name": "panel_changed",
                "kind": "change",
                "region": "panel",
            }
        ],
    }


def test_region_resolves_bottom_right_anchor():
    region = Region("bottom-right", -13, -38, 330, 560)
    assert region.resolve((1920, 1080)) == (1577, 482, 330, 560)


def test_region_resolution_is_clamped_to_window():
    region = Region("top-left", -20, -10, 500, 400)
    assert region.resolve((320, 240)) == (0, 0, 320, 240)


def test_normalized_chat_lines_ignore_ocr_punctuation():
    assert norm_line("[11:03:15] You catch a fish!") == "110315youcatchafish"
    assert norm_line("(11:03:15] You catch a fish!") == "110315youcatchafish"


def test_mean_abs_diff_handles_identical_and_changed_frames():
    frame = np.zeros((2, 2), dtype=np.uint8)
    changed = np.full((2, 2), 4, dtype=np.uint8)
    assert mean_abs_diff(frame, frame) == 0.0
    assert mean_abs_diff(frame, changed) == 4.0


def test_one_full_frame_is_reused_for_multiple_regions(monkeypatch):
    import screen_watcher.capture as capture_module

    calls = []

    def fake_capture(wid, box, out, resize=None):
        calls.append((wid, box))
        image = Image.new("RGB", (8, 8), (10, 20, 30))
        image.save(out, format="PPM")
        return out

    monkeypatch.setattr(capture_module, "capture", fake_capture)
    _FRAME_CACHE.clear()

    first = capture_array("window", (0, 0, 4, 4), cycle=7)
    second = capture_array("window", (4, 4, 4, 4), cycle=7)

    assert len(calls) == 1
    assert calls[0][1] is None
    assert first.shape == (4, 4, 3)
    assert second.shape == (4, 4, 3)


def test_config_validation_accepts_typed_visual_rule():
    cfg = valid_config()
    validate_config(cfg)
    rules = build_rules(cfg)
    assert isinstance(rules[0], VisualRule)


def test_config_validation_rejects_unknown_rule_field_with_hint():
    cfg = valid_config()
    cfg["rules"][0]["cooldwon"] = 10
    with pytest.raises(ConfigError, match="did you mean 'cooldown'"):
        validate_config(cfg)


def test_config_requires_schema_version():
    cfg = valid_config()
    del cfg["schema_version"]
    with pytest.raises(ConfigError, match="schema_version"):
        validate_config(cfg)


def test_activity_replay_uses_live_rule_logic():
    document = {
        "profile": "fishing",
        "rule": {
            "name": "fishing_stopped",
            "kind": "activity",
            "region": "chat",
            "pattern": "you catch",
            "stop_seconds": 5,
            "cooldown": 0,
        },
        "observations": [
            {"t": 1, "text": "You catch a fish."},
            {"t": 2, "text": "You catch a fish."},
            {"t": 7, "text": "The weather changes."},
        ],
    }
    result = replay_document(document)
    assert len(result.alerts) == 1
    assert result.alerts[0].rule_name == "fishing_stopped"
    assert result.alerts[0].evidence["quiet_seconds"] == 6.0


def test_fixture_replay_is_deterministic():
    fixture = Path(__file__).parent / "fixtures" / "activity-replay.json"
    result = replay_file(fixture)
    assert [alert.rule_name for alert in result.alerts] == ["fishing_stopped"]


def test_jsonl_notifier_records_evidence(tmp_path):
    path = tmp_path / "alerts.jsonl"
    notifier = JsonlNotifier(path)
    notifier.deliver(
        Alert(
            rule_name="test",
            title="Test",
            body="Triggered",
            profile="fixture",
            confidence=0.9,
            evidence={"diff": 3.2},
        )
    )
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["profile"] == "fixture"
    assert row["evidence"] == {"diff": 3.2}


def test_next_poll_deadline_skips_missed_intervals():
    assert next_poll_deadline(10.0, 1.5, 10.1) == 11.5
    assert next_poll_deadline(10.0, 1.5, 15.0) == 16.0


def test_bundled_profiles_validate_and_are_versioned():
    fishing = load_config(Path("profiles/fishing.json"))
    thieving = load_config(Path("profiles/thieving.json"))
    assert fishing["schema_version"] == 1
    assert thieving["schema_version"] == 1
    assert fishing["profile_version"] >= 1
    assert thieving["profile_version"] >= 1

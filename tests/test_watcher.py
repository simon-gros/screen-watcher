import numpy as np

import pytest

from watcher import (Region, Rule, load_config, mean_abs_diff, norm_line,
                     validate_config)


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


def test_rule_fire_returns_alert_without_notifying(monkeypatch):
    def fail_notify(*args, **kwargs):
        raise AssertionError("rule evaluation must not notify")

    monkeypatch.setattr("watcher.notify", fail_notify)
    alert = Rule(name="test", kind="change", region="panel").fire(
        100.0, "changed")

    assert alert.title == "test"
    assert alert.body == "changed"
    assert alert.rule_name == "test"


def test_profile_identity_is_written_to_alert_log(tmp_path, monkeypatch):
    import watcher

    watcher.ACTIVE_SKILL = "fishing"
    monkeypatch.setattr(watcher, "STATE_DIR", tmp_path)
    monkeypatch.setattr(watcher, "ALERT_LOG", tmp_path / "alerts.jsonl")

    watcher.log_alert("spot_stopped", "Fishing stopped", "Check the spot")

    record = (tmp_path / "alerts.jsonl").read_text().strip()
    assert '"skill": "fishing"' in record


def test_claim_singleton_reclaims_pid_reused_by_unrelated_process(
        tmp_path, monkeypatch):
    import watcher

    pid_file = tmp_path / "watcher.pid"
    pid_file.write_text("1234")
    monkeypatch.setattr(watcher, "PID_FILE", pid_file)
    monkeypatch.setattr(watcher.os, "getpid", lambda: 5678)
    monkeypatch.setattr(watcher, "_process_identity", lambda pid: None)

    watcher.claim_singleton()

    assert pid_file.read_text() == "5678"


def test_claim_singleton_rejects_verified_watcher(tmp_path, monkeypatch):
    import watcher

    pid_file = tmp_path / "watcher.pid"
    pid_file.write_text("1234")
    monkeypatch.setattr(watcher, "PID_FILE", pid_file)
    monkeypatch.setattr(watcher.os, "getpid", lambda: 5678)
    monkeypatch.setattr(watcher, "_process_identity",
                        lambda pid: ("python watcher.py watch", 42))

    with pytest.raises(SystemExit, match="already running"):
        watcher.claim_singleton()


def valid_config():
    return {
        "window": {"wm_class": "example"},
        "interval": 1.5,
        "regions": {
            "panel": {"anchor": "top-left", "dx": 0, "dy": 0, "w": 100, "h": 100}
        },
        "rules": [
            {"name": "panel_changed", "kind": "change", "region": "panel"}
        ],
    }


def test_validate_config_accepts_valid_configuration():
    validate_config(valid_config())

    config = valid_config()
    config["profile_type"] = "boss"
    validate_config(config)


def test_skill_profiles_load_and_validate():
    assert load_config("profiles/fishing.json")["skill"] == "fishing"
    assert load_config("profiles/thieving.json")["skill"] == "thieving"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"rules": [{"name": "bad", "kind": "change", "region": "missing"}]},
         "unknown region"),
        ({"rules": [{"name": "bad", "kind": "unknown", "region": "panel"}]},
         "unknown kind"),
        ({"rules": [{"name": "bad", "kind": "ocr", "region": "panel",
                     "pattern": "["}]}, "invalid pattern"),
    ],
)
def test_validate_config_rejects_invalid_rules(change, message):
    config = valid_config()
    config.update(change)

    with pytest.raises(ValueError, match=message):
        validate_config(config)


def test_validate_config_rejects_unknown_profile_type():
    config = valid_config()
    config["profile_type"] = "combat"

    with pytest.raises(ValueError, match="profile_type"):
        validate_config(config)

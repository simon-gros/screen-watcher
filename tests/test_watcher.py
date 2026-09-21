import builtins
import json
import os
import re
from pathlib import Path
import numpy as np

import pytest

import watcher
from watcher import (Region, Rule, _FRAME_CACHE, capture_array, load_config,
                     mean_abs_diff, norm_line, validate_config)


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


def test_capture_array_is_reused_within_a_cycle(monkeypatch, tmp_path):
    import watcher
    from PIL import Image

    calls = []

    def fake_capture(wid, box, out, resize=None):
        calls.append(out)
        Image.new("RGB", (2, 2), (1, 2, 3)).save(out, format="PPM")
        return out

    monkeypatch.setattr(watcher, "capture", fake_capture)
    _FRAME_CACHE.clear()

    first = capture_array("window", (0, 0, 2, 2), cycle=7)
    second = capture_array("window", (0, 0, 2, 2), cycle=7)

    assert len(calls) == 1
    assert first is second


def test_rule_fire_returns_alert_without_notifying(monkeypatch):
    def fail_notify(*args, **kwargs):
        raise AssertionError("rule evaluation must not notify")

    monkeypatch.setattr("watcher.notify", fail_notify)
    alert = Rule(name="test", kind="change", region="panel").fire(
        100.0, "changed")

    assert alert.title == "test"
    assert alert.body == "changed"
    assert alert.rule_name == "test"


def test_rule_fire_uses_configured_body_and_keeps_source():
    rule = Rule(name="test", kind="ocr", region="panel",
                alert_body="A better message: {line}")

    alert = rule.fire(100.0, "fallback", source_text="Matched OCR line")

    assert alert.body == "A better message: Matched OCR line"
    assert alert.source_text == "Matched OCR line"


def test_rule_fire_falls_back_to_detector_body_without_configuration():
    rule = Rule(name="test", kind="ocr", region="panel")

    alert = rule.fire(100.0, "Matched OCR line", source_text="Matched OCR line")

    assert alert.body == "Matched OCR line"
    assert alert.source_text == "Matched OCR line"


def test_rule_fire_bad_template_falls_back_instead_of_dropping_alert(capsys):
    rule = Rule(name="test", kind="ocr", region="panel",
                alert_body="Missing value: {does_not_exist}")

    alert = rule.fire(100.0, "Detector fallback")

    assert alert.body == "Detector fallback"
    assert "invalid alert body template" in capsys.readouterr().err


def test_item_count_out_state_uses_separate_template(monkeypatch):
    rule = Rule(
        name="urns", kind="item_count", region="backpack", item="urns",
        warn_below=1, out_below=0, confirm_seconds=0, cooldown=0,
        alert_body="{n} {noun} left.",
        out_alert_body="No urns remain. Restock.",
    )
    region = Region("top-left", 0, 0, 10, 10, (0, 0, 5, 5, 2, 2))
    monkeypatch.setattr(
        watcher, "capture_array",
        lambda *args, **kwargs: np.zeros((10, 10, 3), dtype=np.int16),
    )
    monkeypatch.setattr(watcher, "count_by_colour", lambda *args, **kwargs: 0)

    assert watcher._eval_item_count(
        rule, "w", region, (0, 0, 10, 10), 100.0, 1) is None
    alert = watcher._eval_item_count(
        rule, "w", region, (0, 0, 10, 10), 101.0, 2)

    assert alert is not None
    assert alert.body == "No urns remain. Restock."


def test_profile_identity_is_written_to_alert_log(tmp_path, monkeypatch):
    import watcher

    watcher.ACTIVE_SKILL = "fishing"
    monkeypatch.setattr(watcher, "STATE_DIR", tmp_path)
    monkeypatch.setattr(watcher, "ALERT_LOG", tmp_path / "alerts.jsonl")

    watcher.log_alert("spot_stopped", "Fishing stopped", "Check the spot")

    record = (tmp_path / "alerts.jsonl").read_text().strip()
    assert '"skill": "fishing"' in record


def test_alert_log_records_source_text_when_available(tmp_path, monkeypatch):
    watcher.ACTIVE_SKILL = "fishing"
    monkeypatch.setattr(watcher, "STATE_DIR", tmp_path)
    monkeypatch.setattr(watcher, "ALERT_LOG", tmp_path / "alerts.jsonl")

    watcher.log_alert("level_up", "Level up", "Configured copy",
                      source_text="Congratulations, you've advanced.")

    record = (tmp_path / "alerts.jsonl").read_text().strip()
    assert '"body": "Configured copy"' in record
    assert '"source_text": "Congratulations, you\'ve advanced."' in record


def test_alert_log_omits_source_text_for_non_ocr_alerts(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "STATE_DIR", tmp_path)
    monkeypatch.setattr(watcher, "ALERT_LOG", tmp_path / "alerts.jsonl")

    watcher.log_alert("pack_filling", "Pack full", "Configured copy")

    record = (tmp_path / "alerts.jsonl").read_text().strip()
    assert "source_text" not in record


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


def test_compatibility_configs_match_profiles():
    root = Path(__file__).resolve().parents[1]
    assert (root / "config.json").read_bytes() == (
        root / "profiles/thieving.json").read_bytes()
    assert (root / "config.fishing.json").read_bytes() == (
        root / "profiles/fishing.json").read_bytes()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"rules": [{"name": "bad", "kind": "change", "region": "missing"}]},
         "unknown region"),
        ({"rules": [{"name": "bad", "kind": "unknown", "region": "panel"}]},
         "unknown kind"),
        ({"rules": [{"name": "bad", "kind": "ocr", "region": "panel",
                     "pattern": "["}]}, "invalid pattern"),
        ({"rules": [{"name": "bad", "kind": "counter", "region": "panel"}]},
         "requires pattern"),
        ({"rules": [{"name": "bad", "kind": "counter", "region": "panel",
                     "pattern": r"coins added"}]},
         "needs a capture group"),
        ({"rules": [{"name": "bad", "kind": "loot", "region": "panel"}]},
         "requires item_pattern"),
        ({"rules": [{"name": "bad", "kind": "stack", "region": "panel"}]},
         "requires a region grid"),
        ({"rules": [{"name": "bad", "kind": "change", "region": "panel",
                     "typo_option": 1}]}, "unknown option"),
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


def test_validate_config_rejects_non_string_alert_body():
    config = valid_config()
    config["rules"][0]["alert_body"] = 123

    with pytest.raises(ValueError, match="alert_body"):
        validate_config(config)


def test_validate_config_rejects_non_string_out_alert_body():
    config = valid_config()
    config["rules"][0]["out_alert_body"] = 123

    with pytest.raises(ValueError, match="out_alert_body"):
        validate_config(config)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("enabled", 1, "enabled must be boolean"),
        ("timeout_ms", 1.5, "timeout_ms must be an integer"),
        ("capacity", 0, "capacity must be positive"),
        ("urgency", "urgent", "urgency must be"),
        ("step", 0, "step must be positive"),
    ],
)
def test_validate_config_rejects_invalid_rule_field_types(field, value, message):
    config = valid_config()
    config["rules"][0][field] = value

    with pytest.raises(ValueError, match=message):
        validate_config(config)


def test_validate_config_rejects_non_integer_region_geometry():
    config = valid_config()
    config["regions"]["panel"]["dx"] = 1.5

    with pytest.raises(ValueError, match="dx must be an integer"):
        validate_config(config)


def test_profiles_have_configured_copy_for_representative_rules():
    fishing = load_config(Path("profiles/fishing.json"))
    thieving = load_config(Path("profiles/thieving.json"))
    fishing_rules = {r["name"]: r for r in fishing["rules"]}
    thieving_rules = {r["name"]: r for r in thieving["rules"]}

    assert "slots left" in fishing_rules["pack_nearly_full"]["alert_body"]
    assert "{n}" in fishing_rules["urns_carried"]["alert_body"]
    assert "No urns remain" in fishing_rules["urns_carried"]["out_alert_body"]
    assert "nearing the end" in fishing_rules["urn_full"]["alert_body"]
    assert "{item}" in thieving_rules["loot_drop"]["alert_body"]
    assert "{total}" in thieving_rules["coin_milestone"]["alert_body"]
    assert "{gone:.0f}" in thieving_rules["activity_icon_gone"]["alert_body"]

    coin_rule = Rule(**{
        key: value for key, value in thieving_rules["coin_milestone"].items()
        if not key.startswith("_")
    })
    alert = coin_rule.fire(
        100.0, "1,000,000 coins have been added to your money pouch.",
        source_text="1,000,000 coins have been added to your money pouch.",
        total="1,000,000", n=1, step="1,000,000")
    assert "1,000,000 coins pickpocketed" in alert.body
    assert alert.source_text.startswith("1,000,000 coins")


def test_validate_config_rejects_invalid_inventory_mode():
    config = valid_config()
    config["regions"]["panel"]["grid"] = {
        "x0": 0, "y0": 0, "cell_w": 10, "cell_h": 10, "cols": 2, "rows": 2
    }
    config["rules"] = [{
        "name": "bad", "kind": "inventory", "region": "panel", "mode": "fast"
    }]

    with pytest.raises(ValueError, match="mode must be"):
        validate_config(config)


def _rule(**kw):
    base = dict(name="t", kind="loot", region="chat_tail")
    base.update(kw)
    return watcher.Rule(**base)


def test_activity_started_idle_never_reports_stopped(monkeypatch):
    """Starting while idle must not invent an activity timestamp."""
    monkeypatch.setattr(watcher, "ocr_cached", lambda *args, **kwargs: "")
    rule = _rule(kind="activity", pattern=r"you catch a", stop_seconds=5,
                 cooldown=0)

    assert watcher._eval_activity(rule, "w", (0, 0, 1, 1), 100.0, 1) is None
    assert rule._primed is True
    assert rule._last_activity == 0.0

    assert watcher._eval_activity(rule, "w", (0, 0, 1, 1), 110.0, 2) is None
    assert rule._last_activity == 0.0


def test_activity_ignores_matching_startup_scrollback(monkeypatch):
    """An old visible catch must not start a new post-launch stop timer."""
    pages = {
        1: "[12:00:00] You catch a trout.",
        2: "[12:00:00] You catch a trout.",
    }
    monkeypatch.setattr(
        watcher, "ocr_cached",
        lambda wid, box, cycle, psm=6: pages.get(cycle, ""),
    )
    rule = _rule(kind="activity", pattern=r"you catch a", stop_seconds=5,
                 cooldown=0)

    assert watcher._eval_activity(rule, "w", (0, 0, 1, 1), 100.0, 1) is None
    assert rule._last_activity == 0.0
    assert watcher._eval_activity(rule, "w", (0, 0, 1, 1), 110.0, 2) is None
    assert rule._last_activity == 0.0


def test_activity_reports_stop_only_after_matching_activity(monkeypatch):
    """A stop clock starts only after a matching activity line is observed."""
    text_by_cycle = {
        1: "",
        2: "[12:00:01] You catch a trout.",
        3: "[12:00:01] You catch a trout.",
    }
    monkeypatch.setattr(
        watcher, "ocr_cached",
        lambda wid, box, cycle, psm=6: text_by_cycle.get(cycle, ""),
    )
    rule = _rule(kind="activity", pattern=r"you catch a", stop_seconds=5,
                 cooldown=0)

    assert watcher._eval_activity(rule, "w", (0, 0, 1, 1), 100.0, 1) is None
    assert watcher._eval_activity(rule, "w", (0, 0, 1, 1), 101.0, 2) is None
    assert rule._last_activity == 101.0

    alert = watcher._eval_activity(rule, "w", (0, 0, 1, 1), 106.0, 3)
    assert alert is not None
    assert "No matching activity" in alert.body


def test_loot_ignores_currency_and_reports_item():
    """Coins arrive ~76% of successes; only named drops should alert."""
    rule = _rule(
        item_pattern=r"extra fine sand|sealed clue scroll \(elite\)",
        ignore_pattern=r"coins have been added|you pick the target",
    )
    rule._primed = True
    lines = [
        "[20:49:08] 455 coins have been added to your money pouch.",
        "[20:49:09] You pick the target's pocket.",
        "[20:50:01] You steal Extra fine sand.",
    ]
    hits = []
    for line in lines:
        key = watcher.norm_line(line)
        if re.search(rule.ignore_pattern, line, re.I):
            rule._seen.add(key)
            continue
        m = re.search(rule.item_pattern, line, re.I)
        if m:
            hits.append(m.group(0))
    assert hits == ["Extra fine sand"]


def test_counter_fires_once_per_milestone():
    """2,900 pickpockets at 455 coins must yield exactly one 1M alert."""
    rule = _rule(kind="counter", step=1_000_000)
    fired = []
    for _ in range(2900):
        rule._total += 455
        reached = rule._total // int(rule.step)
        if reached > rule._milestone:
            rule._milestone = reached
            fired.append(reached)
    assert fired == [1]
    assert rule._total == 1_319_500


def test_counter_persists_across_restart(tmp_path, monkeypatch):
    """Counters use wall time and remain isolated by profile."""
    monkeypatch.setattr(watcher, "STATE_DIR", tmp_path)
    monkeypatch.setattr(watcher, "COUNTER_LOG", tmp_path / "counters.jsonl")
    watcher.log_counter("coin_milestone", 987_654, skill="thieving",
                        wall_time=1_700_000_000.0)
    watcher.log_counter("coin_milestone", 123, skill="fishing",
                        wall_time=1_700_000_001.0)

    assert watcher.load_counter("coin_milestone", skill="thieving") == 987_654
    assert watcher.load_counter("coin_milestone", skill="fishing") == 123
    assert watcher.load_counter("never_ran", skill="thieving") == 0

    row = json.loads((tmp_path / "counters.jsonl").read_text().splitlines()[0])
    assert row["t"] == 1_700_000_000.0
    assert row["skill"] == "thieving"


def test_occupancy_history_uses_wall_time_and_profile_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "STATE_DIR", tmp_path)
    monkeypatch.setattr(watcher, "OCCUPANCY_LOG", tmp_path / "occupancy.jsonl")

    # Two complete cycles with overlapping timestamps for different profiles.
    rows = [
        {"t": 1000.0, "skill": "fishing", "occ": 1},
        {"t": 1001.0, "skill": "fishing", "occ": 2},
        {"t": 1002.0, "skill": "fishing", "occ": 3},
        {"t": 1003.0, "skill": "fishing", "occ": 0},
        {"t": 1000.0, "skill": "thieving", "occ": 10},
        {"t": 1001.0, "skill": "thieving", "occ": 11},
        {"t": 1002.0, "skill": "thieving", "occ": 12},
        {"t": 1003.0, "skill": "thieving", "occ": 0},
    ]
    watcher.OCCUPANCY_LOG.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n")

    fishing = watcher.load_cycles(28, skill="fishing")
    thieving = watcher.load_cycles(28, skill="thieving")
    assert len(fishing) == 1 and fishing[0]["peak"] == 3
    assert len(thieving) == 1 and thieving[0]["peak"] == 12

    watcher.log_occupancy(7, skill="fishing", wall_time=1_700_000_010.5)
    last = json.loads(watcher.OCCUPANCY_LOG.read_text().splitlines()[-1])
    assert last == {"t": 1700000010.5, "skill": "fishing", "occ": 7}


def test_stun_pattern_matches_real_contraction():
    """The game says "You've been stunned", not "you have been stunned".

    An earlier guessed pattern used ``you (have been |are )?stunned`` and
    ``you fail to pick``; both missed the real lines, so a stun that halts
    pickpocketing went unreported.
    """
    cfg = load_config(Path("profiles/thieving.json"))
    rules = {r["name"]: r for r in cfg["rules"]}
    stun = rules["stunned"]["pattern"]
    alerted = rules["target_alerted"]["pattern"]
    activity = rules["thieving_stopped"]["pattern"]

    for line in ("You've been stunned.", "You've been stunned,",
                 "You fail to steal from the target."):
        assert re.search(stun, line, re.I), line
        assert not re.search(alerted, line, re.I), line
        # a stun is the opposite of activity and must not keep the timer alive
        assert not re.search(activity, line, re.I), line

    warn = "Your pickpocket target becomes aware of your presence."
    assert re.search(alerted, warn, re.I)
    assert not re.search(stun, warn, re.I)

    for line in ("You pick the target's pocket.",
                 "455 coins have been added to your money pouch."):
        assert re.search(activity, line, re.I), line
        assert not re.search(stun, line, re.I), line


def test_stack_signature_detects_growth_not_shrink():
    """Digit-pixel count must separate a real gain from noise and from use.

    Measured drift on an unchanging stack is +/-2 pixels; a real quantity
    change moves it 7-24. Reading the digit *value* was abandoned because
    tesseract resolved only 5 of 9 slots correctly.
    """
    grid = (17, 86, 61, 55, 5, 6)
    frame = np.zeros((560, 330, 3), dtype=np.int16)
    assert watcher.stack_signature(frame, grid, 0) == 0

    # paint yellow-green digit pixels into slot 0's top-left corner
    frame[88:100, 19:40] = [200, 200, 40]
    grown = watcher.stack_signature(frame, grid, 0)
    assert grown > 100

    # a different slot is unaffected
    assert watcher.stack_signature(frame, grid, 1) == 0


def test_stack_rule_ignores_transient_overlay():
    """Hovering the backpack draws a tooltip that must not read as a drop."""
    rule = _rule(kind="stack", stack_tolerance=3, confirm_seconds=4)
    rule._primed = True
    base = [104, 356, 294, 85]
    fired = []
    now = 0.0
    # tooltip: slot 3 jumps, then reverts before confirm_seconds elapses
    frames = [base, base, [104, 356, 294, 60], base, base, base]
    for cur in frames:
        prev = rule._stacks
        rule._stacks = list(cur)
        if prev and len(prev) == len(cur):
            changed = [i for i, (a, b) in enumerate(zip(prev, cur))
                       if abs(b - a) > rule.stack_tolerance]
            if changed:
                if set(changed) != rule._pending_slots:
                    rule._pending_slots = set(changed)
                    rule._pending_since = now
                    rule._pending_base = prev
            elif rule._pending_slots:
                b0 = rule._pending_base or prev
                slots = sorted(rule._pending_slots)
                if now - rule._pending_since >= rule.confirm_seconds:
                    rule._pending_slots = set()
                    if [i for i in slots
                            if cur[i] - b0[i] > rule.stack_tolerance]:
                        fired.append(now)
        now += 1.5
    assert fired == []


def test_stack_new_slot_uses_real_occupancy_not_digit_pixels(monkeypatch):
    """A quantity-1 drop has no stack digits but still occupies a new slot."""
    region = watcher.Region("top-left", 0, 0, 20, 20,
                            (0, 0, 20, 20, 1, 1))
    empty = np.zeros((20, 20, 3), dtype=np.int16)
    occupied = empty.copy()
    occupied[5:15, 5:10] = [220, 20, 20]

    frames = iter([empty, occupied, occupied])
    monkeypatch.setattr(watcher, "capture_array",
                        lambda *a, **k: next(frames))
    rule = _rule(kind="stack", new_slot_only=True, confirm_seconds=1,
                 cooldown=0, capacity=1)

    assert watcher._eval_stack(rule, "w", region, (0, 0, 20, 20), 0.0, 1) is None
    assert rule._primed is True
    assert watcher._eval_stack(rule, "w", region, (0, 0, 20, 20), 1.0, 2) is None
    alert = watcher._eval_stack(rule, "w", region, (0, 0, 20, 20), 2.1, 3)

    assert alert is not None
    assert "slot 1" in alert.body


def test_stack_new_slot_rejects_existing_stack_one_to_two(monkeypatch):
    region = watcher.Region("top-left", 0, 0, 20, 20,
                            (0, 0, 20, 20, 1, 1))
    frame = np.zeros((20, 20, 3), dtype=np.int16)
    frame[5:15, 5:10] = [220, 20, 20]
    frames = iter([frame, frame, frame])
    signatures = iter([0, 20, 20])

    monkeypatch.setattr(watcher, "capture_array",
                        lambda *a, **k: next(frames))
    monkeypatch.setattr(watcher, "stack_signature",
                        lambda *a, **k: next(signatures))
    rule = _rule(kind="stack", new_slot_only=True, confirm_seconds=1,
                 cooldown=0, capacity=1)

    assert watcher._eval_stack(rule, "w", region, (0, 0, 20, 20), 0.0, 1) is None
    assert watcher._eval_stack(rule, "w", region, (0, 0, 20, 20), 1.0, 2) is None
    assert watcher._eval_stack(rule, "w", region, (0, 0, 20, 20), 2.1, 3) is None


def test_parse_timer_rejects_impossible_readings():
    """A garbled frame must not become a bogus elapsed time."""
    assert watcher.parse_timer("00:32:19") == 1939
    assert watcher.parse_timer("1:05:00") == 3900
    assert watcher.parse_timer("02:00:03") == 7203
    # minute/second fields are anchored on [0-5]\d
    assert watcher.parse_timer("00:82:09") is None
    assert watcher.parse_timer("00:12:99") is None
    assert watcher.parse_timer("garbage") is None
    assert watcher.parse_timer("") is None


def test_timer_milestone_fires_once_per_hour():
    """Crossing an hour alerts once, not on every poll afterwards."""
    rule = _rule(kind="timer", step=3600, cooldown=0)
    fired = []
    for text in ("00:59:58", "01:00:02", "01:00:05", "01:30:00", "02:00:03"):
        secs = watcher.parse_timer(text)
        if secs + 5 < rule._elapsed:
            rule._milestone = 0
        rule._elapsed = secs
        reached = secs // int(rule.step)
        if reached > rule._milestone:
            rule._milestone = reached
            fired.append(reached)
    assert fired == [1, 2]


def test_timer_reset_rewinds_milestones():
    """Resetting the in-game timer should start a fresh session."""
    rule = _rule(kind="timer", step=3600, cooldown=0)
    rule._elapsed = 3700
    rule._milestone = 1
    secs = watcher.parse_timer("00:00:03")
    assert secs + 5 < rule._elapsed
    rule._milestone = 0
    rule._elapsed = secs
    assert rule._milestone == 0


def test_colour_pixels_counts_within_box():
    frame = np.zeros((20, 20, 3), dtype=np.int16)
    frame[0:5, 0:4] = [200, 120, 40]          # inside the ring's orange box
    frame[10:12, 0:4] = [40, 200, 200]        # outside it
    assert watcher.colour_pixels(frame, (150, 80, 0), (255, 200, 90)) == 20


def _presence_frame(present: bool) -> np.ndarray:
    frame = np.zeros((20, 20, 3), dtype=np.int16)
    if present:
        frame[:] = [200, 120, 40]
    return frame


def test_presence_needs_sustained_absence(monkeypatch):
    """Exercise the production evaluator, not a copy of its state machine."""
    state = {"present": True}
    monkeypatch.setattr(
        watcher, "capture_array",
        lambda *args, **kwargs: _presence_frame(state["present"]),
    )
    rule = _rule(kind="presence", present_above=200, absent_seconds=12,
                 cooldown=0, item="thieving")

    assert watcher._eval_presence(rule, "w", (0, 0, 20, 20), 100.0, 1) is None

    state["present"] = False
    assert watcher._eval_presence(rule, "w", (0, 0, 20, 20), 101.0, 2) is None
    assert watcher._eval_presence(rule, "w", (0, 0, 20, 20), 110.0, 3) is None
    alert = watcher._eval_presence(rule, "w", (0, 0, 20, 20), 113.0, 4)

    assert alert is not None
    assert "has stopped" in alert.body


def test_presence_primes_old_scrollback_without_delaying_stop(monkeypatch):
    """A line already visible when the icon disappears is not fresh evidence."""
    state = {"present": True}
    monkeypatch.setattr(
        watcher, "capture_array",
        lambda *args, **kwargs: _presence_frame(state["present"]),
    )
    monkeypatch.setattr(
        watcher, "ocr_cached",
        lambda *args, **kwargs: "[21:00:00] You pick the target's pocket.",
    )
    rule = _rule(kind="presence", present_above=200, absent_seconds=10,
                 cooldown=0, item="thieving",
                 corroborate_region="chat_tail",
                 corroborate_pattern=r"you pick the target'?s pocket")
    rule._corroborate_box = (0, 0, 1, 1)

    assert watcher._eval_presence(rule, "w", (0, 0, 20, 20), 100.0, 1) is None
    state["present"] = False
    assert watcher._eval_presence(rule, "w", (0, 0, 20, 20), 101.0, 2) is None
    alert = watcher._eval_presence(rule, "w", (0, 0, 20, 20), 111.0, 3)

    assert alert is not None


def test_presence_tracks_fresh_corroboration_during_blackout(monkeypatch):
    """Fresh chat during icon loss keeps the activity alive until it goes quiet."""
    state = {"present": True}
    monkeypatch.setattr(
        watcher, "capture_array",
        lambda *args, **kwargs: _presence_frame(state["present"]),
    )
    text_by_cycle = {
        2: "[21:00:00] You pick the target's pocket.",
        3: ("[21:00:00] You pick the target's pocket.\n"
            "[21:00:05] You pick the target's pocket."),
        4: ("[21:00:00] You pick the target's pocket.\n"
            "[21:00:05] You pick the target's pocket."),
        5: ("[21:00:00] You pick the target's pocket.\n"
            "[21:00:05] You pick the target's pocket."),
    }
    monkeypatch.setattr(
        watcher, "ocr_cached",
        lambda wid, box, cycle, psm=6: text_by_cycle.get(cycle, ""),
    )
    rule = _rule(kind="presence", present_above=200, absent_seconds=10,
                 cooldown=0, item="thieving",
                 corroborate_region="chat_tail",
                 corroborate_pattern=r"you pick the target'?s pocket")
    rule._corroborate_box = (0, 0, 1, 1)

    assert watcher._eval_presence(rule, "w", (0, 0, 20, 20), 100.0, 1) is None
    state["present"] = False
    assert watcher._eval_presence(rule, "w", (0, 0, 20, 20), 101.0, 2) is None
    assert watcher._eval_presence(rule, "w", (0, 0, 20, 20), 105.0, 3) is None
    assert watcher._eval_presence(rule, "w", (0, 0, 20, 20), 111.0, 4) is None
    alert = watcher._eval_presence(rule, "w", (0, 0, 20, 20), 116.0, 5)

    assert alert is not None


def test_validate_config_rejects_unknown_corroboration_region():
    config = valid_config()
    config["rules"] = [{
        "name": "presence",
        "kind": "presence",
        "region": "panel",
        "corroborate_region": "missing",
        "corroborate_pattern": "activity",
    }]

    with pytest.raises(ValueError, match="corroborate_region"):
        validate_config(config)


def test_validate_config_rejects_invalid_corroboration_regex():
    config = valid_config()
    config["rules"] = [{
        "name": "presence",
        "kind": "presence",
        "region": "panel",
        "corroborate_region": "panel",
        "corroborate_pattern": "(",
    }]

    with pytest.raises(ValueError, match="corroborate_pattern"):
        validate_config(config)


def test_validate_config_requires_complete_corroboration_pair():
    config = valid_config()
    config["rules"] = [{
        "name": "presence",
        "kind": "presence",
        "region": "panel",
        "corroborate_region": "panel",
    }]

    with pytest.raises(ValueError, match="requires corroborate_region"):
        validate_config(config)


# --------------------------------------------------------------------------
# Priority 0, step 1: capture backend abstraction
# --------------------------------------------------------------------------

class RecordingBackend(watcher.CaptureBackend):
    """A backend with no X11, no ImageMagick, and no game.

    Its existence is the point of the abstraction: if capture can only be
    exercised through a live desktop session, none of it is testable in CI.
    """

    name = "recording"

    def __init__(self, size=(800, 600)):
        self.grabs = []
        self._size = size

    def available(self):
        return True, "ok"

    def find(self, wm_class):
        return f"handle-for-{wm_class}"

    def size(self, handle):
        return self._size

    def grab_array(self, handle, box):
        self.grabs.append(tuple(box))
        _x, _y, w, h = box
        return np.full((h, w, 3), 100, dtype=np.int16)


def test_game_instance_acquires_through_backend():
    backend = RecordingBackend()
    game = watcher.GameInstance("steam_app_1343400", backend=backend)

    assert game.acquire() is True
    assert game.handle == "handle-for-steam_app_1343400"
    assert game.size == (800, 600)


def test_game_instance_reports_missing_window():
    class NoWindow(RecordingBackend):
        def find(self, wm_class):
            return None

    game = watcher.GameInstance("absent", backend=NoWindow())
    assert game.acquire() is False

    with pytest.raises(watcher.CaptureError, match="no game window"):
        game.frame((0, 0, 10, 10))


def test_shared_frame_serves_many_readers_with_one_capture():
    """The Priority 0 acceptance criterion, as a test.

    Several detectors reading the same region in one cycle must not each
    spawn a capture.
    """
    backend = RecordingBackend()
    game = watcher.GameInstance("game", backend=backend)
    game.acquire()
    box = (0, 0, 40, 30)

    game.begin_cycle(1)
    first = game.frame(box)
    second = game.frame(box)
    third = game.frame(box)

    assert len(backend.grabs) == 1
    assert first is second is third

    # a new cycle invalidates the cache
    game.begin_cycle(2)
    game.frame(box)
    assert len(backend.grabs) == 2


def test_masked_and_plain_views_share_one_capture():
    """A mask is derived from the raw pixels, not a second screenshot."""
    backend = RecordingBackend()
    game = watcher.GameInstance("game", backend=backend)
    game.acquire()
    box = (0, 0, 40, 30)

    game.begin_cycle(1)
    plain = game.frame(box)
    masked = game.frame(box, mask="bright")

    assert len(backend.grabs) == 1
    assert plain.max() == 100          # raw frame untouched
    assert not np.array_equal(plain, masked)


def test_distinct_regions_are_captured_separately():
    backend = RecordingBackend()
    game = watcher.GameInstance("game", backend=backend)
    game.acquire()

    game.begin_cycle(1)
    game.frame((0, 0, 10, 10))
    game.frame((5, 5, 10, 10))

    assert backend.grabs == [(0, 0, 10, 10), (5, 5, 10, 10)]


def test_resize_is_detected_and_drops_stale_frames():
    """Recovering from a RuneScape resize without restarting is required."""
    backend = RecordingBackend()
    game = watcher.GameInstance("game", backend=backend)
    game.acquire()

    game.begin_cycle(1)
    game.frame((0, 0, 10, 10))
    assert len(backend.grabs) == 1

    backend._size = (1024, 768)
    assert game.refresh_size() == (1024, 768)
    assert game.size == (1024, 768)

    # cached frames from the old geometry must not survive
    game.frame((0, 0, 10, 10))
    assert len(backend.grabs) == 2


def test_default_backend_is_registered():
    assert watcher.DEFAULT_BACKEND in watcher.BACKENDS
    assert issubclass(watcher.BACKENDS[watcher.DEFAULT_BACKEND],
                      watcher.CaptureBackend)


def test_backend_reports_why_it_is_unavailable():
    """`doctor` will depend on this reason string being actionable."""
    class Broken(watcher.CaptureBackend):
        name = "broken"

        def available(self):
            return False, "missing tool(s): import"

    ok, reason = Broken().available()
    assert ok is False
    assert "import" in reason


# --------------------------------------------------------------------------
# Priority 0, step 2: shared frame scheduler
# --------------------------------------------------------------------------

def _scheduler(backend=None, regions=None):
    backend = backend or RecordingBackend(size=(3840, 2058))
    game = watcher.GameInstance("game", backend=backend)
    game.acquire()
    if regions is None:
        regions = load_config(Path("profiles/thieving.json"))["_regions"]
    return watcher.FrameScheduler(game, regions), backend


def test_scheduler_serves_one_pass_from_one_capture_per_region():
    sched, backend = _scheduler()

    sched.begin()
    sched.frame("chat_tail")
    sched.frame("chat_tail")
    sched.frame("backpack")

    assert len(backend.grabs) == 2
    assert sched.stats["chat_tail"].captures == 1
    assert sched.stats["chat_tail"].reuses == 1


def test_scheduler_advances_cycles():
    sched, backend = _scheduler()

    first = sched.begin()
    sched.frame("chat_tail")
    second = sched.begin()
    sched.frame("chat_tail")

    assert (first, second) == (1, 2)
    assert len(backend.grabs) == 2


def test_scheduler_rejects_unknown_region():
    sched, _ = _scheduler()
    with pytest.raises(KeyError, match="nonexistent"):
        sched.box_for("nonexistent")


def test_prefetch_isolates_a_failing_region():
    """One region mid-repaint must not cost the pass its other regions."""

    class Flaky(RecordingBackend):
        def __init__(self):
            super().__init__(size=(3840, 2058))
            self.fail_boxes = set()

        def grab_array(self, handle, box):
            if tuple(box) in self.fail_boxes:
                raise watcher.CaptureError("simulated repaint")
            return super().grab_array(handle, box)

    backend = Flaky()
    sched, _ = _scheduler(backend=backend)
    backend.fail_boxes = {tuple(sched.box_for("backpack"))}

    sched.begin()
    failed = sched.prefetch(["chat_tail", "backpack", "session_timer"])

    assert failed == ["backpack"]
    assert sched.stats["chat_tail"].captures == 1
    assert sched.stats["session_timer"].captures == 1
    assert sched.stats["backpack"].failures == 1


def test_scheduler_flags_a_frozen_region():
    """A region whose pixels never change is suspect, not trustworthy."""

    class Freezable(RecordingBackend):
        def __init__(self):
            super().__init__(size=(3840, 2058))
            self.frozen = False
            self.tick = 0

        def grab_array(self, handle, box):
            if not self.frozen:
                self.tick += 1
            _x, _y, w, h = box
            self.grabs.append(tuple(box))
            return np.full((h, w, 3), 50 + self.tick, dtype=np.int16)

    backend = Freezable()
    sched, _ = _scheduler(backend=backend)

    for _ in range(5):
        sched.begin()
        sched.frame("chat_tail")
    assert sched.health("chat_tail")[0] == "PASS"

    backend.frozen = True
    for _ in range(25):
        sched.begin()
        sched.frame("chat_tail")
    verdict, reason = sched.health("chat_tail")
    assert verdict == "WARN"
    assert "unchanged" in reason

    backend.frozen = False
    sched.begin()
    sched.frame("chat_tail")
    assert sched.health("chat_tail")[0] == "PASS"


def test_health_reports_never_captured_region():
    sched, _ = _scheduler()
    assert sched.health("chat_tail") == ("WARN", "never captured")


def test_health_fails_when_every_capture_attempt_failed():
    """Attempted-and-failed is not the same state as never sampled."""
    class Broken(RecordingBackend):
        def grab_array(self, handle, box):
            raise watcher.CaptureError("simulated backend failure")

    sched, _ = _scheduler(backend=Broken(size=(3840, 2058)))
    sched.begin()
    assert sched.prefetch(["chat_tail"]) == ["chat_tail"]

    verdict, reason = sched.health("chat_tail")
    assert verdict == "FAIL"
    assert "simulated backend failure" in reason


def test_report_covers_every_touched_region():
    sched, _ = _scheduler()
    sched.begin()
    sched.frame("chat_tail")
    sched.frame("backpack")

    names = [row[0] for row in sched.report()]
    assert names == ["backpack", "chat_tail"]
    assert all(len(row) == 3 for row in sched.report())


# --------------------------------------------------------------------------
# Priority 0, step 3: native XCB backend
# --------------------------------------------------------------------------

def test_xcb_backend_is_registered_and_default():
    """XCB is the default because it measured 42x faster over a full cycle."""
    assert watcher.X11XcbBackend.name in watcher.BACKENDS
    assert watcher.DEFAULT_BACKEND == watcher.X11XcbBackend.name


def test_make_backend_rejects_unknown_name():
    with pytest.raises(ValueError, match="unknown backend"):
        watcher.make_backend("nonsense")


def test_make_backend_returns_requested_backend():
    """An explicit request is honoured so `doctor` can explain a failure."""
    backend = watcher.make_backend("x11-imagemagick")
    assert backend.name == "x11-imagemagick"


def test_make_backend_falls_back_when_default_unavailable(monkeypatch):
    """A host without python-xcffib should still run, just slower."""

    class Unavailable(watcher.X11XcbBackend):
        def available(self):
            return False, "python-xcffib unavailable"

    class Usable(watcher.X11ImageMagickBackend):
        def available(self):
            return True, "ok"

    monkeypatch.setitem(watcher.BACKENDS, "x11-xcb", Unavailable)
    monkeypatch.setattr(watcher, "X11ImageMagickBackend", Usable)

    assert watcher.make_backend().name == "x11-imagemagick"


def test_runtime_backend_rejects_explicit_unavailable_backend(monkeypatch):
    """Explicit --backend must fail before the watch loop starts."""
    class Unavailable(watcher.X11XcbBackend):
        def available(self):
            return False, "python-xcffib unavailable"

    monkeypatch.setitem(watcher.BACKENDS, "x11-xcb", Unavailable)

    with pytest.raises(watcher.CaptureError, match="requested backend.*unavailable"):
        watcher.make_runtime_backend("x11-xcb")


def test_acquire_game_honors_requested_backend(monkeypatch):
    class Requested(RecordingBackend):
        name = "requested"

        def available(self):
            return True, "ok"

    monkeypatch.setattr(watcher, "make_runtime_backend",
                        lambda name=None: Requested(size=(800, 600)))
    cfg = {"window": {"wm_class": "game"}}

    game = watcher.acquire_game(cfg, "requested")

    assert game.backend.name == "requested"
    assert game.handle == "handle-for-game"
    assert game.size == (800, 600)


def test_xcb_backend_rejects_degenerate_boxes():
    backend = watcher.X11XcbBackend()
    for box in ((0, 0, 0, 10), (0, 0, 10, 0)):
        with pytest.raises(watcher.CaptureError, match="degenerate"):
            backend.grab_array("1234", box)


def test_xcb_backend_rejects_empty_handle():
    with pytest.raises(watcher.CaptureError, match="empty window id"):
        watcher.X11XcbBackend().grab_array("", (0, 0, 10, 10))


def test_xcb_backend_reports_missing_binding(monkeypatch):
    """`available` must name the missing dependency, not just say no."""
    backend = watcher.X11XcbBackend()
    monkeypatch.setitem(os.environ, "DISPLAY", ":0")
    monkeypatch.setattr(watcher, "ensure_x_env", lambda: None)

    real_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name.startswith("xcffib"):
            raise ImportError("No module named 'xcffib'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    ok, reason = backend.available()
    assert ok is False
    assert "xcffib" in reason


def test_xcb_grab_file_supports_full_window_and_percentage_resize(tmp_path):
    class FakeXcb(watcher.X11XcbBackend):
        def size(self, handle):
            return (20, 10)

        def grab_array(self, handle, box):
            assert box == (0, 0, 20, 10)
            return np.zeros((10, 20, 3), dtype=np.int16)

    out = tmp_path / "calibrate.png"
    FakeXcb().grab_file("42", None, out, resize="50%")

    with watcher.Image.open(out) as image:
        assert image.size == (10, 5)


# --------------------------------------------------------------------------
# Priority 0, step 4: doctor diagnostics
# --------------------------------------------------------------------------

def _thieving():
    return load_config(Path("profiles/thieving.json"))


def test_doctor_reports_missing_game_window():
    cfg = _thieving()
    game = watcher.GameInstance("no-such-class", backend=RecordingBackend())
    game.backend.find = lambda wm_class: None

    checks = watcher._check_window(cfg, game)

    assert [c.verdict for c in checks] == ["FAIL"]
    assert "no window" in checks[0].detail
    assert checks[0].remedy


def test_doctor_flags_grid_larger_than_its_region():
    cfg = _thieving()
    cfg = dict(cfg)
    cfg["_regions"] = {
        "bad": watcher.Region("top-left", 0, 0, 100, 100,
                              (0, 0, 50, 50, 10, 10)),
    }
    game = watcher.GameInstance("game", backend=RecordingBackend())
    game.acquire()

    checks = watcher._check_grids(cfg, game)

    assert checks[0].verdict == "FAIL"
    assert "spans 500x500" in checks[0].detail


def test_doctor_warns_when_region_position_is_clamped():
    cfg = _thieving()
    cfg = dict(cfg)
    cfg["_regions"] = {
        "bad": watcher.Region("top-left", -50, 10, 100, 100),
    }
    game = watcher.GameInstance(
        "game", backend=RecordingBackend(size=(800, 600)))
    game.acquire()

    checks = watcher._check_regions(cfg, game)

    assert checks[0].verdict == "WARN"
    assert "clamped" in checks[0].detail


def test_doctor_flags_region_clamped_by_a_small_window():
    """`resolve` clamps, so a size mismatch is the only sign of drift."""
    cfg = dict(_thieving())
    cfg["_regions"] = {"huge": watcher.Region("top-left", 0, 0, 9999, 9999)}
    game = watcher.GameInstance("game", backend=RecordingBackend())
    game.acquire()

    checks = watcher._check_regions(cfg, game)

    assert checks[0].verdict == "WARN"
    assert "clamped" in checks[0].detail


def test_doctor_fails_a_profile_with_no_enabled_rules():
    cfg = dict(_thieving())
    cfg["rules"] = [dict(r, enabled=False) for r in cfg["rules"]]

    verdicts = {c.area: c for c in watcher._check_profile(cfg, Path("p.json"))}

    assert verdicts["rules"].verdict == "FAIL"
    assert "no enabled rules" in verdicts["rules"].detail


def test_doctor_warns_when_two_rules_share_a_sound():
    """Per-rule sounds exist so alerts differ without looking at the screen."""
    cfg = dict(_thieving())
    cfg["rules"] = [
        {"name": "a", "kind": "ocr", "region": "chat_tail", "sound": "bell"},
        {"name": "b", "kind": "ocr", "region": "chat_tail", "sound": "bell"},
    ]

    areas = {c.area: c for c in watcher._check_profile(cfg, Path("p.json"))}

    assert areas["sounds distinct"].verdict == "WARN"
    assert "bell" in areas["sounds distinct"].detail


def test_shipped_profiles_use_distinct_sounds():
    """Regression: session_hour and coin_milestone once shared a tone."""
    for name in ("profiles/thieving.json", "profiles/fishing.json"):
        cfg = load_config(Path(name))
        sounds = [r.get("sound") for r in cfg["rules"]
                  if r.get("enabled", True) and r.get("sound")]
        assert len(sounds) == len(set(sounds)), f"{name} reuses a sound"


def test_doctor_detects_a_flat_capture():
    """A blank frame is a capture fault, not quiet gameplay."""
    class Blank(RecordingBackend):
        def grab_array(self, handle, box):
            self.grabs.append(tuple(box))
            _x, _y, w, h = box
            return np.zeros((h, w, 3), dtype=np.int16)

    cfg = dict(_thieving())
    cfg["_regions"] = {"panel": watcher.Region("top-left", 0, 0, 40, 40)}
    game = watcher.GameInstance("game", backend=Blank())
    game.acquire()

    checks = watcher._check_capture(cfg, game)

    assert checks[0].verdict == "WARN"
    assert "flat" in checks[0].detail


def test_doctor_counts_numeric_tokens_as_readable(monkeypatch):
    """A timer region reads digits, not words, and is still healthy."""
    cfg = dict(_thieving())
    cfg["_regions"] = {"timer": watcher.Region("top-left", 0, 0, 40, 20)}
    cfg["rules"] = [{"name": "t", "kind": "timer", "region": "timer"}]
    game = watcher.GameInstance("game", backend=RecordingBackend())
    game.acquire()
    seen = {}

    monkeypatch.setattr(watcher.shutil, "which", lambda t: "/usr/bin/" + t)

    def fake_ocr(*args, **kwargs):
        seen["game"] = kwargs.get("game")
        seen["cycle"] = kwargs.get("cycle")
        return "00:32:19"

    monkeypatch.setattr(watcher, "ocr", fake_ocr)

    checks = watcher._check_ocr(cfg, game)

    assert checks[0].verdict == "PASS"
    assert "3 numbers" in checks[0].detail
    assert seen["game"] is game
    assert seen["cycle"] == 1


def test_doctor_warns_on_unreadable_ocr(monkeypatch):
    cfg = dict(_thieving())
    cfg["_regions"] = {"chat": watcher.Region("top-left", 0, 0, 40, 20)}
    cfg["rules"] = [{"name": "c", "kind": "ocr", "region": "chat"}]
    game = watcher.GameInstance("game", backend=RecordingBackend())
    game.acquire()

    monkeypatch.setattr(watcher.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(watcher, "ocr", lambda *a, **k: "|~ #")

    checks = watcher._check_ocr(cfg, game)

    assert checks[0].verdict == "WARN"


def test_doctor_reports_missing_required_tool(monkeypatch):
    monkeypatch.setattr(watcher.shutil, "which",
                        lambda t: None if t == "tesseract" else "/usr/bin/" + t)

    areas = {c.area: c for c in watcher._check_tools()}

    assert areas["tesseract"].verdict == "FAIL"
    assert areas["xdotool"].verdict == "PASS"
    # optional tools warn rather than fail
    monkeypatch.setattr(watcher.shutil, "which",
                        lambda t: None if t == "paplay" else "/usr/bin/" + t)
    areas = {c.area: c for c in watcher._check_tools()}
    assert areas["paplay"].verdict == "WARN"


# --------------------------------------------------------------------------
# Priority 0, step 5: interface readers
# --------------------------------------------------------------------------

class ChatBackend(RecordingBackend):
    """Backend whose chat region OCRs to a scripted list of lines."""

    def __init__(self, pages):
        super().__init__(size=(2505, 1986))
        self.pages = list(pages)
        self.page = 0

    def next_page(self):
        text = self.pages[min(self.page, len(self.pages) - 1)]
        self.page += 1
        return text


def _chat_env(pages, monkeypatch):
    backend = ChatBackend(pages)
    game = watcher.GameInstance("game", backend=backend)
    game.acquire()
    regions = load_config(Path("profiles/thieving.json"))["_regions"]
    sched = watcher.FrameScheduler(game, regions)
    monkeypatch.setattr(watcher, "ocr_cached",
                        lambda *a, **k: backend.next_page())
    return sched


def test_registry_shares_one_reader_per_region():
    """Two rules on one region must share a dedup set, not keep two."""
    registry = watcher.ReaderRegistry()
    first = registry.chat("chat_tail")
    second = registry.chat("chat_tail")
    other = registry.chat("metrics_xp")

    assert first is second
    assert first is not other
    assert len(registry) == 2


def test_chat_reader_emits_each_line_once(monkeypatch):
    """The chat tail redisplays old lines every poll until they scroll off."""
    page = "[16:10:26] You catch a desert sole.\n[16:10:30] You catch a catfish."
    sched = _chat_env([page, page, page], monkeypatch)
    reader = watcher.ChatReader("chat_tail")

    sched.begin()
    first = reader.read(sched)
    sched.begin()
    second = reader.read(sched)

    assert len(first) == 2
    assert second == []


def test_chat_reader_replays_same_event_list_to_every_rule_in_cycle(monkeypatch):
    """The first consumer must not steal shared events from later rules."""
    page = "[16:10:26] You catch a desert sole.\n[16:10:30] You catch a catfish."
    sched = _chat_env([page, "this second OCR result must be ignored"], monkeypatch)
    reader = watcher.ChatReader("chat_tail")

    sched.begin()
    first = reader.read(sched)
    second = reader.read(sched)

    assert second == first
    assert reader.events_emitted == 2


def test_chat_reader_skips_short_keys(monkeypatch):
    sched = _chat_env(["ok\n[16:10:26] You catch a desert sole."], monkeypatch)
    reader = watcher.ChatReader("chat_tail")

    sched.begin()
    lines = reader.read(sched)

    assert [line.text for line in lines] == ["[16:10:26] You catch a desert sole."]


def test_repeated_events_are_not_collapsed():
    """Regression: catches a second apart differ only by timestamp.

    Fuzzy dedup without timestamp awareness suppressed these, which would
    break every rule that counts occurrences.
    """
    reader = watcher.ChatReader("chat_tail", similarity=0.90)
    keys = [norm_line(f"[16:10:{s}] You catch a desert sole.")
            for s in ("26", "30", "35")]

    reader._recent = [keys[0]]
    assert reader._is_variant(keys[1]) is False
    reader._recent = keys[:2]
    assert reader._is_variant(keys[2]) is False


def test_ocr_variants_of_one_line_are_collapsed():
    """Same timestamp, mangled wording: one game event, not three."""
    reader = watcher.ChatReader("chat_tail", similarity=0.90)
    reader._recent = [norm_line("[16:10:26] You catch a desert sole.")]

    for variant in ("[16:10:26] You catch a desert soIe.",
                    "(16:10:26] YoU catch a desert sole,"):
        assert reader._is_variant(norm_line(variant)) is True


def test_unstamped_lines_are_not_fuzzy_collapsed():
    """Without timestamps, similar wording may be a genuine repeated event."""
    reader = watcher.ChatReader("chat_tail", similarity=0.90)
    reader._recent = [norm_line("Your camouflage outfit keeps you hidden")]

    assert reader._is_variant(
        norm_line("Your camoufiage outfit kesps you hidden")) is False


def test_unstamped_lines_dedup_only_while_still_visible():
    reader = watcher.ChatReader("chat_tail")

    first = reader.consume("You catch a trout.", 1)
    same_view = reader.consume("You catch a trout.", 2)
    reader.consume("", 3)
    later = reader.consume("You catch a trout.", 4)

    assert len(first) == 1
    assert same_view == []
    assert len(later) == 1


def test_unstamped_duplicate_count_can_emit_new_visible_occurrence():
    reader = watcher.ChatReader("chat_tail")
    reader.consume("You catch a trout.", 1)

    events = reader.consume("You catch a trout.\nYou catch a trout.", 2)

    assert len(events) == 1


def test_distinct_messages_stay_distinct():
    """Fuzzy matching must not merge different game events."""
    reader = watcher.ChatReader("chat_tail", similarity=0.90)
    reader._recent = [norm_line("You've been stunned.")]

    for other in ("You fail to steal from the target.",
                  "Your pickpocket target becomes aware of your presence.",
                  "455 coins have been added to your money pouch."):
        assert reader._is_variant(norm_line(other)) is False


def test_similarity_of_one_disables_fuzzy_matching():
    reader = watcher.ChatReader("chat_tail", similarity=1.0)
    reader._recent = [norm_line("[16:10:26] You catch a desert sole.")]

    assert reader._is_variant(
        norm_line("[16:10:26] You catch a desert soIe.")) is False


def test_seen_set_cap_keeps_current_viewport_as_baseline(monkeypatch):
    """History compaction must not make visible scrollback look new again."""
    page = "\n".join(f"[16:10:{s}] You catch a desert sole."
                     for s in ("26", "31", "36"))
    sched = _chat_env([page, page], monkeypatch)
    reader = watcher.ChatReader("chat_tail", max_seen=2)

    sched.begin()
    first = reader.read(sched)
    sched.begin()
    second = reader.read(sched)

    assert len(first) == 3
    assert second == []
    assert len(reader._seen) == 3


def test_registry_reports_reader_statistics(monkeypatch):
    sched = _chat_env(["[16:10:26] You catch a desert sole."], monkeypatch)
    registry = watcher.ReaderRegistry()
    reader = registry.chat("chat_tail")

    sched.begin()
    reader.read(sched)

    assert registry.stats() == [("chat", "chat_tail", 1, 1)]


def test_live_ocr_rules_share_reader_events(monkeypatch):
    """Two production OCR rules must see the same fresh lines in one cycle."""
    region = watcher.Region("top-left", 0, 0, 100, 40)
    first_page = "[10:00:00] Existing scrollback."
    second_page = (
        "[10:00:01] LEVEL EVENT detected.\n"
        "[10:00:02] TARGET EVENT detected."
    )
    pages = {1: first_page, 2: second_page}
    monkeypatch.setattr(
        watcher, "ocr_cached",
        lambda wid, box, cycle, psm=6: pages[cycle],
    )
    watcher.set_active_readers()

    level = watcher.Rule(
        name="level", kind="ocr", region="chat", pattern=r"LEVEL EVENT")
    target = watcher.Rule(
        name="target", kind="ocr", region="chat", pattern=r"TARGET EVENT")

    # Cycle one primes both rules from the exact same reader event list.
    assert watcher.evaluate(level, "w", region, (100, 40), 10.0, 1) is None
    assert watcher.evaluate(target, "w", region, (100, 40), 10.0, 1) is None

    a = watcher.evaluate(level, "w", region, (100, 40), 11.0, 2)
    b = watcher.evaluate(target, "w", region, (100, 40), 11.0, 2)

    assert a is not None and "LEVEL EVENT" in a.body
    assert b is not None and "TARGET EVENT" in b.body


# --------------------------------------------------------------------------
# Priority 0, step 6: layered OCR
# --------------------------------------------------------------------------

def _render_digits(text, pad_left=3, gap=2):
    """Render a string using the shipped templates, as the game would."""
    cells = []
    for ch in text:
        if ch.isdigit():
            cells.append(watcher.DIGIT_TEMPLATES[ch])
        else:
            colon = np.zeros((watcher._GLYPH_H, 2), dtype=bool)
            colon[4, :] = True
            colon[9, :] = True
            cells.append(colon)
    width = pad_left + sum(c.shape[1] + gap for c in cells)
    canvas = np.zeros((watcher._GLYPH_H + 6, width, 3), dtype=np.int16)
    x = pad_left
    for cell in cells:
        h, w = cell.shape
        canvas[3:3 + h, x:x + w][cell] = 220
        x += w + gap
    return canvas


def test_all_digits_have_templates():
    assert sorted(watcher.DIGIT_TEMPLATES) == [str(d) for d in range(10)]
    for template in watcher.DIGIT_TEMPLATES.values():
        assert template.shape == (watcher._GLYPH_H, watcher._GLYPH_W)


def test_sprite_ocr_reads_a_rendered_timer():
    frame = _render_digits("01:23:45")
    assert watcher.read_numeric(frame) == "01:23:45"


def test_sprite_ocr_reads_every_digit():
    for digit in "0123456789":
        frame = _render_digits(digit * 2)
        assert watcher.read_numeric(frame) == digit * 2, digit


def test_segment_glyphs_marks_separators():
    glyphs = watcher.segment_glyphs(_render_digits("12:34"))
    kinds = ["sep" if g is None else "digit" for g in glyphs]
    assert kinds == ["digit", "digit", "sep", "digit", "digit"]


def test_sprite_ocr_refuses_non_numeric_input():
    """Returning None is what makes the Tesseract fallback correct."""
    assert watcher.read_numeric(np.zeros((20, 60, 3), dtype=np.int16)) is None

    rng = np.random.default_rng(0)
    noise = rng.integers(0, 255, (20, 60, 3)).astype(np.int16)
    assert watcher.read_numeric(noise) is None


def test_match_digit_reports_low_confidence():
    glyph = np.zeros((watcher._GLYPH_H, watcher._GLYPH_W), dtype=bool)
    glyph[::2, ::2] = True
    digit, score = watcher.match_digit(glyph, min_score=0.99)
    assert digit is None
    assert 0.0 <= score <= 1.0


def test_ocr_numeric_falls_back_to_tesseract(monkeypatch):
    calls = []
    monkeypatch.setattr(watcher, "ocr",
                        lambda wid, box, psm=6: calls.append(psm) or "fallback")

    blank = np.zeros((20, 60, 3), dtype=np.int16)
    assert watcher.ocr_numeric("w", (0, 0, 60, 20), blank) == "fallback"
    assert calls == [7]


def test_ocr_raises_on_tesseract_process_failure(monkeypatch):
    class Result:
        stdout = ""
        stderr = "language data missing"
        returncode = 1

    monkeypatch.setattr(watcher, "capture", lambda *a, **k: a[2])
    monkeypatch.setattr(watcher.subprocess, "run", lambda *a, **k: Result())

    with pytest.raises(watcher.OCRReadError, match="language data missing"):
        watcher.ocr("w", (0, 0, 10, 10))


def test_ocr_numeric_skips_tesseract_when_sprites_match(monkeypatch):
    monkeypatch.setattr(watcher, "ocr", lambda *a, **k: pytest.fail(
        "tesseract must not run when sprite matching succeeds"))

    frame = _render_digits("00:02:06")
    assert watcher.ocr_numeric("w", (0, 0, 60, 20), frame) == "00:02:06"


def test_parsed_sprite_timer_matches_parse_timer():
    frame = _render_digits("01:00:02")
    text = watcher.read_numeric(frame)
    assert watcher.parse_timer(text) == 3602


# --------------------------------------------------------------------------
# Priority 0, step 10: rules consume the capture stack
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_active_game():
    """No test may leak process-wide capture or reader state into another."""
    watcher.set_active_game(None)
    watcher.set_active_readers()
    yield
    watcher.set_active_game(None)
    watcher.set_active_readers()


def test_capture_array_uses_bound_game_instance():
    backend = RecordingBackend(size=(100, 100))
    game = watcher.GameInstance("game", backend=backend)
    game.acquire()
    watcher.set_active_game(game)

    watcher.capture_array(game.handle, (0, 0, 10, 10), None, cycle=1)

    assert backend.grabs == [(0, 0, 10, 10)]


def test_capture_array_shares_frames_across_rules():
    """Two rules on one region in one cycle must cost one capture."""
    backend = RecordingBackend(size=(100, 100))
    game = watcher.GameInstance("game", backend=backend)
    game.acquire()
    watcher.set_active_game(game)

    for _ in range(3):
        watcher.capture_array(game.handle, (0, 0, 10, 10), None, cycle=7)

    assert len(backend.grabs) == 1


def test_ocr_uses_the_same_bound_frame_as_pixel_rules(monkeypatch):
    """OCR encoding must not recapture pixels already sampled this cycle."""
    backend = RecordingBackend(size=(100, 100))
    game = watcher.GameInstance("game", backend=backend)
    game.acquire()
    watcher.set_active_game(game)
    box = (0, 0, 10, 10)

    watcher.capture_array(game.handle, box, None, cycle=9)

    class Result:
        stdout = "shared frame"
        returncode = 0

    monkeypatch.setattr(watcher.subprocess, "run", lambda *a, **k: Result())
    assert watcher.ocr_cached(game.handle, box, cycle=9) == "shared frame"

    assert backend.grabs == [box]


def test_capture_array_falls_back_without_a_bound_game():
    calls = []
    original = watcher._capture_array_uncached
    try:
        watcher._capture_array_uncached = lambda wid, box: (
            calls.append(wid) or np.zeros((box[3], box[2], 3), dtype=np.int16))
        watcher.capture_array("legacy-id", (0, 0, 10, 10), None, cycle=1)
    finally:
        watcher._capture_array_uncached = original

    assert calls == ["legacy-id"]


def test_capture_array_ignores_a_mismatched_handle():
    """A bound instance must not serve pixels for a different window."""
    backend = RecordingBackend(size=(100, 100))
    game = watcher.GameInstance("game", backend=backend)
    game.acquire()
    watcher.set_active_game(game)

    calls = []
    original = watcher._capture_array_uncached
    try:
        watcher._capture_array_uncached = lambda wid, box: (
            calls.append(wid) or np.zeros((box[3], box[2], 3), dtype=np.int16))
        watcher.capture_array("a-different-window", (0, 0, 10, 10), None,
                              cycle=1)
    finally:
        watcher._capture_array_uncached = original

    assert calls == ["a-different-window"]
    assert backend.grabs == []


def test_pixel_rules_route_through_the_bound_backend():
    """The point of step 10: no rule reaches ImageMagick any more."""
    backend = RecordingBackend(size=(2505, 1986))
    game = watcher.GameInstance("game", backend=backend)
    game.acquire()
    watcher.set_active_game(game)

    cfg = load_config(Path("profiles/thieving.json"))
    rules = [Rule(**{k: v for k, v in r.items() if not k.startswith("_")})
             for r in cfg["rules"]
             if r.get("enabled", True) and r["kind"] in ("inventory", "stack")]
    assert rules, "profile should contain pixel rules"

    for cycle in (1, 2):
        for rule in rules:
            watcher.evaluate(rule, game.handle, cfg["_regions"][rule.region],
                             game.size, 1000.0 + cycle, cycle)

    # every rule here watches `backpack`, so one grab per cycle
    assert len(backend.grabs) == 2


def test_resize_clears_frames_for_bound_instance():
    backend = RecordingBackend(size=(100, 100))
    game = watcher.GameInstance("game", backend=backend)
    game.acquire()
    watcher.set_active_game(game)

    watcher.capture_array(game.handle, (0, 0, 10, 10), None, cycle=1)
    backend._size = (200, 200)
    game.refresh_size()
    watcher.capture_array(game.handle, (0, 0, 10, 10), None, cycle=1)

    assert len(backend.grabs) == 2

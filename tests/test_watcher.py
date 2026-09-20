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
    """A milestone takes hours; an in-memory total would be rewound."""
    monkeypatch.setattr(watcher, "STATE_DIR", tmp_path)
    monkeypatch.setattr(watcher, "COUNTER_LOG", tmp_path / "counters.jsonl")
    watcher.log_counter("coin_milestone", 1.0, 987_654)
    assert watcher.load_counter("coin_milestone") == 987_654
    assert watcher.load_counter("never_ran") == 0


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

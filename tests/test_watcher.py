import builtins
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

    monkeypatch.setattr(watcher.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(watcher, "ocr", lambda *a, **k: "00:32:19")

    checks = watcher._check_ocr(cfg, game)

    assert checks[0].verdict == "PASS"
    assert "3 numbers" in checks[0].detail


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


def test_unstamped_variants_are_collapsed():
    reader = watcher.ChatReader("chat_tail", similarity=0.90)
    reader._recent = [norm_line("Your camouflage outfit keeps you hidden")]

    assert reader._is_variant(
        norm_line("Your camoufiage outfit kesps you hidden")) is True


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


def test_seen_set_is_capped_and_unprimes(monkeypatch):
    """Clearing the set would let on-screen lines re-fire, so re-prime."""
    page = "\n".join(f"[16:10:{s}] You catch a desert sole."
                     for s in ("26", "31", "36"))
    sched = _chat_env([page], monkeypatch)
    reader = watcher.ChatReader("chat_tail", max_seen=2)
    reader.primed = True

    sched.begin()
    reader.read(sched)

    assert reader._seen == set()
    assert reader.primed is False


def test_registry_reports_reader_statistics(monkeypatch):
    sched = _chat_env(["[16:10:26] You catch a desert sole."], monkeypatch)
    registry = watcher.ReaderRegistry()
    reader = registry.chat("chat_tail")

    sched.begin()
    reader.read(sched)

    assert registry.stats() == [("chat", "chat_tail", 1, 1)]


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
    """No test may leak a bound GameInstance into another."""
    watcher.set_active_game(None)
    yield
    watcher.set_active_game(None)


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


# --------------------------------------------------------------------------
# WindowTracker - focus loss must not be mistaken for the game closing
# --------------------------------------------------------------------------

class FakeWindows:
    """Stands in for xdotool, with controllable visibility and identity."""

    def __init__(self, wid="0x100", size=(800, 600)):
        self.wid, self.size = wid, size
        self.visible = True
        self.exists = True
        self.lookups = 0

    def install(self, monkeypatch):
        def find(cls, min_area=100_000, visible_only=True):
            self.lookups += 1
            if not self.exists:
                return None
            if visible_only and not self.visible:
                return None
            return self.wid

        def size(wid):
            return self.size if self.exists and wid == self.wid else None

        monkeypatch.setattr(watcher, "find_window", find)
        monkeypatch.setattr(watcher, "window_size", size)
        return self


@pytest.fixture
def windows(monkeypatch):
    # Keep the tracker off the session bus. Without this, constructing a
    # WindowTracker probes KWin over D-Bus for real, which makes these tests
    # depend on the developer's desktop and fail outright in CI.
    monkeypatch.setattr(watcher, "kwin_available", lambda: (False, "test"))
    return FakeWindows().install(monkeypatch)


def test_minimised_window_reports_hidden_not_gone(windows):
    """Alt-tabbing away used to kill the watcher permanently.

    `find_window` passes --onlyvisible, so an unmapped window looked
    identical to a closed one and three capture misses called sys.exit.
    """
    windows.visible = False
    tracker = watcher.WindowTracker("game", "0x100", (800, 600))

    for _ in range(watcher.WindowTracker.MISS_LIMIT):
        due = tracker.note_miss()
    assert due

    status, _ = tracker.reacquire()
    assert status == "hidden"
    assert tracker.hidden


def test_closed_window_still_reports_gone(windows):
    """The hidden case must not mask a real exit."""
    windows.exists = False
    tracker = watcher.WindowTracker("game", "0x100", (800, 600))

    assert tracker.reacquire()[0] == "gone"


def test_restored_window_is_reacquired_under_a_new_id(windows):
    """A client restart gives the game a different window id."""
    windows.visible = False
    tracker = watcher.WindowTracker("game", "0x100", (800, 600))
    assert tracker.reacquire()[0] == "hidden"

    windows.visible = True
    windows.wid, windows.size = "0x200", (1024, 768)
    tracker._wait = 0

    status, was = tracker.reacquire()
    assert (status, was) == ("ok", "0x100")
    assert (tracker.wid, tracker.size) == ("0x200", (1024, 768))
    assert not tracker.hidden
    assert tracker.misses == 0


def test_hidden_reacquire_backs_off(windows):
    """A long alt-tab must not spawn an xdotool pair every cycle."""
    windows.visible = False
    tracker = watcher.WindowTracker("game", "0x100", (800, 600))

    for _ in range(120):
        tracker.reacquire()

    # Without backoff this would be 240 - two lookups on every cycle.
    assert windows.lookups < 40


def test_reacquire_never_adopts_a_missing_size(windows):
    """Geometry can vanish between find and size on a racing resize.

    Adopting size=None made every later resize check compare against
    nothing, reporting a phantom resize on every single cycle.
    """
    windows.visible = True
    tracker = watcher.WindowTracker("game", "0x100", (800, 600))
    windows.exists = False          # find succeeds, size returns None

    def find(cls, min_area=100_000, visible_only=True):
        return "0x100"

    watcher.find_window = find
    status, _ = tracker.reacquire()

    assert status == "hidden"
    assert tracker.size == (800, 600)


def test_note_resize_ignores_unchanged_and_missing_geometry(windows):
    tracker = watcher.WindowTracker("game", "0x100", (800, 600))

    assert tracker.note_resize((800, 600)) is False
    assert tracker.note_resize(None) is False
    assert tracker.note_resize((1024, 768)) is True
    assert tracker.size == (1024, 768)


def test_successful_cycle_clears_the_miss_counter(windows):
    tracker = watcher.WindowTracker("game", "0x100", (800, 600))

    tracker.note_miss()
    tracker.note_miss()
    tracker.note_success()

    assert tracker.misses == 0
    assert tracker.note_miss() is False


# --------------------------------------------------------------------------
# Incremental chat OCR - read only the rows that actually scrolled
# --------------------------------------------------------------------------

def _text_frame(lines, width=200, pitch=20, height=None):
    """Synthetic chat frame: one bright bar per line, at a fixed pitch.

    `detect_scroll` only looks at per-row ink counts, so bars of differing
    widths are a faithful stand-in for rendered text and make the expected
    shift exact rather than approximate.
    """
    height = height or len(lines) * pitch
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    for i, w in enumerate(lines):
        top = i * pitch
        if top + 8 <= height:
            frame[top:top + 8, :w] = 255
    return frame


def test_detect_scroll_finds_exact_shift():
    lines = [30, 60, 90, 120, 150, 180, 45, 75]
    prev = _text_frame(lines)
    # Scrolling up by two lines drops the top two and exposes two new ones.
    cur = _text_frame(lines[2:] + [100, 130])
    assert watcher.detect_scroll(prev, cur) == 40


def test_detect_scroll_reports_zero_when_nothing_moved():
    frame = _text_frame([30, 60, 90, 120, 150, 180])
    assert watcher.detect_scroll(frame, frame.copy()) == 0


def test_detect_scroll_rejects_unrelated_frames():
    """A full re-read is the correct answer when no offset explains the frame.

    Accepting a bogus shift here would OCR the wrong strip and stitch
    unrelated text onto the cache.
    """
    prev = _text_frame([30, 60, 90, 120, 150, 180])
    cur = np.zeros_like(prev)
    cur[::3, :180] = 255
    assert watcher.detect_scroll(prev, cur) is None


def test_detect_scroll_rejects_mismatched_shapes():
    assert watcher.detect_scroll(_text_frame([30, 60]),
                                 _text_frame([30, 60], width=300)) is None


def test_is_readable_keeps_real_chat_and_drops_ocr_noise():
    """Both sets are verbatim OCR output from the live game."""
    real = [
        "[14:19:41] 455 coins have been added to your money pouch.",
        "(14:19;42] You pick the target's pocket.",
        "[14:12:51] Your camouflage outfit keeps you hidden and you steal",
        "[12:03:11] You've just advanced a Thieving level! You are now level 71.",
        "You are stunned!",
        "loot.",
    ]
    noise = [
        "g e e P e S e e oL ARl Ty presaetle ]",
        "T14- 94321 ACE rrire fanses nnry aeirdart $73 3% 17 e e 1B",
        "E 8 AN AT ATE ook St P TN s At -",
        "TN AR AL roirve Ianen Ity ariciart $75 2% 17 AP st i By -",
        "[14- 13- 191 ACE rrirne e By B et 73 3% 17 e rvest ioby -",
    ]
    assert all(watcher.is_readable(line) for line in real)
    assert not any(watcher.is_readable(line) for line in noise)


def test_stitch_appends_only_new_lines():
    old = "line one\nline two\nline three"
    # The strip overlaps the previous read, so "line three" arrives twice.
    new = "line three\nline four"
    assert watcher._stitch(old, new).splitlines() == [
        "line one", "line two", "line three", "line four"]


def test_stitch_tolerates_ocr_wobble_in_the_overlap():
    """Tesseract is not byte-stable, so dedup must normalise before comparing."""
    old = "[11:03:15] You pick the target's pocket."
    new = "(11:03:15] You pick the target's pocket.\n[11:03:17] New line."
    assert watcher._stitch(old, new).splitlines() == [
        "[11:03:15] You pick the target's pocket.", "[11:03:17] New line."]


def test_stitch_is_bounded():
    """Unbounded growth would let rules read lines that scrolled off screen."""
    text = "\n".join(f"line {i}" for i in range(100))
    assert len(watcher._stitch(text, "line 100", keep=40).splitlines()) == 40


def test_stitch_discards_clipped_garbage():
    old = "[11:03:15] You pick the target's pocket."
    new = "E 8 AN AT ATE ook St P TN s At -\n[11:03:17] You find a nest."
    assert watcher._stitch(old, new).splitlines() == [
        "[11:03:15] You pick the target's pocket.",
        "[11:03:17] You find a nest."]


def test_ocr_scrolling_reads_only_the_new_strip(monkeypatch):
    """The whole point: a scrolled frame must not re-OCR the full region."""
    lines = [30, 60, 90, 120, 150, 180, 45, 75]
    first = _text_frame(lines)
    second = _text_frame(lines[2:] + [100, 130])
    frames = iter([first, second])
    monkeypatch.setattr(watcher, "capture_array",
                        lambda *a, **k: next(frames))

    seen = []

    def fake_ocr(frame, psm=6):
        seen.append(frame.shape[0])
        return "alpha\nbravo" if len(seen) == 1 else "bravo\ncharlie"

    monkeypatch.setattr(watcher, "ocr_array", fake_ocr)
    watcher._SCROLL_CACHE.clear()

    box = (0, 0, 200, 160)
    assert watcher.ocr_scrolling("0x1", box, cycle=1) == "alpha\nbravo"
    text = watcher.ocr_scrolling("0x1", box, cycle=2)

    assert seen[0] == 160                    # first pass reads everything
    assert seen[1] < 160                     # second reads only the new strip
    assert text.splitlines() == ["alpha", "bravo", "charlie"]


def test_ocr_scrolling_reuses_text_when_nothing_scrolled(monkeypatch):
    frame = _text_frame([30, 60, 90, 120, 150, 180])
    monkeypatch.setattr(watcher, "capture_array",
                        lambda *a, **k: frame.copy())
    calls = []

    def fake_ocr(f, psm=6):
        calls.append(f.shape[0])
        return "only line"

    monkeypatch.setattr(watcher, "ocr_array", fake_ocr)
    watcher._SCROLL_CACHE.clear()

    box = (0, 0, 200, 120)
    watcher.ocr_scrolling("0x1", box, cycle=1)
    assert watcher.ocr_scrolling("0x1", box, cycle=2) == "only line"
    assert len(calls) == 1                   # no second tesseract pass


def test_ocr_scrolling_falls_back_on_unrelated_frame(monkeypatch):
    first = _text_frame([30, 60, 90, 120, 150, 180])
    second = np.zeros_like(first)
    second[::3, :180] = 255
    frames = iter([first, second])
    monkeypatch.setattr(watcher, "capture_array",
                        lambda *a, **k: next(frames))
    seen = []
    monkeypatch.setattr(watcher, "ocr_array",
                        lambda f, psm=6: (seen.append(f.shape[0]), "text")[1])
    watcher._SCROLL_CACHE.clear()

    box = (0, 0, 200, 120)
    watcher.ocr_scrolling("0x1", box, cycle=1)
    watcher.ocr_scrolling("0x1", box, cycle=2)

    assert seen == [120, 120]                # both are full reads


# --------------------------------------------------------------------------
# KWin read-only window discovery (Priority 0, step 7)
# --------------------------------------------------------------------------

#: A verbatim report from the live compositor, minus unrelated windows.
KWIN_REPORT = (
    "plasmashell\t\t0\t0\t2194\t1234\tfalse\tfalse\tfalse\t{aaa}\n"
    "firefox\tmain - Mozilla Firefox\t344\t341\t594\t733\tfalse\ttrue\t"
    "false\t{bbb}\n"
    "steam_app_1343400\tRuneScape\t0\t0\t2194\t1204\tfalse\tfalse\tfalse\t"
    "{ccc}"
)


def test_parse_kwin_report_reads_live_output():
    wins = watcher._parse_kwin_report(KWIN_REPORT)

    assert [w.wm_class for w in wins] == [
        "plasmashell", "firefox", "steam_app_1343400"]
    game = wins[2]
    assert game.caption == "RuneScape"
    assert game.geometry == (0, 0, 2194, 1204)
    assert game.active is False
    assert wins[1].active is True
    assert game.internal_id == "{ccc}"


def test_parse_kwin_report_skips_malformed_rows():
    """Diagnostic data from another process must not break discovery."""
    payload = ("short\trow\n"
               "bad\twidth\t0\t0\tNOTANUMBER\t10\tfalse\tfalse\tfalse\t{x}\n"
               + KWIN_REPORT)
    assert len(watcher._parse_kwin_report(payload)) == 3


def test_kwin_find_prefers_the_largest_match():
    """A launcher can share the game's class; the game is the big one."""
    payload = (
        "steam_app_1343400\tLauncher\t0\t0\t400\t300\tfalse\tfalse\tfalse\t{a}\n"
        "steam_app_1343400\tRuneScape\t0\t0\t2194\t1204\tfalse\tfalse\tfalse\t{b}"
    )
    wins = watcher._parse_kwin_report(payload)
    assert watcher.kwin_find("steam_app_1343400", wins).caption == "RuneScape"


def test_kwin_find_returns_none_when_absent():
    wins = watcher._parse_kwin_report(KWIN_REPORT)
    assert watcher.kwin_find("no.such.class", wins) is None


def test_kwin_scale_uses_width_only():
    """Height disagrees: KWin reports frame geometry including decoration.

    Measured live at 2107 against an X11 client area of 2058, while the
    width matched exactly. Deriving scale from height would be wrong.
    """
    win = watcher.kwin_find("steam_app_1343400",
                            watcher._parse_kwin_report(KWIN_REPORT))
    assert watcher.kwin_scale(win, (3840, 2058)) == pytest.approx(1.75, abs=0.01)


def test_kwin_scale_rejects_implausible_results():
    """A mismatch means the two sources describe different windows."""
    win = watcher.kwin_find("steam_app_1343400",
                            watcher._parse_kwin_report(KWIN_REPORT))
    assert watcher.kwin_scale(win, (100, 100)) is None


def test_describe_hidden_distinguishes_minimised_from_other_desktop(
        windows, monkeypatch):
    """The whole reason to consult KWin: X11 conflates these two states."""
    tracker = watcher.WindowTracker("game", "0x100", (800, 600),
                                    use_kwin=True)

    def kwin(cls, wins=None, minimized=True):
        return watcher.KWinWindow("game", "RuneScape", 0, 0, 800, 600,
                                  minimized, False, False, "{id}")

    monkeypatch.setattr(watcher, "kwin_find", kwin)
    assert tracker.describe_hidden() == "minimised"

    monkeypatch.setattr(watcher, "kwin_find",
                        lambda c, w=None: kwin(c, minimized=False))
    assert tracker.describe_hidden() == "on another desktop"


def test_describe_hidden_is_silent_without_kwin(windows):
    """No KWin means keep the caller's existing wording, not a guess."""
    tracker = watcher.WindowTracker("game", "0x100", (800, 600),
                                    use_kwin=False)
    assert tracker.describe_hidden() is None


def test_describe_hidden_survives_a_broken_kwin(windows, monkeypatch):
    tracker = watcher.WindowTracker("game", "0x100", (800, 600),
                                    use_kwin=True)

    def boom(*a, **k):
        raise RuntimeError("dbus exploded")

    monkeypatch.setattr(watcher, "kwin_find", boom)
    assert tracker.describe_hidden() is None


def test_hidden_reacquire_reports_the_kwin_reason(windows, monkeypatch):
    windows.visible = False
    tracker = watcher.WindowTracker("game", "0x100", (800, 600),
                                    use_kwin=True)
    monkeypatch.setattr(
        watcher, "kwin_find",
        lambda c, w=None: watcher.KWinWindow("game", "RuneScape", 0, 0,
                                             800, 600, True, False, False,
                                             "{id}"))

    assert tracker.reacquire() == ("hidden", "minimised")


# --------------------------------------------------------------------------
# Wayland overlay helpers (Priority 0, step 9)
#
# The Qt/QML surface itself needs a live compositor and is verified by hand
# against the running game; what is unit-testable is the environment
# recovery that decides whether it can start at all.
# --------------------------------------------------------------------------

def _load_overlay():
    """Import tools/overlay.py without requiring PySide6 at module scope."""
    import importlib.util
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "sw_overlay", root / "tools" / "overlay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_overlay_keeps_an_existing_wayland_env(monkeypatch):
    overlay = _load_overlay()
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-9")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")

    def explode(*a, **k):
        raise AssertionError("must not scan /proc when the env is already set")

    monkeypatch.setattr(overlay.subprocess, "run", explode)
    assert overlay.ensure_wayland_env() is True


def test_overlay_recovers_wayland_env_from_the_session(monkeypatch, tmp_path):
    """Started outside the session, Qt does not fall back - it dumps core.

    Reproduced while building this: with WAYLAND_DISPLAY unset, PySide6
    aborted the interpreter rather than raising, so the variables have to be
    recovered before Qt is touched.
    """
    overlay = _load_overlay()
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    env = tmp_path / "environ"
    env.write_bytes(
        b"WAYLAND_DISPLAY=wayland-0\x00XDG_RUNTIME_DIR=/run/user/1000\x00"
        b"UNRELATED=x\x00")

    class R:
        stdout = "4242"

    monkeypatch.setattr(overlay.subprocess, "run", lambda *a, **k: R())
    monkeypatch.setattr(overlay, "Path",
                        lambda p: env if "4242" in str(p) else Path(p))

    assert overlay.ensure_wayland_env() is True
    assert os.environ["WAYLAND_DISPLAY"] == "wayland-0"
    assert os.environ["XDG_RUNTIME_DIR"] == "/run/user/1000"


def test_overlay_reports_no_wayland_session(monkeypatch):
    """An X11-only machine must get a clear refusal, not a crash."""
    overlay = _load_overlay()
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    class R:
        stdout = ""

    monkeypatch.setattr(overlay.subprocess, "run", lambda *a, **k: R())
    assert overlay.ensure_wayland_env() is False


def test_overlay_qml_declares_a_click_through_overlay_surface():
    """Guard the three properties that make this an overlay, not a window.

    Each has been verified live: LayerOverlay draws above the game,
    KeyboardInteractivityNone keeps active=False in KWin, and a negative
    exclusion zone stops the compositor reserving screen space.
    """
    qml = (Path(__file__).resolve().parents[1] / "tools" / "overlay.qml"
           ).read_text()
    assert "LayerShell.Window.layer: LayerShell.Window.LayerOverlay" in qml
    assert ("LayerShell.Window.keyboardInteractivity: "
            "LayerShell.Window.KeyboardInteractivityNone") in qml
    assert "LayerShell.Window.exclusionZone: -1" in qml
    assert "Qt.WindowTransparentForInput" in qml


# --------------------------------------------------------------------------
# Replay backend - exercise the real reader/event code without the game
# --------------------------------------------------------------------------

def _write_frames(directory, count=3, size=(120, 80)):
    """Frames with a distinguishable band per index, so order is checkable."""
    from PIL import Image
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        arr = np.zeros((size[1], size[0], 3), dtype=np.uint8)
        arr[:, :, 0] = i * 40          # red channel encodes the frame number
        Image.fromarray(arr).save(directory / f"frame{i:05d}.png")
    return directory


def test_replay_backend_reports_unusable_without_frames(tmp_path):
    backend = watcher.ReplayBackend(tmp_path)
    ok, why = backend.available()
    assert ok is False and "no frames" in why


def test_replay_backend_needs_a_directory(monkeypatch):
    monkeypatch.delenv("SCREEN_WATCHER_REPLAY", raising=False)
    ok, why = watcher.ReplayBackend("").available()
    assert ok is False and "SCREEN_WATCHER_REPLAY" in why


def test_replay_backend_takes_its_size_from_the_first_frame(tmp_path):
    """A recording made at another resolution still resolves its regions."""
    backend = watcher.ReplayBackend(_write_frames(tmp_path, size=(640, 480)))
    assert backend.find("anything") == watcher.ReplayBackend.HANDLE
    assert backend.size(backend.HANDLE) == (640, 480)


def test_replay_backend_crops_like_a_real_frame(tmp_path):
    backend = watcher.ReplayBackend(_write_frames(tmp_path))
    crop = backend.grab_array(backend.HANDLE, (10, 20, 30, 40))
    assert crop.shape == (40, 30, 3)


def test_replay_backend_advances_through_frames(tmp_path):
    backend = watcher.ReplayBackend(_write_frames(tmp_path, count=3))
    seen = [int(backend.grab_array(backend.HANDLE, (0, 0, 1, 1))[0, 0, 0])]
    while backend.advance():
        seen.append(int(backend.grab_array(backend.HANDLE, (0, 0, 1, 1))[0, 0, 0]))
    assert seen == [0, 40, 80]


def test_replay_backend_holds_on_the_last_frame(tmp_path):
    """Looping would replay a one-off event forever - a manufactured alert."""
    backend = watcher.ReplayBackend(_write_frames(tmp_path, count=2))
    backend.advance()
    assert backend.advance() is False
    assert int(backend.grab_array(backend.HANDLE, (0, 0, 1, 1))[0, 0, 0]) == 40


def test_replay_backend_rejects_an_out_of_bounds_region(tmp_path):
    backend = watcher.ReplayBackend(_write_frames(tmp_path, size=(100, 100)))
    with pytest.raises(watcher.CaptureError):
        backend.grab_array(backend.HANDLE, (500, 500, 50, 50))


def test_replay_backend_is_registered(tmp_path):
    assert watcher.BACKENDS["replay"] is watcher.ReplayBackend


def test_replay_backend_reads_the_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("SCREEN_WATCHER_REPLAY", str(_write_frames(tmp_path)))
    assert watcher.ReplayBackend().available()[0] is True


# --------------------------------------------------------------------------
# Poll scheduling - a late cycle must not cause a catch-up burst
# --------------------------------------------------------------------------

def test_next_deadline_advances_one_interval_when_on_time():
    assert watcher.next_deadline(10.0, 1.5, now=10.2) == pytest.approx(11.5)


def test_next_deadline_skips_missed_deadlines():
    """Advancing by one interval fires immediately when already overdue."""
    # Deadline was 11.5; it is now 18.0, so 11.5, 13.0, 14.5, 16.0 and 17.5
    # have all passed. The next real boundary is 19.0.
    assert watcher.next_deadline(10.0, 1.5, now=18.0) == pytest.approx(19.0)


def test_next_deadline_preserves_phase():
    """Recovery keeps the original cadence rather than restarting from now.

    Restarting at `now` would work too, but drifts the schedule on every
    slow cycle; preserving phase keeps poll times predictable.
    """
    deadline = watcher.next_deadline(0.0, 1.5, now=7.05)
    assert deadline == pytest.approx(7.5)
    assert (deadline / 1.5) == pytest.approx(round(deadline / 1.5))


def test_next_deadline_recovers_a_steady_cadence_after_a_stall():
    """The regression: a 7s stall used to fire three cycles 0.05s apart."""
    interval, deadline, now = 1.5, 0.0, 0.0
    gaps, last = [], 0.0
    for work in (0.05, 0.05, 7.0, 0.05, 0.05, 0.05):
        now += work
        deadline = watcher.next_deadline(deadline, interval, now=now)
        now += max(0.0, deadline - now)
        gaps.append(now - last)
        last = now

    assert gaps[2] == pytest.approx(7.5)          # the slow cycle itself
    for gap in gaps[3:]:
        assert gap == pytest.approx(interval)     # never a burst


def test_next_deadline_handles_an_exactly_due_deadline():
    """`now` landing exactly on the deadline must still move forward."""
    assert watcher.next_deadline(10.0, 1.5, now=11.5) == pytest.approx(13.0)


def test_terminate_releases_the_singleton(tmp_path, monkeypatch):
    """SIGTERM/SIGINT must not leave a stale pid file behind.

    atexit alone was not enough: measured in isolation, catching
    KeyboardInterrupt in __main__ did not reliably run atexit handlers when
    the signal arrived via `kill` rather than from a terminal.
    """
    pid_file = tmp_path / "watcher.pid"
    pid_file.write_text(str(os.getpid()))
    monkeypatch.setattr(watcher, "PID_FILE", pid_file)

    with pytest.raises(SystemExit):
        watcher._terminate(watcher.signal.SIGTERM, None)

    assert not pid_file.exists()


def test_terminate_leaves_another_process_pid_file_alone():
    """Only the owner removes it, or a restart could delete a live claim."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        pid_file = Path(d) / "watcher.pid"
        pid_file.write_text(str(os.getpid() + 1))
        original = watcher.PID_FILE
        watcher.PID_FILE = pid_file
        try:
            watcher._release_singleton()
            assert pid_file.exists()
        finally:
            watcher.PID_FILE = original

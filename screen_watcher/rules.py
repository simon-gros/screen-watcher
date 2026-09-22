"""Rule definitions and the detectors that evaluate them.

Extracted from `watcher.py` as part of the modular split. The largest
single piece: the Rule dataclass, the Alert it produces, and the fourteen
evaluators behind the rule kinds a profile can declare.

Everything it borrows - capture_array, the OCR entry points, the
occupancy and counter logs - resolves through a late import of `watcher`.
That is not ceremony: fifty tests patch `watcher.ocr_array`,
`watcher.ocr_cached` and `watcher.capture_array` to feed the evaluators
synthetic frames and chat, and binding those names here would make every
one of them reach nothing while still passing.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

import numpy as np


def _w():
    """The `watcher` module, imported late."""
    import watcher
    return watcher


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Alert:
    """A rule result that can be delivered by any notification backend."""

    rule_name: str
    title: str
    body: str
    urgency: str
    sound: str | None
    timeout_ms: int
    source_text: str | None = None


@dataclass
class Rule:
    name: str
    kind: str                       # idle | change | ocr
    region: str
    message: str = ""
    alert_body: str = ""
    out_alert_body: str = ""
    cooldown: float = 120.0
    mask: str | None = None
    sound: str | None = None
    urgency: str = "normal"
    timeout_ms: int = 8000
    # idle / change
    idle_seconds: float = 45.0
    threshold: float = 2.0
    # ocr
    pattern: str | None = None
    # inventory
    capacity: int = 28
    lead_seconds: float = 90.0
    warn_free: int = 3
    # Minimum occupancy before a projected-fill alert may fire. 0 disables
    # the guard. For a grind where something empties the pack mid-run (a
    # wood box, a bank preset), this stops a reset fill-history producing
    # a bogus ETA at low occupancy. The warn_free floor ignores it.
    min_occupancy: int = 0
    cell_threshold: float = 8.0
    # "lead" warns ahead of time from the projected fill rate; "overflow" waits
    # until the pack has actually been full for overflow_seconds. Short cycles
    # want overflow - see _eval_overflow.
    mode: str = "lead"
    overflow_seconds: float = 20.0
    log_occupancy: bool = True
    # activity
    stop_seconds: float = 25.0
    suppress_pattern: str | None = None
    # gauge
    maximum: int = 0
    warn_below: float = 0.0
    warn_at: int = 0
    warn_at_or_above: int = 0
    column: int = 0
    prime_on_start: bool = False
    confirm_readings: int = 2
    # supply
    item: str = "supplies"
    trip_pattern: str | None = None
    out_pattern: str | None = None
    out_message: str = ""
    warn_streak: int = 3
    # loot
    item_pattern: str | None = None
    # presence
    colour_lo: tuple = (150, 80, 0)
    colour_hi: tuple = (255, 200, 90)
    present_above: int = 200
    absent_seconds: float = 12.0
    corroborate_region: str | None = None
    corroborate_pattern: str | None = None
    # An `activity` rule may require supplies to still be present before it
    # reports a stop. Distinguishes "the activity died on its own" from
    # "the consumable ran out", which look identical in chat.
    require_items_region: str | None = None
    require_items_above: int = 1
    # stack
    stack_tolerance: int = 3
    new_slot_only: bool = False
    ignore_pattern: str | None = None
    # counter
    step: float = 1_000_000
    milestone_message: str = "{total} reached."
    # item_count
    min_blue: float = 40.0
    min_red: float = 0.0
    min_cover: float = 0.30
    warn_below: int = 2
    out_below: int = 0
    confirm_seconds: float = 6.0
    repeat_seconds: float = 0.0
    enabled: bool = True

    _last: np.ndarray | None = field(default=None, repr=False)
    _last_change: float = field(default=0.0, repr=False)
    _last_fired: float = field(default=0.0, repr=False)
    _armed: bool = field(default=True, repr=False)
    _seen: set = field(default_factory=set, repr=False)
    _low_streak: int = field(default=0, repr=False)
    _high_streak: int = field(default=0, repr=False)
    _last_reading: int | None = field(default=None, repr=False)
    _primed: bool = field(default=False, repr=False)
    _history: list = field(default_factory=list, repr=False)
    _full_since: float = field(default=0.0, repr=False)
    _last_activity: float = field(default=0.0, repr=False)
    _last_activity_source: str | None = field(default=None, repr=False)
    _streak: int = field(default=0, repr=False)
    _level: str = field(default="", repr=False)
    _counts: dict = field(default_factory=dict, repr=False)
    _stacks: list = field(default_factory=list, repr=False)
    _pending_slots: set = field(default_factory=set, repr=False)
    _pending_since: float = field(default=0.0, repr=False)
    _pending_base: list = field(default_factory=list, repr=False)
    _elapsed: int = field(default=0, repr=False)
    _absent_since: float = field(default=0.0, repr=False)
    _corroborate_box: tuple | None = field(default=None, repr=False)
    _items_box: tuple | None = field(default=None, repr=False)
    _items_grid: tuple | None = field(default=None, repr=False)
    _total: int = field(default=0, repr=False)
    _milestone: int = field(default=0, repr=False)
    _level_since: float = field(default=0.0, repr=False)

    def ready(self, now: float) -> bool:
        # _last_fired == 0.0 means this rule has never fired. Treating that as a
        # real timestamp compares against the epoch, so with a long cooldown the
        # rule stays muted until `cooldown` seconds after the watcher starts -
        # silently swallowing the first alert, which for a supply rule is the
        # one that matters most.
        if self._last_fired == 0.0:
            return True
        return (now - self._last_fired) >= self.cooldown

    def fire(self, now: float, body: str, source_text: str | None = None,
             body_template: str | None = None, **context) -> Alert:
        self._last_fired = now
        rendered = body
        template = self.alert_body if body_template is None else body_template
        if template:
            values = {
                "body": body,
                "line": source_text or body,
                "source_text": source_text or "",
                "text": source_text or body,
                **context,
            }
            try:
                rendered = template.format(**values)
            except (KeyError, IndexError, ValueError, AttributeError) as exc:
                print(f"rule {self.name!r}: invalid alert body template "
                      f"({exc}); using detector message", file=sys.stderr,
                      flush=True)
        return Alert(self.name, self.message or self.name, rendered,
                     self.urgency, self.sound, self.timeout_ms, source_text)

    def reset(self) -> None:
        self._last = None
        self._armed = True
        self._history.clear()
        self._full_since = 0.0
        self._last_activity = 0.0
        self._last_activity_source = None
        self._primed = False
        self._level = ""
        self._level_since = 0.0
        self._absent_since = 0.0
        self._stacks.clear()
        self._pending_slots = set()


def _eval_inventory(rule: Rule, wid: str, region: "_w().Region", box, now: float,
                    cycle: int = 0) -> Alert | None:
    """Warn *before* the pack fills, with enough lead time to reach a bank.

    A 'pack is full' alert is useless: by then the grind has already stopped.
    So this tracks the fill rate and fires when the projected time-to-full
    drops under lead_seconds.
    """
    if not region.grid:
        return
    frame = _w().capture_array(wid, box, cycle=cycle)
    occ, cells = _w().count_occupied(frame, region.grid, threshold=rule.cell_threshold)

    # A bank, loot or level-up interface drawn over the backpack makes every
    # covered cell read as occupied, and a mid-bank capture can instead catch
    # the panel half-drawn and read near-empty. Either way the number is a lie,
    # and feeding it to the fill-rate history corrupts the ETA for minutes.
    # Exceeding capacity is the reliable tell for the overlay case; drop those
    # frames entirely rather than trusting them.
    if occ > rule.capacity:
        return

    free = max(0, rule.capacity - occ)

    prev = rule._history[-1][1] if rule._history else None
    # Only one rule may write the occupancy log. Two inventory rules watching
    # the same backpack (a lead warning plus an overflow backstop) would
    # otherwise double every transition, which silently corrupts `stats` -
    # duplicate rows inflate the cycle count and halve the apparent fill rate.
    if (prev is None or occ != prev) and rule.log_occupancy:
        _w().log_occupancy(now, occ)
    # Banking empties the pack; drop the old trend and re-arm for the next run.
    if prev is not None and occ < prev - 2:
        rule._history.clear()
        rule._armed = True
    rule._history.append((now, occ))
    if len(rule._history) > 400:
        del rule._history[:200]

    rate = _w().fill_rate(rule._history)
    eta = free / rate if rate > 1e-6 else float("inf")

    if rule.mode == "overflow":
        return _eval_overflow(rule, now, occ, free)

    if not rule._armed:
        return
    hit_lead = eta <= rule.lead_seconds
    hit_floor = free <= rule.warn_free
    # An ETA alone is not enough when something empties the pack mid-grind.
    # A Magic wood box swallows logs in batches, so occupancy falls 7->1
    # without any banking; that clears the fill history, and the next few
    # rapid gains look like an explosive fill rate. MEASURED against a real
    # wood-box sequence: the rule fired "18 slots left - bank soon" at
    # 10/28 and was then disarmed all the way to 27/28, so it cried wolf
    # early and stayed silent when the pack genuinely was about to fill.
    # `min_occupancy` requires the pack to actually be filling up before a
    # projection may fire. The slot floor is unaffected: it reads the
    # current count, which is true whatever the history says.
    if rule.min_occupancy > 0 and occ < rule.min_occupancy:
        hit_lead = False
    if (hit_lead or hit_floor) and rule.ready(now):
        rule._armed = False
        if hit_lead and eta != float("inf"):
            body = (f"{free} slots left, filling at {rate*60:.1f}/min - "
                    f"full in ~{eta:.0f}s. Head to the bank.")
        else:
            body = f"{free} slots left. Head to the bank."
        return rule.fire(now, body, free=free, rate=rate, eta=eta)


def _eval_overflow(rule: Rule, now: float, occ: int, free: int) -> Alert | None:
    """Fire only once the pack has *stayed* full, i.e. fishing has halted.

    Predictive alerting is the wrong tool for a short cycle. Measured here the
    fill is ~73s end to end, so a lead-time warning fires once per bank trip -
    ~50 times an hour - and says nothing the player has not already noticed.
    Peak occupancy averaged 27.0/28, so those trips were being banked correctly
    without any prompt; the alert was pure noise.

    Overflow is the event actually worth interrupting for. A full pack does not
    waste catches - RS3 halts fishing outright with "You can't carry any more
    fish" - so the cost is idle time, not lost fish. Every second past full is a
    second not spent fishing. A single frame at capacity is not enough, because
    that happens on every normal trip in the moment before banking. Requiring
    the state to persist is what separates 'about to bank' from 'AFK'.
    """
    if free > 0:
        # Any free slot means the pack is no longer full: re-arm for next time.
        rule._full_since = 0.0
        rule._armed = True
        return

    if rule._full_since == 0.0:
        rule._full_since = now
        return

    stuck = now - rule._full_since
    if rule._armed and stuck >= rule.overflow_seconds and rule.ready(now):
        rule._armed = False
        return rule.fire(now, f"Pack full for {stuck:.0f}s - activity has stopped. "
                         f"Check the game.", stuck=stuck)


def _eval_activity(rule: Rule, wid: str, box, now: float,
                   cycle: int = 0) -> Alert | None:
    """Alert when a recurring chat line *stops* arriving.

    The inverse of an `ocr` rule: instead of firing on a message, this fires on
    the absence of one. A fishing spot is depleted and moves elsewhere without
    announcing itself - the only evidence is that 'You catch a ...' stops.

    Measured over 166 real catch intervals: median 3.7s, p99 19.7s, max 21.3s.
    So `stop_seconds` wants to sit above ~22s to clear normal variance. This is
    much tighter than inferring the same thing from Metrics XP, which needs a
    60s window to be safe and cannot tell 'spot gone' from 'client minimised'.

    Only alerts if activity was seen *first*. Without that, starting the watcher
    while docked at a bank would immediately claim fishing had stopped.

    `suppress_pattern` marks a stop the player already knows about. A full pack
    halts fishing outright ("You can't carry any more fish"), so catch messages
    stop on *every* bank trip. Without suppression this rule would fire once per
    cycle - ~50 times an hour at the measured 73s cycle - which is the same
    noise the inventory rule was retuned to avoid. Seeing that message means the
    stop is explained, so stay quiet until fishing actually resumes.
    """
    text = _w().ocr_cached(wid, box, cycle)
    for line in text.splitlines():
        line = line.strip()
        key = _w().norm_line(line)
        if len(key) < 8 or _w().seen_before(key, rule._seen):
            continue
        rule._seen.add(key)
        if rule.suppress_pattern and re.search(rule.suppress_pattern, line, re.I):
            # An explained stop. Disarm rather than touch the activity clock, so
            # resuming still needs a real catch to re-arm.
            if rule._primed:
                rule._armed = False
            continue
        if re.search(rule.pattern, line, re.I):
            rule._last_activity = now
            rule._armed = True
            rule._last_activity_source = line
    if len(rule._seen) > 400:
        rule._seen.clear()
        rule._primed = False

    if not rule._primed:
        # Prime the visible scrollback, but do not invent an activity timestamp.
        # A watcher started while idle must remain silent indefinitely.
        rule._primed = True
        return

    # Never seen activity at all: nothing to report stopping.
    if rule._last_activity == 0.0:
        return

    quiet = now - rule._last_activity
    if not (rule._armed and quiet >= rule.stop_seconds and rule.ready(now)):
        return
    # Optionally require supplies to still be present. A bonfire dying and
    # the pack running empty both stop the chat line, and chat alone cannot
    # tell them apart - the RS3 burnout is silent, confirmed by the player.
    # Items still in the pack mean the supply is not the cause, so the
    # activity stopped on its own. Without this the two events share one
    # message and neither names its own remedy.
    if rule.require_items_region and rule._items_box:
        frame = _w().capture_array(wid, rule._items_box, cycle=cycle)
        occupied, _cells = _w().count_occupied(frame, rule._items_grid)
        if occupied < max(1, int(rule.require_items_above)):
            return
    rule._armed = False
    return rule.fire(now, f"No matching activity for {quiet:.0f}s. "
                     f"Check the game.",
                     source_text=rule._last_activity_source, quiet=quiet)


def count_by_colour(frame: np.ndarray, grid: tuple, min_blue: float,
                    lum_floor: float = 90.0, pad: float = 0.22,
                    min_cover: float = 0.30, min_red: float = 0.0) -> int:
    """Count backpack slots whose icon matches a colour signature.

    `min_blue` selects blue-dominant icons (blue minus red); `min_red`
    selects red/brown-dominant ones (red minus blue). They are opposite ends
    of the same axis, so a rule sets one or the other.

    Desert sole - the food carried at Arch-Glacor - measured +64 to +66 on
    the red axis across thirteen slots, against -48 to +54 for every other
    item in the same pack, so a threshold near +60 separates them cleanly.

    Reading the stack digits was tried first and abandoned: they are small,
    anti-aliased and drawn over the icon, and OCR of an unchanging stack was
    measured flickering between 6, 13 and 1. Colour is far steadier.

    Decorated fishing urns are strongly blue; everything else carried on this
    grind is not. Measured over one backpack (blueness = mean B - mean R of the
    icon's bright pixels):

        urns   +104, +105
        coins  -174
        fish   -10 .. -62

    A threshold near +40 therefore sits ~60 away from both populations.
    `min_cover` rejects slots whose icon barely fills the cell, which is what a
    part-drawn panel or a tooltip edge looks like. It is configurable because
    icon bulk varies: an urn fills ~0.4 of its cell, while a desert sole - a
    slim fish - fills only 0.22 and was rejected outright by the 0.30 default.
    """
    x0, y0, cw, ch, cols, rows = grid
    n = 0
    for r in range(rows):
        for c in range(cols):
            x, y = int(x0 + c * cw), int(y0 + r * ch)
            px, py = int(cw * pad), int(ch * pad)
            patch = frame[y + py:y + ch - py, x + px:x + cw - px]
            if patch.size == 0:
                continue
            lum = patch.mean(axis=2)
            icon = lum > lum_floor
            if icon.mean() < min_cover:
                continue
            if not icon.any():
                # No bright pixels at all: an empty slot. With min_cover=0
                # the check above lets this through, and the mean of an
                # empty selection is NaN - which compares False and so
                # gave the right answer by accident, with a RuntimeWarning
                # on every empty cell of every poll.
                continue
            red, _green, blue = patch[icon].mean(axis=0)
            if min_red > 0.0:
                matched = float(red) - float(blue) >= min_red
            else:
                matched = float(blue) - float(red) >= min_blue
            if matched:
                n += 1
    return n


def _note_activity(rule: Rule, wid: str, now: float, cycle: int) -> bool:
    """Refresh `_last_activity` from the corroboration region.

    Returns False when the rule is not configured to corroborate, so a
    caller can tell "no evidence of activity" apart from "not asked to
    look". Only lines that are new since the last poll count: the chat
    tail keeps old lines on screen for many cycles, so matching whatever
    is visible would make stale scrollback look like fresh activity
    forever.
    """
    if not (rule._corroborate_box and rule.corroborate_pattern):
        return False
    text = _w().ocr_cached(wid, rule._corroborate_box, cycle)
    visible = set()
    for line in text.splitlines():
        key = _w().norm_line(line.strip())
        if len(key) < 8:
            continue
        visible.add(key)
        if key in rule._seen:
            continue
        rule._seen.add(key)
        if re.search(rule.corroborate_pattern, line, re.I):
            rule._last_activity = now
            rule._last_activity_source = line.strip()
    if len(rule._seen) > 400:
        # Retain the viewport as the new baseline; clearing outright would
        # make old scrollback look fresh on the next poll.
        rule._seen = visible
    return True


def _eval_item_count(rule: Rule, wid: str, region: "_w().Region", box,
                     now: float, cycle: int = 0) -> Alert | None:
    """Warn when a carried item runs low, counted by icon colour.

    `supply` rules watch the *bank* coming up short across trips. This watches
    what is actually in the backpack right now, which is the question behind
    'I only have one urn left'. The two are independent: the bank can be full
    while the pack is nearly out, and vice versa.

    Counting is deliberately conservative. A bank or loot interface drawn over
    the backpack hides slots, which would read as a sudden drop to zero and fire
    a false 'out' alert. So a low reading must persist for `confirm_seconds`
    before it counts - a real consumable drains slot by slot and stays drained,
    while an overlay clears within a frame or two.

    Being out of urns is a *persisting* loss, not a moment: every fish caught
    while empty earns no urn XP, and that continues until it is fixed. A single
    alert is therefore not enough - it was observed firing once and then going
    quiet for 8 minutes while fishing continued with no urns. `repeat_seconds`
    re-arms an unresolved 'out' so it keeps reminding, unlike a one-shot event
    such as a spot moving.
    """
    if not region.grid:
        return
    # Read corroboration every cycle, before any early return: the activity
    # clock has to keep advancing while the count is healthy, or the first
    # dip to "low" would see a stale `_last_activity` and stay silent.
    corroborates = _note_activity(rule, wid, now, cycle)
    frame = _w().capture_array(wid, box, cycle=cycle)
    n = count_by_colour(frame, region.grid, rule.min_blue,
                        min_red=rule.min_red,
                        min_cover=rule.min_cover)

    # Track how long the count has been at or under each threshold.
    if n <= rule.out_below:
        level = "out"
    elif n <= rule.warn_below:
        level = "low"
    else:
        level = "ok"

    if level != rule._level:
        rule._level = level
        rule._level_since = now
        # Prime on the transition too, not only on a settled "ok". The very
        # first reading always changes level (from "") and returned here,
        # so a rule whose first sight of the pack was healthy never primed
        # and then stayed silent for the whole session.
        if level == "ok":
            rule._primed = True
        return

    if level == "ok":
        rule._armed = True
        rule._primed = True
        return
    # A shortage that was already there when the watcher started is not an
    # event. Observed firing six times with no fishing under way at all:
    # the watcher was started with an empty or banked backpack, and since
    # `_armed` defaults to True the rule announced "No urns remain" within
    # `confirm_seconds` of launch. Requiring a prior "ok" reading means the
    # count has to be seen draining, which is what the alert claims.
    # Opt-in, exactly as for gauge and percent rules, so an item that is
    # genuinely meant to alert from a cold start can still do so.
    if rule.prime_on_start and not rule._primed:
        return
    # An unresolved "out" re-arms on a timer: the loss is ongoing, so one alert
    # that scrolls past is not enough. "low" stays one-shot - it is advice, and
    # repeating it while the count is legitimately low would just be nagging.
    if (not rule._armed and level == "out" and rule.repeat_seconds > 0
            and (now - rule._last_fired) >= rule.repeat_seconds):
        rule._armed = True
    if not rule._armed or (now - rule._level_since) < rule.confirm_seconds:
        return
    if not rule.ready(now):
        return
    # Last gate: is the activity actually running? Priming stops a cold
    # start alerting, but not the mid-session case - bank, then stand idle
    # with an empty pack, and the shortage is real while the alert is
    # useless, because nothing is being consumed. `absent_seconds` reuses
    # the presence rule's field: no activity line for that long means the
    # grind has stopped, so the shortage can wait until it resumes.
    if corroborates and rule.absent_seconds > 0:
        if now - rule._last_activity >= rule.absent_seconds:
            return

    rule._armed = False
    if level == "out":
        stuck = now - rule._level_since
        extra = f" (empty for {stuck/60:.0f} min)" if stuck >= 120 else ""
        return rule.fire(now, (rule.out_message or f"Out of {rule.item}.") + extra,
                         body_template=rule.out_alert_body,
                         n=n, level=level, stuck=stuck)
    else:
        noun = rule.item.rstrip("s") if n == 1 else rule.item
        return rule.fire(now, f"{n} {noun} left. Restock on the next bank trip.",
                         n=n, noun=noun, level=level)


def _eval_supply(rule: Rule, wid: str, box, now: float,
                 cycle: int = 0) -> Alert | None:
    """Track a consumable across bank restocks: running low, then exhausted.

    RS3 never states how much bait or how many urns remain, so a count is not
    available - not from chat, and not reliably from the backpack either: the
    stack digits are small, anti-aliased and drawn over the icon, and OCR of
    them was measured flickering between 6, 13 and 1 on a stack that never
    changed. Alerts built on that number would be worse than none.

    What *is* reliable is the preset loader. Each bank trip it reports items it
    could not supply in full. Measured over 4 preset loads: bait and urn
    shortfalls appeared on every single one, because the preset routinely asks
    for more than the bank holds. So a single shortfall means nothing.

    The signal is in the *streak*. A bank with plenty of stock satisfies the
    preset eventually; a bank running dry fails trip after trip. `warn_streak`
    consecutive failures means low, and `out_pattern` - the loader reporting it
    could not supply the item at all, with no fallback - means exhausted.

    The streak resets whenever a trip loads the item cleanly, so restocking
    silences the rule without any manual action.
    """
    text = _w().ocr_cached(wid, box, cycle)
    saw_trip = False
    saw_fail = False
    saw_out = False
    fail_line = None
    out_line = None
    for line in text.splitlines():
        line = line.strip()
        key = _w().norm_line(line)
        if len(key) < 8 or _w().seen_before(key, rule._seen):
            continue
        rule._seen.add(key)
        if rule.trip_pattern and re.search(rule.trip_pattern, line, re.I):
            saw_trip = True
        if rule.out_pattern and re.search(rule.out_pattern, line, re.I):
            saw_out = True
            out_line = line
        if re.search(rule.pattern, line, re.I):
            saw_fail = True
            fail_line = line
    if len(rule._seen) > 400:
        rule._seen.clear()
        rule._primed = False

    # Scrollback on the first pass predates the watcher; count it, never alert.
    if not rule._primed:
        rule._primed = True
        return

    if saw_out and rule.ready(now):
        rule._streak = 0
        return rule.fire(now, rule.out_message or
                         f"Out of {rule.item}. Restock before the next trip.",
                         source_text=out_line)

    if saw_trip:
        if saw_fail:
            rule._streak += 1
            if rule._streak >= rule.warn_streak and rule.ready(now):
                return rule.fire(
                    now, f"{rule.item} low - the bank has come up short "
                    f"{rule._streak} trips running. Restock soon.",
                    source_text=fail_line)
        else:
            # A clean load means the bank is stocked again.
            rule._streak = 0


def stack_signature(frame: np.ndarray, grid: tuple, i: int) -> int:
    """Count stack-digit pixels in one slot.

    RS3 draws stack counts as yellow-green text in the slot's top-left. Reading
    the *number* was tried and abandoned: the digits are small, anti-aliased and
    drawn over the icon, and tesseract managed only 5 of 9 slots correctly even
    after tuning the crop, returning values like 1271 where two adjacent slots
    bled together.

    The pixel count is far steadier, because it does not need to resolve glyph
    shapes - only how much digit ink is present. Measured drift on an unchanging
    stack is +/-2 pixels, while a quantity change moves it by 7-24. That is a
    wide enough margin to detect 'this stack grew' without ever knowing by how
    much, which is all a drop alert needs.
    """
    x0, y0, cw, ch, cols, _rows = grid
    x, y = int(x0 + (i % cols) * cw), int(y0 + (i // cols) * ch)
    patch = frame[y + 1:y + int(ch * 0.38), x + 1:x + int(cw * 0.70)]
    if patch.size == 0:
        return 0
    red, green, blue = patch[:, :, 0], patch[:, :, 1], patch[:, :, 2]
    return int(((red > 120) & (green > 120) & (blue < 120)).sum())


def colour_pixels(frame: np.ndarray, lo: tuple, hi: tuple) -> int:
    """Count pixels inside an inclusive RGB box."""
    red, green, blue = frame[:, :, 0], frame[:, :, 1], frame[:, :, 2]
    return int(((red >= lo[0]) & (red <= hi[0])
                & (green >= lo[1]) & (green <= hi[1])
                & (blue >= lo[2]) & (blue <= hi[2])).sum())


def _eval_presence(rule: Rule, wid: str, box, now: float,
                   cycle: int = 0) -> Alert | None:
    """Alert when a distinctive on-screen indicator disappears.

    The icon is useful but not authoritative: live measurements showed a
    continuous 60s icon blackout while pickpocketing continued. When a
    corroborating chat region is configured, track it throughout the blackout
    instead of waiting until the stop threshold has already expired.

    Existing scrollback is primed when the icon first disappears, so an old
    success line cannot be mistaken for fresh evidence 75s later. Only lines
    that appear after the blackout begins extend `_last_activity`.
    """
    frame = _w().capture_array(wid, box, None, cycle)
    n = colour_pixels(frame, tuple(rule.colour_lo), tuple(rule.colour_hi))
    present = n >= rule.present_above

    if present:
        rule._absent_since = 0.0
        rule._last_activity = now
        rule._armed = True
        rule._seen.clear()
        return None

    corroborates = bool(rule._corroborate_box and rule.corroborate_pattern)
    if rule._absent_since == 0.0:
        rule._absent_since = now
        rule._last_activity = now
        if corroborates:
            # Prime the visible scrollback without treating it as new activity.
            text = _w().ocr_cached(wid, rule._corroborate_box, cycle)
            rule._seen = {
                key for line in text.splitlines()
                if len(key := _w().norm_line(line.strip())) >= 8
            }
        return None

    if corroborates:
        text = _w().ocr_cached(wid, rule._corroborate_box, cycle)
        visible = set()
        for line in text.splitlines():
            key = _w().norm_line(line.strip())
            if len(key) < 8:
                continue
            visible.add(key)
            if key in rule._seen:
                continue
            rule._seen.add(key)
            if re.search(rule.corroborate_pattern, line, re.I):
                # Fresh independent evidence that the activity is still alive.
                rule._last_activity = now
                rule._last_activity_source = line.strip()
        if len(rule._seen) > 400:
            # Retain the current viewport as the new baseline. Clearing the set
            # outright would make old scrollback look fresh on the next poll.
            rule._seen = visible

    gone = now - rule._absent_since
    if not rule._armed or gone < rule.absent_seconds or not rule.ready(now):
        return None
    if corroborates and now - rule._last_activity < rule.absent_seconds:
        return None

    rule._armed = False
    return rule.fire(now, f"No activity icon for {gone:.0f}s - "
                          f"{rule.item} has stopped.", gone=gone)


TIMER_RE = re.compile(r"(\d{1,2}):([0-5]\d):([0-5]\d)")

#: OCR confusions seen on the RS3 vitals row, where the separator between a
#: current and maximum value renders as a bracket or a letter.
_GAUGE_FIXUPS = str.maketrans({"[": "/", "]": "/", "I": "/", "|": "/",
                               "&": "8", "l": "1", "O": "0", "o": "0"})


def parse_total(text: str, column: int = 0) -> int | None:
    """Read one column of a numeric Metrics row.

    The gold row prints three figures side by side - Gain, Drops and GP/h -
    so a rule has to say which it wants. `column` is a zero-based index
    into the numbers found, left to right.

    RS3 abbreviates large values, and the suffix carries the magnitude:
    "1.2M" is 1,200,000, not 1.2. Dropping it would understate the total by
    six orders of magnitude and the milestone would never fire.
    """
    # Only fix an O that sits against other digits. A blanket substitution
    # turned "no numbers here" into "n0 numbers here" and read it as zero.
    cleaned = re.sub(r"(?<=\d)[Oo]|[Oo](?=\d)", "0", text.replace(",", ""))
    values = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*([KMB])?", cleaned, re.I):
        raw, suffix = m.group(1), (m.group(2) or "").upper()
        try:
            value = float(raw)
        except ValueError:
            continue
        scale = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1)
        if suffix or value == int(value):
            values.append(int(value * scale))
    return values[column] if len(values) > column else None


def parse_percent(text: str) -> int | None:
    """Read a bare percentage, such as the adrenaline readout.

    Adrenaline is the one vitals field with no maximum to anchor against -
    it is printed as "100%", not "100/100" - so `parse_gauge` cannot read
    it and it needs the percent sign as its anchor instead.

    Values above 100 are rejected. Adrenaline cannot exceed 100%, so a
    larger number means the digits ran together with the life total beside
    them, and a confident wrong reading is worse than none.
    """
    best = None
    for m in re.finditer(r"(\d{1,3})\s*%", text):
        value = int(m.group(1))
        if 0 <= value <= 100:
            best = value
    return best


def parse_gauge(text: str, maximum: int, tolerance: int | None = None
                ) -> tuple[int, int] | None:
    """Read a ``current/maximum`` pair whose maximum is already known.

    The RS3 life/prayer row decodes with the icons as noise and the slash
    frequently mangled - ``I§9,347[1o,597 @85% @3&2[7&0`` is a real reading
    of 9,347/10,597 health, 85% adrenaline and 382/780 prayer. Parsing that
    as free text is hopeless.

    Knowing the maximum makes it tractable: find that number in the stream
    and take the value immediately before it. `tolerance` allows the
    maximum's own digits to be misread, since a gauge maximum is fixed for
    a given character and any near match is the right anchor.

    The default tolerance scales with the maximum - 0.5%, at least 2 - and
    that matters. A 10,597 life pool was observed reading as both 10,597
    and 10,557, because OCR confuses 9 and 5 in this font. A flat tolerance
    of 2 dropped those frames entirely, and with `confirm_readings` needing
    consecutive low readings, losing alternate frames delays a critical
    health alert at exactly the moment it is needed.

    Returns ``(current, maximum)``, or None when no plausible pair is found.
    Values above the maximum are rejected rather than clamped: they mean the
    reading was wrong, and a confident wrong number is worse than silence.
    """
    if tolerance is None:
        tolerance = max(2, int(maximum * 0.005))
    fixed = text.translate(_GAUGE_FIXUPS)
    values = []
    # A period is accepted as a thousands separator. OCR renders the comma
    # in "2,309" as a full stop often enough that ignoring it read the
    # number as 309 - a tenfold underread that fires a false critical
    # health alert. Only a period followed by exactly three digits is
    # treated this way, so a genuine decimal is not silently multiplied.
    for m in re.finditer(r"\d[\d,.]*\d|\d", fixed):
        token = m.group(0)
        if not re.fullmatch(r"\d+|\d{1,3}(?:[,.]\d{3})+", token):
            continue
        try:
            value = int(token.replace(",", "").replace(".", ""))
        except ValueError:
            continue
        # A number followed by '%' is the adrenaline readout, not a gauge
        # value. Without this, '@100% @ 780/780' read prayer as 100 - the
        # percentage happens to sit immediately before the maximum.
        percent = fixed[m.end():m.end() + 2].lstrip().startswith("%")
        values.append((value, percent))

    # Walk right to left. The *last* occurrence of the maximum closes the
    # pair, which matters when current == maximum and the stream reads
    # "... 10,597 / 10,597 ...": matching the first one treats whatever
    # precedes it as the current value. Observed live as "I 6 10,597/10,597"
    # - leading icon noise - which read health as 6 and raised a critical
    # alert at full health.
    for i in range(len(values) - 1, 0, -1):
        if abs(values[i][0] - maximum) > tolerance or values[i][1]:
            continue
        current, current_is_percent = values[i - 1]
        if current_is_percent:
            # The real current value is separated from its maximum by the
            # adrenaline field, which means this maximum is the *second*
            # half of a pair whose first half we already passed.
            continue
        if 0 <= current <= maximum:
            return current, maximum
    return None


def parse_timer(text: str) -> int | None:
    """Seconds from an ``H:MM:SS`` reading, or None if it does not parse.

    Anchored on the ``[0-5]\\d`` minute/second fields so an OCR misread such as
    ``00:82:09`` is rejected outright rather than silently becoming a bogus
    elapsed time.
    """
    m = TIMER_RE.search(text)
    if not m:
        return None
    h, mi, s = (int(g) for g in m.groups())
    return h * 3600 + mi * 60 + s


def _eval_percent(rule: Rule, wid: str, box, now: float,
                  cycle: int = 0) -> Alert | None:
    """Alert when a bare percentage reaches or passes a threshold.

    Separate from `gauge` rather than a flag on it, because the two differ
    in both parser and direction: a gauge reads `current/maximum` and warns
    on the way *down*, while this reads a percent sign and fires on the way
    *up*. Folding them together would mean a rule whose fields only make
    sense in combinations the config cannot express.

    Built for adrenaline reaching 100%, which is a cue to act rather than a
    danger - so the default urgency is normal, not critical.
    """
    if rule.warn_at_or_above <= 0:
        return None
    value = parse_percent(_w().ocr_array(_w().capture_array(wid, box, None, cycle)))
    if value is None:
        return None

    if value < rule.warn_at_or_above:
        rule._low_streak = 0
        # Re-arm only on confirmed recovery, as for gauge rules.
        rule._high_streak += 1
        if rule._high_streak >= max(1, int(rule.confirm_readings)):
            rule._armed = True
            rule._primed = True
        return None
    rule._high_streak = 0

    rule._low_streak += 1
    if rule._low_streak < max(1, int(rule.confirm_readings)):
        return None
    # Already at the threshold when the watcher started: pre-existing
    # state, not an event. Opt-in, as for gauge rules.
    if rule.prime_on_start and not rule._primed:
        return None
    if not rule._armed or not rule.ready(now):
        return None
    # Re-arms only once the value drops below the threshold again, so
    # sitting at 100% produces one alert rather than one per poll.
    rule._armed = False
    return rule.fire(now, rule.alert_body or f"{value}%",
                     source_text=f"{value}%", percent=str(value),
                     current=str(value), item=rule.item)


def _eval_gauge(rule: Rule, wid: str, box, now: float,
                cycle: int = 0) -> Alert | None:
    """Alert when a `current/maximum` readout falls below a threshold.

    The capability the `ocr` kind cannot provide: it matches text and cannot
    compare numbers, so "prayer below 20%" was impossible to express and a
    health rule written that way would fire on every single reading.

    This exists for AFK bossing, where nobody is watching the screen. Prayer
    draining to zero is the classic silent failure - measured on a live
    Arch-Glacor kill at roughly 150 points/minute, which empties a 780-point
    pool in about five minutes with no chat line to announce it.

    `confirm_readings` guards against a single bad OCR frame: an overlay,
    a damage splat, or a hitsplat drawn over the digits can produce one
    wrong number, and waking someone for that is exactly the false alarm
    that makes an alert worth ignoring. Two consecutive readings below the
    threshold are required by default.
    """
    if rule.maximum <= 0 or (rule.warn_below <= 0 and rule.warn_at <= 0):
        return None
    frame = _w().capture_array(wid, box, None, cycle)
    reading = parse_gauge(_w().ocr_array(frame), rule.maximum)
    if reading is None:
        # An unreadable frame is not evidence of a low gauge. Hold the
        # streak rather than resetting it, so a single bad frame in a
        # genuine decline does not restart the confirmation count.
        return None
    current, maximum = reading

    # Reject a reading that is a *fraction* of the previous one, which is
    # what losing a leading digit looks like: a hitsplat drawn over the
    # readout turned 8,000 into 8, and that parses as a perfectly valid
    # number - which is how "Life 4/10,597 (0%)" was reported while health
    # was almost full. The observed misreads were ~99% collapses, so the
    # test is deliberately narrow: only a drop to under a tenth of the
    # previous value is rejected, and only on a four-figure gauge where a
    # lost digit changes the magnitude. A genuine heavy hit, even one
    # halving the pool, still alerts, and a small gauge such as prayer -
    # which really can go from full to nearly empty when a restore wears
    # off - is left alone. The frame is discarded; the next one decides.
    previous = rule._last_reading
    if (previous is not None and maximum >= 1000
            and previous >= maximum * 0.2
            and current < previous * 0.1):
        return None

    # The same guard, for the first reading of a run. With no previous value
    # to compare against, a bad frame at startup had nothing to contradict
    # it and fired immediately - which is how a critical alert arrived while
    # health was full. A four-figure gauge reading under 1% is very much
    # more likely to be a lost digit than a real state: a player that close
    # to zero is about to die, and one more frame costs a second.
    if (previous is None and maximum >= 1000
            and current < maximum * 0.01):
        rule._last_reading = None
        return None
    rule._last_reading = current

    # `warn_below` is always a percentage, and `warn_at` is an absolute
    # value. Inferring one from the other by magnitude was a trap: a rule
    # wanting "below half a percent" wrote warn_below=0.5 and got half the
    # pool, firing PRAYER OUT at 100/780. Two explicit fields cannot be
    # misread that way.
    if rule.warn_at > 0:
        low = current <= rule.warn_at
    else:
        low = (current / maximum if maximum else 1.0) <= rule.warn_below / 100.0

    if not low:
        rule._low_streak = 0
        # Re-arming needs the same confirmation as firing does. A single
        # misread frame - prayer sitting at 0 but decoding as 780 once -
        # otherwise re-armed the rule, and the next reading fired the alert
        # again, which is how an empty prayer kept re-announcing itself.
        rule._high_streak += 1
        if rule._high_streak >= max(1, int(rule.confirm_readings)):
            rule._armed = True
            rule._primed = True
        return None
    rule._high_streak = 0

    rule._low_streak += 1
    if rule._low_streak < max(1, int(rule.confirm_readings)):
        return None
    # `prime_on_start` suppresses a state that predates the watcher. Every
    # restart otherwise re-announced prayer that had been at zero for
    # twenty minutes - five identical pairs of alerts across five test
    # runs - exactly as OCR rules would re-report old chat scrollback.
    #
    # Opt-in per rule, deliberately. The two cases differ in consequence:
    # a stale prayer warning is noise, but starting the watcher while
    # already at 5% health is precisely when an alert is most needed, and
    # suppressing that could be fatal. Set it on the nagging rules only.
    if rule.prime_on_start and not rule._primed:
        return None
    if not rule._armed or not rule.ready(now):
        return None
    # Re-arms only when the gauge recovers above the threshold, so a long
    # decline produces one alert rather than one per poll.
    rule._armed = False
    fraction = current / maximum if maximum else 0.0
    return rule.fire(
        now, rule.alert_body or f"{current:,}/{maximum:,}",
        source_text=f"{current}/{maximum}", current=f"{current:,}",
        maximum=f"{maximum:,}", percent=f"{fraction * 100:.0f}",
        item=rule.item)


def _eval_timer(rule: Rule, wid: str, box, now: float,
                cycle: int = 0) -> Alert | None:
    """Alert when the in-game session timer passes a threshold.

    The Metrics panel keeps its own elapsed-time counter, which is a better
    measure of a session than wall-clock time in the watcher: it is the value
    the player is already reading, it pauses when they pause it, and it survives
    a watcher restart because the game owns it.

    Reading it is reliable - measured 10/10 clean parses over 30s, ticking
    monotonically - but a single garbled frame must not trigger the alert, so a
    reading is only acted on when it parses and moves forward.

    Milestones are emitted per `step`, so a one-hour threshold fires once at the
    hour rather than on every poll afterwards.
    """
    # Sprite matching first: measured 2516x faster than Tesseract on this
    # readout (0.066 ms vs 165.5 ms), with automatic fallback when the glyphs
    # do not match confidently.
    text = _w().ocr_numeric(wid, box, _w().capture_array(wid, box, None, cycle))
    secs = parse_timer(text)
    if secs is None:
        return None
    # A timer reset (new session) rewinds the milestone counter with it.
    if secs + 5 < rule._elapsed:
        rule._milestone = 0
    rule._elapsed = secs
    step = max(1, int(rule.step))
    reached = secs // step
    if reached <= rule._milestone:
        return None
    rule._milestone = reached
    if not rule.ready(now):
        return None
    hours = secs / 3600.0
    label = (f"{hours:.0f} hour" if abs(hours - round(hours)) < 0.02
             and round(hours) == 1 else f"{hours:.1f} hours")
    if step % 3600 == 0 and reached >= 1:
        label = f"{reached} hour" + ("s" if reached > 1 else "")
    return rule.fire(now, rule.milestone_message.format(
        label=label, elapsed=text.strip(), n=reached),
        source_text=text.strip(), label=label, elapsed=text.strip(), n=reached)


def _eval_stack(rule: Rule, wid: str, region: "_w().Region", box, now: float,
                cycle: int = 0) -> Alert | None:
    """Alert when a carried stack grows, i.e. an item dropped.

    Chat is the obvious place to look for a drop, but it is the weaker signal
    here: lines survive a measured median of 11s before scrolling off, and OCR
    of the chat font degrades badly when a busy 3D scene shows through the
    panel. The inventory is authoritative and persistent - the item is simply
    there.

    Transient overlays are the failure mode to guard against. Hovering the
    backpack draws a tooltip over neighbouring cells, which was observed making
    occupancy oscillate 9/10/11 and stack signatures jump and revert within a
    few seconds. So a change is only reported once it has *held* for
    `confirm_seconds`; a real drop stays, a tooltip does not.
    """
    if not region.grid:
        return None
    frame = _w().capture_array(wid, box, None, cycle)
    cols, rows = region.grid[4], region.grid[5]
    cur = [stack_signature(frame, region.grid, i) for i in range(cols * rows)]
    occ, _cells = _w().count_occupied(frame, region.grid, threshold=rule.cell_threshold)

    # An interface drawn over the backpack makes covered cells read as
    # occupied; above capacity is the reliable tell, so drop the frame.
    if occ > rule.capacity:
        return None

    prev = rule._stacks
    rule._stacks = cur
    if not prev or len(prev) != len(cur):
        return None

    changed = [i for i, (a, b) in enumerate(zip(prev, cur))
               if abs(b - a) > rule.stack_tolerance]
    if changed:
        # Restart confirmation whenever the set of moving slots changes, so a
        # tooltip sweeping across cells cannot accumulate toward a report.
        if set(changed) != rule._pending_slots:
            rule._pending_slots = set(changed)
            rule._pending_since = now
            rule._pending_base = prev
        return None

    if not rule._pending_slots:
        return None

    # The slots stopped moving. Confirmation measures how long the NEW value
    # has persisted since then, not how long it was still changing - a tooltip
    # reverts within a frame or two, while a real drop stays put.
    base = rule._pending_base or prev
    slots = sorted(rule._pending_slots)
    if now - rule._pending_since < rule.confirm_seconds:
        return None
    rule._pending_slots = set()
    if not rule._primed:
        rule._primed = True
        return None
    grew = [i for i in slots
            if i < len(cur) and cur[i] - base[i] > rule.stack_tolerance]
    if rule.new_slot_only:
        # A stack that was already present growing is a routine top-up - at a
        # 10% drop rate that happens every ~20s. An item appearing in a slot
        # that held nothing is the first of its kind, which is the event worth
        # interrupting for.
        grew = [i for i in grew if base[i] == 0]
    if not grew or not rule.ready(now):
        return None
    where = ", ".join(f"slot {i + 1}" for i in grew[:4])
    return rule.fire(now, f"Item gained in {where}. Check the backpack.",
                     where=where)


def _eval_loot(rule: Rule, wid: str, box, now: float,
               cycle: int = 0) -> Alert | None:
    """Announce a named item drop, ignoring the routine currency line.

    Pickpocketing a Menaphos market guard yields coins on ~76% of successes,
    straight into the money pouch. Alerting on those would fire roughly every
    two seconds and drown the drops that actually matter - an elite clue scroll
    is ~0.5%, a master ~0.005%. So `ignore_pattern` drops the currency line and
    the plain success line, and only a match against `item_pattern` - built
    from the wiki drop table - is announced.

    The item name is echoed back in the alert body, because 'you got something'
    is not useful when the table spans extra fine sand and a master clue.
    """
    text = _w().ocr_cached(wid, box, cycle)
    if not rule.item_pattern:
        return None
    for line in text.splitlines():
        line = line.strip()
        key = _w().norm_line(line)
        if len(key) < 8 or _w().seen_before(key, rule._seen):
            continue
        if rule.ignore_pattern and re.search(rule.ignore_pattern, line, re.I):
            rule._seen.add(key)
            continue
        m = re.search(rule.item_pattern, line, re.I)
        if not m:
            continue
        rule._seen.add(key)
        if not rule._primed:
            continue
        item = (m.group(0) or "").strip(" .,;:")
        rule._counts[item.lower()] = rule._counts.get(item.lower(), 0) + 1
        if rule.ready(now):
            n = rule._counts[item.lower()]
            suffix = f" (x{n} this session)" if n > 1 else ""
            return rule.fire(now, f"{item}{suffix}", source_text=line,
                             item=item, count=n)
    rule._primed = True
    if len(rule._seen) > 400:
        rule._seen.clear()
        rule._primed = False
    return None


#: Leading chat timestamp. A line without one is a wrapped continuation of
#: the line above it, which is how a long drop message is split.
_CHAT_TIMESTAMP = re.compile(r"^[\(\[]?\s*\d{1,2}[:;.]\d{2}[:;.]\d{2}")

#: OCR digit confusions seen in drop quantities: "1|" for 11, "2O" for 20.
_QTY_FIXUPS = str.maketrans({"|": "1", "l": "1", "I": "1", "O": "0",
                             "o": "0", "S": "5", "B": "8", "Z": "2"})


def join_wrapped_lines(text: str) -> list[str]:
    """Rejoin chat lines the client wrapped mid-message.

    A long drop announcement is split across two rendered lines, with the
    quantity at the end of the first and the item name on the second:

        15:41:55] ... You receive: 12 x
        slacor remnants.

    Rules evaluate line by line, so neither half alone carries both facts.
    Every real chat line starts with a timestamp, so a line without one is
    a continuation and belongs to its predecessor.
    """
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if out and not _CHAT_TIMESTAMP.match(line):
            out[-1] = f"{out[-1]} {line}"
        else:
            out.append(line)
    return out


def parse_quantity(raw: str) -> int | None:
    """Read a drop quantity, repairing common OCR digit confusions.

    Observed in real captures: "1|" for 11, and "&" where the digits were
    lost entirely. Returns None for the latter - a drop reported with the
    wrong count is worse than one reported without it.
    """
    fixed = raw.translate(_QTY_FIXUPS)
    digits = "".join(c for c in fixed if c.isdigit())
    return int(digits) if digits else None


def _eval_item_drop(rule: Rule, wid: str, box, now: float,
                    cycle: int = 0) -> Alert | None:
    """Alert on a named item dropping, reporting how many.

    Separate from the generic drop rule because it answers a different
    question: not "something dropped" but "how much of this specific thing
    have I just been given". The quantity is the point.

    Works on rejoined lines, since the client wraps the announcement and
    splits the quantity from the item name.
    """
    if not rule.item_pattern:
        return None
    for line in join_wrapped_lines(_w().ocr_cached(wid, box, cycle)):
        key = _w().norm_line(line)
        if len(key) < 8 or _w().seen_before(key, rule._seen):
            continue
        if not re.search(rule.item_pattern, line, re.I):
            continue
        rule._seen.add(key)
        if not rule._primed:
            continue
        m = re.search(r"receive[:;]?\s*([\dIl|&SBOoZ]{1,5})\s*[xX]",
                      line, re.I)
        count = parse_quantity(m.group(1)) if m else None
        if count is not None:
            rule._total += count
        if not rule.ready(now):
            continue
        amount = (f"{count:,}" if count is not None
                  else "an unreadable number of")
        return rule.fire(
            now, rule.alert_body or f"{amount} {rule.item}",
            source_text=line[:120], n=count if count is not None else 0,
            amount=amount, total=f"{rule._total:,}", item=rule.item,
            line=line)
    rule._primed = True
    if len(rule._seen) > 400:
        rule._seen.clear()
        rule._primed = False
    return None


def _eval_total(rule: Rule, wid: str, box, now: float,
                cycle: int = 0) -> Alert | None:
    """Alert on each milestone of a running total the game already keeps.

    Distinct from `counter`, which accumulates a total itself by summing
    repeated chat lines. Here the game owns the number - the Metrics
    panel's session Gain - so summing anything would be wrong. Reading it
    directly also means a watcher restart mid-session resumes at the true
    figure rather than from zero, which is the flaw that left the
    pickpocketing counter reading 513k against a real 2.3M.

    The reading must not go backwards. The panel only counts up within a
    session, so a lower value means either a misread or a session reset,
    and neither should fire a milestone. A large fall is treated as a
    reset and rewinds the milestone counter so the next session starts
    clean; a small one is discarded as noise.
    """
    step = max(1, int(rule.step))
    text = _w().ocr_array(_w().capture_array(wid, box, None, cycle))
    total = parse_total(text, column=rule.column)
    if total is None:
        return None

    if total + step < rule._total:
        rule._milestone = total // step
        rule._total = total
        return None
    if total < rule._total:
        return None
    rule._total = total

    reached = total // step
    if reached <= rule._milestone:
        return None
    # Check the cooldown BEFORE claiming the milestone. Advancing first
    # meant a milestone blocked by the cooldown was marked as reported
    # and never fired - silently losing that million rather than
    # delaying it. Returning without claiming leaves it pending, so the
    # next poll past the cooldown announces it.
    if not rule.ready(now):
        return None
    rule._milestone = reached
    # Persist so a restart does not re-announce a milestone already
    # passed. The game owns the running figure, so only the milestone
    # matters here - but it is watcher state, and without this every
    # restart replayed it. `counter` has always written; `total` did
    # not, which is why three consecutive woodcutting runs each fired
    # "1M" at a higher and higher gold figure.
    _w().log_counter(rule.name, now, total)
    fields = {"total": f"{total:,}", "n": reached, "step": f"{step:,}",
              "item": rule.item}
    template = rule.milestone_message or rule.alert_body or "{total}"
    try:
        body = template.format(**fields)
    except (KeyError, IndexError, ValueError):
        # A template naming a field this rule does not provide must not
        # take the watcher down; report the raw total instead.
        body = f"{total:,}"
    return rule.fire(now, body, source_text=text.strip()[:120], **fields)


def _eval_counter(rule: Rule, wid: str, box, now: float,
                  cycle: int = 0) -> Alert | None:
    """Sum a repeating numeric chat line and alert on each milestone.

    Coins from pickpocketing go to the money pouch, which the backpack grid
    cannot see - the only evidence is the chat line, so the total has to be
    accumulated from it. Each distinct line is counted once via the same
    dedup key the OCR rules use, because the chat tail keeps old lines on
    screen for many polls.

    Milestones fire per `step` (1,000,000 by default), not per gain, so a
    target of a million coins produces one alert rather than ~2,200.
    """
    text = _w().ocr_cached(wid, box, cycle)
    if not rule.pattern:
        return None
    gained = 0
    source_line = None
    visible = set()
    for line in text.splitlines():
        line = line.strip()
        key = _w().norm_line(line)
        if len(key) < 8:
            continue
        visible.add(key)
        if key in rule._seen:
            continue
        m = re.search(rule.pattern, line, re.I)
        if not m:
            continue
        rule._seen.add(key)
        if not rule._primed:
            continue
        try:
            gained += int(re.sub(r"[^0-9]", "", m.group(1)))
            source_line = line
        except (ValueError, IndexError):
            continue
    # End the priming pass. Every other rule kind does this after its first
    # sweep; the counter did not, so `_primed` stayed False forever and the
    # `continue` above discarded EVERY gain. Found live: 45s of continuous
    # pickpocketing at ~1.2 coin lines/s added nothing to the total, and
    # state/counters.jsonl had no automatic write in its whole history.
    # Must come after the loop, so the lines already on screen when the
    # watcher starts are treated as scrollback rather than fresh income.
    rule._primed = True
    if len(rule._seen) > 400:
        # Retain the current viewport as the new baseline, the same way the
        # presence rule does. Clearing the set and re-priming dropped a
        # whole cycle's income: re-priming treats every line then on screen
        # as pre-existing scrollback, so at ~2 coin lines a second the
        # counter silently lost a gain every few minutes and drifted
        # further below the real total the longer it ran.
        rule._seen = visible
    if gained:
        rule._total += gained
        _w().log_counter(rule.name, now, rule._total)
    step = max(1, int(rule.step))
    reached = rule._total // step
    if reached > rule._milestone:
        rule._milestone = reached
        return rule.fire(now, rule.milestone_message.format(
            total=f"{rule._total:,}", n=reached, step=f"{step:,}"),
            source_text=source_line, total=f"{rule._total:,}", n=reached,
            step=f"{step:,}")
    return None


def evaluate(rule: Rule, wid: str, region: "_w().Region", size, now: float,
             cycle: int = 0) -> Alert | None:
    box = region.resolve(size)
    if rule.kind == "presence":
        return _eval_presence(rule, wid, box, now, cycle)
    if rule.kind == "item_drop":
        return _eval_item_drop(rule, wid, box, now, cycle)
    if rule.kind == "total":
        return _eval_total(rule, wid, box, now, cycle)
    if rule.kind == "percent":
        return _eval_percent(rule, wid, box, now, cycle)
    if rule.kind == "gauge":
        return _eval_gauge(rule, wid, box, now, cycle)
    if rule.kind == "timer":
        return _eval_timer(rule, wid, box, now, cycle)
    if rule.kind == "stack":
        return _eval_stack(rule, wid, region, box, now, cycle)
    if rule.kind == "loot":
        return _eval_loot(rule, wid, box, now, cycle)
    if rule.kind == "counter":
        return _eval_counter(rule, wid, box, now, cycle)
    if rule.kind == "inventory":
        return _eval_inventory(rule, wid, region, box, now, cycle)
    if rule.kind == "activity":
        return _eval_activity(rule, wid, box, now, cycle)
    if rule.kind == "supply":
        return _eval_supply(rule, wid, box, now, cycle)
    if rule.kind == "item_count":
        return _eval_item_count(rule, wid, region, box, now, cycle)
    if rule.kind == "ocr":
        text = _w().ocr_cached(wid, box, cycle)
        if not rule.pattern:
            return
        for line in text.splitlines():
            line = line.strip()
            key = _w().norm_line(line)
            if len(key) < 8 or _w().seen_before(key, rule._seen):
                continue
            # A line can contain the pattern and mean its opposite: with a
            # Fingerfeather necklace the game says "You nimbly avoid getting
            # stunned", which is a *success*. Suppression is checked first so
            # such a line can never alert, whatever the pattern matches.
            if rule.suppress_pattern and re.search(rule.suppress_pattern,
                                                   line, re.I):
                rule._seen.add(key)
                continue
            if re.search(rule.pattern, line, re.I):
                rule._seen.add(key)
                # The chat tail is full of scrollback on startup. Record what
                # is already on screen without alerting, so the first poll
                # cannot fire on an event from before the watcher existed.
                if rule._primed and rule.ready(now):
                    return rule.fire(now, line[:120], source_text=line,
                                     line=line, text=line)
        rule._primed = True
        if len(rule._seen) > 400:
            # Dropping the whole set would let lines still on screen re-fire,
            # so re-prime on the next pass instead of alerting again.
            rule._seen.clear()
            rule._primed = False
        return

    frame = _w().capture_array(wid, box, rule.mask, cycle)
    if rule._last is None:
        rule._last, rule._last_change = frame, now
        return

    d = _w().mean_abs_diff(frame, rule._last)
    rule._last = frame
    if d != d:                       # NaN: shape changed under us
        rule._last_change = now
        return

    moved = d >= rule.threshold

    if rule.kind == "change":
        if moved and rule.ready(now):
            return rule.fire(now, f"changed (diff {d:.1f})")
    elif rule.kind == "idle":
        if moved:
            rule._last_change = now
            rule._armed = True
            return
        still = now - rule._last_change
        if rule._armed and still >= rule.idle_seconds and rule.ready(now):
            rule._armed = False
            return rule.fire(now, f"nothing for {still:.0f}s - probably needs you",
                             still=still)

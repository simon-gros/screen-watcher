from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .capture import capture_array
from .events import Alert
from .regions import Region
from .signals import (
    count_by_colour,
    count_occupied,
    fill_rate,
    mean_abs_diff,
    norm_line,
    ocr_cached,
    log_occupancy,
)


@dataclass(kw_only=True)
class RuleBase:
    name: str
    kind: str
    region: str
    message: str = ""
    cooldown: float = 120.0
    sound: str | None = None
    urgency: str = "normal"
    timeout_ms: int = 8000
    enabled: bool = True
    _last_fired: float = field(default=0.0, init=False, repr=False)

    def ready(self, now: float) -> bool:
        return self._last_fired == 0.0 or (now - self._last_fired) >= self.cooldown

    def fire(
        self,
        now: float,
        body: str,
        *,
        evidence: dict[str, Any] | None = None,
        confidence: float | None = None,
    ) -> Alert:
        self._last_fired = now
        return Alert(
            rule_name=self.name,
            title=self.message or self.name,
            body=body,
            urgency=self.urgency,
            sound=self.sound,
            timeout_ms=self.timeout_ms,
            evidence=evidence or {},
            confidence=confidence,
        )

    def reset(self) -> None:
        """Reset observation state without defeating the cooldown."""


@dataclass(kw_only=True)
class VisualRule(RuleBase):
    mask: str | None = None
    idle_seconds: float = 45.0
    threshold: float = 2.0
    _last: np.ndarray | None = field(default=None, init=False, repr=False)
    _last_change: float = field(default=0.0, init=False, repr=False)
    _armed: bool = field(default=True, init=False, repr=False)

    def reset(self) -> None:
        self._last = None
        self._last_change = 0.0
        self._armed = True


@dataclass(kw_only=True)
class OCRRule(RuleBase):
    pattern: str = ""
    _seen: set[str] = field(default_factory=set, init=False, repr=False)
    _primed: bool = field(default=False, init=False, repr=False)

    def reset(self) -> None:
        self._seen.clear()
        self._primed = False


@dataclass(kw_only=True)
class ActivityRule(OCRRule):
    suppress_pattern: str | None = None
    stop_seconds: float = 25.0
    _last_activity: float = field(default=0.0, init=False, repr=False)
    _armed: bool = field(default=True, init=False, repr=False)

    def reset(self) -> None:
        super().reset()
        self._last_activity = 0.0
        self._armed = True


@dataclass(kw_only=True)
class SupplyRule(OCRRule):
    item: str = "supplies"
    trip_pattern: str | None = None
    out_pattern: str | None = None
    out_message: str = ""
    warn_streak: int = 3
    _streak: int = field(default=0, init=False, repr=False)

    def reset(self) -> None:
        super().reset()
        self._streak = 0


@dataclass(kw_only=True)
class InventoryRule(RuleBase):
    capacity: int = 28
    lead_seconds: float = 90.0
    warn_free: int = 3
    cell_threshold: float = 8.0
    mode: str = "lead"
    overflow_seconds: float = 20.0
    log_occupancy: bool = True
    _history: list[tuple[float, int]] = field(default_factory=list, init=False, repr=False)
    _full_since: float = field(default=0.0, init=False, repr=False)
    _armed: bool = field(default=True, init=False, repr=False)

    def reset(self) -> None:
        self._history.clear()
        self._full_since = 0.0
        self._armed = True


@dataclass(kw_only=True)
class ItemCountRule(RuleBase):
    min_blue: float = 40.0
    warn_below: int = 2
    out_below: int = 0
    confirm_seconds: float = 6.0
    repeat_seconds: float = 0.0
    item: str = "supplies"
    out_message: str = ""
    _level: str = field(default="", init=False, repr=False)
    _level_since: float = field(default=0.0, init=False, repr=False)
    _armed: bool = field(default=True, init=False, repr=False)

    def reset(self) -> None:
        self._level = ""
        self._level_since = 0.0
        self._armed = True


RULE_CLASS = {
    "change": VisualRule,
    "idle": VisualRule,
    "ocr": OCRRule,
    "activity": ActivityRule,
    "supply": SupplyRule,
    "inventory": InventoryRule,
    "item_count": ItemCountRule,
}

# Backwards-compatible symbol for code that only needs ready()/fire().
Rule = RuleBase


def rule_from_dict(spec: dict) -> RuleBase:
    cleaned = {key: value for key, value in spec.items() if not key.startswith("_")}
    kind = cleaned.get("kind")
    if not isinstance(kind, str):
        raise ValueError(f"unknown rule kind {kind!r}")
    cls = RULE_CLASS.get(kind)
    if cls is None:
        raise ValueError(f"unknown rule kind {kind!r}")
    return cls(**cleaned)


def _fresh_lines(rule: OCRRule, text: str) -> list[str]:
    output: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        key = norm_line(line)
        if len(key) < 8 or key in rule._seen:
            continue
        rule._seen.add(key)
        output.append(line)
    if len(rule._seen) > 500:
        rule._seen.clear()
        rule._primed = False
    return output


def _eval_ocr_text(rule: OCRRule, text: str, now: float) -> Alert | None:
    lines = _fresh_lines(rule, text)
    if not rule._primed:
        rule._primed = True
        return None
    for line in lines:
        if re.search(rule.pattern, line, re.I) and rule.ready(now):
            return rule.fire(
                now,
                line[:120],
                evidence={"matched_line": line[:240], "pattern": rule.pattern},
            )
    return None


def _eval_activity_text(rule: ActivityRule, text: str, now: float) -> Alert | None:
    lines = _fresh_lines(rule, text)

    if not rule._primed:
        rule._primed = True
        if any(re.search(rule.pattern, line, re.I) for line in lines):
            rule._last_activity = now
        return None

    for line in lines:
        if rule.suppress_pattern and re.search(rule.suppress_pattern, line, re.I):
            rule._armed = False
            continue
        if re.search(rule.pattern, line, re.I):
            rule._last_activity = now
            rule._armed = True

    if rule._last_activity == 0.0:
        return None

    quiet = now - rule._last_activity
    if rule._armed and quiet >= rule.stop_seconds and rule.ready(now):
        rule._armed = False
        return rule.fire(
            now,
            f"No matching activity for {quiet:.0f}s. Check the game.",
            evidence={
                "quiet_seconds": round(quiet, 3),
                "stop_seconds": rule.stop_seconds,
                "pattern": rule.pattern,
            },
        )
    return None


def _eval_supply_text(rule: SupplyRule, text: str, now: float) -> Alert | None:
    lines = _fresh_lines(rule, text)
    if not rule._primed:
        rule._primed = True
        return None

    saw_trip = False
    saw_fail = False
    saw_out = False
    evidence_lines: list[str] = []
    for line in lines:
        if rule.trip_pattern and re.search(rule.trip_pattern, line, re.I):
            saw_trip = True
            evidence_lines.append(line)
        if rule.out_pattern and re.search(rule.out_pattern, line, re.I):
            saw_out = True
            evidence_lines.append(line)
        if re.search(rule.pattern, line, re.I):
            saw_fail = True
            evidence_lines.append(line)

    if saw_out and rule.ready(now):
        rule._streak = 0
        return rule.fire(
            now,
            rule.out_message or f"Out of {rule.item}. Restock before the next trip.",
            evidence={"lines": evidence_lines[-5:], "state": "out"},
        )

    if saw_trip:
        if saw_fail:
            rule._streak += 1
            if rule._streak >= rule.warn_streak and rule.ready(now):
                return rule.fire(
                    now,
                    f"{rule.item} low - the bank has come up short "
                    f"{rule._streak} trips running. Restock soon.",
                    evidence={"lines": evidence_lines[-5:], "streak": rule._streak},
                )
        else:
            rule._streak = 0
    return None


def evaluate_text(rule: RuleBase, text: str, now: float) -> Alert | None:
    """Pure text-rule evaluation used by both live OCR and replay fixtures."""
    if isinstance(rule, ActivityRule):
        return _eval_activity_text(rule, text, now)
    if isinstance(rule, SupplyRule):
        return _eval_supply_text(rule, text, now)
    if isinstance(rule, OCRRule):
        return _eval_ocr_text(rule, text, now)
    raise TypeError(f"{type(rule).__name__} is not a text rule")


def _eval_inventory(
    rule: InventoryRule,
    wid: str,
    region: Region,
    box: tuple,
    now: float,
    cycle: int,
) -> Alert | None:
    if not region.grid:
        return None
    frame = capture_array(wid, box, cycle=cycle)
    occupied, _cells = count_occupied(frame, region.grid, threshold=rule.cell_threshold)
    if occupied > rule.capacity:
        return None

    free = max(0, rule.capacity - occupied)
    prev = rule._history[-1][1] if rule._history else None
    if (prev is None or occupied != prev) and rule.log_occupancy:
        log_occupancy(time.time(), occupied)
    if prev is not None and occupied < prev - 2:
        rule._history.clear()
        rule._armed = True
    rule._history.append((now, occupied))
    if len(rule._history) > 400:
        del rule._history[:200]

    rate = fill_rate(rule._history)
    eta = free / rate if rate > 1e-6 else float("inf")

    if rule.mode == "overflow":
        if free > 0:
            rule._full_since = 0.0
            rule._armed = True
            return None
        if rule._full_since == 0.0:
            rule._full_since = now
            return None
        stuck = now - rule._full_since
        if rule._armed and stuck >= rule.overflow_seconds and rule.ready(now):
            rule._armed = False
            return rule.fire(
                now,
                f"Pack full for {stuck:.0f}s - activity has stopped. Check the game.",
                evidence={"occupied": occupied, "free": free, "full_seconds": round(stuck, 3)},
            )
        return None

    if not rule._armed:
        return None
    hit_lead = eta <= rule.lead_seconds
    hit_floor = free <= rule.warn_free
    if (hit_lead or hit_floor) and rule.ready(now):
        rule._armed = False
        if hit_lead and eta != float("inf"):
            body = f"{free} slots left, filling at {rate*60:.1f}/min - full in ~{eta:.0f}s. Head to the bank."
        else:
            body = f"{free} slots left. Head to the bank."
        return rule.fire(
            now,
            body,
            evidence={
                "occupied": occupied,
                "free": free,
                "fill_rate_per_min": round(rate * 60, 3),
                "eta_seconds": None if eta == float("inf") else round(eta, 3),
            },
        )
    return None


def _eval_item_count(
    rule: ItemCountRule,
    wid: str,
    region: Region,
    box: tuple,
    now: float,
    cycle: int,
) -> Alert | None:
    if not region.grid:
        return None
    frame = capture_array(wid, box, cycle=cycle)
    count = count_by_colour(frame, region.grid, rule.min_blue)
    level = "out" if count <= rule.out_below else "low" if count <= rule.warn_below else "ok"

    if level != rule._level:
        rule._level = level
        rule._level_since = now
        return None
    if level == "ok":
        rule._armed = True
        return None
    if (
        not rule._armed
        and level == "out"
        and rule.repeat_seconds > 0
        and (now - rule._last_fired) >= rule.repeat_seconds
    ):
        rule._armed = True
    if not rule._armed or (now - rule._level_since) < rule.confirm_seconds or not rule.ready(now):
        return None

    rule._armed = False
    evidence = {"count": count, "level": level, "confirmed_seconds": round(now - rule._level_since, 3)}
    if level == "out":
        stuck = now - rule._level_since
        extra = f" (empty for {stuck/60:.0f} min)" if stuck >= 120 else ""
        return rule.fire(now, (rule.out_message or f"Out of {rule.item}.") + extra, evidence=evidence)
    noun = rule.item.rstrip("s") if count == 1 else rule.item
    return rule.fire(now, f"{count} {noun} left. Restock on the next bank trip.", evidence=evidence)


def _eval_visual(
    rule: VisualRule,
    wid: str,
    box: tuple,
    now: float,
    cycle: int,
) -> Alert | None:
    frame = capture_array(wid, box, rule.mask, cycle)
    if rule._last is None:
        rule._last = frame
        rule._last_change = now
        return None

    diff = mean_abs_diff(frame, rule._last)
    rule._last = frame
    if diff != diff:
        rule._last_change = now
        return None

    moved = diff >= rule.threshold
    if rule.kind == "change":
        if moved and rule.ready(now):
            return rule.fire(
                now,
                f"changed (diff {diff:.1f})",
                evidence={"diff": round(diff, 4), "threshold": rule.threshold},
            )
        return None

    if moved:
        rule._last_change = now
        rule._armed = True
        return None
    still = now - rule._last_change
    if rule._armed and still >= rule.idle_seconds and rule.ready(now):
        rule._armed = False
        return rule.fire(
            now,
            f"nothing for {still:.0f}s - probably needs you",
            evidence={"still_seconds": round(still, 3), "threshold": rule.threshold},
        )
    return None


def evaluate(
    rule: RuleBase,
    wid: str,
    region: Region,
    size: tuple[int, int],
    now: float,
    cycle: int = 0,
) -> Alert | None:
    box = region.resolve(size)
    if isinstance(rule, InventoryRule):
        return _eval_inventory(rule, wid, region, box, now, cycle)
    if isinstance(rule, ItemCountRule):
        return _eval_item_count(rule, wid, region, box, now, cycle)
    if isinstance(rule, (ActivityRule, SupplyRule, OCRRule)):
        text = ocr_cached(wid, box, cycle)
        return evaluate_text(rule, text, now)
    if isinstance(rule, VisualRule):
        return _eval_visual(rule, wid, box, now, cycle)
    raise TypeError(f"unsupported rule class {type(rule).__name__}")

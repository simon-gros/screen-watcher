"""Persistent records: occupancy transitions and running counters.

Extracted from `watcher.py` as part of the modular split.

Every log path is read back through `watcher` on each use rather than
bound here. Seven tests redirect `watcher.OCCUPANCY_LOG` and
`watcher.COUNTER_LOG` to a temporary directory, and a module-level
constant would freeze the repository path before any redirect applied -
the mistake already made three times in this split, with the load_config
default argument, the PID_FILE constant and a dataclass field.

Records use Unix wall-clock timestamps. Monotonic time is for in-process
cooldowns only: it restarts from an arbitrary base each boot, so a
persisted monotonic value cannot be compared across runs. An occupancy
log carrying both bases produced 57-year phantom gaps that split every
bank cycle `stats` tried to measure.
"""

from __future__ import annotations

import json
import time

import numpy as np


def _w():
    """The `watcher` module, imported late."""
    import watcher
    return watcher


#: thousands rather than the billions.
_WALL_CLOCK_FLOOR = 1_577_836_800.0


def log_counter(name: str, ts: float, total: int) -> None:
    """Persist a running counter so a restart does not reset progress.

    A milestone like 'one million coins' takes hours of pickpocketing. Holding
    the total only in memory would mean a watcher restart - or the game window
    briefly disappearing - silently rewinds it to zero and the alert never
    arrives.

    Timestamps are wall-clock for the same reason as the occupancy log: the
    caller passes the loop's monotonic clock, which restarts from an
    arbitrary base each boot and is meaningless once written to disk.
    `load_counter` reads by name and ignores `t`, so this was not breaking
    anything - but a persisted timestamp that cannot be compared across runs
    is a trap for whatever reads it next.
    """
    try:
        _w().STATE_DIR.mkdir(exist_ok=True)
        with _w().COUNTER_LOG.open("a") as f:
            f.write(json.dumps({"t": round(time.time(), 1), "name": name,
                                "total": total}) + "\n")
    except OSError:
        pass


def load_counter(name: str) -> int:
    """Last recorded total for a counter, or 0 if it has never run."""
    if not _w().COUNTER_LOG.exists():
        return 0
    total = 0
    try:
        for line in _w().COUNTER_LOG.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("name") == name:
                total = int(row.get("total", 0))
    except OSError:
        return 0
    return total


def log_occupancy(ts: float, occ: int) -> None:
    """Append an occupancy change. Only transitions are recorded, so an hour
    of fishing costs a few hundred bytes rather than thousands of samples.

    `ts` is the loop's monotonic clock, which is right for elapsed-time
    arithmetic and wrong to persist: it restarts from an arbitrary base on
    every boot. The log was carrying both bases at once - real rows of
    1789967548 next to rows of 1001.0 - and `load_cycles` segments cycles by
    time gaps, so those backwards jumps silently split or merged bank trips
    and corrupted `stats`. Wall-clock is written instead, matching the alert
    and counter logs.
    """
    try:
        _w().STATE_DIR.mkdir(exist_ok=True)
        with _w().OCCUPANCY_LOG.open("a") as f:
            f.write(json.dumps({"t": round(time.time(), 1), "occ": occ}) + "\n")
    except OSError:
        pass


def load_cycles(capacity: int, gap: float = 300.0) -> list[dict]:
    """Segment the occupancy log into fill/bank cycles.

    A cycle runs from the first gain after a bank to the next bank. That
    boundary is what lets us report a rate including bank time, which is the
    number that actually matters and which the in-game Metrics panel, counting
    only XP, cannot show.
    """
    if not _w().OCCUPANCY_LOG.exists():
        return []
    rows = []
    for line in _w().OCCUPANCY_LOG.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and "t" in row and "occ" in row:
            rows.append(row)
    # Existing logs carry rows written against the monotonic clock before
    # that was fixed. They are indistinguishable from wall-clock rows except
    # by magnitude, and mixing the two produces enormous phantom gaps that
    # split every cycle. Keep only the wall-clock era rather than guessing
    # at their real times.
    rows = [r for r in rows if r["t"] >= _WALL_CLOCK_FLOOR]
    cycles, cur = [], None
    for i, r in enumerate(rows):
        t, occ = r["t"], r["occ"]
        prev = rows[i - 1] if i else None
        # a large time gap means the watcher was stopped; start fresh
        if prev and t - prev["t"] > gap:
            cur = None
        if prev and occ < prev["occ"] - 2:          # banked
            if cur:
                cur["banked_at"] = t
                cur["peak"] = cur.get("peak", prev["occ"])
                cur["emptied_to"] = occ
                if cur.get("first_gain") is not None:
                    cycles.append(cur)
            cur = {"start": t, "first_gain": None, "peak": occ}
            continue
        if cur is None:
            cur = {"start": t, "first_gain": None, "peak": occ}
        if prev and occ > prev["occ"]:
            if cur["first_gain"] is None:
                cur["first_gain"] = t
            # last_gain, not full_at: the pack often gets banked before it
            # fills, so keying transit time off capacity would measure nothing.
            cur["last_gain"] = t
            cur["peak"] = max(cur.get("peak", 0), occ)
            if occ >= capacity and "full_at" not in cur:
                cur["full_at"] = t
    return cycles


def fill_rate(history: list[tuple[float, int]], window: float = 180.0) -> float:
    """Slots gained per second, least-squares over the recent window.

    Regression rather than first/last: fishing yields arrive in bursts, so
    a two-point estimate swings wildly between catches.
    """
    if len(history) < 4:
        return 0.0
    now = history[-1][0]
    pts = [(t, v) for t, v in history if now - t <= window]
    if len(pts) < 4:
        return 0.0
    ts = np.array([p[0] for p in pts]) - pts[0][0]
    vs = np.array([p[1] for p in pts], dtype=float)
    if ts[-1] <= 0 or vs.max() == vs.min():
        return 0.0
    slope = float(np.polyfit(ts, vs, 1)[0])
    return max(0.0, slope)

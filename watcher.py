#!/usr/bin/env python3
"""Screen Watcher - region-based window monitor with notifications.

Reads the screen only. Never sends input.

Regions are anchored to a window corner rather than stored as absolute
pixels, because RS3 (and most games) pin their panels to edges at a fixed
size instead of scaling them. A resized window therefore keeps working.

Subcommands:
  calibrate   scaled full-window shot for picking coordinates
  shot        capture one named region (or raw anchor/x,y,w,h) to a file
  probe       report per-region frame-to-frame diff, to pick thresholds
  watch       run the polling loop and fire notifications
  regions     list configured regions and rules, resolved at current size
  status      report whether the watcher process is running
  pause       pause the running watcher
  resume      resume a paused watcher
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
REGION_DIR = ROOT / "regions"
STATE_DIR = ROOT / "state"
ACTIVE_SKILL = ""

ANCHORS = {"top-left", "top-right", "bottom-left", "bottom-right",
           "top-center", "bottom-center", "center"}
RULE_KINDS = {"inventory", "activity", "supply", "item_count",
              "ocr", "change", "idle", "loot", "counter"}
PROFILE_TYPES = {"skill", "quest", "boss"}


# --------------------------------------------------------------------------
# window resolution
# --------------------------------------------------------------------------

def ensure_x_env() -> None:
    """Find DISPLAY/XAUTHORITY from the running desktop session.

    RS3 runs under XWayland, and KWin writes its X cookie to a randomly named
    file in /run/user/<uid>/ that changes on every login - there is no
    ~/.Xauthority to fall back on. A watcher started from anywhere without a
    desktop session (a service, a cron job, an agent shell) therefore dies with
    "DISPLAY environment variable is empty", and one started before a re-login
    fails with "Authorization required" against a stale cookie.

    Reading both values out of a live session process is the only reliable
    source, so do that instead of trusting the caller's environment.
    """
    if os.environ.get("DISPLAY") and os.environ.get("XAUTHORITY"):
        if Path(os.environ["XAUTHORITY"]).exists():
            return
    for proc in ("plasmashell", "kwin_wayland", "gnome-shell"):
        r = subprocess.run(["pgrep", "-u", str(os.getuid()), proc],
                           capture_output=True, text=True)
        for pid in r.stdout.split():
            try:
                env = Path(f"/proc/{pid}/environ").read_bytes().decode(
                    "utf-8", "replace")
            except OSError:
                continue
            found = dict(
                kv.split("=", 1) for kv in env.split("\0")
                if kv.count("=") >= 1 and kv.split("=", 1)[0] in
                ("DISPLAY", "XAUTHORITY"))
            if found.get("DISPLAY") and Path(
                    found.get("XAUTHORITY", "")).exists():
                os.environ.update(found)
                return


def _xdo(*args: str) -> str:
    r = subprocess.run(["xdotool", *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def find_window(wm_class: str, min_area: int = 100_000) -> str | None:
    """Largest viewable window matching WM_CLASS.

    WM_CLASS beats title matching: it survives title changes and skips
    launcher windows that share the game's name.
    """
    best, best_area = None, 0
    for wid in _xdo("search", "--onlyvisible", "--class", wm_class).splitlines():
        wid = wid.strip()
        if not wid:
            continue
        size = window_size(wid)
        if not size:
            continue
        area = size[0] * size[1]
        if area > best_area:
            best, best_area = wid, area
    return best if best and best_area >= min_area else None


def window_size(wid: str) -> tuple[int, int] | None:
    m = re.search(r"Geometry:\s*(\d+)x(\d+)", _xdo("getwindowgeometry", wid))
    return (int(m.group(1)), int(m.group(2))) if m else None


# --------------------------------------------------------------------------
# anchored regions
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Region:
    anchor: str
    dx: int
    dy: int
    w: int
    h: int
    grid: tuple | None = None   # (x0, y0, cell_w, cell_h, cols, rows)

    @staticmethod
    def parse(spec) -> "Region":
        if isinstance(spec, list):          # legacy absolute [x,y,w,h]
            x, y, w, h = spec
            return Region("top-left", x, y, w, h)
        a = spec.get("anchor", "top-left")
        if a not in ANCHORS:
            raise ValueError(f"bad anchor {a!r}; expected one of {sorted(ANCHORS)}")
        g = spec.get("grid")
        grid = (g["x0"], g["y0"], g["cell_w"], g["cell_h"], g["cols"], g["rows"]) if g else None
        return Region(a, spec["dx"], spec["dy"], spec["w"], spec["h"], grid)

    def resolve(self, win: tuple[int, int]) -> tuple[int, int, int, int]:
        """Absolute (x, y, w, h) for a window of size `win`, clamped in-bounds."""
        W, H = win
        if self.anchor.startswith("top"):
            y = self.dy
        elif self.anchor.startswith("bottom"):
            y = H + self.dy - self.h
        else:
            y = (H - self.h) // 2 + self.dy

        if self.anchor.endswith("left"):
            x = self.dx
        elif self.anchor.endswith("right"):
            x = W + self.dx - self.w
        else:
            x = (W - self.w) // 2 + self.dx

        w = max(1, min(self.w, W))
        h = max(1, min(self.h, H))
        x = max(0, min(x, W - w))
        y = max(0, min(y, H - h))
        return (x, y, w, h)


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------

class CaptureError(RuntimeError):
    pass


def capture(wid: str, box: tuple[int, int, int, int] | None, out: Path,
            resize: str | None = None) -> Path:
    """Capture a window or crop. Refuses an empty id, which would make
    `import` drop into interactive crosshair mode and hang."""
    if not wid:
        raise CaptureError("empty window id (would trigger interactive crosshair)")
    args = ["import", "-window", wid]
    if box:
        x, y, w, h = box
        args += ["-crop", f"{w}x{h}+{x}+{y}", "+repage"]
    if resize:
        args += ["-resize", resize]
    args.append(str(out))
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        raise CaptureError("import timed out")
    if r.returncode != 0 or not out.exists():
        raise CaptureError(f"import failed: {r.stderr.strip()[:200]}")
    return out


def capture_array(wid: str, box, mask: str | None = None,
                  cycle: int | None = None) -> np.ndarray:
    """Capture a region once per cycle and return its RGB array."""
    key = (wid, tuple(box), mask)
    if cycle is not None:
        hit = _FRAME_CACHE.get(key)
        if hit is not None and hit[0] == cycle:
            return hit[1]
    with tempfile.NamedTemporaryFile(suffix=".ppm") as tmp:
        capture(wid, box, Path(tmp.name))
        try:
            with Image.open(tmp.name) as im:
                arr = np.asarray(im.convert("RGB"), dtype=np.int16)
        except (OSError, ValueError) as e:
            # `import` can leave a short file while the window is repainting.
            raise CaptureError(f"unreadable capture: {e}") from e
    if mask == "bright":
        # Game panels are semi-transparent, so the moving 3D world bleeds
        # through and creates a constant diff floor. Text is much brighter
        # than the bleed-through, so a luminance threshold isolates it and
        # drops the noise floor to ~0.
        lum = arr.mean(axis=2)
        arr = (lum > 140).astype(np.int16) * 255
    if cycle is not None:
        _FRAME_CACHE[key] = (cycle, arr)
    return arr


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------

def count_occupied(frame: np.ndarray, grid: tuple, pad: float = 0.22,
                   threshold: float = 8.0) -> tuple[int, int]:
    """Count occupied cells in an item grid. Returns (occupied, total_cells).

    An empty RS3 slot is flat background; an item icon adds colour spread.
    Measured separation is ~15x (empty std 1.2-1.9, occupied 28-48), so the
    threshold sits far from both populations. Sampling the middle of each
    cell (pad) avoids slot borders and stack-count digits at the corners.
    """
    x0, y0, cw, ch, cols, rows = grid
    occupied = 0
    for r in range(rows):
        for c in range(cols):
            x, y = int(x0 + c * cw), int(y0 + r * ch)
            px, py = int(cw * pad), int(ch * pad)
            patch = frame[y + py:y + ch - py, x + px:x + cw - px]
            if patch.size == 0:
                continue
            if float(patch.std(axis=(0, 1)).mean()) > threshold:
                occupied += 1
    return occupied, cols * rows


def slot_signatures(frame: np.ndarray, grid: tuple, pad: float = 0.22,
                    threshold: float = 8.0) -> list[dict]:
    """Per-slot fingerprint: occupancy plus a colour signature.

    Counting occupied slots alone misses everything that happens *within* a
    slot - a food stack dropping 10 -> 9, a potion losing a dose. The mean
    colour of the cell shifts when the icon or its stack digits change, so it
    catches consumption and drops that leave the slot occupied.
    """
    x0, y0, cw, ch, cols, rows = grid
    out = []
    for r in range(rows):
        for c in range(cols):
            x, y = int(x0 + c * cw), int(y0 + r * ch)
            px, py = int(cw * pad), int(ch * pad)
            patch = frame[y + py:y + ch - py, x + px:x + cw - px]
            if patch.size == 0:
                out.append({"i": r * cols + c, "occ": False,
                            "rgb": (0.0, 0.0, 0.0), "cover": 0.0})
                continue
            std = float(patch.std(axis=(0, 1)).mean())
            # The panel is semi-transparent, so the 3D scene behind it moves
            # and would dominate a whole-cell mean. Item icons are brighter
            # than that bleed-through, so describe only the bright pixels:
            # the icon's colour and how much of the cell it covers.
            lum = patch.mean(axis=2)
            icon = lum > 90
            cover = float(icon.mean())
            if icon.any():
                rgb = tuple(float(v) for v in patch[icon].mean(axis=0))
            else:
                rgb = (0.0, 0.0, 0.0)
            out.append({"i": r * cols + c, "occ": std > threshold,
                        "rgb": rgb, "cover": cover})
    return out


def diff_slots(prev: list[dict], cur: list[dict],
               colour_tol: float = 6.0) -> list[dict]:
    """Classify per-slot changes between two frames."""
    changes = []
    for a, b in zip(prev, cur):
        if a["occ"] and not b["occ"]:
            changes.append({"slot": b["i"], "kind": "emptied"})
        elif not a["occ"] and b["occ"]:
            changes.append({"slot": b["i"], "kind": "gained"})
        elif a["occ"] and b["occ"]:
            d = max(abs(x - y) for x, y in zip(a["rgb"], b["rgb"]))
            if d > colour_tol:
                changes.append({"slot": b["i"], "kind": "changed",
                                "delta": round(d, 1)})
    return changes


OCCUPANCY_LOG = STATE_DIR / "occupancy.jsonl"
COUNTER_LOG = STATE_DIR / "counters.jsonl"


def log_counter(name: str, ts: float, total: int) -> None:
    """Persist a running counter so a restart does not reset progress.

    A milestone like 'one million coins' takes hours of pickpocketing. Holding
    the total only in memory would mean a watcher restart - or the game window
    briefly disappearing - silently rewinds it to zero and the alert never
    arrives.
    """
    try:
        STATE_DIR.mkdir(exist_ok=True)
        with COUNTER_LOG.open("a") as f:
            f.write(json.dumps({"t": round(ts, 1), "name": name,
                                "total": total}) + "\n")
    except OSError:
        pass


def load_counter(name: str) -> int:
    """Last recorded total for a counter, or 0 if it has never run."""
    if not COUNTER_LOG.exists():
        return 0
    total = 0
    try:
        for line in COUNTER_LOG.read_text().splitlines():
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
    of fishing costs a few hundred bytes rather than thousands of samples."""
    try:
        STATE_DIR.mkdir(exist_ok=True)
        with OCCUPANCY_LOG.open("a") as f:
            f.write(json.dumps({"t": round(ts, 1), "occ": occ}) + "\n")
    except OSError:
        pass


def load_cycles(capacity: int, gap: float = 300.0) -> list[dict]:
    """Segment the occupancy log into fill/bank cycles.

    A cycle runs from the first gain after a bank to the next bank. That
    boundary is what lets us report a rate including bank time, which is the
    number that actually matters and which the in-game Metrics panel, counting
    only XP, cannot show.
    """
    if not OCCUPANCY_LOG.exists():
        return []
    rows = []
    for line in OCCUPANCY_LOG.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
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


def mean_abs_diff(a: np.ndarray, b: np.ndarray) -> float:
    if a is None or b is None or a.shape != b.shape:
        return float("nan")
    # Avoid unsigned integer wraparound turning a small negative difference
    # into a large positive one.
    return float(np.mean(np.abs(a.astype(np.float32) - b.astype(np.float32))))


def norm_line(s: str) -> str:
    """Dedup key for an OCR'd chat line.

    Tesseract is not deterministic on this font: the same line can come back
    as '[11:03:15]' on one pass and '(11:03:15]' on the next. Comparing raw
    strings therefore re-reports old lines as new. Collapsing to lowercase
    alphanumerics makes the key stable across those wobbles.
    """
    return re.sub(r"[^a-z0-9]", "", s.lower())


_OCR_CACHE: dict = {}
_FRAME_CACHE: dict = {}


def ocr_cached(wid: str, box, cycle: int, psm: int = 6) -> str:
    """OCR a region once per poll cycle, however many rules ask for it.

    Five rules watch `chat_tail`, and each was running its own tesseract pass
    over an identical image. Measured at ~0.8s per pass, that is ~4s of every
    poll cycle spent re-reading the same pixels - which pushed the real interval
    to ~5.9s and made the impling alert arrive 36s after the chat line.

    Keying on the cycle counter (not a timestamp) means every rule in one pass
    sees exactly the same text, so a line cannot be consumed by one rule and
    missed by another that polled a fraction later.
    """
    key = (tuple(box), psm)      # Region.resolve always returns (x, y, w, h)
    hit = _OCR_CACHE.get(key)
    if hit is not None and hit[0] == cycle:
        return hit[1]
    text = ocr(wid, box, psm)
    _OCR_CACHE[key] = (cycle, text)
    return text


def ocr(wid: str, box, psm: int = 6) -> str:
    with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
        capture(wid, box, Path(tmp.name))
        r = subprocess.run(["tesseract", tmp.name, "stdout", "--psm", str(psm)],
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip()


# --------------------------------------------------------------------------
# notification
# --------------------------------------------------------------------------

SOUND_DIR = Path("/usr/share/sounds/ocean/stereo")


def play(sound: str | None) -> None:
    """Play a per-rule alert sound, non-blocking.

    KDE's own notification sound already fires for every notify-send, so an
    alert is audible but indistinguishable from any other desktop event.
    A distinct sound per rule is what makes 'bank now' recognisable without
    looking at the screen.
    """
    if not sound:
        return
    path = Path(sound) if "/" in sound else SOUND_DIR / f"{sound}.oga"
    if not path.exists():
        return
    try:
        subprocess.Popen(["paplay", str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, FileNotFoundError):
        pass


ALERT_LOG = STATE_DIR / "alerts.jsonl"


def log_alert(rule_name: str, title: str, body: str) -> None:
    """Append a fired alert.

    Without this there is no record of what fired and when, so a complaint that
    the watcher is 'beeping too often' cannot be answered from evidence - the
    only option is to re-run it live and hope the noise reproduces. Attributing
    each alert to its rule is the point: 'which rule' is the first question, and
    the notification title alone does not answer it.
    """
    try:
        STATE_DIR.mkdir(exist_ok=True)
        with ALERT_LOG.open("a") as f:
            f.write(json.dumps({
                "t": round(time.time(), 1),
                "skill": ACTIVE_SKILL or "unknown",
                "rule": rule_name,
                "title": title,
                "body": body[:200],
            }) + "\n")
    except OSError:
        pass


def notify(title: str, body: str, urgency: str = "normal",
           sound: str | None = None, timeout_ms: int = 8000,
           rule_name: str = "") -> None:
    """Post a desktop notification.

    KDE treats `critical` urgency as sticky: it ignores the expiry timeout and
    keeps the popup until it is dismissed by hand. For an alert that fires
    every bank trip that means constant manual cleanup, so rules default to
    `normal` urgency with an explicit timeout and close themselves.
    """
    cmd = ["notify-send", "-a", "Screen Watcher", "-u", urgency]
    if urgency != "critical":
        cmd += ["-t", str(timeout_ms)]
    cmd += [title, body]
    subprocess.run(cmd, capture_output=True)
    play(sound)
    log_alert(rule_name or title, title, body)
    print(f"[{time.strftime('%H:%M:%S')}] NOTIFY {title}: {body}", flush=True)


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


@dataclass
class Rule:
    name: str
    kind: str                       # idle | change | ocr
    region: str
    message: str = ""
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
    # supply
    item: str = "supplies"
    trip_pattern: str | None = None
    out_pattern: str | None = None
    out_message: str = ""
    warn_streak: int = 3
    # loot
    item_pattern: str | None = None
    ignore_pattern: str | None = None
    # counter
    step: float = 1_000_000
    milestone_message: str = "{total} reached."
    # item_count
    min_blue: float = 40.0
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
    _primed: bool = field(default=False, repr=False)
    _history: list = field(default_factory=list, repr=False)
    _full_since: float = field(default=0.0, repr=False)
    _last_activity: float = field(default=0.0, repr=False)
    _streak: int = field(default=0, repr=False)
    _level: str = field(default="", repr=False)
    _counts: dict = field(default_factory=dict, repr=False)
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

    def fire(self, now: float, body: str) -> Alert:
        self._last_fired = now
        return Alert(self.name, self.message or self.name, body,
                     self.urgency, self.sound, self.timeout_ms)

    def reset(self) -> None:
        self._last = None
        self._armed = True
        self._history.clear()
        self._full_since = 0.0
        self._last_activity = 0.0
        self._primed = False
        self._level = ""
        self._level_since = 0.0


def _eval_inventory(rule: Rule, wid: str, region: "Region", box, now: float,
                    cycle: int = 0) -> Alert | None:
    """Warn *before* the pack fills, with enough lead time to reach a bank.

    A 'pack is full' alert is useless: by then the grind has already stopped.
    So this tracks the fill rate and fires when the projected time-to-full
    drops under lead_seconds.
    """
    if not region.grid:
        return
    frame = capture_array(wid, box, cycle=cycle)
    occ, cells = count_occupied(frame, region.grid, threshold=rule.cell_threshold)

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
        log_occupancy(now, occ)
    # Banking empties the pack; drop the old trend and re-arm for the next run.
    if prev is not None and occ < prev - 2:
        rule._history.clear()
        rule._armed = True
    rule._history.append((now, occ))
    if len(rule._history) > 400:
        del rule._history[:200]

    rate = fill_rate(rule._history)
    eta = free / rate if rate > 1e-6 else float("inf")

    if rule.mode == "overflow":
        return _eval_overflow(rule, now, occ, free)

    if not rule._armed:
        return
    hit_lead = eta <= rule.lead_seconds
    hit_floor = free <= rule.warn_free
    if (hit_lead or hit_floor) and rule.ready(now):
        rule._armed = False
        if hit_lead and eta != float("inf"):
            body = (f"{free} slots left, filling at {rate*60:.1f}/min - "
                    f"full in ~{eta:.0f}s. Head to the bank.")
        else:
            body = f"{free} slots left. Head to the bank."
        return rule.fire(now, body)


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
                         f"Check the game.")


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
    text = ocr_cached(wid, box, cycle)
    for line in text.splitlines():
        line = line.strip()
        key = norm_line(line)
        if len(key) < 8 or key in rule._seen:
            continue
        rule._seen.add(key)
        if rule.suppress_pattern and re.search(rule.suppress_pattern, line, re.I):
            # An explained stop. Disarm rather than touch the activity clock, so
            # resuming still needs a real catch to re-arm.
            if rule._primed:
                rule._armed = False
            continue
        if re.search(rule.pattern, line, re.I):
            # Scrollback on the first pass predates the watcher, so prime the
            # activity clock from it without treating it as a live catch.
            if rule._primed:
                rule._last_activity = now
                rule._armed = True
    if len(rule._seen) > 400:
        rule._seen.clear()
        rule._primed = False

    if not rule._primed:
        rule._primed = True
        rule._last_activity = now
        return

    # Never seen activity at all: nothing to report stopping.
    if rule._last_activity == 0.0:
        return

    quiet = now - rule._last_activity
    if rule._armed and quiet >= rule.stop_seconds and rule.ready(now):
        rule._armed = False
        return rule.fire(now, f"No matching activity for {quiet:.0f}s. "
                         f"Check the game.")


def count_by_colour(frame: np.ndarray, grid: tuple, min_blue: float,
                    lum_floor: float = 90.0, pad: float = 0.22,
                    min_cover: float = 0.30) -> int:
    """Count backpack slots whose icon matches a blue colour signature.

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
    part-drawn panel or a tooltip edge looks like.
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
            red, _green, blue = patch[icon].mean(axis=0)
            if float(blue) - float(red) >= min_blue:
                n += 1
    return n


def _eval_item_count(rule: Rule, wid: str, region: "Region", box,
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
    frame = capture_array(wid, box, cycle=cycle)
    n = count_by_colour(frame, region.grid, rule.min_blue)

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
        return

    if level == "ok":
        rule._armed = True
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

    rule._armed = False
    if level == "out":
        stuck = now - rule._level_since
        extra = f" (empty for {stuck/60:.0f} min)" if stuck >= 120 else ""
        return rule.fire(now, (rule.out_message or f"Out of {rule.item}.") + extra)
    else:
        noun = rule.item.rstrip("s") if n == 1 else rule.item
        return rule.fire(now, f"{n} {noun} left. Restock on the next bank trip.")


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
    text = ocr_cached(wid, box, cycle)
    saw_trip = False
    saw_fail = False
    saw_out = False
    for line in text.splitlines():
        line = line.strip()
        key = norm_line(line)
        if len(key) < 8 or key in rule._seen:
            continue
        rule._seen.add(key)
        if rule.trip_pattern and re.search(rule.trip_pattern, line, re.I):
            saw_trip = True
        if rule.out_pattern and re.search(rule.out_pattern, line, re.I):
            saw_out = True
        if re.search(rule.pattern, line, re.I):
            saw_fail = True
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
                         f"Out of {rule.item}. Restock before the next trip.")

    if saw_trip:
        if saw_fail:
            rule._streak += 1
            if rule._streak >= rule.warn_streak and rule.ready(now):
                return rule.fire(
                    now, f"{rule.item} low - the bank has come up short "
                    f"{rule._streak} trips running. Restock soon.")
        else:
            # A clean load means the bank is stocked again.
            rule._streak = 0


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
    text = ocr_cached(wid, box, cycle)
    if not rule.item_pattern:
        return None
    for line in text.splitlines():
        line = line.strip()
        key = norm_line(line)
        if len(key) < 8 or key in rule._seen:
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
            return rule.fire(now, f"{item}{suffix}")
    rule._primed = True
    if len(rule._seen) > 400:
        rule._seen.clear()
        rule._primed = False
    return None


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
    text = ocr_cached(wid, box, cycle)
    if not rule.pattern:
        return None
    gained = 0
    for line in text.splitlines():
        line = line.strip()
        key = norm_line(line)
        if len(key) < 8 or key in rule._seen:
            continue
        m = re.search(rule.pattern, line, re.I)
        if not m:
            continue
        rule._seen.add(key)
        if not rule._primed:
            continue
        try:
            gained += int(re.sub(r"[^0-9]", "", m.group(1)))
        except (ValueError, IndexError):
            continue
    if len(rule._seen) > 400:
        rule._seen.clear()
        rule._primed = False
    if not rule._primed:
        rule._primed = True
        return None
    if gained:
        rule._total += gained
        log_counter(rule.name, now, rule._total)
    step = max(1, int(rule.step))
    reached = rule._total // step
    if reached > rule._milestone:
        rule._milestone = reached
        return rule.fire(now, rule.milestone_message.format(
            total=f"{rule._total:,}", n=reached, step=f"{step:,}"))
    return None


def evaluate(rule: Rule, wid: str, region: "Region", size, now: float,
             cycle: int = 0) -> Alert | None:
    box = region.resolve(size)
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
        text = ocr_cached(wid, box, cycle)
        if not rule.pattern:
            return
        for line in text.splitlines():
            line = line.strip()
            key = norm_line(line)
            if len(key) < 8 or key in rule._seen:
                continue
            if re.search(rule.pattern, line, re.I):
                rule._seen.add(key)
                # The chat tail is full of scrollback on startup. Record what
                # is already on screen without alerting, so the first poll
                # cannot fire on an event from before the watcher existed.
                if rule._primed and rule.ready(now):
                    return rule.fire(now, line[:120])
        rule._primed = True
        if len(rule._seen) > 400:
            # Dropping the whole set would let lines still on screen re-fire,
            # so re-prime on the next pass instead of alerting again.
            rule._seen.clear()
            rule._primed = False
        return

    frame = capture_array(wid, box, rule.mask, cycle)
    if rule._last is None:
        rule._last, rule._last_change = frame, now
        return

    d = mean_abs_diff(frame, rule._last)
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
            return rule.fire(now, f"nothing for {still:.0f}s - probably needs you")


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> dict:
    path = Path(path)
    if not path.exists():
        sys.exit(f"no config at {path}")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"invalid JSON in {path}: {exc}")
    try:
        validate_config(cfg)
    except ValueError as exc:
        sys.exit(f"invalid configuration: {exc}")
    cfg["_regions"] = {k: Region.parse(v) for k, v in cfg["regions"].items()
                       if not k.startswith("_")}
    return cfg


def validate_config(cfg: object) -> None:
    """Validate configuration before any window or capture side effects."""
    if not isinstance(cfg, dict):
        raise ValueError("root must be an object")
    window = cfg.get("window")
    if not isinstance(window, dict) or not isinstance(window.get("wm_class"), str):
        raise ValueError("window.wm_class must be a string")
    if "skill" in cfg and (
            not isinstance(cfg["skill"], str) or not cfg["skill"].strip()):
        raise ValueError("skill must be a non-empty string")
    profile_type = cfg.get("profile_type", "skill")
    if profile_type not in PROFILE_TYPES:
        raise ValueError(
            f"profile_type must be one of {sorted(PROFILE_TYPES)}")
    interval = cfg.get("interval", 1.0)
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
        raise ValueError("interval must be a positive number")

    raw_regions = cfg.get("regions")
    if not isinstance(raw_regions, dict) or not raw_regions:
        raise ValueError("regions must be a non-empty object")
    regions = {}
    for name, spec in raw_regions.items():
        if name.startswith("_"):
            continue
        if not isinstance(name, str):
            raise ValueError("region names must be strings")
        try:
            region = Region.parse(spec)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"region {name!r}: {exc}") from exc
        if region.w <= 0 or region.h <= 0:
            raise ValueError(f"region {name!r} dimensions must be positive")
        if region.grid:
            x0, y0, cw, ch, cols, rows = region.grid
            if x0 < 0 or y0 < 0 or min(cw, ch, cols, rows) <= 0:
                raise ValueError(f"region {name!r} grid values must be positive")
            if x0 + cw * cols > region.w or y0 + ch * rows > region.h:
                raise ValueError(f"region {name!r} grid exceeds region bounds")
        regions[name] = region

    rules = cfg.get("rules")
    if not isinstance(rules, list):
        raise ValueError("rules must be an array")
    names = set()
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"rule {index} must be an object")
        name = rule.get("name")
        kind = rule.get("kind")
        region = rule.get("region")
        if not isinstance(name, str) or not name:
            raise ValueError(f"rule {index} needs a non-empty name")
        if name in names:
            raise ValueError(f"duplicate rule name {name!r}")
        names.add(name)
        if kind not in RULE_KINDS:
            raise ValueError(f"rule {name!r} has unknown kind {kind!r}")
        if region not in regions:
            raise ValueError(f"rule {name!r} references unknown region {region!r}")
        if kind in {"activity", "ocr", "supply"} and not rule.get("pattern"):
            raise ValueError(f"rule {name!r} requires pattern")
        if kind in {"inventory", "item_count"} and not regions[region].grid:
            raise ValueError(f"rule {name!r} requires a region grid")
        for key in ("pattern", "suppress_pattern", "trip_pattern", "out_pattern"):
            pattern = rule.get(key)
            if pattern is not None:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"rule {name!r} has invalid {key}: {exc}") from exc
        for key in ("cooldown", "idle_seconds", "threshold", "lead_seconds",
                    "overflow_seconds", "stop_seconds", "confirm_seconds",
                    "repeat_seconds"):
            value = rule.get(key)
            if value is not None and (
                    not isinstance(value, (int, float)) or isinstance(value, bool)
                    or value < 0):
                raise ValueError(f"rule {name!r}: {key} must be non-negative")


def resolve_window(cfg: dict) -> tuple[str, tuple[int, int]]:
    wid = find_window(cfg["window"]["wm_class"])
    if not wid:
        sys.exit(f"window not found (class={cfg['window']['wm_class']!r}) - is the game running?")
    size = window_size(wid)
    if not size:
        sys.exit(f"could not determine geometry for window {wid}")
    return wid, size


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_calibrate(args) -> None:
    cfg = load_config(args.config)
    wid, size = resolve_window(cfg)
    out = ROOT / "calibrate.png"
    capture(wid, None, out, resize=args.scale)
    with Image.open(out) as im:
        shot = im.size
    print(f"window {wid}  actual {size[0]}x{size[1]}")
    print(f"wrote {out}  ({shot[0]}x{shot[1]})")
    print(f"multiply coords read off that image by {size[0]/shot[0]:.4f}")


def cmd_shot(args) -> None:
    cfg = load_config(args.config)
    wid, size = resolve_window(cfg)
    if args.box:
        parts = args.box.split(",")
        if len(parts) == 5:
            anchor, values = parts[0], parts[1:]
            if anchor not in ANCHORS:
                sys.exit(f"invalid --box anchor {anchor!r}")
        elif len(parts) == 4:
            anchor, values = "top-left", parts
        else:
            sys.exit("--box must be anchor,dx,dy,w,h or x,y,w,h")
        try:
            reg = Region(anchor, *(int(v) for v in values))
        except ValueError as exc:
            sys.exit(f"invalid --box values: {exc}")
        label = "custom"
    else:
        if not args.region:
            sys.exit("shot requires a region name or --box")
        if args.region not in cfg["_regions"]:
            sys.exit(f"unknown region {args.region!r}")
        reg, label = cfg["_regions"][args.region], args.region
    box = reg.resolve(size)
    out = Path(args.out) if args.out else ROOT / f"shot_{label}.png"
    capture(wid, box, out)
    print(f"wrote {out}  {label} anchor={reg.anchor} -> box={box} (window {size[0]}x{size[1]})")


def cmd_regions(args) -> None:
    cfg = load_config(args.config)
    wid, size = resolve_window(cfg)
    print(f"window {wid}  {size[0]}x{size[1]}\n")
    print(f"{'region':<16} {'anchor':<14} {'resolved x,y,w,h'}")
    for n, r in cfg["_regions"].items():
        print(f"{n:<16} {r.anchor:<14} {r.resolve(size)}")
    print(f"\n{'rule':<18} {'kind':<7} {'region':<16} {'params'}")
    for r in cfg["rules"]:
        extra = {k: v for k, v in r.items()
                 if k not in ("name", "kind", "region", "message")}
        flag = "" if r.get("enabled", True) else "  (disabled)"
        print(f"{r['name']:<18} {r['kind']:<7} {r['region']:<16} {extra}{flag}")


def cmd_probe(args) -> None:
    cfg = load_config(args.config)
    wid, size = resolve_window(cfg)
    regs = cfg["_regions"]
    masks = {r["region"]: r.get("mask") for r in cfg["rules"]}
    prev = {k: None for k in regs}
    print(f"window {wid} {size[0]}x{size[1]} - {args.count} samples @ {args.interval}s")
    print("frame-to-frame mean abs diff; pick a threshold above the idle floor\n")
    print(f"{'t':>6}  " + "  ".join(f"{k:>14}" for k in regs))
    for i in range(args.count):
        row = []
        for k, r in regs.items():
            try:
                a = capture_array(wid, r.resolve(size), masks.get(k))
                d = mean_abs_diff(a, prev[k])
                prev[k] = a
                row.append(f"{d:14.2f}" if d == d else f"{'--':>14}")
            except CaptureError:
                row.append(f"{'ERR':>14}")
        print(f"{i*args.interval:6.1f}  " + "  ".join(row), flush=True)
        time.sleep(args.interval)


def cmd_inv(args) -> None:
    """Live per-slot inventory change feed."""
    cfg = load_config(args.config)
    wid, size = resolve_window(cfg)
    reg = cfg["_regions"][args.region]
    if not reg.grid:
        sys.exit(f"region {args.region!r} has no grid defined")
    cols = reg.grid[4]
    prev = None
    # Some item icons are animated (glow, pulse), so their colour shifts every
    # frame and they would drown real events. Learn which slots do that and
    # suppress their colour changes; occupancy changes are still reported.
    seen_frames = 0
    change_count: dict[int, int] = {}
    warmup = 8
    print(f"watching {args.region} every {args.interval}s - ctrl-c to stop")
    t0 = time.monotonic()
    while time.monotonic() - t0 < args.duration:
        frame = capture_array(wid, reg.resolve(size))
        cur = slot_signatures(frame, reg.grid)
        occ = sum(1 for s in cur if s["occ"])
        # An interface (bank, loot, level-up) drawn over the backpack makes
        # every covered cell look occupied and "changed". Exceeding capacity
        # is the reliable tell, so drop the frame rather than report ghosts.
        if occ > args.capacity:
            prev = None
            time.sleep(args.interval)
            continue
        if prev is not None:
            ch = diff_slots(prev, cur)
            seen_frames += 1
            for c in ch:
                if c["kind"] == "changed":
                    change_count[c["slot"]] = change_count.get(c["slot"], 0) + 1
            if seen_frames > warmup:
                noisy = {s for s, n in change_count.items()
                         if n / seen_frames > 0.5}
                ch = [c for c in ch
                      if c["kind"] != "changed" or c["slot"] not in noisy]
            else:
                ch = []          # stay quiet while learning the animated slots
            if ch:
                stamp = time.strftime("%H:%M:%S")
                desc = ", ".join(
                    f"#{c['slot']}({c['slot'] // cols + 1},{c['slot'] % cols + 1}) {c['kind']}"
                    + (f" d{c['delta']}" if "delta" in c else "")
                    for c in ch)
                print(f"{stamp}  occ={occ:<3} {desc}", flush=True)
        prev = cur
        time.sleep(args.interval)
    noisy = {s for s, n in change_count.items() if n / max(1, seen_frames) > 0.5}
    if noisy:
        print(f"\nanimated slots suppressed: {sorted(noisy)}")


def cmd_stats(args) -> None:
    cfg = load_config(args.config)
    cap = next((r.get("capacity", 28) for r in cfg["rules"]
                if r["kind"] == "inventory"), 28)
    cycles = load_cycles(cap)
    if not cycles:
        sys.exit("no complete cycles logged yet - run `watch` through a bank trip first")

    print(f"{'#':>2} {'items':>6} {'peak':>5} {'fish_s':>7} {'transit_s':>10} {'cycle_s':>8} {'eff/hr':>8}")
    fish, transit, totals, gained, peaks = [], [], [], [], []
    for i, c in enumerate(cycles, 1):
        got = c["peak"] - c.get("emptied_to", 0)
        last = c.get("last_gain")
        if last is None:
            continue
        f_s = last - c["first_gain"]
        t_s = c["banked_at"] - last
        total = c["banked_at"] - c["first_gain"]
        if total <= 0:
            continue
        fish.append(f_s); transit.append(t_s); totals.append(total)
        gained.append(got); peaks.append(c["peak"])
        print(f"{i:>2} {got:>6} {c['peak']:>5} {f_s:>7.0f} {t_s:>10.0f} "
              f"{total:>8.0f} {got/total*3600:>8.0f}")

    if not totals:
        sys.exit("no usable cycles")
    n = len(totals)
    tot_items, tot_time = sum(gained), sum(totals)
    mean_transit = sum(transit) / n
    raw = sum(gained[i] / fish[i] for i in range(n) if fish[i] > 0) / max(1, sum(1 for f in fish if f > 0)) * 3600
    print(f"\ncycles: {n}")
    print(f"  mean fishing  {sum(fish)/n:6.0f}s")
    print(f"  mean transit  {mean_transit:6.0f}s   <- walk to bank + banking")
    print(f"  mean cycle    {tot_time/n:6.0f}s")
    print(f"  mean peak     {sum(peaks)/n:6.1f} / {cap} slots used")
    print(f"\n  raw rate        {raw:7.0f} items/hr   (while actually fishing)")
    print(f"  effective rate  {tot_items/tot_time*3600:7.0f} items/hr   (including transit)")
    lost = 100 * (1 - (tot_items/tot_time) / (raw/3600)) if raw else 0
    print(f"  transit costs   {lost:7.1f}%  of your throughput")
    print(f"\nsuggested lead_seconds: {max(5, round(mean_transit)):.0f}"
          f"   (so the pack fills just as you reach the bank)")
    unused = cap - sum(peaks)/n
    if unused > 2:
        print(f"note: averaging {unused:.1f} unused slots per trip - the warning may be "
              f"firing early enough that you bank before filling")


def cmd_alerts(args) -> None:
    """Per-rule alert rates, for answering 'why is it beeping so much'."""
    if not ALERT_LOG.exists():
        sys.exit("no alerts logged yet")
    rows = []
    for line in ALERT_LOG.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    if args.hours:
        cutoff = time.time() - args.hours * 3600
        rows = [r for r in rows if r["t"] >= cutoff]
    if not rows:
        sys.exit(f"no alerts in the last {args.hours}h")

    span = (rows[-1]["t"] - rows[0]["t"]) / 3600 or (1 / 3600)
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["rule"], []).append(r["t"])

    print(f"{len(rows)} alerts over {span:.2f}h  ({len(rows)/span:.1f}/hour)\n")
    print(f"{'rule':<20} {'count':>6} {'per hour':>9} {'median gap':>11}")
    for name, ts in sorted(by.items(), key=lambda kv: -len(kv[1])):
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        med = sorted(gaps)[len(gaps) // 2] if gaps else float("nan")
        gap = f"{med:>10.0f}s" if gaps else f"{'-':>11}"
        print(f"{name:<20} {len(ts):>6} {len(ts)/span:>9.1f} {gap}")

    if args.tail:
        print(f"\nlast {args.tail}:")
        for r in rows[-args.tail:]:
            stamp = time.strftime("%H:%M:%S", time.localtime(r["t"]))
            print(f"  {stamp}  {r['rule']:<18} {r['body'][:70]}")


PID_FILE = STATE_DIR / "watcher.pid"


def claim_singleton() -> None:
    """Refuse to start if another watcher is already running.

    Two instances double every alert, which is indistinguishable from a rule
    being mistuned - and sends you tuning thresholds that were never the
    problem. A stale pid file (crash, SIGKILL) is reclaimed rather than
    treated as fatal, so a hard kill cannot lock the watcher out.
    """
    STATE_DIR.mkdir(exist_ok=True)
    if PID_FILE.exists():
        try:
            old = int(PID_FILE.read_text().strip())
        except (ValueError, OSError):
            old = None
        if old and old != os.getpid():
            if _process_identity(old) is not None:
                sys.exit(f"watcher already running (pid {old}) - "
                         f"stop it first, or delete {PID_FILE}")
    PID_FILE.write_text(str(os.getpid()))
    atexit.register(_release_singleton)


def _release_singleton() -> None:
    try:
        if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
            PID_FILE.unlink()
    except OSError:
        pass


def cmd_watch(args) -> None:
    global ACTIVE_SKILL
    claim_singleton()
    cfg = load_config(args.config)
    ACTIVE_SKILL = cfg.get("skill", "unnamed")
    wid, size = resolve_window(cfg)
    regs = cfg["_regions"]
    rules = [Rule(**{k: v for k, v in r.items() if not k.startswith("_")})
             for r in cfg["rules"] if r.get("enabled", True)]
    if not rules:
        sys.exit("no enabled rules")
    for r in rules:
        if r.kind == "counter":
            r._total = load_counter(r.name)
            r._milestone = int(r._total // max(1, int(r.step)))
            if r._total:
                print(f"  {r.name}: resuming from {r._total:,}", flush=True)

    interval = cfg.get("interval", 1.0)
    wm_class = cfg["window"]["wm_class"]
    print(f"watching skill={ACTIVE_SKILL!r} profile={args.config} "
          f"{wid} ({wm_class}) {size[0]}x{size[1]} every {interval}s")
    for r in rules:
        print(f"  {r.name:<18} {r.kind:<7} -> {r.region}")
    print("ctrl-c to stop", flush=True)

    misses = 0
    cycle = 0
    next_poll = time.monotonic()
    while True:
        cycle += 1
        now = time.monotonic()
        cur = window_size(wid)
        if cur and cur != size:
            print(f"window resized {size} -> {cur}, regions re-anchored", flush=True)
            size = cur
            for r in rules:
                r.reset()
        try:
            for rule in rules:
                try:
                    alert = evaluate(rule, wid, regs[rule.region], size, now, cycle)
                    if alert is not None:
                        notify(alert.title, alert.body, alert.urgency,
                               alert.sound, alert.timeout_ms, alert.rule_name)
                except CaptureError:
                    raise            # handled below: miss counter / reacquire
                except Exception as e:
                    # One malformed rule must not take the watcher down with it.
                    # Losing every alert because a single regex or grid is wrong
                    # is far worse than losing that one rule, and a silent death
                    # looks identical to "it was never running".
                    print(f"rule {rule.name!r} failed: {type(e).__name__}: {e}",
                          flush=True)
            misses = 0
        except CaptureError as e:
            misses += 1
            if misses >= 3:
                new = find_window(wm_class)
                if new:
                    print(f"reacquired window {wid} -> {new}", flush=True)
                    wid, size, misses = new, window_size(new), 0
                    for r in rules:
                        r.reset()
                else:
                    notify("Screen Watcher", "game window gone - stopping", "critical")
                    sys.exit("window gone")
            else:
                print(f"capture miss: {e}", flush=True)
        next_poll += interval
        time.sleep(max(0.0, next_poll - time.monotonic()))


def main() -> None:
    ensure_x_env()
    STATE_DIR.mkdir(exist_ok=True)
    REGION_DIR.mkdir(exist_ok=True)
    p = argparse.ArgumentParser(prog="watcher", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, default=CONFIG_PATH,
                   help="skill profile JSON (default: config.json)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("calibrate"); c.add_argument("--scale", default="30%")
    c.set_defaults(func=cmd_calibrate)

    s = sub.add_parser("shot")
    s.add_argument("region", nargs="?")
    s.add_argument("--box", help="anchor,dx,dy,w,h or x,y,w,h")
    s.add_argument("--out"); s.set_defaults(func=cmd_shot)

    r = sub.add_parser("regions"); r.set_defaults(func=cmd_regions)

    pr = sub.add_parser("probe")
    pr.add_argument("-n", "--count", type=int, default=12)
    pr.add_argument("-i", "--interval", type=float, default=1.5)
    pr.set_defaults(func=cmd_probe)

    iv = sub.add_parser("inv", help="live per-slot inventory change feed")
    iv.add_argument("region", nargs="?", default="backpack")
    iv.add_argument("-i", "--interval", type=float, default=1.0)
    iv.add_argument("-d", "--duration", type=float, default=60)
    iv.add_argument("-c", "--capacity", type=int, default=28,
                    help="frames reporting more occupied slots than this are "
                         "treated as an interface overlay and skipped")
    iv.set_defaults(func=cmd_inv)

    st = sub.add_parser("stats", help="fill/bank cycle analysis from the occupancy log")
    st.set_defaults(func=cmd_stats)

    al = sub.add_parser("alerts", help="per-rule alert rates - which rule is beeping")
    al.add_argument("-H", "--hours", type=float, default=0,
                    help="only consider the last N hours (0 = all)")
    al.add_argument("-t", "--tail", type=int, default=10,
                    help="also show the last N alerts (0 to hide)")
    al.set_defaults(func=cmd_alerts)

    w = sub.add_parser("watch"); w.set_defaults(func=cmd_watch)
    stp = sub.add_parser("status"); stp.set_defaults(func=cmd_status)
    pa = sub.add_parser("pause"); pa.set_defaults(func=cmd_pause)
    res = sub.add_parser("resume"); res.set_defaults(func=cmd_resume)

    args = p.parse_args()
    args.func(args)


# --------------------------------------------------------------------------
# CLI helper commands – status, pause, resume
# --------------------------------------------------------------------------

def _get_pid() -> int | None:
    """Return watcher PID if it identifies this watcher's live process.

    The PID file is only a hint: Linux can reuse a PID after a crash. Verify
    both the command line and process start time before sending a signal.
    """
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    identity = _process_identity(pid)
    if identity is None:
        return None
    return pid


def _process_identity(pid: int) -> tuple[str, int] | None:
    """Return command line and Linux start time for a live watcher PID."""
    proc = Path(f"/proc/{pid}")
    try:
        cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        stat = (proc / "stat").read_text()
    except (OSError, UnicodeDecodeError):
        return None
    fields = stat.rsplit(")", 1)
    if len(fields) != 2:
        return None
    values = fields[1].split()
    if len(values) <= 19 or "watcher.py" not in cmdline or " watch" not in f" {cmdline}":
        return None
    try:
        return cmdline, int(values[19])
    except ValueError:
        return None


def _signal_watcher(sig: signal.Signals) -> int | None:
    """Signal the process recorded by the PID file if its identity is stable."""
    pid = _get_pid()
    if pid is None:
        return None
    before = _process_identity(pid)
    if before is None:
        return None
    try:
        pidfd = os.pidfd_open(pid)
    except (AttributeError, OSError):
        pidfd = None
    try:
        after = _process_identity(pid)
        if after != before:
            return None
        if pidfd is not None and hasattr(signal, "pidfd_send_signal"):
            signal.pidfd_send_signal(pidfd, sig)
        else:
            os.kill(pid, sig)
        return pid
    except OSError:
        return None
    finally:
        if pidfd is not None:
            os.close(pidfd)


def cmd_status(args) -> None:
    """Print a one‑line status of the watcher.

    If the watcher is running, the output looks like::

        running pid 12345

    otherwise::

        stopped
    """
    pid = _get_pid()
    if pid is None:
        print("stopped")
    else:
        print(f"running pid {pid}")


def cmd_pause(args) -> None:
    """Send SIGSTOP to the running watcher, effectively pausing it.

    If the watcher is not running, the command informs the user.
    """
    pid = _signal_watcher(signal.SIGSTOP)
    if pid is None:
        print("no running watcher to pause")
        return
    print(f"paused pid {pid}")


def cmd_resume(args) -> None:
    """Send SIGCONT to a paused watcher to resume it.

    If the watcher is not running, the command informs the user.
    """
    pid = _signal_watcher(signal.SIGCONT)
    if pid is None:
        print("no running watcher to resume")
        return
    print(f"resumed pid {pid}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")

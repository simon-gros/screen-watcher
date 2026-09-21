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
import json
import os
import shutil                       # noqa: F401 - tests patch watcher.shutil
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
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
              "ocr", "change", "idle", "loot", "counter", "stack", "timer",
              "presence", "gauge", "percent", "total", "item_drop"}
PROFILE_TYPES = {"skill", "quest", "boss"}


# --------------------------------------------------------------------------
# window resolution
#
# Lives in `screen_watcher/windows.py`. Re-exported because the backends,
# WindowTracker and the tests all address these as `watcher.find_window`
# and friends; keeping the names here means a test patching
# `watcher.find_window` still reaches every caller that stayed behind.
# --------------------------------------------------------------------------

from screen_watcher.windows import (       # noqa: E402,F401
    _xdo, ensure_x_env, find_window, window_size,
)
# --------------------------------------------------------------------------
# KWin read-only window discovery
#
# Lives in `screen_watcher/kwin.py`. The names are re-exported here because
# the application and its tests address them as `watcher.kwin_find` and so
# on, and because `_check_kwin` and `WindowTracker` call them by module
# attribute so a test patching `watcher.kwin_find` still takes effect.
# --------------------------------------------------------------------------


from screen_watcher import kwin as _kwin   # noqa: E402
from screen_watcher.kwin import (          # noqa: E402,F401
    KWIN_BUS_NAME, KWIN_BUS_PATH, KWinWindow, _parse_kwin_report, _qdbus,
    _session_is_wayland, kwin_available, kwin_find, kwin_scale, kwin_windows,
)

# Keep the extracted module writing to the same state directory the rest of
# the application uses, including when a test redirects it.
_kwin.STATE_DIR = STATE_DIR
# --------------------------------------------------------------------------
# capture backends
#
# Live in `screen_watcher/capture.py`. Re-exported because the rest of the
# application and the tests address them as `watcher.GameInstance`,
# `watcher.make_backend` and so on.
#
# CaptureError must be the SAME class object here and there: `except
# watcher.CaptureError` appears throughout the watch loop, and a second
# class with the same name would silently fail to catch anything.
# --------------------------------------------------------------------------

from screen_watcher.capture import (              # noqa: E402,F401
    BACKENDS, DEFAULT_BACKEND, PORTAL_TOKEN_FILE, CaptureBackend,
    CaptureError, GameInstance, ReplayBackend, WaylandPortalBackend,
    X11ImageMagickBackend, X11XcbBackend, _apply_bright_mask,
    _capture_array_uncached, _portal_screencast_available, capture,
    make_backend,
)
# --------------------------------------------------------------------------
# shared frame scheduler
#
# Lives in `screen_watcher/scheduler.py`. Re-exported because `doctor` and
# the tests build one as `watcher.FrameScheduler`.
# --------------------------------------------------------------------------

from screen_watcher.scheduler import (    # noqa: E402,F401
    FrameScheduler, RegionStats,
)


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

# CaptureError is defined in screen_watcher.capture and imported above;
# redefining it here created a SECOND class, so `except watcher.CaptureError`
# stopped catching anything the backends raised.


def set_active_game(game: "GameInstance | None") -> None:
    """Route capture through a `GameInstance` for the rest of the process.

    Priority 0, step 10. The rule evaluators each take a raw window id and
    call `capture_array`, so migrating them individually would mean changing
    fourteen signatures and every caller at once. Binding the active game
    here instead means one change reaches all of them, and behaviour is
    identical because `GameInstance.frame` owns the same per-cycle cache.

    The win is that rules stop depending on the ImageMagick path: with an
    active instance they inherit whichever backend it holds, which is the
    XCB one by default.
    """
    global ACTIVE_GAME
    ACTIVE_GAME = game


def capture_array(wid: str, box, mask: str | None = None,
                  cycle: int | None = None) -> np.ndarray:
    """Capture a region once per cycle and return its RGB array.

    Delegates to the active `GameInstance` when one is bound, so existing
    call sites get the selected backend without changing their signature.
    """
    game = ACTIVE_GAME
    if game is not None and game.handle == wid and cycle is not None:
        game.begin_cycle(cycle)
        return game.frame(box, mask)
    key = (wid, tuple(box), mask)
    if cycle is not None:
        hit = _FRAME_CACHE.get(key)
        if hit is not None and hit[0] == cycle:
            return hit[1]
    arr = _capture_array_uncached(wid, box)
    if mask == "bright":
        arr = _apply_bright_mask(arr)
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


#: Unix time for 2020-01-01. Any persisted timestamp below this came from
#: the monotonic clock, which counts seconds since boot and so lands in the
# --------------------------------------------------------------------------
# persistence
#
# Lives in `screen_watcher/persistence.py`. The log paths stay here: seven
# tests redirect OCCUPANCY_LOG and COUNTER_LOG to a temporary directory,
# and the module reads them back through `watcher` on each use.
# --------------------------------------------------------------------------

_WALL_CLOCK_FLOOR = 1_577_836_800.0

OCCUPANCY_LOG = STATE_DIR / "occupancy.jsonl"
COUNTER_LOG = STATE_DIR / "counters.jsonl"

from screen_watcher.persistence import (  # noqa: E402,F401
    fill_rate, load_counter, load_cycles, log_counter, log_occupancy,
)


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

    Trailing fragments are stripped before that. The chat panel's scrollbar
    and the 3D scene behind it bleed a stray glyph or two past the end of a
    line, and those land *inside* the key - observed in the alert log as one
    stun firing three times 0.2s apart from:

        "You fail to steal from the target."
        "You fail to steal from the target. v"
        "You fail to steal from the target. v I"

    Three different keys, so the cooldown never applied: each looked like a
    separate event. Anything after the sentence-ending punctuation that is
    too short to be a word is dropped.
    """
    s = re.sub(r"(?<=[.!?])\s+(?:[a-zA-Z]\s*){1,3}$", "", s.strip())
    return re.sub(r"[^a-z0-9]", "", s.lower())


_OCR_CACHE: dict = {}
_FRAME_CACHE: dict = {}

#: Last frame and text per region, for incremental scroll-aware OCR.
_SCROLL_CACHE: dict = {}

#: Rows of overlap kept when OCR'ing only the newly scrolled strip. One text
#: line plus slack, so a line straddling the seam is never cut in half.
_SCROLL_OVERLAP = 24

#: Largest scroll treated as incremental. Beyond this the regions share too
#: little to be worth stitching, so re-read the whole thing.
_SCROLL_MAX = 200


def _row_signature(frame: np.ndarray) -> np.ndarray:
    """Per-row ink counts - a cheap fingerprint for matching scrolled frames."""
    if frame.ndim == 3:
        frame = frame[..., :3].mean(axis=2)
    return (frame > 140).sum(axis=1).astype(np.int32)


def detect_scroll(prev: np.ndarray, cur: np.ndarray,
                  max_shift: int = _SCROLL_MAX) -> int | None:
    """Rows `cur` has scrolled up relative to `prev`, or None if unrelated.

    Chat scrolls upward: the text on row `y` of the previous frame reappears
    on row `y - shift` now. Matching per-row ink counts finds that offset
    without OCR - measured at 42px (two 21px lines) on a live pickpocketing
    session, and 0 when nothing new arrived.

    Returns None when no offset explains the frame, which is the signal to
    fall back to a full read: a resize, a cleared chat, or a jump larger
    than `max_shift`.

    Acceptance is by *margin*, not absolute error. Measured over live cycles,
    a true match scored 2.45-4.60 while its runner-up scored 5.35-7.62 - the
    ranges overlap, so any fixed cutoff either rejects good matches (an
    absolute 3.0 threw away a valid 4.60 shift) or accepts noise. What
    separates them reliably is that the correct offset is distinctly better
    than the next best, so require it to win by a clear factor.
    """
    if prev is None or cur is None or prev.shape != cur.shape:
        return None
    ps, cs = _row_signature(prev), _row_signature(cur)
    n = len(ps)
    scored = []
    for d in range(min(max_shift, n - 1) + 1):
        a = ps[d:] if d else ps
        b = cs[:n - d] if d else cs
        scored.append((float(np.abs(a - b).mean()), d))
    if not scored:
        return None
    scored.sort()
    best_err, best_shift = scored[0]
    if best_shift == 0:
        # An unchanged frame has nothing to beat; judge it on its own error.
        return 0 if best_err <= 3.0 else None
    # Ignore near neighbours of the winner: a one-row-off alignment scores
    # almost as well and would mask a genuinely decisive match.
    rivals = [e for e, d in scored[1:] if abs(d - best_shift) > 4]
    if not rivals:
        return best_shift
    return best_shift if rivals[0] >= best_err * 1.5 else None


def _stitch(old_text: str, new_text: str, keep: int = 40) -> str:
    """Append genuinely new trailing lines of `new_text` to `old_text`.

    Both reads overlap deliberately, so the same line appears in each - and
    Tesseract is not byte-stable on this font, hence `norm_line` rather than
    string equality. Anything in the new strip whose normalised form already
    sits in the old tail is dropped as a re-read.

    The result is capped at `keep` lines. Without that, stitching grows the
    text without bound - observed climbing 31 -> 52 lines over six live
    cycles - so a rule asking for "the last N lines" would silently start
    reading text that has already scrolled off screen.

    Unreadable lines are discarded. The strip's top edge cuts a line through
    the middle of its glyphs, and Tesseract renders that as noise such as
    "g e e P e S e e oL ARl Ty presaetle". Deduplication cannot catch it -
    it matches nothing, precisely because it is garbage - so it would be
    stitched in as a real chat line and shown to every rule.
    """
    old_lines = [ln for ln in old_text.splitlines() if ln.strip()]
    new_lines = [ln for ln in new_text.splitlines() if ln.strip()]
    seen = {norm_line(ln) for ln in old_lines[-12:] if norm_line(ln)}
    fresh = [ln for ln in new_lines
             if norm_line(ln) and norm_line(ln) not in seen
             and is_readable(ln)]
    return "\n".join((old_lines + fresh)[-keep:])


def is_readable(line: str) -> bool:
    """True when a line looks like real chat rather than OCR noise.

    A clipped glyph row decodes into scattered fragments. What separates
    those from real chat is the share of tokens that look like words - three
    or more letters, mostly lowercase. Measured over live captures, real
    lines scored 0.50-0.91 and OCR noise 0.08-0.31, with no overlap; mean
    token length alone was not enough, because noise like "T14- 94321 ACE
    rrire fanses nnry aeirdart" averages a respectable 3.54.

    Lines with fewer than six tokens are exempt: a genuine "You are stunned!"
    is legitimately short, and the ratio is unstable on so few samples.
    """
    tokens = [t for t in re.split(r"\s+", line.strip()) if t]
    if len(tokens) < 6:
        return True
    alpha = [t for t in tokens if any(c.isalnum() for c in t)]
    if not alpha:
        return False
    wordish = sum(
        1 for t in alpha
        if len(t) >= 3 and t.isalpha()
        and sum(c.islower() for c in t) / len(t) > 0.6
    )
    return wordish / len(alpha) >= 0.40


def ocr_scrolling(wid: str, box, cycle: int, psm: int = 6) -> str:
    """OCR a scrolling text region, reading only what actually moved.

    `chat_tail` is 650px of scrollback, but between two 1.5s polls only the
    newest line or two is new - the other ~30 are the same lines shifted up.
    Tesseract cost ~800 ms over the full region and dominated the poll cycle
    (~98% of it), so re-reading those 30 lines every cycle was the single
    largest expense in the program.

    This detects the scroll offset from row ink signatures, OCRs only the
    newly exposed strip, and stitches it onto the cached text. Measured on a
    live session: ~800 ms -> ~215 ms, a 3.7x saving, with identical lines.

    Falls back to a full read whenever the frames cannot be related - a
    resize, a cleared chat, or a jump beyond `_SCROLL_MAX`.
    """
    key = (tuple(box), psm)
    frame = capture_array(wid, box, None, cycle=cycle)
    prev = _SCROLL_CACHE.get(key)

    if prev is not None:
        shift = detect_scroll(prev[0], frame)
        if shift == 0:
            # Nothing moved; the cached text still describes this frame.
            _SCROLL_CACHE[key] = (frame, prev[1])
            return prev[1]
        if shift is not None:
            strip = frame[max(0, frame.shape[0] - shift - _SCROLL_OVERLAP):]
            text = _stitch(prev[1], ocr_array(strip, psm))
            _SCROLL_CACHE[key] = (frame, text)
            return text

    text = ocr_array(frame, psm)
    _SCROLL_CACHE[key] = (frame, text)
    return text


class OcrError(RuntimeError):
    """Tesseract could not be run, or did not finish.

    Distinct from `CaptureError`: the pixels arrived, so the window is fine
    and the watcher must not treat this as a lost window and start trying to
    reacquire it. The per-rule guard in the poll loop catches this and drops
    that one rule for the cycle.
    """


def _run_tesseract(path: str, psm: int, env: dict | None = None) -> str:
    """One tesseract pass over a PNG on disk."""
    try:
        r = subprocess.run(["tesseract", path, "stdout", "--psm", str(psm)],
                           capture_output=True, text=True, timeout=30,
                           env=env)
    except subprocess.TimeoutExpired:
        raise OcrError("tesseract timed out")
    except OSError:
        raise OcrError("tesseract not installed")
    return r.stdout.strip()


def ocr_array(frame: np.ndarray, psm: int = 6) -> str:
    """Tesseract over an in-memory frame.

    `OMP_THREAD_LIMIT=1` is deliberate. Tesseract's OpenMP parallelism is a
    net loss on this workload: measured on a 24-core host, the default took
    1181 ms against 790 ms pinned to a single thread, because the region is
    small enough that thread coordination costs more than it saves.
    """
    if frame.ndim == 3:
        frame = frame[..., :3].mean(axis=2)
    img = np.clip(frame, 0, 255).astype(np.uint8)
    env = dict(os.environ, OMP_THREAD_LIMIT="1")
    with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
        Image.fromarray(img).save(tmp.name)
        return _run_tesseract(tmp.name, psm, env)


def ocr_cached(wid: str, box, cycle: int, psm: int = 6) -> str:
    """OCR a region once per poll cycle, however many rules ask for it.

    Five rules watch `chat_tail`, and each was running its own tesseract pass
    over an identical image. Measured at ~0.8s per pass, that is ~4s of every
    poll cycle spent re-reading the same pixels - which pushed the real interval
    to ~5.9s and made the impling alert arrive 36s after the chat line.

    Keying on the cycle counter (not a timestamp) means every rule in one pass
    sees exactly the same text, so a line cannot be consumed by one rule and
    missed by another that polled a fraction later.

    The pass itself goes through `ocr_scrolling`, which re-reads only the rows
    that actually scrolled rather than the whole region.
    """
    key = (tuple(box), psm)      # Region.resolve always returns (x, y, w, h)
    hit = _OCR_CACHE.get(key)
    if hit is not None and hit[0] == cycle:
        return hit[1]
    text = ocr_scrolling(wid, box, cycle, psm)
    _OCR_CACHE[key] = (cycle, text)
    return text


def ocr(wid: str, box, psm: int = 6) -> str:
    """Tesseract over one region.

    The capture goes through the active `GameInstance` when one is bound, so
    OCR rules inherit the selected backend instead of always paying for an
    ImageMagick subprocess.
    """
    with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
        game = ACTIVE_GAME
        if game is not None and game.handle == wid:
            game.save(box, Path(tmp.name))
        else:
            capture(wid, box, Path(tmp.name))
        return _run_tesseract(tmp.name, psm)


# --------------------------------------------------------------------------
# layered OCR
#
# Priority 0, step 6. RuneScape renders its UI numbers from a fixed sprite
# font, so a template match is both faster and more reliable than general
# OCR on exactly the values that matter most: timers, stack counts, resource
# numbers.
#
# Measured on the session timer: Tesseract needs ~166 ms per read, which is
# ten times the entire four-region XCB capture cycle. After step 3 it is the
# dominant cost in the pipeline.
#
# Templates below were harvested from live gameplay by sampling the session
# timer until all ten digits had been observed, cross-checked against
# Tesseract's reading of the same frame.
# --------------------------------------------------------------------------

_GLYPH_H, _GLYPH_W = 13, 9

_DIGIT_BITS = {
    "0": "000100000001111000111001100110001100110000100100000100100000100100000100100000100100000100110000100010001100001111000",
    "1": "000100000011100000111100000101100000001100000001100000001100000001100000001100000001100000001100000001100000001100000",
    "2": "000100000001111000000000100000000100000000100000000100000001000000011000000110000000100000001100000011000000111100100",
    "3": "000100000011110000000000100000000100000001100000011100001111000001111000000001100000000100000001100000011100111111000",
    "4": "000000100000001110000011100000011100000110100000100100001000100001000100110000100111011110111111111000000100000000100",
    "5": "001111000111111000100000000100000000100000000111111000100011000000001100000000100000000100000000100000001100111111000",
    "6": "000001000000111100001000000011000000010000000110111000110001100110000110110000110110000110110000110010001110001111000",
    "7": "011111011111111111000000001000000010000000110000000110000001110000001100000001000000001000000010000000110000000110000",
    "8": "000110000001111100110000110110000110110000110011101100001111000001111000010001100110000110110000110110000110011111100",
    "9": "000010000001111000010001000110000110110000110110000110011111110001111110000000110000000110000001100000001100001111000",
}


def _load_digit_templates() -> dict[str, np.ndarray]:
    out = {}
    for digit, bits in _DIGIT_BITS.items():
        flat = np.frombuffer(bits.encode(), dtype=np.uint8) - ord("0")
        out[digit] = flat.astype(bool).reshape(_GLYPH_H, _GLYPH_W)
    return out


DIGIT_TEMPLATES = _load_digit_templates()


def segment_glyphs(frame: np.ndarray, threshold: float = 140.0
                   ) -> list[np.ndarray | None]:
    """Split a bright-on-dark numeric readout into normalized glyph boxes.

    Returns one entry per column run: a boolean bitmap for a digit, or None
    for a separator such as a colon. RuneScape lays these out at fixed width
    on one baseline, so column runs segment them exactly - measured on the
    session timer as six 7-8px digits and two 2px colons.
    """
    lum = frame.mean(axis=2)
    mask = lum > threshold
    rows = mask.any(axis=1)
    ys = np.where(rows)[0]
    if not len(ys):
        return []
    top, bottom = int(ys.min()), int(ys.max()) + 1
    cols = mask.any(axis=0)
    runs, start = [], None
    for i, on in enumerate(cols):
        if on and start is None:
            start = i
        elif not on and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(cols)))

    glyphs: list[np.ndarray | None] = []
    for a, b in runs:
        if b - a <= 3:                     # colon / separator
            glyphs.append(None)
            continue
        cell = mask[top:bottom, a:b]
        # Crop each glyph to its OWN ink rather than the whole readout's
        # vertical extent. A window resize changes the UI scale, so glyphs
        # render a pixel taller and every one shifts inside a fixed box -
        # observed as a 14px readout against 13px templates, which dropped
        # the second digit of each pair to 0.75-0.78 and silently disabled
        # the fast path. Per-glyph cropping makes matching scale-tolerant.
        own = cell.any(axis=1)
        ink = np.where(own)[0]
        if len(ink):
            cell = cell[int(ink.min()):int(ink.max()) + 1]
        box = np.zeros((_GLYPH_H, _GLYPH_W), dtype=bool)
        h = min(_GLYPH_H, cell.shape[0])
        w = min(_GLYPH_W, cell.shape[1])
        box[:h, :w] = cell[:h, :w]
        glyphs.append(box)
    return glyphs


def _best_template_score(glyph: np.ndarray) -> tuple[str | None, float]:
    """Best digit and score over small alignment offsets.

    A window resize changes the UI scale, so the same digit renders a pixel
    taller or shifted inside its cell. Measured on a live resize, the second
    digit of each pair fell to 0.75-0.78 and silently disabled the fast
    path - a one-pixel shift recovered it to 0.80+. Trying a +/-1 offset
    costs nine comparisons against a 117-pixel bitmap and removes a whole
    class of scale-dependent failures.
    """
    best, best_score = None, 0.0
    for dy in (0, -1, 1):
        for dx in (0, -1, 1):
            shifted = glyph
            if dy:
                shifted = np.roll(shifted, dy, axis=0)
            if dx:
                shifted = np.roll(shifted, dx, axis=1)
            for digit, template in DIGIT_TEMPLATES.items():
                score = float((shifted == template).sum()) / template.size
                if score > best_score:
                    best, best_score = digit, score
    return best, best_score


def match_digit(glyph: np.ndarray, min_score: float = 0.80
                ) -> tuple[str | None, float]:
    """Best-matching digit for a glyph bitmap, with its agreement score."""
    best, best_score = _best_template_score(glyph)
    return (best, best_score) if best_score >= min_score else (None, best_score)


def read_numeric(frame: np.ndarray, separator: str = ":",
                 threshold: float = 140.0, min_score: float = 0.80
                 ) -> str | None:
    """Read a numeric readout by sprite matching, or None if unsure.

    Returning None rather than a guess is the point: the caller falls back
    to Tesseract, so a confident wrong answer is much worse than admitting
    the match failed.
    """
    glyphs = segment_glyphs(frame, threshold)
    if not glyphs:
        return None
    chars = []
    for glyph in glyphs:
        if glyph is None:
            chars.append(separator)
            continue
        digit, _score = match_digit(glyph, min_score)
        if digit is None:
            return None
        chars.append(digit)
    text = "".join(chars)
    return text if any(c.isdigit() for c in text) else None


def ocr_numeric(wid: str, box, frame: np.ndarray | None = None,
                psm: int = 7) -> str:
    """Layered read: sprite matching first, Tesseract as fallback.

    `frame` lets a caller reuse a frame the scheduler already captured,
    which is what makes the fast path cost effectively nothing.
    """
    if frame is None:
        try:
            frame = _capture_array_uncached(wid, box)
        except CaptureError:
            return ocr(wid, box, psm)
    text = read_numeric(frame)
    if text is not None:
        return text
    return ocr(wid, box, psm)


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


def log_alert(rule_name: str, title: str, body: str,
              source_text: str | None = None) -> None:
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
            record = {
                "t": round(time.time(), 1),
                "skill": ACTIVE_SKILL or "unknown",
                "rule": rule_name,
                "title": title,
                "body": body[:200],
            }
            if source_text is not None:
                record["source_text"] = source_text[:200]
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


# Lives in `screen_watcher/overlay.py`. Re-exported because `notify`
# and `cmd_watch` address it as `watcher.OverlayChannel`, and the
# tests construct it the same way.
from screen_watcher.overlay import OverlayChannel   # noqa: E402,F401

#: Set by `watch` when the overlay is enabled; None everywhere else.
ACTIVE_OVERLAY: "OverlayChannel | None" = None


def notify(title: str, body: str, urgency: str = "normal",
           sound: str | None = None, timeout_ms: int = 8000,
           rule_name: str = "", source_text: str | None = None) -> None:
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
    try:
        subprocess.run(cmd, capture_output=True)
    except OSError:
        # The desktop banner is one of four deliveries; the sound, the alert
        # log and the console line below are the other three. Losing the
        # banner is a degraded alert, but dying here loses the alert itself -
        # and it would happen mid-session, hours after the last green start.
        print("notify-send not installed: banner skipped", flush=True)
    play(sound)
    if ACTIVE_OVERLAY is not None:
        ACTIVE_OVERLAY.send(rule_name or title, body, urgency)
    log_alert(rule_name or title, title, body, source_text)
    print(f"[{time.strftime('%H:%M:%S')}] NOTIFY {title}: {body}", flush=True)


# --------------------------------------------------------------------------
# rules
#
# Live in `screen_watcher/rules.py`. Re-exported because the watch loop,
# the profiles and fifty tests all address them through `watcher`. The
# module resolves capture_array and the OCR entry points through a late
# import, so patching those still reaches the evaluators.
# --------------------------------------------------------------------------

from screen_watcher.rules import (        # noqa: E402,F401
    Alert, Rule, TIMER_RE, _CHAT_TIMESTAMP, _GAUGE_FIXUPS, _QTY_FIXUPS,
    _eval_activity, _eval_counter, _eval_gauge, _eval_inventory,
    _eval_item_count, _eval_item_drop, _eval_loot, _eval_overflow,
    _eval_percent, _eval_presence, _eval_stack, _eval_supply,
    _eval_timer, _eval_total, colour_pixels, count_by_colour, evaluate,
    join_wrapped_lines, parse_gauge, parse_percent, parse_quantity,
    parse_timer, parse_total, stack_signature,
)
# --------------------------------------------------------------------------
# config
#
# Lives in `screen_watcher/config.py`. Re-exported because the CLI, the
# diagnostics and the tests all call `watcher.load_config`. It resolves
# Region, Rule and the kind tables through a late import of this module,
# since those are the vocabulary a profile is written in.
# --------------------------------------------------------------------------

from screen_watcher.config import (       # noqa: E402,F401
    SCHEMA_VERSION, check_fingerprint, load_config, resolve_window,
    validate_config, validate_schema_version,
)
# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# interface readers
#
# Live in `screen_watcher/readers.py`. Re-exported because the tests build
# a ChatReader as `watcher.ChatReader`. The module resolves `ocr_cached`
# and `norm_line` through a late import, so patching those still reaches
# the readers.
# --------------------------------------------------------------------------

from screen_watcher.readers import (      # noqa: E402,F401
    ChatLine, ChatReader, InterfaceReader, ReaderRegistry, _split_stamp,
)
# --------------------------------------------------------------------------
# doctor
#
# Lives in `screen_watcher/diagnostics.py`. A leaf - nothing calls into it -
# so it moved whole. It imports `watcher` lazily, which breaks the circular
# import and keeps `watcher.ocr` and `watcher.capture_array` resolvable at
# call time, so tests patching those still reach the checks that use them.
# --------------------------------------------------------------------------

from screen_watcher.diagnostics import (   # noqa: E402,F401
    FAIL, PASS, WARN, Check, _check_backends, _check_capture, _check_grids,
    _check_kwin, _check_ocr, _check_outputs, _check_profile, _check_regions,
    _check_session, _check_tools, _check_window, cmd_doctor, run_doctor,
)


def cmd_backends(args) -> None:
    """Report which capture backends can run here, and which would be used."""
    selected = make_backend(args.backend)
    print(f"{'backend':<20} {'status':<8} detail")
    for name in sorted(BACKENDS):
        ok, why = BACKENDS[name]().available()
        mark = "*" if name == selected.name else " "
        print(f"{mark}{name:<19} {'OK' if ok else 'UNUSABLE':<8} {why}")
    print(f"\n* selected: {selected.name}")
    if args.backend and selected.name != args.backend:
        print(f"  (requested {args.backend!r})")


def cmd_record(args) -> None:
    """Record full-window frames for later replay.

    Full-window rather than per-region on purpose: a recording outlives the
    region layout it was made under, so a later calibration change can be
    tested against frames captured before it.
    """
    cfg = load_config(Path(args.config)) if args.config else load_config()
    wm_class = cfg["window"]["wm_class"]
    game = GameInstance(wm_class, backend=make_backend(args.backend))
    if not game.acquire():
        sys.exit(f"no window for class {wm_class!r}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    w, h = game.size
    print(f"recording {args.frames} frames of {w}x{h} every "
          f"{args.interval}s to {out}", flush=True)

    written = 0
    for i in range(args.frames):
        game.begin_cycle(i)
        try:
            frame = game.frame((0, 0, w, h), None)
        except CaptureError as e:
            print(f"  frame {i}: {e}", flush=True)
            continue
        path = out / f"frame{i:05d}.png"
        Image.fromarray(frame.astype(np.uint8)).save(path)
        written += 1
        if i + 1 < args.frames:
            time.sleep(args.interval)
    print(f"wrote {written} frames to {out}")
    print(f"replay with: SCREEN_WATCHER_REPLAY={out} "
          f"{sys.argv[0]} doctor --backend replay")


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


#: Never shown in the rule summary: identity is already in its own column,
#: and `_note` fields are multi-paragraph design rationale that made this
#: command's output unreadable when dumped inline.
_RULE_SUMMARY_SKIP = ("name", "kind", "region", "message", "enabled")


def summarise_rule(rule: dict, width: int = 96, verbose: bool = False) -> str:
    """One readable line of a rule's tuning parameters.

    `regions` is a diagnostic command, so its job is to show at a glance what
    a rule is set to. Printing the raw dict defeated that: the profiles carry
    long `_note` essays explaining why each threshold was chosen, and a single
    rule ran to over 2,000 characters of mostly prose.
    """
    parts = []
    for k, v in rule.items():
        if k in _RULE_SUMMARY_SKIP:
            continue
        if k.startswith("_") and not verbose:
            continue
        if isinstance(v, str) and not verbose and len(v) > 32:
            v = v[:29] + "..."
        parts.append(f"{k}={v}")
    line = " ".join(parts)
    if not verbose and len(line) > width:
        line = line[:width - 3] + "..."
    return line


def cmd_counter(args) -> None:
    """Show or correct a persisted counter total.

    The watcher can only count what it sees, so its total is the amount
    earned *while it was watching* - not the session total the game's own
    Metrics panel reports. After running without the watcher, or after the
    counter has drifted, `--set` realigns it so the next milestone lands
    where it should.
    """
    cfg = load_config(args.config)
    counters = [r for r in cfg["rules"] if r["kind"] == "counter"]
    if not counters:
        sys.exit("no counter rules in this profile")

    if args.set is None:
        print(f"{'counter':<20} {'total':>14} {'next milestone':>16}")
        for r in counters:
            total = load_counter(r["name"])
            step = max(1, int(r.get("step", 1)))
            nxt = (total // step + 1) * step
            print(f"{r['name']:<20} {total:>14,} {nxt:>16,}")
        return

    name = args.name or counters[0]["name"]
    if name not in {r["name"] for r in counters}:
        sys.exit(f"unknown counter {name!r}")
    try:
        value = int(str(args.set).replace(",", "").replace("_", ""))
    except ValueError:
        sys.exit(f"not a number: {args.set!r}")
    if value < 0:
        sys.exit("counter totals cannot be negative")

    was = load_counter(name)
    # Written through the normal append path, so the watcher picks it up on
    # its next start exactly as it would any other recorded total.
    log_counter(name, time.monotonic(), value)
    step = max(1, int(next(r for r in counters if r["name"] == name)
                      .get("step", 1)))
    print(f"{name}: {was:,} -> {value:,}")
    print(f"next milestone at {((value // step) + 1) * step:,}")
    print("restart the watcher for this to take effect")


def cmd_regions(args) -> None:
    cfg = load_config(args.config)
    wid, size = resolve_window(cfg)
    print(f"window {wid}  {size[0]}x{size[1]}\n")
    print(f"{'region':<16} {'anchor':<14} {'resolved x,y,w,h'}")
    for n, r in cfg["_regions"].items():
        print(f"{n:<16} {r.anchor:<14} {r.resolve(size)}")
    verbose = getattr(args, "verbose", False)
    print(f"\n{'rule':<18} {'kind':<9} {'region':<16} {'params'}")
    for r in cfg["rules"]:
        flag = "" if r.get("enabled", True) else "  (disabled)"
        print(f"{r['name']:<18} {r['kind']:<9} {r['region']:<16} "
              f"{summarise_rule(r, verbose=verbose)}{flag}")
    if not verbose:
        print("\n(--verbose for full parameters and notes)")


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


# --------------------------------------------------------------------------
# watch-loop runtime
#
# Lives in `screen_watcher/runtime.py`. Re-exported because cmd_watch and
# the tests build these as `watcher.WindowTracker` and friends. The module
# resolves find_window, window_size and kwin_find through a late import, so
# the six tests that drive WindowTracker by patching `watcher.find_window`
# still reach it.
# --------------------------------------------------------------------------

from screen_watcher.runtime import (      # noqa: E402,F401
    RegionHealth, WindowTracker, _release_singleton, _terminate,
    claim_singleton, next_deadline,
)

#: Owned here, not by the runtime module: three tests redirect it to a
#: temporary path, and the helpers there read it back through `watcher`.
PID_FILE = STATE_DIR / "watcher.pid"


def cmd_watch(args) -> None:
    global ACTIVE_SKILL
    claim_singleton()
    cfg = load_config(args.config)
    ACTIVE_SKILL = cfg.get("skill", "unnamed")
    wid, size = resolve_window(cfg)
    # Bind the capture stack built in steps 1-3 so every rule inherits the
    # selected backend instead of spawning ImageMagick per region.
    backend = make_backend(getattr(args, "backend", None))
    game = GameInstance(cfg["window"]["wm_class"], backend=backend)
    if game.acquire():
        wid, size = game.handle, game.size
        set_active_game(game)
    else:
        game = None
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
          f"{wid} ({wm_class}) {size[0]}x{size[1]} every {interval}s "
          f"backend={backend.name if game else 'imagemagick (no window)'}")
    for r in rules:
        print(f"  {r.name:<18} {r.kind:<7} -> {r.region}")
    print("ctrl-c to stop", flush=True)

    tracker = WindowTracker(wm_class, wid, size, game)
    health = RegionHealth()

    global ACTIVE_OVERLAY
    if getattr(args, "overlay", False):
        channel = OverlayChannel()
        ok, why = channel.available()
        if ok and channel.start():
            ACTIVE_OVERLAY = channel
            print("overlay: on-screen alerts enabled", flush=True)
        else:
            # Opt-in and non-essential: say why once and carry on, rather
            # than refusing to watch because a display extra is missing.
            print(f"overlay unavailable ({why}); continuing without it",
                  flush=True)
    cycle = 0
    next_poll = time.monotonic()
    while True:
        cycle += 1
        now = time.monotonic()
        if tracker.hidden:
            # The window is unmapped (minimised, or on another desktop).
            # Capturing it would fail every cycle, so skip the rules
            # entirely and spend the cycle trying to get it back.
            status, _ = tracker.reacquire()
            if status == "ok":
                wid, size = tracker.wid, tracker.size
                print(f"window back {wid} {size[0]}x{size[1]}", flush=True)
                for r in rules:
                    r.reset()
            elif status == "gone":
                notify("Screen Watcher", "game window gone - stopping", "critical")
                sys.exit("window gone")
            next_poll = next_deadline(next_poll, interval)
            time.sleep(max(0.0, next_poll - time.monotonic()))
            continue
        cur = tracker.poll_size()
        if cur and tracker.note_resize(cur):
            print(f"window resized {size} -> {cur}, regions re-anchored", flush=True)
            size = cur
            for r in rules:
                r.reset()
        try:
            # Health is read from the per-cycle frame cache the rules are
            # about to populate, so watching costs one dictionary lookup
            # per region rather than another capture.
            for name in {r.region for r in rules}:
                try:
                    frame = capture_array(wid, regs[name].resolve(size),
                                          None, cycle)
                except CaptureError:
                    continue
                problem = health.note(name, frame)
                if problem:
                    print(f"health: {problem}", flush=True)
                    notify("Screen Watcher", problem, "critical")
            for rule in rules:
                try:
                    if rule.corroborate_region:
                        # Resolved here because only the loop knows the current
                        # window size and the full region table.
                        rule._corroborate_box = regs[
                            rule.corroborate_region].resolve(size)
                    alert = evaluate(rule, wid, regs[rule.region], size, now, cycle)
                    if alert is not None:
                        notify(alert.title, alert.body, alert.urgency,
                               alert.sound, alert.timeout_ms, alert.rule_name,
                               alert.source_text)
                except CaptureError:
                    raise            # handled below: miss counter / reacquire
                except Exception as e:
                    # One malformed rule must not take the watcher down with it.
                    # Losing every alert because a single regex or grid is wrong
                    # is far worse than losing that one rule, and a silent death
                    # looks identical to "it was never running".
                    print(f"rule {rule.name!r} failed: {type(e).__name__}: {e}",
                          flush=True)
            tracker.note_success()
        except CaptureError as e:
            if not tracker.note_miss():
                print(f"capture miss: {e}", flush=True)
            else:
                status, was = tracker.reacquire()
                if status == "ok":
                    wid, size = tracker.wid, tracker.size
                    if was:
                        print(f"reacquired window {was} -> {wid}", flush=True)
                    for r in rules:
                        r.reset()
                elif status == "hidden":
                    why = was or "minimised or off-desktop"
                    print(f"window hidden ({why}), waiting", flush=True)
                else:
                    notify("Screen Watcher", "game window gone - stopping", "critical")
                    sys.exit("window gone")
        next_poll = next_deadline(next_poll, interval)
        time.sleep(max(0.0, next_poll - time.monotonic()))


def main() -> None:
    ensure_x_env()
    STATE_DIR.mkdir(exist_ok=True)
    REGION_DIR.mkdir(exist_ok=True)
    p = argparse.ArgumentParser(prog="watcher", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, default=CONFIG_PATH,
                   help="skill profile JSON (default: config.json)")
    p.add_argument("--backend", default=None, choices=sorted(BACKENDS),
                   help=f"capture backend (default: {DEFAULT_BACKEND}, "
                        f"falls back when unavailable)")
    sub = p.add_subparsers(dest="cmd", required=True)

    bk = sub.add_parser("backends",
                        help="list capture backends and whether they work here")
    bk.set_defaults(func=cmd_backends)

    dr = sub.add_parser("doctor",
                        help="PASS/WARN/FAIL diagnostics for the whole stack")
    dr.set_defaults(func=cmd_doctor)

    rec = sub.add_parser("record",
                         help="record full-window frames for replay")
    rec.add_argument("--out", default="state/recording",
                     help="directory to write frames to")
    rec.add_argument("--frames", type=int, default=20,
                     help="how many frames to record")
    rec.add_argument("--interval", type=float, default=1.5,
                     help="seconds between frames")
    rec.set_defaults(func=cmd_record)

    c = sub.add_parser("calibrate"); c.add_argument("--scale", default="30%")
    c.set_defaults(func=cmd_calibrate)

    s = sub.add_parser("shot")
    s.add_argument("region", nargs="?")
    s.add_argument("--box", help="anchor,dx,dy,w,h or x,y,w,h")
    s.add_argument("--out"); s.set_defaults(func=cmd_shot)

    ct = sub.add_parser("counter",
                        help="show or correct a persisted counter total")
    ct.add_argument("--set", help="new total, e.g. 2300000")
    ct.add_argument("--name", help="counter rule name (default: the first)")
    ct.set_defaults(func=cmd_counter)

    r = sub.add_parser("regions",
                       help="show regions and rules resolved against the "
                            "current window")
    r.add_argument("-v", "--verbose", action="store_true",
                   help="show full rule parameters including design notes")
    r.set_defaults(func=cmd_regions)

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

    w = sub.add_parser("watch",
                       help="run the polling and notification loop")
    w.add_argument("--overlay", action="store_true",
                   help="also show alerts in a click-through on-screen "
                        "overlay (Wayland only, needs pyside6)")
    w.set_defaults(func=cmd_watch)
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

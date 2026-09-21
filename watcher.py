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
import difflib
import json
import os
import shutil
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
VERSION_PATH = ROOT / "VERSION"
__version__ = VERSION_PATH.read_text(encoding="utf-8").strip()
CONFIG_PATH = ROOT / "config.json"
STATE_DIR = ROOT / "state"
ACTIVE_SKILL = ""

ANCHORS = {"top-left", "top-right", "bottom-left", "bottom-right",
           "top-center", "bottom-center", "center"}
RULE_KINDS = {"inventory", "activity", "supply", "item_count",
              "ocr", "change", "idle", "loot", "counter", "stack", "timer", "presence"}
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
# capture backends
#
# Priority 0, step 1: separate "which window and how do we get pixels" from
# "what do those pixels mean". Detectors and profiles must not name
# ImageMagick, xdotool, or any other platform tool; they ask a GameInstance
# for a frame and get a numpy array back.
#
# This is the seam the documented Wayland/PipeWire and replay backends plug
# into later. It is deliberately introduced before those backends exist,
# because retrofitting an interface underneath a dozen call sites is what
# makes that work expensive.
# --------------------------------------------------------------------------

class CaptureBackend:
    """How to find the game window and read pixels out of it.

    Subclasses own every platform detail. The rest of the application sees
    only `find`, `size`, `grab_file`, and `grab_array`.
    """

    name = "abstract"

    def available(self) -> tuple[bool, str]:
        """Whether this backend can run here, and why not when it cannot."""
        return False, "not implemented"

    def find(self, wm_class: str) -> str | None:
        raise NotImplementedError

    def size(self, handle: str) -> tuple[int, int] | None:
        raise NotImplementedError

    def grab_file(self, handle: str, box, out: Path,
                  resize: str | None = None) -> Path:
        raise NotImplementedError

    def grab_array(self, handle: str, box) -> np.ndarray:
        raise NotImplementedError


class X11ImageMagickBackend(CaptureBackend):
    """Current capture path: xdotool for discovery, ImageMagick for pixels.

    This is the behaviour Screen Watcher has been measured against, so it
    stays the default until a native XCB/XShm backend is benchmarked against
    it (Priority 0, step 3). Wrapping it unchanged keeps that comparison
    honest - the new backend has to beat a known quantity.
    """

    name = "x11-imagemagick"

    def available(self) -> tuple[bool, str]:
        ensure_x_env()
        if not os.environ.get("DISPLAY"):
            return False, "no DISPLAY (X11/XWayland session not reachable)"
        missing = [t for t in ("xdotool", "import") if not shutil.which(t)]
        if missing:
            return False, f"missing tool(s): {', '.join(missing)}"
        return True, "ok"

    def find(self, wm_class: str) -> str | None:
        return find_window(wm_class)

    def size(self, handle: str) -> tuple[int, int] | None:
        return window_size(handle)

    def grab_file(self, handle: str, box, out: Path,
                  resize: str | None = None) -> Path:
        return capture(handle, box, out, resize)

    def grab_array(self, handle: str, box) -> np.ndarray:
        return _capture_array_uncached(handle, box)


class X11XcbBackend(CaptureBackend):
    """Native X11 capture through XCB `GetImage`.

    Measured against the ImageMagick path on a 3840x2058 RuneScape window,
    same four regions, 15 iterations each:

        region           ImageMagick    XCB     speedup
        chat_tail          308.9 ms   11.3 ms     27x
        backpack           178.5 ms    3.5 ms     51x
        session_timer       80.1 ms    0.2 ms    387x
        activity_icon       81.2 ms    0.3 ms    242x
        FULL CYCLE         648.6 ms   15.3 ms     42x

    The gap is mostly fixed cost: ImageMagick pays process spawn, PPM encode,
    and PPM decode per region, so small regions are penalised hardest. XCB
    asks the X server for the pixels and gets them back over the existing
    connection.

    Output is byte-identical to the ImageMagick path (mean abs diff 0.000,
    max 0) on the same window, which is what makes it safe to swap in.

    Window discovery still uses xdotool: it is not on the hot path, it runs
    once per acquire rather than per region, and reimplementing WM_CLASS
    matching over raw XCB would add risk for no measurable gain.
    """

    name = "x11-xcb"

    def __init__(self):
        self._conn = None
        self._display = None

    # -- connection --------------------------------------------------------

    def _connect(self):
        """Open the X connection lazily and reuse it.

        A per-capture connection would reintroduce exactly the fixed cost
        this backend exists to remove.
        """
        display = os.environ.get("DISPLAY")
        if self._conn is not None and self._display == display:
            return self._conn
        import xcffib
        self._conn = xcffib.connect(display=display)
        self._display = display
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.disconnect()
            except Exception:
                pass
            self._conn = None

    def available(self) -> tuple[bool, str]:
        ensure_x_env()
        if not os.environ.get("DISPLAY"):
            return False, "no DISPLAY (X11/XWayland session not reachable)"
        try:
            import xcffib                      # noqa: F401
            import xcffib.xproto               # noqa: F401
        except ImportError as e:
            return False, f"python-xcffib unavailable: {e}"
        if not shutil.which("xdotool"):
            return False, "missing tool(s): xdotool"
        try:
            self._connect().get_setup()
        except Exception as e:
            return False, f"cannot connect to X display: {type(e).__name__}"
        return True, "ok"

    # -- capture -----------------------------------------------------------

    def find(self, wm_class: str) -> str | None:
        return find_window(wm_class)

    def size(self, handle: str) -> tuple[int, int] | None:
        return window_size(handle)

    def grab_array(self, handle: str, box) -> np.ndarray:
        # Validate arguments before touching the optional xcffib dependency.
        # This keeps invalid-input behaviour deterministic even on hosts that
        # intentionally rely on the ImageMagick fallback.
        if not handle:
            raise CaptureError("empty window id")
        x, y, w, h = (int(v) for v in box)
        if w <= 0 or h <= 0:
            raise CaptureError(f"degenerate capture box {box!r}")
        try:
            import xcffib.xproto as xproto
        except ImportError as e:
            raise CaptureError(f"python-xcffib unavailable: {e}") from e
        try:
            conn = self._connect()
            reply = conn.core.GetImage(
                xproto.ImageFormat.ZPixmap, int(handle),
                x, y, w, h, 0xFFFFFFFF).reply()
        except Exception as e:
            # A resize or unmap between geometry lookup and capture surfaces
            # as a protocol error. Drop the connection so the next attempt
            # reconnects rather than reusing a poisoned one.
            self.close()
            raise CaptureError(
                f"xcb GetImage failed: {type(e).__name__}") from e
        buf = bytes(reply.data)
        expected = w * h * 4
        if len(buf) < expected:
            raise CaptureError(
                f"short xcb capture: {len(buf)} of {expected} bytes")
        arr = np.frombuffer(buf[:expected], dtype=np.uint8).reshape(h, w, 4)
        # X11 ZPixmap at depth 24 is BGRX on little-endian hosts.
        return arr[:, :, [2, 1, 0]].astype(np.int16)

    def grab_file(self, handle: str, box, out: Path,
                  resize: str | None = None) -> Path:
        """Write a capture without introducing an ImageMagick dependency.

        XCB's array path requires an explicit rectangle, so a full-window shot
        resolves the current geometry first. Calibration percentage resizing
        is then handled by Pillow; an XCB-only installation can therefore use
        `calibrate` and `shot` just like `watch`.
        """
        if box is None:
            size = self.size(handle)
            if not size:
                raise CaptureError(f"could not determine geometry for window {handle}")
            box = (0, 0, size[0], size[1])
        arr = self.grab_array(handle, box)
        image = Image.fromarray(arr.astype(np.uint8))
        if resize:
            m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)%\s*", str(resize))
            if not m:
                raise CaptureError(
                    f"XCB calibration resize expects a percentage, got {resize!r}")
            scale = float(m.group(1)) / 100.0
            if scale <= 0:
                raise CaptureError("resize percentage must be positive")
            target = (max(1, round(image.width * scale)),
                      max(1, round(image.height * scale)))
            image = image.resize(target, Image.Resampling.LANCZOS)
        image.save(out)
        return out


BACKENDS: dict[str, type[CaptureBackend]] = {
    X11ImageMagickBackend.name: X11ImageMagickBackend,
    X11XcbBackend.name: X11XcbBackend,
}
DEFAULT_BACKEND = X11XcbBackend.name


def make_backend(name: str | None = None) -> CaptureBackend:
    """Build a backend by name, falling back when it cannot run here.

    Falling back rather than failing keeps the application usable on a host
    without python-xcffib, at the cost of the measured 42x speedup.
    """
    chosen = name or DEFAULT_BACKEND
    if chosen not in BACKENDS:
        raise ValueError(f"unknown backend {chosen!r}; "
                         f"known: {', '.join(sorted(BACKENDS))}")
    backend = BACKENDS[chosen]()
    ok, _why = backend.available()
    if ok or name:
        # An explicitly requested backend is returned even when unavailable,
        # so `doctor` can report precisely why it will not work.
        return backend
    fallback = X11ImageMagickBackend()
    return fallback if fallback.available()[0] else backend


def make_runtime_backend(name: str | None = None) -> CaptureBackend:
    """Return a backend that is actually usable for a live watch session.

    `make_backend` deliberately returns an explicitly requested backend even
    when it is unavailable so diagnostics can explain the problem. A live
    watcher has different semantics: it must fail before entering the polling
    loop rather than turning every detector call into the same dependency
    error.
    """
    backend = make_backend(name)
    ok, why = backend.available()
    if not ok:
        requested = f"requested backend {name!r}" if name else "capture backend"
        raise CaptureError(f"{requested} unavailable: {why}")
    return backend


class GameInstance:
    """One game window observed through one backend.

    Holds the window handle and current size so callers stop threading `wid`
    and `size` through every function, and owns the per-cycle frame cache so
    several readers looking at the same region in the same cycle cause one
    capture rather than one each.
    """

    def __init__(self, wm_class: str, backend: CaptureBackend | None = None):
        self.wm_class = wm_class
        self.backend = backend or X11ImageMagickBackend()
        self.handle: str | None = None
        self.size: tuple[int, int] | None = None
        self._frames: dict = {}
        self._cycle: int | None = None

    # -- discovery ---------------------------------------------------------

    def acquire(self) -> bool:
        """Locate the window. False when the game is not running."""
        handle = self.backend.find(self.wm_class)
        if not handle:
            return False
        size = self.backend.size(handle)
        if not size:
            return False
        self.handle, self.size = handle, size
        self._frames.clear()
        return True

    def refresh_size(self) -> tuple[int, int] | None:
        """Re-read geometry; callers use this to notice a resize."""
        if not self.handle:
            return None
        size = self.backend.size(self.handle)
        if size and size != self.size:
            self.size = size
            self._frames.clear()
        return size

    # -- frames ------------------------------------------------------------

    def begin_cycle(self, cycle: int) -> None:
        """Start a new sampling cycle, dropping the previous cycle's frames.

        Keying the cache on an explicit cycle rather than a timestamp means
        every reader in one pass sees the same pixels, so two detectors
        cannot disagree about a frame that changed between them.
        """
        if cycle != self._cycle:
            self._cycle = cycle
            self._frames.clear()

    def frame(self, box, mask: str | None = None) -> np.ndarray:
        """RGB array for `box`, captured at most once per cycle."""
        if not self.handle:
            raise CaptureError("no game window acquired")
        key = (tuple(box), mask)
        hit = self._frames.get(key)
        if hit is not None:
            return hit
        # Masks are derived from the raw pixels, so cache the unmasked frame
        # too: asking for a plain and a masked view of one region in the same
        # cycle must still cost a single capture.
        raw_key = (tuple(box), None)
        raw = self._frames.get(raw_key)
        if raw is None:
            raw = self.backend.grab_array(self.handle, box)
            self._frames[raw_key] = raw
        arr = _apply_bright_mask(raw) if mask == "bright" else raw
        self._frames[key] = arr
        return arr

    def save(self, box, out: Path, resize: str | None = None) -> Path:
        if not self.handle:
            raise CaptureError("no game window acquired")
        return self.backend.grab_file(self.handle, box, out, resize)


# --------------------------------------------------------------------------
# shared frame scheduler
#
# Priority 0, step 2. The architecture note asks for the game window to be
# treated as "a continuously sampled data source, not a sequence of unrelated
# screenshot subprocesses".
#
# Note what that does NOT mean here. The obvious reading - grab one immutable
# full-window frame and crop every region out of it - was measured and is
# much worse at this resolution:
#
#     4 cropped captures      4.2 ms/cycle
#     1 full + numpy crops   48.1 ms/cycle   (11.4x slower)
#
# The thieving profile's four live regions total 0.69 MPx against a 7.90 MPx
# window, so a full grab moves ~11x more pixels and the encode/decode cost
# tracks that ratio exactly. Capture strategy is therefore per-region, and
# the scheduler's job is coherence and accounting rather than fewer pixels.
# --------------------------------------------------------------------------

@dataclass
class RegionStats:
    """Per-region capture accounting, surfaced later by `doctor`."""

    name: str
    captures: int = 0
    reuses: int = 0
    failures: int = 0
    last_error: str = ""
    total_seconds: float = 0.0

    @property
    def mean_ms(self) -> float:
        return (self.total_seconds / self.captures * 1000.0) if self.captures else 0.0


class FrameScheduler:
    """Drives one sampling pass over a set of named regions.

    Responsibilities:

    - own the cycle counter so callers stop hand-rolling one;
    - resolve named regions against the current window size;
    - serve every reader in a pass from one coherent set of frames;
    - record per-region timing, reuse, and failure counts;
    - report frame health so a blank or frozen region is visible as such
      rather than silently producing confident detections.

    It deliberately does not decide *what* a region means. Readers and rules
    sit above it.
    """

    def __init__(self, game: GameInstance, regions: dict):
        self.game = game
        self.regions = regions
        self.cycle = 0
        self.stats: dict[str, RegionStats] = {}
        self._last_hashes: dict[str, int] = {}
        self._static_cycles: dict[str, int] = {}

    def _stat(self, name: str) -> RegionStats:
        if name not in self.stats:
            self.stats[name] = RegionStats(name)
        return self.stats[name]

    def begin(self) -> int:
        """Open a new sampling pass and return its cycle number."""
        self.cycle += 1
        self.game.begin_cycle(self.cycle)
        return self.cycle

    def box_for(self, name: str):
        """Absolute box for a configured region at the current window size."""
        if name not in self.regions:
            raise KeyError(f"unknown region {name!r}")
        if not self.game.size:
            raise CaptureError("no game window acquired")
        return self.regions[name].resolve(self.game.size)

    def frame(self, name: str, mask: str | None = None) -> np.ndarray:
        """Frame for a named region, captured at most once per cycle."""
        box = self.box_for(name)
        stat = self._stat(name)
        key = (tuple(box), mask)
        if key in self.game._frames:
            stat.reuses += 1
            return self.game._frames[key]
        started = time.monotonic()
        try:
            arr = self.game.frame(box, mask)
        except CaptureError as e:
            stat.failures += 1
            stat.last_error = str(e)[:200]
            raise
        stat.captures += 1
        stat.total_seconds += time.monotonic() - started
        if mask is None:
            self._track_health(name, arr)
        return arr

    def prefetch(self, names) -> list[str]:
        """Capture several regions up front, returning the ones that failed.

        A pass that needs three regions should not abandon the other two
        because the first was mid-repaint, so failures are collected rather
        than raised.
        """
        failed = []
        for name in names:
            try:
                self.frame(name)
            except (CaptureError, KeyError):
                failed.append(name)
        return failed

    # -- health ------------------------------------------------------------

    def _track_health(self, name: str, arr: np.ndarray) -> None:
        """Notice blank and frozen regions.

        A region that is uniformly flat is almost certainly a capture fault
        rather than real content, and one whose pixels never change across
        cycles suggests a frozen or occluded window. Both produce confident
        but meaningless detections if nothing watches for them.
        """
        digest = hash(arr.tobytes())
        if self._last_hashes.get(name) == digest:
            self._static_cycles[name] = self._static_cycles.get(name, 0) + 1
        else:
            self._static_cycles[name] = 0
        self._last_hashes[name] = digest

    def health(self, name: str, static_limit: int = 20) -> tuple[str, str]:
        """PASS/WARN/FAIL plus a reason, in the shape `doctor` will print."""
        stat = self.stats.get(name)
        if stat is None:
            return "WARN", "never captured"
        if stat.captures == 0:
            if stat.failures:
                return "FAIL", stat.last_error or "all captures failed"
            return "WARN", "never captured"
        static = self._static_cycles.get(name, 0)
        if static >= static_limit:
            return "WARN", f"unchanged for {static} cycles (frozen or occluded?)"
        if stat.failures:
            return "WARN", f"{stat.failures} capture failure(s); {stat.last_error}"
        return "PASS", f"{stat.captures} captures, {stat.mean_ms:.1f} ms mean"

    def report(self) -> list[tuple[str, str, str]]:
        """Health for every region the scheduler has been asked about."""
        return [(n, *self.health(n)) for n in sorted(self.stats)]


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

    def unclamped(self, win: tuple[int, int]) -> tuple[int, int, int, int]:
        """Configured absolute rectangle before window-boundary clamping."""
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
        return (x, y, self.w, self.h)

    def resolve(self, win: tuple[int, int]) -> tuple[int, int, int, int]:
        """Absolute (x, y, w, h) for a window of size `win`, clamped in-bounds."""
        W, H = win
        x, y, configured_w, configured_h = self.unclamped(win)
        w = max(1, min(configured_w, W))
        h = max(1, min(configured_h, H))
        x = max(0, min(x, W - w))
        y = max(0, min(y, H - h))
        return (x, y, w, h)


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------

class CaptureError(RuntimeError):
    pass


class OCRReadError(RuntimeError):
    """Tesseract failed rather than legitimately reading no text."""
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


def _apply_bright_mask(arr: np.ndarray) -> np.ndarray:
    """Isolate bright text from a semi-transparent panel.

    Game panels let the moving 3D world bleed through, which creates a
    constant frame-to-frame diff floor. Text is much brighter than the
    bleed-through, so a luminance threshold drops that floor to ~0.
    """
    lum = arr.mean(axis=2)
    return (lum > 140).astype(np.int16) * 255


def _capture_array_uncached(wid: str, box) -> np.ndarray:
    """One region capture, no caching and no masking."""
    with tempfile.NamedTemporaryFile(suffix=".ppm") as tmp:
        capture(wid, box, Path(tmp.name))
        try:
            with Image.open(tmp.name) as im:
                return np.asarray(im.convert("RGB"), dtype=np.int16)
        except (OSError, ValueError) as e:
            # `import` can leave a short file while the window is repainting.
            raise CaptureError(f"unreadable capture: {e}") from e


ACTIVE_GAME: "GameInstance | None" = None


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


OCCUPANCY_LOG = STATE_DIR / "occupancy.jsonl"
COUNTER_LOG = STATE_DIR / "counters.jsonl"


def log_counter(name: str, total: int, skill: str | None = None,
                wall_time: float | None = None) -> None:
    """Persist a running counter with a cross-process wall-clock timestamp."""
    try:
        STATE_DIR.mkdir(exist_ok=True)
        record = {
            "t": round(time.time() if wall_time is None else wall_time, 1),
            "skill": skill or ACTIVE_SKILL or "unknown",
            "name": name,
            "total": total,
        }
        with COUNTER_LOG.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def load_counter(name: str, skill: str | None = None) -> int:
    """Last recorded total for one profile counter, with legacy fallback."""
    if not COUNTER_LOG.exists():
        return 0
    rows = []
    try:
        for line in COUNTER_LOG.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("name") == name:
                rows.append(row)
    except OSError:
        return 0
    if skill:
        scoped = [row for row in rows if row.get("skill") == skill]
        rows = scoped if scoped else [row for row in rows if "skill" not in row]
    return int(rows[-1].get("total", 0)) if rows else 0


def log_occupancy(occ: int, skill: str | None = None,
                  wall_time: float | None = None) -> None:
    """Append one profile-scoped occupancy transition using Unix wall time."""
    try:
        STATE_DIR.mkdir(exist_ok=True)
        record = {
            "t": round(time.time() if wall_time is None else wall_time, 1),
            "skill": skill or ACTIVE_SKILL or "unknown",
            "occ": occ,
        }
        with OCCUPANCY_LOG.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def load_cycles(capacity: int, gap: float = 300.0,
                skill: str | None = None) -> list[dict]:
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
    if skill:
        scoped = [row for row in rows if row.get("skill") == skill]
        # Existing pre-migration logs have no skill field. Use them only when
        # no scoped records exist, rather than mixing old and new histories.
        rows = scoped if scoped else [row for row in rows if "skill" not in row]
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
    text = ocr(wid, box, psm, cycle=cycle)
    _OCR_CACHE[key] = (cycle, text)
    return text


def ocr(wid: str, box, psm: int = 6, cycle: int | None = None,
        game: "GameInstance | None" = None) -> str:
    """Tesseract over one region using the selected capture backend.

    When a `GameInstance` and cycle are available, OCR is encoded from the
    exact frame already cached for that cycle. Pixel rules and text rules can
    therefore consume the same pixels rather than recapturing the region.
    `game` is explicit for diagnostics; normal live rules use ACTIVE_GAME.
    """
    selected = game if game is not None else ACTIVE_GAME
    with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
        if selected is not None and selected.handle == wid:
            if cycle is not None:
                selected.begin_cycle(cycle)
                frame = selected.frame(box)
                Image.fromarray(frame.astype(np.uint8)).save(tmp.name)
            else:
                selected.save(box, Path(tmp.name))
        else:
            capture(wid, box, Path(tmp.name))
        r = subprocess.run(["tesseract", tmp.name, "stdout", "--psm", str(psm)],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            detail = (r.stderr or "").strip().replace("\n", " ")[:200]
            raise OCRReadError(
                f"tesseract failed with exit {r.returncode}"
                + (f": {detail}" if detail else ""))
        return r.stdout.strip()


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
        box = np.zeros((_GLYPH_H, _GLYPH_W), dtype=bool)
        h = min(_GLYPH_H, cell.shape[0])
        w = min(_GLYPH_W, cell.shape[1])
        box[:h, :w] = cell[:h, :w]
        glyphs.append(box)
    return glyphs


def match_digit(glyph: np.ndarray, min_score: float = 0.80
                ) -> tuple[str | None, float]:
    """Best-matching digit for a glyph bitmap, with its agreement score."""
    best, best_score = None, 0.0
    size = glyph.size
    for digit, template in DIGIT_TEMPLATES.items():
        score = float((glyph == template).sum()) / size
        if score > best_score:
            best, best_score = digit, score
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
                psm: int = 7, cycle: int | None = None) -> str:
    """Layered read: sprite matching first, Tesseract as fallback.

    `frame` lets a caller reuse a frame the scheduler already captured,
    which is what makes the fast path cost effectively nothing. The fallback
    keeps the same cycle so Tesseract is encoded from those same pixels.
    """
    if frame is None:
        try:
            frame = capture_array(wid, box, cycle=cycle)
        except CaptureError:
            return (ocr(wid, box, psm, cycle=cycle)
                    if cycle is not None else ocr(wid, box, psm))
    text = read_numeric(frame)
    if text is not None:
        return text
    return (ocr(wid, box, psm, cycle=cycle)
            if cycle is not None else ocr(wid, box, psm))


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
    subprocess.run(cmd, capture_output=True)
    play(sound)
    log_alert(rule_name or title, title, body, source_text)
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
    # presence
    colour_lo: tuple = (150, 80, 0)
    colour_hi: tuple = (255, 200, 90)
    present_above: int = 200
    absent_seconds: float = 12.0
    corroborate_region: str | None = None
    corroborate_pattern: str | None = None
    # stack
    stack_tolerance: int = 3
    new_slot_only: bool = False
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
    _primed: bool = field(default=False, repr=False)
    _history: list = field(default_factory=list, repr=False)
    _full_since: float = field(default=0.0, repr=False)
    _last_activity: float = field(default=0.0, repr=False)
    _last_activity_source: str | None = field(default=None, repr=False)
    _streak: int = field(default=0, repr=False)
    _level: str = field(default="", repr=False)
    _counts: dict = field(default_factory=dict, repr=False)
    _stacks: list = field(default_factory=list, repr=False)
    _slot_occupancy: list = field(default_factory=list, repr=False)
    _pending_slots: set = field(default_factory=set, repr=False)
    _pending_since: float = field(default=0.0, repr=False)
    _pending_base: list = field(default_factory=list, repr=False)
    _pending_occupancy_base: list = field(default_factory=list, repr=False)
    _elapsed: int = field(default=0, repr=False)
    _absent_since: float = field(default=0.0, repr=False)
    _corroborate_box: tuple | None = field(default=None, repr=False)
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
        self._slot_occupancy.clear()
        self._pending_slots = set()
        self._pending_occupancy_base.clear()


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
        log_occupancy(occ, skill=ACTIVE_SKILL)
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
    events = chat_events(rule.region, wid, box, cycle)
    if not rule._primed:
        # The first read is pre-existing scrollback. Record it in ChatReader,
        # but do not let an old catch fabricate a new activity timestamp.
        rule._primed = True
        return

    for event in events:
        line = event.text
        if rule.suppress_pattern and re.search(rule.suppress_pattern, line, re.I):
            # An explained stop. Disarm rather than touch the activity clock, so
            # resuming still needs a real catch to re-arm.
            rule._armed = False
            continue
        if re.search(rule.pattern, line, re.I):
            rule._last_activity = now
            rule._armed = True
            rule._last_activity_source = line

    # Never seen activity at all: nothing to report stopping.
    if rule._last_activity == 0.0:
        return

    quiet = now - rule._last_activity
    if rule._armed and quiet >= rule.stop_seconds and rule.ready(now):
        rule._armed = False
        return rule.fire(now, f"No matching activity for {quiet:.0f}s. "
                         f"Check the game.",
                         source_text=rule._last_activity_source, quiet=quiet)


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
    saw_trip = False
    saw_fail = False
    saw_out = False
    fail_line = None
    out_line = None
    for event in chat_events(rule.region, wid, box, cycle):
        line = event.text
        if rule.trip_pattern and re.search(rule.trip_pattern, line, re.I):
            saw_trip = True
        if rule.out_pattern and re.search(rule.out_pattern, line, re.I):
            saw_out = True
            out_line = line
        if re.search(rule.pattern, line, re.I):
            saw_fail = True
            fail_line = line

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
    frame = capture_array(wid, box, None, cycle)
    n = colour_pixels(frame, tuple(rule.colour_lo), tuple(rule.colour_hi))
    present = n >= rule.present_above

    if present:
        rule._absent_since = 0.0
        rule._last_activity = now
        rule._armed = True
        return None

    corroborates = bool(rule._corroborate_box and rule.corroborate_pattern)
    if rule._absent_since == 0.0:
        rule._absent_since = now
        rule._last_activity = now
        if corroborates:
            # Prime the shared reader without treating visible scrollback as
            # fresh evidence for this newly started blackout.
            chat_events(rule.corroborate_region, wid,
                        rule._corroborate_box, cycle)
        return None

    if corroborates:
        for event in chat_events(rule.corroborate_region, wid,
                                 rule._corroborate_box, cycle):
            line = event.text
            if re.search(rule.corroborate_pattern, line, re.I):
                # Fresh independent evidence that the activity is still alive.
                rule._last_activity = now
                rule._last_activity_source = line

    gone = now - rule._absent_since
    if not rule._armed or gone < rule.absent_seconds or not rule.ready(now):
        return None
    if corroborates and now - rule._last_activity < rule.absent_seconds:
        return None

    rule._armed = False
    return rule.fire(now, f"No activity icon for {gone:.0f}s - "
                          f"{rule.item} has stopped.", gone=gone)


TIMER_RE = re.compile(r"(\d{1,2}):([0-5]\d):([0-5]\d)")


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
    text = ocr_numeric(wid, box, capture_array(wid, box, None, cycle),
                       cycle=cycle)
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


def _eval_stack(rule: Rule, wid: str, region: "Region", box, now: float,
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
    frame = capture_array(wid, box, None, cycle)
    cols, rows = region.grid[4], region.grid[5]
    cur = [stack_signature(frame, region.grid, i) for i in range(cols * rows)]
    signatures = slot_signatures(
        frame, region.grid, threshold=rule.cell_threshold)
    cur_occ = [slot["occ"] for slot in signatures]
    occ = sum(cur_occ)

    # An interface drawn over the backpack makes covered cells read as
    # occupied; above capacity is the reliable tell, so drop the frame.
    if occ > rule.capacity:
        return None

    prev = rule._stacks
    prev_occ = rule._slot_occupancy
    rule._stacks = cur
    rule._slot_occupancy = cur_occ
    if (not prev or len(prev) != len(cur)
            or not prev_occ or len(prev_occ) != len(cur_occ)):
        # This frame is the startup baseline. The next sustained change is a
        # real post-start event and must not be swallowed as another "prime".
        rule._primed = True
        return None

    stack_changed = {
        i for i, (a, b) in enumerate(zip(prev, cur))
        if abs(b - a) > rule.stack_tolerance
    }
    occupancy_gained = {
        i for i, (a, b) in enumerate(zip(prev_occ, cur_occ))
        if not a and b
    }
    changed = stack_changed | occupancy_gained
    if changed:
        # Restart confirmation whenever the set of moving slots changes, so a
        # tooltip sweeping across cells cannot accumulate toward a report.
        if changed != rule._pending_slots:
            rule._pending_slots = set(changed)
            rule._pending_since = now
            rule._pending_base = list(prev)
            rule._pending_occupancy_base = list(prev_occ)
        return None

    if not rule._pending_slots:
        return None

    # The slots stopped moving. Confirmation measures how long the NEW value
    # has persisted since then, not how long it was still changing - a tooltip
    # reverts within a frame or two, while a real drop stays put.
    base = rule._pending_base or prev
    base_occ = rule._pending_occupancy_base or prev_occ
    slots = sorted(rule._pending_slots)
    if now - rule._pending_since < rule.confirm_seconds:
        return None
    rule._pending_slots = set()

    grew = {
        i for i in slots
        if i < len(cur) and cur[i] - base[i] > rule.stack_tolerance
    }
    newly_occupied = {
        i for i in slots
        if i < len(cur_occ) and not base_occ[i] and cur_occ[i]
    }
    if rule.new_slot_only:
        # "New slot" is an occupancy transition, not "stack digit count used
        # to be zero". That distinction catches quantity-1/unstackable drops
        # and rejects an existing stack changing from one item to two.
        gained = sorted(newly_occupied)
    else:
        gained = sorted(grew | newly_occupied)
    if not gained or not rule.ready(now):
        return None
    where = ", ".join(f"slot {i + 1}" for i in gained[:4])
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
    if not rule.item_pattern:
        return None
    for event in chat_events(rule.region, wid, box, cycle):
        line = event.text
        if rule.ignore_pattern and re.search(rule.ignore_pattern, line, re.I):
            continue
        m = re.search(rule.item_pattern, line, re.I)
        if not m:
            continue
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
    if not rule.pattern:
        return None
    gained = 0
    source_line = None
    for event in chat_events(rule.region, wid, box, cycle):
        line = event.text
        m = re.search(rule.pattern, line, re.I)
        if not m:
            continue
        if not rule._primed:
            continue
        try:
            gained += int(re.sub(r"[^0-9]", "", m.group(1)))
            source_line = line
        except (ValueError, IndexError):
            continue
    if not rule._primed:
        rule._primed = True
        return None
    if gained:
        rule._total += gained
        log_counter(rule.name, rule._total, skill=ACTIVE_SKILL)
    step = max(1, int(rule.step))
    reached = rule._total // step
    if reached > rule._milestone:
        rule._milestone = reached
        return rule.fire(now, rule.milestone_message.format(
            total=f"{rule._total:,}", n=reached, step=f"{step:,}"),
            source_text=source_line, total=f"{rule._total:,}", n=reached,
            step=f"{step:,}")
    return None


def evaluate(rule: Rule, wid: str, region: "Region", size, now: float,
             cycle: int = 0) -> Alert | None:
    box = region.resolve(size)
    if rule.kind == "presence":
        return _eval_presence(rule, wid, box, now, cycle)
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
        if not rule.pattern:
            return
        for event in chat_events(rule.region, wid, box, cycle):
            line = event.text
            if re.search(rule.pattern, line, re.I):
                # The chat tail is full of scrollback on startup. The reader
                # records it once and every rule sees the same event list, but
                # each rule still owns its own priming/cooldown semantics.
                if rule._primed and rule.ready(now):
                    return rule.fire(now, line[:120], source_text=line,
                                     line=line, text=line)
        rule._primed = True
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
            return rule.fire(now, f"nothing for {still:.0f}s - probably needs you",
                             still=still)


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
        for field_name, value in (
                ("dx", region.dx), ("dy", region.dy),
                ("w", region.w), ("h", region.h)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(
                    f"region {name!r} {field_name} must be an integer")
        if region.w <= 0 or region.h <= 0:
            raise ValueError(f"region {name!r} dimensions must be positive")
        if region.grid:
            x0, y0, cw, ch, cols, rows = region.grid
            if any(not isinstance(v, int) or isinstance(v, bool)
                   for v in region.grid):
                raise ValueError(f"region {name!r} grid values must be integers")
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
        for body_key in ("alert_body", "out_alert_body"):
            if body_key in rule and not isinstance(rule[body_key], str):
                raise ValueError(
                    f"rule {name!r}: {body_key} must be a string")
        unknown = sorted(
            key for key in rule
            if not key.startswith("_") and key not in Rule.__dataclass_fields__)
        if unknown:
            raise ValueError(
                f"rule {name!r} has unknown option(s): {', '.join(unknown)}")
        if kind in {"activity", "ocr", "supply", "counter"} and not rule.get("pattern"):
            raise ValueError(f"rule {name!r} requires pattern")
        if kind == "loot" and not rule.get("item_pattern"):
            raise ValueError(f"rule {name!r} requires item_pattern")
        if kind in {"inventory", "item_count", "stack"} and not regions[region].grid:
            raise ValueError(f"rule {name!r} requires a region grid")
        if kind == "inventory" and rule.get("mode", "lead") not in {"lead", "overflow"}:
            raise ValueError(
                f"rule {name!r}: mode must be 'lead' or 'overflow'")
        for key in ("enabled", "log_occupancy", "new_slot_only"):
            if key in rule and not isinstance(rule[key], bool):
                raise ValueError(f"rule {name!r}: {key} must be boolean")
        for key in ("message", "sound", "item", "out_message",
                    "milestone_message"):
            if key in rule and rule[key] is not None and not isinstance(rule[key], str):
                raise ValueError(f"rule {name!r}: {key} must be a string")
        if "urgency" in rule and rule["urgency"] not in {"low", "normal", "critical"}:
            raise ValueError(
                f"rule {name!r}: urgency must be low, normal, or critical")
        for key in ("capacity", "warn_free", "warn_streak", "timeout_ms",
                    "warn_below", "out_below"):
            if key in rule and (
                    not isinstance(rule[key], int) or isinstance(rule[key], bool)):
                raise ValueError(f"rule {name!r}: {key} must be an integer")
        if rule.get("capacity", 1) <= 0:
            raise ValueError(f"rule {name!r}: capacity must be positive")
        if rule.get("warn_streak", 1) <= 0:
            raise ValueError(f"rule {name!r}: warn_streak must be positive")
        if rule.get("timeout_ms", 0) < 0:
            raise ValueError(f"rule {name!r}: timeout_ms must be non-negative")
        for key in ("colour_lo", "colour_hi"):
            if key in rule:
                value = rule[key]
                if (not isinstance(value, (list, tuple)) or len(value) != 3
                        or any(not isinstance(v, (int, float)) or isinstance(v, bool)
                               or v < 0 or v > 255 for v in value)):
                    raise ValueError(
                        f"rule {name!r}: {key} must contain three values 0..255")
        corroborate_region = rule.get("corroborate_region")
        corroborate_pattern = rule.get("corroborate_pattern")
        if (corroborate_region is None) != (corroborate_pattern is None):
            raise ValueError(
                f"rule {name!r} requires corroborate_region and "
                "corroborate_pattern together")
        if corroborate_region is not None:
            if kind != "presence":
                raise ValueError(
                    f"rule {name!r}: corroboration is only valid for presence rules")
            if corroborate_region not in regions:
                raise ValueError(
                    f"rule {name!r} references unknown corroborate_region "
                    f"{corroborate_region!r}")
        for key in ("pattern", "suppress_pattern", "trip_pattern", "out_pattern",
                    "item_pattern", "ignore_pattern", "corroborate_pattern"):
            pattern = rule.get(key)
            if pattern is not None:
                try:
                    compiled = re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"rule {name!r} has invalid {key}: {exc}") from exc
                if kind == "counter" and key == "pattern" and compiled.groups < 1:
                    raise ValueError(
                        f"rule {name!r}: counter pattern needs a capture group")
        for key in ("cooldown", "idle_seconds", "threshold", "lead_seconds",
                    "overflow_seconds", "stop_seconds", "confirm_seconds",
                    "repeat_seconds", "absent_seconds", "present_above",
                    "stack_tolerance", "cell_threshold", "step"):
            value = rule.get(key)
            if value is not None and (
                    not isinstance(value, (int, float)) or isinstance(value, bool)
                    or value < 0):
                raise ValueError(f"rule {name!r}: {key} must be non-negative")
        if rule.get("step") is not None and rule["step"] <= 0:
            raise ValueError(f"rule {name!r}: step must be positive")


def resolve_window(cfg: dict) -> tuple[str, tuple[int, int]]:
    """Legacy xdotool resolver retained for compatibility tests."""
    wid = find_window(cfg["window"]["wm_class"])
    if not wid:
        sys.exit(f"window not found (class={cfg['window']['wm_class']!r}) - is the game running?")
    size = window_size(wid)
    if not size:
        sys.exit(f"could not determine geometry for window {wid}")
    return wid, size


def acquire_game(cfg: dict, requested_backend: str | None = None) -> GameInstance:
    """Acquire the configured game through the same backend used by `watch`."""
    try:
        backend = make_runtime_backend(requested_backend)
    except CaptureError as e:
        sys.exit(str(e))
    game = GameInstance(cfg["window"]["wm_class"], backend=backend)
    if not game.acquire():
        sys.exit(f"window not found (class={cfg['window']['wm_class']!r}) - "
                 "is the game running?")
    return game


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# interface readers
#
# Priority 0, step 5. A reader turns one region's pixels into normalized
# events that any number of rules can consume, instead of each rule owning
# its own OCR loop.
#
# The duplication this removes is concrete: five rule kinds (`ocr`,
# `activity`, `supply`, `loot`, `counter`) each carried their own copy of
# "OCR the region, split lines, normalize a dedup key, skip keys already
# seen, cap the seen-set". Each copy had drifted slightly, and a fix to one
# never reached the others.
# --------------------------------------------------------------------------

_STAMP_RE = re.compile(r"^(\d{6})(.*)$")


def _split_stamp(key: str) -> tuple[str, str]:
    """Split a normalized key into its leading HHMMSS stamp and the rest.

    `norm_line` strips punctuation, so `[16:10:26] You catch...` becomes
    `161026youcatch...`. Separating the stamp lets fuzzy matching compare
    wording without letting the clock dominate the similarity ratio.
    """
    m = _STAMP_RE.match(key)
    return (m.group(1), m.group(2)) if m else ("", key)


@dataclass(frozen=True)
class ChatLine:
    """One newly observed chat line.

    `key` is the normalized dedup key rather than the raw text, because
    tesseract is not deterministic on this font: the same line comes back as
    `[11:03:15]` on one pass and `(11:03:15]` on the next.
    """

    text: str
    key: str
    cycle: int


class InterfaceReader:
    """Base class: one region, one kind of meaning, normalized output."""

    kind = "abstract"

    def __init__(self, region: str):
        self.region = region

    def read(self, sched: "FrameScheduler") -> list:
        raise NotImplementedError


class ChatReader(InterfaceReader):
    """Emits chat lines that have not been seen before.

    The chat tail keeps old lines on screen for many polls, so the same line
    is re-read every cycle until it scrolls off. Deduplication is therefore
    the reader's core job, not an optimisation.

    Ownership matters here. When each rule kept its own seen-set, a line was
    consumed independently by every rule, which worked but meant N copies of
    the same 400-entry set and N chances for the capping logic to differ. One
    reader keeps one set and hands every rule the same event list.
    """

    kind = "chat"

    def __init__(self, region: str, min_key_len: int = 8,
                 max_seen: int = 400, similarity: float = 0.90):
        super().__init__(region)
        self.min_key_len = min_key_len
        self.max_seen = max_seen
        self.similarity = similarity
        self._seen: set[str] = set()
        self._recent: list[str] = []
        self.lines_read = 0
        self.events_emitted = 0
        self.variants_suppressed = 0
        self._cycle: int | None = None
        self._cycle_events: list[ChatLine] = []
        self._visible_unstamped: dict[str, int] = {}

    def _is_variant(self, key: str) -> bool:
        """Whether `key` is an OCR variant of a line already emitted.

        Exact-key dedup is not enough. Tesseract mis-reads this font
        differently on each pass, so one unchanging chat line yields a
        stream of distinct keys: measured on live chat, 67% of emitted
        events were >85% similar to a key already seen. Without this a rule
        can fire several times for one game event.

        Only the recent window is compared, because a line that has scrolled
        off and genuinely recurs should be reported again.
        """
        if self.similarity >= 1.0:
            return False
        stamp, body = _split_stamp(key)
        if not stamp:
            # Without a source timestamp there is no safe way to distinguish
            # "same event, OCR changed" from "same wording happened again".
            # Exact viewport overlap is handled separately; fuzzy suppression
            # is reserved for timestamped lines.
            return False
        for prev in self._recent:
            prev_stamp, prev_body = _split_stamp(prev)
            if not prev_stamp or stamp != prev_stamp:
                continue
            if abs(len(prev_body) - len(body)) > max(4, len(body) // 4):
                continue          # cheap length gate before the real compare
            if difflib.SequenceMatcher(None, body, prev_body).ratio() >= self.similarity:
                return True
        return False

    def consume(self, text: str, cycle: int) -> list[ChatLine]:
        """Normalize one OCR result into events shared by every rule.

        Re-reading in the same cycle returns the exact same event list. This is
        what lets several rules consume one authoritative dedup stream without
        the first rule accidentally stealing events from the others.
        """
        if self._cycle == cycle:
            return list(self._cycle_events)

        fresh: list[ChatLine] = []
        visible: set[str] = set()
        visible_unstamped: dict[str, int] = {}
        for raw in text.splitlines():
            line = raw.strip()
            key = norm_line(line)
            self.lines_read += 1
            if len(key) < self.min_key_len:
                continue
            stamp, _body = _split_stamp(key)
            if not stamp:
                occurrence = visible_unstamped.get(key, 0) + 1
                visible_unstamped[key] = occurrence
                if occurrence <= self._visible_unstamped.get(key, 0):
                    continue
                fresh.append(ChatLine(line, key, cycle))
                continue

            visible.add(key)
            if key in self._seen:
                continue
            self._seen.add(key)
            if self._is_variant(key):
                self.variants_suppressed += 1
                continue
            self._recent.append(key)
            if len(self._recent) > 60:
                del self._recent[:30]
            fresh.append(ChatLine(line, key, cycle))
        self._visible_unstamped = visible_unstamped
        if len(self._seen) > self.max_seen:
            # Keep the current timestamped viewport as the new baseline.
            self._seen = visible
            self._recent = [key for key in self._recent if key in visible][-60:]
        self.events_emitted += len(fresh)
        self._cycle = cycle
        self._cycle_events = list(fresh)
        return fresh

    def read(self, sched: "FrameScheduler", psm: int = 6) -> list[ChatLine]:
        """New lines visible this cycle, oldest first."""
        box = sched.box_for(self.region)
        text = ocr_cached(sched.game.handle, box, sched.cycle, psm)
        return self.consume(text, sched.cycle)

    def forget(self, key: str) -> None:
        """Allow a key to be emitted again; used by replay and tests."""
        self._seen.discard(key)


class ReaderRegistry:
    """Named readers for one profile, built once and shared by every rule.

    Keyed on `(kind, region)` so two rules watching the same chat region get
    the same reader - which is what makes one dedup set authoritative.
    """

    def __init__(self):
        self._readers: dict[tuple[str, str], InterfaceReader] = {}

    def chat(self, region: str) -> ChatReader:
        key = ("chat", region)
        if key not in self._readers:
            self._readers[key] = ChatReader(region)
        return self._readers[key]

    def get(self, kind: str, region: str) -> InterfaceReader | None:
        return self._readers.get((kind, region))

    def __len__(self) -> int:
        return len(self._readers)

    def stats(self) -> list[tuple[str, str, int, int]]:
        """(kind, region, lines_read, events_emitted) for diagnostics."""
        out = []
        for (kind, region), reader in sorted(self._readers.items()):
            out.append((kind, region,
                        getattr(reader, "lines_read", 0),
                        getattr(reader, "events_emitted", 0)))
        return out


ACTIVE_READERS = ReaderRegistry()


def set_active_readers(registry: ReaderRegistry | None = None) -> ReaderRegistry:
    """Replace the process-wide reader registry and return the new instance."""
    global ACTIVE_READERS
    ACTIVE_READERS = registry if registry is not None else ReaderRegistry()
    return ACTIVE_READERS


def chat_events(region: str, wid: str, box, cycle: int,
                psm: int = 6) -> list[ChatLine]:
    """Return one shared, deduplicated chat event stream for a region/cycle."""
    reader = ACTIVE_READERS.chat(region)
    text = ocr_cached(wid, box, cycle, psm)
    return reader.consume(text, cycle)


# --------------------------------------------------------------------------
# doctor
#
# Priority 0, step 4. Silent capture failure is otherwise indistinguishable
# from "nothing happened in game" - the watcher keeps polling, reports no
# alerts, and looks healthy. Every check below exists because some form of
# that confusion has already cost time during development.
# --------------------------------------------------------------------------

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Check:
    """One diagnostic result, in the shape the spec asks doctor to print."""

    area: str
    verdict: str
    detail: str
    remedy: str = ""


def _check_session() -> list[Check]:
    """X/Wayland session reachability."""
    out = []
    ensure_x_env()
    display = os.environ.get("DISPLAY")
    xauth = os.environ.get("XAUTHORITY", "")
    session = os.environ.get("XDG_SESSION_TYPE") or "unknown"
    wayland = os.environ.get("WAYLAND_DISPLAY")
    if display:
        out.append(Check("session", PASS,
                         f"DISPLAY={display} session={session}"
                         + (f" wayland={wayland}" if wayland else "")))
    else:
        out.append(Check("session", FAIL, "no DISPLAY",
                         "start from a desktop session, or ensure a "
                         "plasmashell/kwin process is running so the "
                         "X cookie can be recovered"))
    if xauth and not Path(xauth).exists():
        out.append(Check("xauthority", WARN,
                         f"XAUTHORITY={xauth} does not exist",
                         "the cookie rotates on login; it is re-read "
                         "automatically from the running session"))
    elif xauth:
        out.append(Check("xauthority", PASS, xauth))
    return out


def _check_tools() -> list[Check]:
    """External executables the current paths depend on."""
    out = []
    required = {
        "xdotool": "window discovery",
        "tesseract": "OCR rules",
        "notify-send": "desktop notifications",
    }
    optional = {
        "import": "ImageMagick fallback capture",
        "paplay": "per-rule alert sounds",
    }
    for tool, why in required.items():
        path = shutil.which(tool)
        out.append(Check(tool, PASS, path) if path else
                   Check(tool, FAIL, f"not found ({why} unavailable)",
                         f"install {tool}"))
    for tool, why in optional.items():
        path = shutil.which(tool)
        out.append(Check(tool, PASS, path) if path else
                   Check(tool, WARN, f"not found ({why} unavailable)",
                         f"install {tool} if you want {why}"))
    return out


def _check_backends(requested: str | None) -> tuple[list[Check], CaptureBackend]:
    """Which capture backends work, and which would actually be used."""
    out = []
    for name in sorted(BACKENDS):
        ok, why = BACKENDS[name]().available()
        out.append(Check(f"backend:{name}", PASS if ok else WARN, why,
                         "" if ok else "this backend will not be used"))
    selected = make_backend(requested)
    ok, why = selected.available()
    if ok:
        out.append(Check("backend", PASS, f"using {selected.name}"))
    else:
        out.append(Check("backend", FAIL, f"{selected.name}: {why}",
                         "no usable capture backend; install python-xcffib "
                         "or ImageMagick"))
    return out, selected


def _check_window(cfg: dict, game: GameInstance) -> list[Check]:
    """Game window discovery and geometry."""
    wm_class = cfg["window"]["wm_class"]
    if not game.acquire():
        return [Check("window", FAIL, f"no window for class {wm_class!r}",
                      "start RuneScape, or correct window.wm_class "
                      "in the profile")]
    w, h = game.size
    out = [Check("window", PASS, f"{game.handle} {w}x{h} class={wm_class}")]
    if w < 800 or h < 600:
        out.append(Check("geometry", WARN, f"window is small ({w}x{h})",
                         "regions were calibrated on a larger window and "
                         "may not resolve meaningfully"))
    return out


def _check_regions(cfg: dict, game: GameInstance) -> list[Check]:
    """Every configured region must resolve inside the window."""
    if not game.size:
        return [Check("regions", WARN, "skipped (no window)")]
    W, H = game.size
    out = []
    for name, region in cfg["_regions"].items():
        raw = region.unclamped(game.size)
        x, y, w, h = region.resolve(game.size)
        if w <= 0 or h <= 0:
            out.append(Check(f"region:{name}", FAIL,
                             f"degenerate box {(x, y, w, h)}",
                             "fix the region's w/h in the profile"))
        elif (x, y, w, h) != raw:
            out.append(Check(
                f"region:{name}", WARN,
                f"configured {raw} was clamped to {(x, y, w, h)} "
                f"in a {W}x{H} window",
                "re-run calibrate at this window size"))
        else:
            out.append(Check(f"region:{name}", PASS, f"{(x, y, w, h)}"))
    return out


def _check_grids(cfg: dict, game: GameInstance) -> list[Check]:
    """Inventory grid geometry must fit inside its own region."""
    out = []
    for name, region in cfg["_regions"].items():
        if not region.grid:
            continue
        x0, y0, cw, ch, cols, rows = region.grid
        span_w, span_h = x0 + cols * cw, y0 + rows * ch
        if span_w > region.w or span_h > region.h:
            out.append(Check(f"grid:{name}", FAIL,
                             f"grid spans {span_w}x{span_h} inside a "
                             f"{region.w}x{region.h} region",
                             "recalibrate the grid origin or cell size"))
        else:
            out.append(Check(f"grid:{name}", PASS,
                             f"{cols}x{rows} cells, {cw}x{ch} px"))
    return out


def _check_capture(cfg: dict, game: GameInstance) -> list[Check]:
    """Actually capture each region and judge the frames.

    A region that resolves in bounds can still come back blank, so this
    samples real pixels rather than trusting the geometry alone.
    """
    if not game.handle:
        return [Check("capture", WARN, "skipped (no window)")]
    sched = FrameScheduler(game, cfg["_regions"])
    out = []
    for _ in range(2):                    # two passes for capture/timing sanity
        sched.begin()
        sched.prefetch(list(cfg["_regions"]))
    for name in sorted(cfg["_regions"]):
        stat = sched.stats.get(name)
        if stat is None or stat.captures == 0:
            out.append(Check(f"capture:{name}", FAIL,
                             stat.last_error if stat else "not captured",
                             "check the region and backend"))
            continue
        try:
            frame = sched.frame(name)
        except CaptureError as e:
            out.append(Check(f"capture:{name}", FAIL, str(e)[:120]))
            continue
        spread = float(frame.std())
        if spread < 0.5:
            out.append(Check(f"capture:{name}", WARN,
                             f"frame is flat (std {spread:.2f}) - blank or "
                             f"occluded?",
                             "check the interface is open and visible"))
        else:
            out.append(Check(f"capture:{name}", PASS,
                             f"{stat.mean_ms:.1f} ms, std {spread:.1f}"))
    return out


def _check_ocr(cfg: dict, game: GameInstance) -> list[Check]:
    """Read a text region and judge whether OCR produced usable words.

    OCR that returns noise looks identical to a quiet chat log from the
    outside, which is exactly the failure this command exists to surface.
    """
    if not shutil.which("tesseract"):
        return [Check("ocr", FAIL, "tesseract not installed",
                      "install tesseract and English language data")]
    if not game.handle:
        return [Check("ocr", WARN, "skipped (no window)")]
    text_regions = sorted({r["region"] for r in cfg["rules"]
                           if r.get("enabled", True)
                           and r["kind"] in ("ocr", "activity", "supply",
                                             "loot", "counter", "timer")
                           and r["region"] in cfg["_regions"]})
    if not text_regions:
        return [Check("ocr", PASS, "no text rules in this profile")]
    out = []
    for name in text_regions:
        box = cfg["_regions"][name].resolve(game.size)
        try:
            # Exercise the backend selected by doctor, not the legacy
            # ImageMagick capture path. A clean XCB-only installation should
            # not fail diagnostics merely because the fallback is absent.
            text = ocr(game.handle, box, cycle=1, game=game)
        except Exception as e:
            out.append(Check(f"ocr:{name}", FAIL, f"{type(e).__name__}: {e}"))
            continue
        words = re.findall(r"[A-Za-z]{3,}", text)
        # Numeric readouts (timers, counters) are legitimately word-free, so
        # digit groups count as readable tokens too. Judging them by word
        # count alone reports a healthy timer as noise.
        numbers = re.findall(r"\d{2,}", text)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        tokens = len(words) + len(numbers)
        if not lines:
            out.append(Check(f"ocr:{name}", WARN, "no text read",
                             "expected for an empty chat; suspicious if the "
                             "region should contain text"))
        elif tokens < 2:
            out.append(Check(f"ocr:{name}", WARN,
                             f"{len(lines)} lines but only {tokens} "
                             f"readable token(s)",
                             "OCR may be reading noise; verify the region "
                             "with `shot`"))
        else:
            out.append(Check(f"ocr:{name}", PASS,
                             f"{len(lines)} lines, {len(words)} words, "
                             f"{len(numbers)} numbers"))
    return out


def _check_outputs() -> list[Check]:
    """Notification, sound, and state-directory writability."""
    out = []
    if shutil.which("notify-send"):
        out.append(Check("notifications", PASS, "notify-send present"))
    else:
        out.append(Check("notifications", FAIL, "notify-send missing",
                         "install libnotify"))
    if SOUND_DIR.exists():
        n = len(list(SOUND_DIR.glob("*.oga")))
        out.append(Check("sounds", PASS, f"{n} sounds in {SOUND_DIR}"))
    else:
        out.append(Check("sounds", WARN, f"{SOUND_DIR} not found",
                         "per-rule sounds will be skipped; alerts still "
                         "deliver"))
    try:
        STATE_DIR.mkdir(exist_ok=True)
        probe = STATE_DIR / ".doctor-write-test"
        probe.write_text("ok")
        probe.unlink()
        out.append(Check("state dir", PASS, f"{STATE_DIR} writable"))
    except OSError as e:
        out.append(Check("state dir", FAIL, f"{STATE_DIR}: {e}",
                         "alert and occupancy history cannot be recorded"))
    return out


def _check_profile(cfg: dict, path: Path) -> list[Check]:
    """Profile identity, rule coverage, and region references."""
    out = [Check("profile", PASS,
                 f"{path} skill={cfg.get('skill', 'unnamed')!r} "
                 f"type={cfg.get('profile_type', 'skill')}")]
    enabled = [r for r in cfg["rules"] if r.get("enabled", True)]
    disabled = len(cfg["rules"]) - len(enabled)
    if not enabled:
        out.append(Check("rules", FAIL, "no enabled rules",
                         "the watcher will refuse to start"))
    else:
        out.append(Check("rules", PASS,
                         f"{len(enabled)} enabled, {disabled} disabled"))
    referenced = {r["region"] for r in cfg["rules"]}
    unused = sorted(set(cfg["_regions"]) - referenced)
    if unused:
        out.append(Check("regions unused", WARN,
                         f"referenced by no rule: {', '.join(unused)}",
                         "remove obsolete regions or add the intended rule"))
    sounds = [r.get("sound") for r in enabled if r.get("sound")]
    if len(sounds) != len(set(sounds)):
        dupes = sorted({s for s in sounds if sounds.count(s) > 1})
        out.append(Check("sounds distinct", WARN,
                         f"shared by several rules: {', '.join(dupes)}",
                         "distinct sounds let you identify an alert "
                         "without looking at the screen"))
    return out


def run_doctor(cfg: dict, path: Path, requested_backend: str | None = None
               ) -> list[Check]:
    """Collect every diagnostic. Pure enough to test without a desktop."""
    checks: list[Check] = []
    checks += _check_session()
    checks += _check_tools()
    backend_checks, backend = _check_backends(requested_backend)
    checks += backend_checks
    checks += _check_profile(cfg, path)
    game = GameInstance(cfg["window"]["wm_class"], backend=backend)
    checks += _check_window(cfg, game)
    checks += _check_regions(cfg, game)
    checks += _check_grids(cfg, game)
    checks += _check_capture(cfg, game)
    checks += _check_ocr(cfg, game)
    checks += _check_outputs()
    return checks


def cmd_doctor(args) -> None:
    cfg = load_config(args.config)
    checks = run_doctor(cfg, args.config, args.backend)
    width = max(len(c.area) for c in checks)
    for c in checks:
        print(f"  {c.verdict:<4}  {c.area:<{width}}  {c.detail}")
        if c.remedy and c.verdict != PASS:
            print(f"  {'':<4}  {'':<{width}}  -> {c.remedy}")
    fails = sum(1 for c in checks if c.verdict == FAIL)
    warns = sum(1 for c in checks if c.verdict == WARN)
    passes = len(checks) - fails - warns
    print(f"\n{passes} pass, {warns} warn, {fails} fail")
    if fails:
        sys.exit(1)


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


def cmd_calibrate(args) -> None:
    cfg = load_config(args.config)
    game = acquire_game(cfg, getattr(args, "backend", None))
    wid, size = game.handle, game.size
    out = ROOT / "calibrate.png"
    game.save(None, out, resize=args.scale)
    with Image.open(out) as im:
        shot = im.size
    print(f"window {wid}  actual {size[0]}x{size[1]} backend={game.backend.name}")
    print(f"wrote {out}  ({shot[0]}x{shot[1]})")
    print(f"multiply coords read off that image by {size[0]/shot[0]:.4f}")


def cmd_shot(args) -> None:
    cfg = load_config(args.config)
    game = acquire_game(cfg, getattr(args, "backend", None))
    size = game.size
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
    game.save(box, out)
    print(f"wrote {out}  {label} anchor={reg.anchor} -> box={box} "
          f"(window {size[0]}x{size[1]}, backend={game.backend.name})")


def cmd_regions(args) -> None:
    cfg = load_config(args.config)
    game = acquire_game(cfg, getattr(args, "backend", None))
    wid, size = game.handle, game.size
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
    game = acquire_game(cfg, getattr(args, "backend", None))
    wid, size = game.handle, game.size
    regs = cfg["_regions"]
    masks = {r["region"]: r.get("mask") for r in cfg["rules"]}
    prev = {k: None for k in regs}
    print(f"window {wid} {size[0]}x{size[1]} backend={game.backend.name} - "
          f"{args.count} samples @ {args.interval}s")
    print("frame-to-frame mean abs diff; pick a threshold above the idle floor\n")
    print(f"{'t':>6}  " + "  ".join(f"{k:>14}" for k in regs))
    for i in range(args.count):
        game.begin_cycle(i + 1)
        row = []
        for k, r in regs.items():
            try:
                a = game.frame(r.resolve(size), masks.get(k))
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
    game = acquire_game(cfg, getattr(args, "backend", None))
    size = game.size
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
    cycle = 0
    while time.monotonic() - t0 < args.duration:
        cycle += 1
        game.begin_cycle(cycle)
        frame = game.frame(reg.resolve(size))
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
    cycles = load_cycles(cap, skill=cfg.get("skill"))
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

    game = acquire_game(cfg, getattr(args, "backend", None))
    backend = game.backend
    wid, size = game.handle, game.size
    set_active_game(game)
    set_active_readers(ReaderRegistry())

    regs = cfg["_regions"]
    rules = [Rule(**{k: v for k, v in r.items() if not k.startswith("_")})
             for r in cfg["rules"] if r.get("enabled", True)]
    if not rules:
        sys.exit("no enabled rules")
    for r in rules:
        if r.kind == "counter":
            r._total = load_counter(r.name, skill=ACTIVE_SKILL)
            r._milestone = int(r._total // max(1, int(r.step)))
            if r._total:
                print(f"  {r.name}: resuming from {r._total:,}", flush=True)

    interval = cfg.get("interval", 1.0)
    wm_class = cfg["window"]["wm_class"]
    print(f"Screen Watcher {__version__}")
    print(f"watching skill={ACTIVE_SKILL!r} profile={args.config} "
          f"{wid} ({wm_class}) {size[0]}x{size[1]} every {interval}s "
          f"backend={backend.name}")
    for r in rules:
        print(f"  {r.name:<18} {r.kind:<7} -> {r.region}")
    print("ctrl-c to stop", flush=True)

    misses = 0
    scheduler = FrameScheduler(game, regs)
    needed_regions = sorted({
        name
        for rule in rules
        for name in (rule.region, rule.corroborate_region)
        if name
    })
    next_poll = time.monotonic()
    while True:
        cycle = scheduler.begin()
        now = time.monotonic()
        cur = game.refresh_size()
        if cur and cur != size:
            print(f"window resized {size} -> {cur}, regions re-anchored", flush=True)
            size = cur
            # Geometry changes invalidate both pixel frames and the text-reader
            # baseline; rules are re-primed against the newly resolved regions.
            scheduler = FrameScheduler(game, regs)
            set_active_readers(ReaderRegistry())
            for r in rules:
                r.reset()
        failed_regions = set(scheduler.prefetch(needed_regions))
        if failed_regions:
            misses += 1
            details = []
            for name in sorted(failed_regions):
                stat = scheduler.stats.get(name)
                details.append(
                    f"{name}: {stat.last_error if stat else 'capture failed'}")
            print("capture miss: " + "; ".join(details), flush=True)
        else:
            misses = 0

        for rule in rules:
            dependencies = {rule.region}
            if rule.corroborate_region:
                dependencies.add(rule.corroborate_region)
            if dependencies & failed_regions:
                continue
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
            except (CaptureError, OCRReadError) as e:
                print(f"rule {rule.name!r} observation failed: {e}", flush=True)
            except Exception as e:
                # One malformed rule must not take the watcher down with it.
                print(f"rule {rule.name!r} failed: {type(e).__name__}: {e}",
                      flush=True)

        if misses >= 3:
            old = wid
            if game.acquire():
                wid, size, misses = game.handle, game.size, 0
                print(f"reacquired window {old} -> {wid}", flush=True)
                scheduler = FrameScheduler(game, regs)
                set_active_game(game)
                set_active_readers(ReaderRegistry())
                for r in rules:
                    r.reset()
            else:
                notify("Screen Watcher", "game window gone - stopping", "critical")
                sys.exit("window gone")

        next_poll += interval
        current = time.monotonic()
        if next_poll <= current:
            # Do not run back-to-back catch-up polls after a slow OCR cycle.
            missed = int((current - next_poll) // interval) + 1
            next_poll += missed * interval
        time.sleep(max(0.0, next_poll - time.monotonic()))


def main() -> None:
    p = argparse.ArgumentParser(prog="watcher", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
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
    ensure_x_env()
    STATE_DIR.mkdir(exist_ok=True)
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

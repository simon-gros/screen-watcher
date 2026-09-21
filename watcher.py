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
        if not handle:
            raise CaptureError("empty window id")
        x, y, w, h = (int(v) for v in box)
        if w <= 0 or h <= 0:
            raise CaptureError(f"degenerate capture box {box!r}")
        import xcffib.xproto as xproto
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
        """Write a capture to disk.

        Delegates to ImageMagick when a resize is requested, because that is
        a one-off calibration path where correctness matters more than the
        few milliseconds saved.
        """
        if resize:
            return capture(handle, box, out, resize)
        arr = self.grab_array(handle, box)
        Image.fromarray(arr.astype(np.uint8)).save(out)
        return out


#: Where the ScreenCast restore token is kept. It lets a later run reuse a
#: granted capture without showing the picker again, which is the difference
#: between a watcher that can restart unattended and one that needs a human.
PORTAL_TOKEN_FILE = STATE_DIR / "portal-token"


def _portal_screencast_available() -> bool:
    """Whether the ScreenCast portal answers on the session bus.

    The main loop is installed first, deliberately. python-dbus caches the
    session connection, so the FIRST SessionBus created in a process fixes
    whether asynchronous calls work for every later user of it. Probing
    without the loop attached poisoned the connection, and the portal then
    refused with "D-Bus connections must be attached to a main loop" -
    diagnosed only because the failure was reported rather than swallowed.
    """
    try:
        import dbus
        import dbus.mainloop.glib
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SessionBus()
        obj = bus.get_object("org.freedesktop.portal.Desktop",
                             "/org/freedesktop/portal/desktop")
        dbus.Interface(obj, "org.freedesktop.DBus.Properties").Get(
            "org.freedesktop.portal.ScreenCast", "version")
        return True
    except Exception:                                # noqa: BLE001
        return False


class WaylandPortalBackend(CaptureBackend):
    """Capture through the XDG ScreenCast portal and PipeWire.

    Native Wayland has no `XGetImage` equivalent - a client cannot capture a
    window it does not own - so the portal, which asks the user to grant a
    source, is the supported route.

    The shape mismatch is the interesting part. `CaptureBackend` asks for a
    rectangle on demand; PipeWire pushes a continuous stream. This holds the
    newest frame and crops from it, which is not a workaround but the
    cheaper design: measured at step 8, the portal delivered a full
    3840x2107 frame in 16.8 ms against 35.0 ms for XCB's six separate region
    requests. Area stops dominating once the compositor is compositing those
    pixels anyway.

    Consent cannot be bypassed, and that is the point - it is what makes
    arbitrary window capture safe on Wayland. A restore token is persisted
    so a later run can reuse the grant; the portal may decline to issue one,
    so its absence is normal rather than an error.
    """

    name = "wayland-portal"

    #: Reported as the handle. The portal identifies a stream, not a window.
    HANDLE = "portal:0"

    def __init__(self) -> None:
        self._session = None
        self._reader = None
        self._size: tuple[int, int] | None = None
        self._frame: np.ndarray | None = None
        self._decoration: int | None = None
        self._failed = ""

    def available(self) -> tuple[bool, str]:
        if self._failed:
            return False, self._failed
        if not _session_is_wayland():
            return False, "not a Wayland session"
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst            # noqa: F401
            import dbus                              # noqa: F401
        except (ImportError, ValueError) as e:
            return False, f"missing dependency: {e}"
        if not _portal_screencast_available():
            return False, "ScreenCast portal not on the session bus"
        return True, "ok"

    def _ensure_session(self) -> bool:
        """Open a portal session, reusing a stored grant when possible."""
        if self._reader is not None:
            return True
        ok, why = self.available()
        if not ok:
            self._failed = why
            return False
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            from tools.portal_poc import PipeWireReader, PortalSession
        except ImportError as e:
            self._failed = f"portal helper unavailable: {e}"
            return False

        token = None
        if PORTAL_TOKEN_FILE.exists():
            try:
                token = PORTAL_TOKEN_FILE.read_text().strip() or None
            except OSError:
                token = None

        session = PortalSession()
        try:
            session.create()
            session.select(restore_token=token)
            node = session.start()
        except Exception as e:                       # noqa: BLE001
            # A cancelled picker is a refusal, not a crash: report it once
            # so the caller can fall back to another backend.
            self._failed = f"portal session refused: {e}"
            return False

        if session.restore_token:
            self._store_token(session.restore_token)
        self._session = session
        self._reader = PipeWireReader(node)
        return True

    @staticmethod
    def _store_token(token: str) -> None:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            PORTAL_TOKEN_FILE.write_text(token)
            # The token grants screen capture until revoked, so keep it to
            # the owner rather than world-readable inside a repository.
            PORTAL_TOKEN_FILE.chmod(0o600)
        except OSError:
            pass

    def find(self, wm_class: str) -> str | None:
        return self.HANDLE if self._ensure_session() else None

    def size(self, handle: str) -> tuple[int, int] | None:
        if self._size is None:
            frame = self._latest()
            if frame is not None:
                self._size = (frame.shape[1], frame.shape[0])
        return self._size

    def _latest(self, timeout: float = 5.0) -> np.ndarray | None:
        """Newest frame, or the previous one when none has arrived.

        A momentary gap is not a capture failure - the compositor sends a
        frame when something changes - so the last frame is reused rather
        than raising. `RegionHealth` is what notices a genuinely frozen
        source.
        """
        if not self._ensure_session():
            return None
        frame = self._reader.frame(timeout=timeout)
        if frame is not None:
            self._frame = self._strip_decoration(frame)
        return self._frame

    def _strip_decoration(self, frame: np.ndarray) -> np.ndarray:
        """Drop the window titlebar so regions see the client area.

        The portal hands over the *framed* window; XCB hands over the client
        area. Measured here that is 49px of height and no width, which
        shifts every bottom-anchored region by a titlebar and makes an X11
        calibration unusable through the portal.

        The height is measured rather than hardcoded, because it depends on
        the window decoration theme. The titlebar is a flat dark band and
        the game is not, so the first row whose brightness jumps sharply is
        the boundary - observed as 35 to 101 at exactly y=49.

        Measured once and reused: the decoration does not change mid-session,
        and rescanning every frame would cost a pass per capture.
        """
        if self._decoration is None:
            self._decoration = self._measure_decoration(frame)
        return frame[self._decoration:] if self._decoration else frame

    @staticmethod
    def _measure_decoration(frame: np.ndarray, limit: int = 120,
                            jump: float = 40.0) -> int:
        """Rows of titlebar at the top of a framed capture, or 0 if none."""
        if frame.ndim != 3 or frame.shape[0] <= limit:
            return 0
        rows = frame[:limit].mean(axis=(1, 2))
        for y in range(1, limit):
            if rows[y] - rows[y - 1] > jump:
                return y
        return 0

    def grab_array(self, handle: str, box) -> np.ndarray:
        frame = self._latest()
        if frame is None:
            raise CaptureError("portal stream produced no frame")
        x, y, w, h = box
        crop = frame[y:y + h, x:x + w]
        if crop.size == 0:
            raise CaptureError(
                f"region {box} lies outside the {frame.shape[1]}x"
                f"{frame.shape[0]} portal frame")
        return crop

    def grab_file(self, handle: str, box, out: Path,
                  resize: str | None = None) -> Path:
        Image.fromarray(self.grab_array(handle, box)).save(out)
        return out

    def close(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            try:
                reader.close()
            except Exception:                        # noqa: BLE001
                pass


class ReplayBackend(CaptureBackend):
    """Replay recorded full-window frames instead of capturing live ones.

    The point is that everything above the backend - scheduler, readers,
    OCR, rules, events, notifications - runs unchanged. A detector bug that
    needs a rare chat line, a stun, or a full inventory can be reproduced
    from a recording rather than by playing until it happens again.

    Frames come from a directory of images, in sorted filename order, and
    are cropped in memory exactly as a real full-window frame would be.
    The window size is taken from the first frame, so recordings made at a
    different resolution still resolve their regions correctly.

    Set `SCREEN_WATCHER_REPLAY` to the directory, then select the backend:

        SCREEN_WATCHER_REPLAY=state/recording watcher.py doctor \\
            --backend replay
    """

    name = "replay"

    #: Replay has no window manager, so it reports a stable fake handle.
    HANDLE = "replay:0"

    def __init__(self, directory: str | os.PathLike | None = None):
        raw = str(directory or os.environ.get("SCREEN_WATCHER_REPLAY", ""))
        # Path("") is ".", a real directory, so an unset variable would
        # silently scan the working directory instead of reporting itself
        # unconfigured.
        self.directory = Path(raw) if raw else None
        self._frames: list[Path] = []
        self._pos = 0
        self._size: tuple[int, int] | None = None

    def available(self) -> tuple[bool, str]:
        if self.directory is None:
            return False, "set SCREEN_WATCHER_REPLAY to a frame directory"
        if not self.directory.is_dir():
            return False, f"{self.directory} is not a directory"
        if not self._list():
            return False, f"no frames in {self.directory}"
        return True, f"{len(self._list())} frames in {self.directory}"

    def _list(self) -> list[Path]:
        if not self._frames and self.directory and self.directory.is_dir():
            self._frames = sorted(
                p for p in self.directory.iterdir()
                if p.suffix.lower() in (".png", ".ppm", ".jpg", ".jpeg"))
        return self._frames

    def find(self, wm_class: str) -> str | None:
        return self.HANDLE if self._list() else None

    def size(self, handle: str) -> tuple[int, int] | None:
        if self._size is None:
            frames = self._list()
            if not frames:
                return None
            with Image.open(frames[0]) as im:
                self._size = im.size
        return self._size

    def _current(self) -> np.ndarray:
        frames = self._list()
        if not frames:
            raise CaptureError(f"no frames in {self.directory}")
        # Hold on the last frame rather than wrapping: looping would make a
        # recording of a one-off event replay it forever, which is exactly
        # the false positive a replay harness must not manufacture.
        path = frames[min(self._pos, len(frames) - 1)]
        try:
            with Image.open(path) as im:
                return np.asarray(im.convert("RGB"))
        except (OSError, ValueError) as e:
            raise CaptureError(f"replay frame {path.name}: {e}") from e

    def advance(self) -> bool:
        """Step to the next frame. False once the recording is exhausted."""
        if self._pos < len(self._list()) - 1:
            self._pos += 1
            return True
        return False

    def grab_array(self, handle: str, box) -> np.ndarray:
        x, y, w, h = box
        frame = self._current()
        crop = frame[y:y + h, x:x + w]
        if crop.size == 0:
            raise CaptureError(
                f"region {box} lies outside the {frame.shape[1]}x"
                f"{frame.shape[0]} replay frame")
        return crop

    def grab_file(self, handle: str, box, out: Path,
                  resize: str | None = None) -> Path:
        Image.fromarray(self.grab_array(handle, box)).save(out)
        return out


BACKENDS: dict[str, type[CaptureBackend]] = {
    X11ImageMagickBackend.name: X11ImageMagickBackend,
    X11XcbBackend.name: X11XcbBackend,
    ReplayBackend.name: ReplayBackend,
    WaylandPortalBackend.name: WaylandPortalBackend,
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
        if stat is None or stat.captures == 0:
            return "WARN", "never captured"
        if stat.failures and stat.captures == 0:
            return "FAIL", stat.last_error or "all captures failed"
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
    except OSError:
        raise CaptureError("import not installed (ImageMagick)")
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


#: Unix time for 2020-01-01. Any persisted timestamp below this came from
#: the monotonic clock, which counts seconds since boot and so lands in the
#: thousands rather than the billions.
_WALL_CLOCK_FLOOR = 1_577_836_800.0

OCCUPANCY_LOG = STATE_DIR / "occupancy.jsonl"
COUNTER_LOG = STATE_DIR / "counters.jsonl"


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
        STATE_DIR.mkdir(exist_ok=True)
        with COUNTER_LOG.open("a") as f:
            f.write(json.dumps({"t": round(time.time(), 1), "name": name,
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
        STATE_DIR.mkdir(exist_ok=True)
        with OCCUPANCY_LOG.open("a") as f:
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
    if not OCCUPANCY_LOG.exists():
        return []
    rows = []
    for line in OCCUPANCY_LOG.read_text().splitlines():
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
    if rule._armed and quiet >= rule.stop_seconds and rule.ready(now):
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
            red, _green, blue = patch[icon].mean(axis=0)
            if min_red > 0.0:
                matched = float(red) - float(blue) >= min_red
            else:
                matched = float(blue) - float(red) >= min_blue
            if matched:
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
    text = ocr_cached(wid, box, cycle)
    saw_trip = False
    saw_fail = False
    saw_out = False
    fail_line = None
    out_line = None
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
    frame = capture_array(wid, box, None, cycle)
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
            text = ocr_cached(wid, rule._corroborate_box, cycle)
            rule._seen = {
                key for line in text.splitlines()
                if len(key := norm_line(line.strip())) >= 8
            }
        return None

    if corroborates:
        text = ocr_cached(wid, rule._corroborate_box, cycle)
        visible = set()
        for line in text.splitlines():
            key = norm_line(line.strip())
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
    value = parse_percent(ocr_array(capture_array(wid, box, None, cycle)))
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
    frame = capture_array(wid, box, None, cycle)
    reading = parse_gauge(ocr_array(frame), rule.maximum)
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
    text = ocr_numeric(wid, box, capture_array(wid, box, None, cycle))
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
    occ, _cells = count_occupied(frame, region.grid, threshold=rule.cell_threshold)

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
    for line in join_wrapped_lines(ocr_cached(wid, box, cycle)):
        key = norm_line(line)
        if len(key) < 8 or key in rule._seen:
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
    text = ocr_array(capture_array(wid, box, None, cycle))
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
    rule._milestone = reached
    if not rule.ready(now):
        return None
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
    text = ocr_cached(wid, box, cycle)
    if not rule.pattern:
        return None
    gained = 0
    source_line = None
    visible = set()
    for line in text.splitlines():
        line = line.strip()
        key = norm_line(line)
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
        log_counter(rule.name, now, rule._total)
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
        text = ocr_cached(wid, box, cycle)
        if not rule.pattern:
            return
        for line in text.splitlines():
            line = line.strip()
            key = norm_line(line)
            if len(key) < 8 or key in rule._seen:
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


#: Profile schema this build understands. Bump the MAJOR part when a change
#: makes older profiles misbehave rather than merely lack a feature.
SCHEMA_VERSION = 1


def validate_schema_version(cfg: dict) -> None:
    """Reject a profile written for a schema this build cannot honour.

    A profile missing the field is accepted as version 1: every profile
    predates the field, and refusing them would break working setups to
    enforce bookkeeping.

    A *newer* major version is refused outright. Silently ignoring fields
    it does not understand is the failure mode worth preventing - a
    profile that relies on a detector this build lacks would run with that
    protection quietly absent.
    """
    raw = cfg.get("schema_version", SCHEMA_VERSION)
    if isinstance(raw, str):
        raw = raw.split(".")[0]
    try:
        version = int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"schema_version must be a number, got {cfg.get('schema_version')!r}")
    if version > SCHEMA_VERSION:
        raise ValueError(
            f"profile needs schema version {version}, this build supports "
            f"{SCHEMA_VERSION} - update Screen Watcher")
    if version < 1:
        raise ValueError(f"schema_version must be 1 or greater, got {version}")


def check_fingerprint(cfg: dict, size: tuple[int, int],
                      backend: str = "") -> list[str]:
    """Compare a profile's calibration assumptions against reality.

    Regions are pixel offsets measured on one window size. At any other
    size they resolve somewhere else entirely, and the failure is silent:
    OCR returns nothing or, worse, reads a neighbouring panel. This
    already happened during development - a gold row measured from a
    scaled screenshot landed 80px off and read as garbage for hours.

    Returns human-readable mismatches rather than raising, because a
    mismatch is a warning: the profile may still work, and refusing to
    start would be worse than reporting a risk.
    """
    fp = cfg.get("fingerprint")
    if not isinstance(fp, dict):
        return []
    problems = []

    expected = fp.get("window_size")
    if isinstance(expected, list) and len(expected) == 2:
        if tuple(expected) != tuple(size):
            problems.append(
                f"window is {size[0]}x{size[1]}, profile calibrated at "
                f"{expected[0]}x{expected[1]} - regions may resolve wrongly")

    expected_backend = fp.get("capture_backend")
    if expected_backend and backend and expected_backend != backend:
        problems.append(
            f"capture backend is {backend}, profile recorded "
            f"{expected_backend}")

    return problems


def validate_config(cfg: object) -> None:
    """Validate configuration before any window or capture side effects."""
    if not isinstance(cfg, dict):
        raise ValueError("root must be an object")
    validate_schema_version(cfg)
    fp = cfg.get("fingerprint")
    if fp is not None and not isinstance(fp, dict):
        raise ValueError("fingerprint must be an object")
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
        self.primed = False
        self.lines_read = 0
        self.events_emitted = 0
        self.variants_suppressed = 0

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
        for prev in self._recent:
            prev_stamp, prev_body = _split_stamp(prev)
            # A different in-game timestamp means a different event, however
            # similar the wording. Repeated catches a second apart differ
            # only by their stamp, and collapsing those would break every
            # rule that counts occurrences.
            if stamp and prev_stamp and stamp != prev_stamp:
                continue
            if abs(len(prev_body) - len(body)) > max(4, len(body) // 4):
                continue          # cheap length gate before the real compare
            if difflib.SequenceMatcher(None, body, prev_body).ratio() >= self.similarity:
                return True
        return False

    def read(self, sched: "FrameScheduler", psm: int = 6) -> list[ChatLine]:
        """New lines visible this cycle, oldest first."""
        box = sched.box_for(self.region)
        text = ocr_cached(sched.game.handle, box, sched.cycle, psm)
        fresh: list[ChatLine] = []
        for raw in text.splitlines():
            line = raw.strip()
            key = norm_line(line)
            self.lines_read += 1
            if len(key) < self.min_key_len or key in self._seen:
                continue
            self._seen.add(key)
            if self._is_variant(key):
                self.variants_suppressed += 1
                continue
            self._recent.append(key)
            if len(self._recent) > 60:
                del self._recent[:30]
            fresh.append(ChatLine(line, key, sched.cycle))
        if len(self._seen) > self.max_seen:
            # Dropping the whole set would let lines still on screen re-fire,
            # so callers re-prime rather than alert on the next pass.
            self._seen.clear()
            self._recent.clear()
            self.primed = False
        self.events_emitted += len(fresh)
        return fresh

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
    # XDG_SESSION_TYPE is empty outside the desktop session (a service, an
    # agent shell), so fall back to detecting the compositor directly rather
    # than reporting "unknown" for a perfectly identifiable session.
    session = (os.environ.get("XDG_SESSION_TYPE")
               or ("wayland" if _session_is_wayland() else "unknown"))
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


def _check_kwin(cfg: dict) -> list[Check]:
    """KWin read-only window discovery (Priority 0, step 7).

    Advisory, never fatal: capture still runs through XWayland, so a session
    without KWin scripting is fully supported. What KWin adds is state X11
    cannot express - an explicitly minimised window rather than an absent
    one, and real focus - which is why it is reported separately.
    """
    ok, why = kwin_available()
    if not ok:
        return [Check("kwin", WARN, why,
                      "optional; X11 discovery is used instead")]

    wm_class = cfg["window"]["wm_class"]
    try:
        windows = kwin_windows()
    except Exception as e:                       # noqa: BLE001 - diagnostic
        return [Check("kwin", WARN, f"{type(e).__name__}: {e}",
                      "optional; X11 discovery is used instead")]
    if not windows:
        return [Check("kwin", WARN, "script returned no windows",
                      "KWin scripting may be restricted on this session")]

    out = [Check("kwin", PASS, f"{len(windows)} windows via KWin scripting")]
    win = kwin_find(wm_class, windows)
    if win is None:
        out.append(Check("kwin:window", WARN,
                         f"no window for class {wm_class!r}",
                         "start RuneScape, or correct window.wm_class"))
        return out

    state = ("minimized" if win.minimized
             else "active" if win.active else "mapped")
    out.append(Check("kwin:window", PASS,
                     f"{win.caption or wm_class} {win.width}x{win.height} "
                     f"logical, {state}"))

    # Focus is an acceptance criterion in its own right, and X11 discovery
    # cannot answer it: a mapped window and a focused one look identical
    # through `xdotool search`.
    if win.minimized:
        out.append(Check("focus", WARN, "game window is minimised",
                         "capture will fail until it is restored"))
    else:
        out.append(Check("focus", PASS,
                         "game has focus" if win.active
                         else "game visible but not focused"))

    pixels = window_size(find_window(wm_class, visible_only=False) or "")
    scale = kwin_scale(win, pixels) if pixels else None
    if scale is not None:
        out.append(Check("kwin:scale", PASS,
                         f"{scale:.2f}x logical -> pixels "
                         f"({win.width} -> {pixels[0]})"))
    elif pixels:
        out.append(Check("kwin:scale", WARN,
                         f"logical {win.width} vs pixels {pixels[0]}",
                         "KWin and X11 may be describing different windows"))
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
        # An unconfigured replay backend is the normal state, not a fault:
        # it is opt-in for reproducing detector bugs. Warning about it on
        # every run trains people to ignore the warning column.
        if not ok and name == ReplayBackend.name and requested != name:
            out.append(Check(f"backend:{name}", PASS, "not configured "
                             "(opt-in; see SCREEN_WATCHER_REPLAY)"))
            continue
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
    backend = getattr(getattr(game, "backend", None), "name", "")
    for problem in check_fingerprint(cfg, (w, h), backend):
        out.append(Check("fingerprint match", WARN, problem,
                         "recalibrate the regions for this window, or "
                         "restore the recorded size"))
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
        x, y, w, h = region.resolve(game.size)
        if w <= 0 or h <= 0:
            out.append(Check(f"region:{name}", FAIL,
                             f"degenerate box {(x, y, w, h)}",
                             "fix the region's w/h in the profile"))
        elif (w, h) != (region.w, region.h):
            # `resolve` clamps to the window rather than returning an
            # out-of-bounds box, so a size mismatch is the only visible sign
            # that the region no longer fits where it was calibrated.
            out.append(Check(f"region:{name}", WARN,
                             f"clamped to {w}x{h} from configured "
                             f"{region.w}x{region.h} in a {W}x{H} window",
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
    for _ in range(2):                    # two passes to detect frozen frames
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
            text = ocr(game.handle, box)
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
                 f"type={cfg.get('profile_type', 'skill')} "
                 f"schema={cfg.get('schema_version', SCHEMA_VERSION)}")]
    fp = cfg.get("fingerprint")
    if not isinstance(fp, dict):
        out.append(Check("fingerprint", WARN, "profile records no calibration "
                         "assumptions",
                         "add a fingerprint so a resolution change is "
                         "reported rather than silently misreading regions"))
    else:
        recorded = fp.get("window_size")
        out.append(Check("fingerprint", PASS,
                         f"calibrated at {recorded[0]}x{recorded[1]}"
                         if isinstance(recorded, list) and len(recorded) == 2
                         else "recorded"))
    enabled = [r for r in cfg["rules"] if r.get("enabled", True)]
    disabled = len(cfg["rules"]) - len(enabled)
    if not enabled:
        out.append(Check("rules", FAIL, "no enabled rules",
                         "the watcher will refuse to start"))
    else:
        out.append(Check("rules", PASS,
                         f"{len(enabled)} enabled, {disabled} disabled"))
    # Name them. A disabled rule is silent by design, which is exactly what
    # makes an accidental one impossible to notice: the watcher runs, the
    # other alerts arrive, and the missing detector looks like an activity
    # that simply never happened.
    off = [r["name"] for r in cfg["rules"] if not r.get("enabled", True)]
    if off:
        out.append(Check("rules disabled", WARN, ", ".join(off),
                         "these detectors never fire; enable them in the "
                         "profile if that is not deliberate"))
    unused = sorted(set(cfg["_regions"])
                    - {r["region"] for r in enabled})
    if unused:
        out.append(Check("regions unused", WARN,
                         f"captured by no enabled rule: {', '.join(unused)}",
                         "harmless, but they cost nothing to remove"))
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
    checks += _check_kwin(cfg)
    game = GameInstance(cfg["window"]["wm_class"], backend=backend)
    # Bind it, or checks that call `ocr`/`capture_array` directly fall back
    # to ImageMagick against a handle the selected backend owns. The replay
    # backend exposed this: `doctor --backend replay` reported "import timed
    # out" because ImageMagick was being handed a fake window id.
    set_active_game(game)
    try:
        checks += _check_window(cfg, game)
        checks += _check_regions(cfg, game)
        checks += _check_grids(cfg, game)
        checks += _check_capture(cfg, game)
        checks += _check_ocr(cfg, game)
    finally:
        # Leave no global behind: `run_doctor` is called from tests, and a
        # stale instance would silently redirect their captures.
        set_active_game(None)
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


def next_deadline(previous: float, interval: float,
                  now: float | None = None) -> float:
    """The next poll deadline, skipping any that have already passed.

    Advancing by exactly one interval looks right and is wrong after a slow
    cycle. Measured with the loop's own arithmetic, a single 7s stall at a
    1.5s interval produced:

        cycle 2   work 7.00s   sleep 0.00s
        cycle 3   work 0.05s   sleep 0.00s   <- 0.05s after the previous
        cycle 4   work 0.05s   sleep 0.00s   <- and again
        cycle 5   work 0.05s   sleep 0.00s   <- and again

    Three cycles fired back-to-back while the schedule caught up. That
    bursts capture and OCR work, and any rule reasoning about elapsed time
    sees a near-zero gap that never happened on screen.

    Skipping missed deadlines keeps the cadence honest: a late cycle is
    simply late, and the next one lands on the following real boundary.
    """
    now = time.monotonic() if now is None else now
    deadline = previous + interval
    if deadline <= now:
        # Land on the next boundary strictly in the future, preserving the
        # original phase rather than restarting the clock from `now`.
        missed = int((now - deadline) // interval) + 1
        deadline += missed * interval
    return deadline


class RegionHealth:
    """Notices when a watched region stops carrying real content.

    `FrameScheduler` already tracks this, but only `doctor` builds one -
    the watch loop calls `evaluate` directly, so during an actual run
    nothing was watching for a frozen region. That is the case worth
    catching: a region that has silently stopped updating produces no
    alerts at all, which looks exactly like a quiet session.

    Two distinct failures, deliberately reported differently:

    - **frozen**: pixels identical for many consecutive cycles. Chat and
      the vitals row change constantly in play, so a long freeze means the
      client is paused, occluded, or the capture is stale.
    - **blank**: uniformly flat. Almost always a capture fault rather than
      real content.

    Thresholds are generous because a false "your watcher is broken" is
    worse than a slow one: at a 1s interval, 120 cycles is two minutes.
    """

    def __init__(self, freeze_cycles: int = 120, blank_cycles: int = 30):
        self.freeze_cycles = freeze_cycles
        self.blank_cycles = blank_cycles
        self._digest: dict[str, int] = {}
        self._static: dict[str, int] = {}
        self._flat: dict[str, int] = {}
        self._reported: set[str] = set()

    def note(self, name: str, frame: np.ndarray) -> str | None:
        """Record a frame. Returns a message the first time it looks wrong.

        Reports once per episode rather than every cycle: the point is to
        tell someone the watcher has gone blind, not to fill the log.
        """
        if frame is None or frame.size == 0:
            return None

        # A cheap digest; an exact comparison would cost a full copy.
        digest = hash(frame.tobytes())
        if self._digest.get(name) == digest:
            self._static[name] = self._static.get(name, 0) + 1
        else:
            self._static[name] = 0
        self._digest[name] = digest

        if float(frame.std()) < 0.5:
            self._flat[name] = self._flat.get(name, 0) + 1
        else:
            self._flat[name] = 0

        if self._flat[name] >= self.blank_cycles:
            return self._once(name, f"{name} has been blank for "
                                    f"{self._flat[name]} cycles - capture "
                                    f"may have failed")
        if self._static[name] >= self.freeze_cycles:
            return self._once(name, f"{name} unchanged for "
                                    f"{self._static[name]} cycles - client "
                                    f"paused, occluded, or capture stale")
        # Recovered: allow the next episode to be reported.
        if self._static[name] == 0 and self._flat[name] == 0:
            self._reported.discard(name)
        return None

    def _once(self, name: str, message: str) -> str | None:
        if name in self._reported:
            return None
        self._reported.add(name)
        return message


class WindowTracker:
    """Keeps a watch loop pointed at the live game window.

    Extracted from `cmd_watch` because the interesting behaviour only shows
    up in states that are awkward to produce by hand: a client that is
    minimised, moved to another virtual desktop, restarted under a new
    window id, or caught mid-resize between the geometry call and the
    capture. Inline, none of that could be tested.

    The rule it encodes: **a window that is merely unmapped is not a window
    that is gone.** Previously three consecutive capture failures ran
    `find_window`, which passes `--onlyvisible` and therefore reports
    nothing for a minimised client - so alt-tabbing away for three seconds
    made the watcher exit with "game window gone", permanently, mid-session.
    Distinguishing the two costs one extra xdotool call on the failure path
    and is the difference between pausing and dying.
    """

    #: Consecutive capture failures tolerated before reacquiring.
    MISS_LIMIT = 3
    #: Cycles to wait between reacquire attempts while hidden, and the cap.
    #: Backoff keeps a long alt-tab from spawning an xdotool call per second.
    BACKOFF_START = 1
    BACKOFF_MAX = 30

    def __init__(self, wm_class: str, wid: str, size: tuple[int, int],
                 game: "GameInstance | None" = None,
                 use_kwin: bool | None = None):
        self.wm_class = wm_class
        self.wid = wid
        self.size = size
        self.game = game
        # Resolved once: the availability probe costs a D-Bus round trip,
        # and the answer cannot change while the session is running.
        self.use_kwin = (kwin_available()[0] if use_kwin is None
                         else use_kwin)
        self.misses = 0
        self.hidden = False
        self._wait = 0
        self._backoff = self.BACKOFF_START

    # -- state transitions -------------------------------------------------

    def note_success(self) -> None:
        self.misses = 0

    def poll_size(self) -> tuple[int, int] | None:
        """Current geometry, or None when the window is not answering.

        Returns None rather than a stale value so callers never compare
        against a size that was never read; `size` itself is only ever
        replaced by a real measurement.
        """
        return window_size(self.wid)

    def note_resize(self, cur: tuple[int, int]) -> bool:
        """Adopt a new geometry. True when it actually changed."""
        if not cur or cur == self.size:
            return False
        self.size = cur
        if self.game is not None:
            self.game.refresh_size()
        return True

    def note_miss(self) -> bool:
        """Record a capture failure. True once reacquisition is due."""
        self.misses += 1
        return self.misses >= self.MISS_LIMIT

    # -- reacquisition -----------------------------------------------------

    def describe_hidden(self) -> str | None:
        """Why the window is hidden, when KWin can say.

        X11 conflates every unmapped state into one silence: minimised, on
        another virtual desktop, and shaded all look identical through
        `--onlyvisible`. KWin distinguishes them, which turns an opaque
        "window hidden, waiting" log line into an actionable one.

        Returns None when KWin is unavailable, so the caller keeps its
        existing wording rather than inventing a reason.
        """
        if not self.use_kwin:
            return None
        try:
            win = kwin_find(self.wm_class)
        except Exception:                        # noqa: BLE001 - diagnostic
            return None
        if win is None:
            return None
        if win.minimized:
            return "minimised"
        # Mapped as far as KWin is concerned, yet invisible to X11: the
        # usual cause is another virtual desktop.
        return "on another desktop"

    def reacquire(self) -> tuple[str, str | None]:
        """Try to re-point at the game.

        Returns `(status, detail)` where status is one of:

        - ``"ok"``     - pointing at a live, visible window;
        - ``"hidden"`` - the window exists but is unmapped; wait, do not exit;
        - ``"gone"``   - no window under this WM_CLASS at all; stop.
        """
        if self._wait > 0:
            self._wait -= 1
            return "hidden", None

        new = find_window(self.wm_class)
        if not new:
            # Not visible. Before declaring it gone, ask again including
            # unmapped windows - that is the minimised/other-desktop case.
            if find_window(self.wm_class, visible_only=False):
                self.hidden = True
                self._wait = self._backoff
                self._backoff = min(self._backoff * 2, self.BACKOFF_MAX)
                return "hidden", self.describe_hidden()
            return "gone", None

        size = window_size(new)
        if self.game is not None and self.game.acquire():
            new, size = self.game.handle, self.game.size
        if not size:
            # Found it, but it vanished again before geometry could be read.
            # Treat as still hidden rather than adopting size=None, which
            # would make every later resize check compare against nothing
            # and report a phantom resize on each cycle.
            self.hidden = True
            self._wait = self._backoff
            self._backoff = min(self._backoff * 2, self.BACKOFF_MAX)
            return "hidden", None

        was, self.wid, self.size = self.wid, new, size
        self.misses = 0
        self.hidden = False
        self._wait = 0
        self._backoff = self.BACKOFF_START
        return "ok", (was if was != new else None)


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
    # atexit does not run on SIGTERM - Python's default handler terminates
    # immediately - so `systemctl stop`, `kill`, and `pkill` all left the
    # pid file behind. Stale files are recovered from via /proc identity
    # checks, but raising SystemExit here means the normal path cleans up
    # and the recovery is a backstop rather than the usual case.
    # SIGINT is included deliberately. Catching KeyboardInterrupt in
    # __main__ looks like it covers Ctrl-C, but measured in isolation it
    # does not reliably run atexit when the signal arrives by `kill` rather
    # than from a terminal - the pid file survived. Handling the signal
    # explicitly makes cleanup the same code path in every case.
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _terminate)
        except (OSError, ValueError):
            # Not the main thread, or the signal is unavailable here.
            pass


def _terminate(signum, frame) -> None:
    """Exit cleanly on a termination signal, running atexit handlers."""
    _release_singleton()
    name = signal.Signals(signum).name if hasattr(signal, "Signals") else signum
    raise SystemExit(f"stopped ({name})")


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

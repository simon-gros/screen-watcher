"""Capture backends: how pixels are obtained from the game window.

Extracted from `watcher.py` as part of the modular split. The backends form
one inheritance hierarchy - X11 through ImageMagick or XCB, native Wayland
through the XDG ScreenCast portal, and recorded frames for replay - so they
move together; separating one would split the hierarchy across modules for
no benefit.

`capture_array` deliberately stays in `watcher.py`. Thirteen rule
evaluators call it and thirty-two tests patch it as `watcher.capture_array`;
moving it away from its callers would make those patches silently
ineffective.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

from screen_watcher.kwin import _session_is_wayland
from screen_watcher.windows import ensure_x_env, find_window, window_size

#: Overridden by `watcher` so both agree on where runtime state lives.
STATE_DIR = Path(__file__).resolve().parents[1] / "state"


class CaptureError(RuntimeError):
    """Raised when a capture cannot be completed.

    Defined here rather than imported: the backends raise it, and a module
    that raises an exception should own it. `watcher` re-exports this one
    so existing `except watcher.CaptureError` handlers keep working.
    """

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

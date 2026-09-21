"""Read-only KWin window discovery for KDE Wayland sessions.

Extracted from `watcher.py` as the first step of the modular split the
Priority 0 gate calls for. Chosen first because it is genuinely
self-contained: it needs only the standard library, and nothing else in
the application reaches into its internals.

Strictly read-only, per the architecture notes: discovery, identity,
geometry, focus, and health. Never input injection.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Where the temporary KWin script is written. Defined here rather than
#: imported from `watcher` to keep this module free of a circular import;
#: `watcher` overrides it when its own STATE_DIR differs.
STATE_DIR = Path(__file__).resolve().parents[1] / "state"

# --------------------------------------------------------------------------
# KWin read-only window discovery
#
# Priority 0, step 7. Native Wayland deliberately stops applications from
# enumerating each other's windows the X11 way, so `xdotool` sees only what
# XWayland exposes. KWin's scripting API is KDE's supported route to that
# metadata, and it answers questions X11 cannot: whether the game is on the
# current virtual desktop, whether it is genuinely active rather than merely
# mapped, and its identity across a restart.
#
# Strictly read-only, per the architecture notes: discovery, identity,
# geometry, focus, and health. Never input injection.
# --------------------------------------------------------------------------

#: Where the KWin script posts its findings back to.
KWIN_BUS_NAME = "org.screenwatcher.KWin"
KWIN_BUS_PATH = "/Discovery"

#: Loaded into KWin, runs once, reports every window, and is then unloaded.
#: `windowList` is Plasma 6; `clientList` is the Plasma 5 spelling.
_KWIN_SCRIPT = """
var out = [];
var wins = workspace.windowList ? workspace.windowList() : workspace.clientList();
for (var i = 0; i < wins.length; i++) {
    var w = wins[i];
    if (!w.resourceClass) continue;
    out.push([w.resourceClass, w.caption,
              Math.round(w.frameGeometry.x), Math.round(w.frameGeometry.y),
              Math.round(w.frameGeometry.width),
              Math.round(w.frameGeometry.height),
              w.minimized, w.active, w.onAllDesktops,
              w.internalId].join("\\t"));
}
callDBus("%(bus)s", "%(path)s", "%(bus)s", "Report", out.join("\\n"));
"""


@dataclass
class KWinWindow:
    """One window as KWin sees it.

    `geometry` is in KWin's *logical* coordinates, which on a scaled display
    are not the pixel coordinates XCB and ImageMagick use - measured 1.75x
    apart on this machine (2194 logical against 3840 physical). Capture code
    must never consume these numbers directly; `scale_to_pixels` converts.
    """

    wm_class: str
    caption: str
    x: int
    y: int
    width: int
    height: int
    minimized: bool
    active: bool
    on_all_desktops: bool
    internal_id: str

    @property
    def geometry(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)

    def scale_to_pixels(self, scale: float) -> tuple[int, int, int, int]:
        """Logical geometry converted to physical pixels."""
        return (round(self.x * scale), round(self.y * scale),
                round(self.width * scale), round(self.height * scale))


def kwin_available() -> tuple[bool, str]:
    """Whether KWin scripting discovery can run here."""
    if not _session_is_wayland():
        return False, "not a Wayland session"
    if not shutil.which("qdbus6") and not shutil.which("qdbus"):
        return False, "missing qdbus (install qt6-tools)"
    try:
        import dbus  # noqa: F401
    except ImportError:
        return False, "missing python-dbus"
    if _qdbus("org.kde.KWin", "/Scripting",
              "org.kde.kwin.Scripting.isScriptLoaded", "probe") is None:
        return False, "KWin scripting not reachable on the session bus"
    return True, "ok"


def _session_is_wayland() -> bool:
    """True when the desktop session is Wayland, not X11.

    `XDG_SESSION_TYPE` is empty for processes started outside the session
    (a service, an agent shell), which is exactly where this runs, so fall
    back to reading it out of the live compositor like `ensure_x_env` does.
    """
    if os.environ.get("XDG_SESSION_TYPE") == "wayland":
        return True
    try:
        r = subprocess.run(["pgrep", "-u", str(os.getuid()), "kwin_wayland"],
                           capture_output=True, text=True)
    except OSError:
        return False        # no pgrep: fall back to the X11 assumption
    return bool(r.stdout.split())


def _qdbus(*args: str) -> str | None:
    """One qdbus call, or None when it fails."""
    tool = shutil.which("qdbus6") or shutil.which("qdbus")
    if not tool:
        return None
    try:
        r = subprocess.run([tool, *args], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def kwin_windows(timeout: float = 5.0) -> list[KWinWindow]:
    """Every window KWin knows about, via a one-shot script.

    KWin scripts cannot return a value to their caller, so the script calls
    back into a temporary D-Bus service we own. The alternative - scraping
    `console.info` out of the journal - needs log access and races with log
    rotation, so it is not used.
    """
    import dbus
    import dbus.service
    import dbus.mainloop.glib
    from gi.repository import GLib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    received: list[str] = []

    class _Sink(dbus.service.Object):
        @dbus.service.method(KWIN_BUS_NAME, in_signature="s")
        def Report(self, payload):        # noqa: N802 - D-Bus method name
            received.append(str(payload))
            loop.quit()

    bus = dbus.SessionBus()
    name = dbus.service.BusName(KWIN_BUS_NAME, bus)
    sink = _Sink(bus, KWIN_BUS_PATH)
    loop = GLib.MainLoop()

    # A unique plugin name, so concurrent runs cannot unload each other's
    # script - KWin keys loaded scripts by that name alone.
    plugin = f"screenwatcher{os.getpid()}"
    body = _KWIN_SCRIPT % {"bus": KWIN_BUS_NAME, "path": KWIN_BUS_PATH}
    path = STATE_DIR / f"{plugin}.js"
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        loaded = _qdbus("org.kde.KWin", "/Scripting",
                        "org.kde.kwin.Scripting.loadScript", str(path), plugin)
        if loaded is None:
            return []
        _qdbus("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.start")
        GLib.timeout_add(int(timeout * 1000), lambda: (loop.quit(), False)[1])
        loop.run()
    finally:
        _qdbus("org.kde.KWin", "/Scripting",
               "org.kde.kwin.Scripting.unloadScript", plugin)
        path.unlink(missing_ok=True)
        sink.remove_from_connection()
        del name

    return _parse_kwin_report(received[0]) if received else []


def _parse_kwin_report(payload: str) -> list[KWinWindow]:
    """Parse the script's tab-separated report.

    Malformed rows are skipped rather than raising: this is diagnostic data
    from another process, and one odd window must not break discovery.
    """
    out = []
    for line in payload.splitlines():
        parts = line.split("\t")
        if len(parts) != 10:
            continue
        try:
            out.append(KWinWindow(
                wm_class=parts[0], caption=parts[1],
                x=int(float(parts[2])), y=int(float(parts[3])),
                width=int(float(parts[4])), height=int(float(parts[5])),
                minimized=parts[6] == "true", active=parts[7] == "true",
                on_all_desktops=parts[8] == "true", internal_id=parts[9]))
        except ValueError:
            continue
    return out


def kwin_scale(win: KWinWindow, pixels: tuple[int, int]) -> float | None:
    """Logical-to-physical scale factor, derived from a known pixel size.

    Measured from width alone. Height disagrees: KWin reports *frame*
    geometry including decoration, while the X11 client area excludes it -
    observed as 2107 against 2058 on a window whose width matched exactly.
    Width has no such decoration on a maximised game window, so it is the
    reliable axis.

    Returns None when the result is not close to a plausible scale, which
    means the two sources are describing different windows.
    """
    if not win.width or not pixels or not pixels[0]:
        return None
    scale = pixels[0] / win.width
    return scale if 0.5 <= scale <= 4.0 else None


def kwin_find(wm_class: str, windows=None) -> KWinWindow | None:
    """Largest window matching `wm_class`, as KWin sees it.

    Matches the `find_window` rule - largest wins - so the two discovery
    paths agree on which window is the game when a launcher shares its class.
    """
    wins = kwin_windows() if windows is None else windows
    matching = [w for w in wins if w.wm_class == wm_class]
    if not matching:
        return None
    return max(matching, key=lambda w: w.width * w.height)

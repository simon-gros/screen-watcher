"""Finding the game window and recovering the X session that owns it.

Extracted from `watcher.py` as part of the modular split. These are the
lowest layer the capture backends sit on: every backend needs to locate
the window before it can read pixels out of it.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


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
        try:
            r = subprocess.run(["pgrep", "-u", str(os.getuid()), proc],
                               capture_output=True, text=True)
        except OSError:
            return          # no pgrep: keep whatever the caller's env has
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
    """One xdotool call, or "" when it fails.

    An absent xdotool is treated exactly like a search that matched nothing.
    It is a required tool, so the honest report belongs to `doctor`, which
    names it directly; letting the OSError escape from here instead killed
    `doctor` before it could print that line - the one command whose job is
    to say which tools are missing.
    """
    try:
        r = subprocess.run(["xdotool", *args], capture_output=True, text=True)
    except OSError:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def find_window(wm_class: str, min_area: int = 100_000,
                visible_only: bool = True) -> str | None:
    """Largest window matching WM_CLASS.

    WM_CLASS beats title matching: it survives title changes and skips
    launcher windows that share the game's name.

    `visible_only=False` also returns unmapped windows. A minimised client,
    or one on another virtual desktop, is invisible to `--onlyvisible` but
    is very much still running - and the two cases need opposite responses.
    Losing the window means stop; losing focus means wait.
    """
    args = ["search", "--class", wm_class]
    if visible_only:
        args.insert(1, "--onlyvisible")
    best, best_area = None, 0
    for wid in _xdo(*args).splitlines():
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

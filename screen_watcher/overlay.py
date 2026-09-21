"""On-screen alert delivery through the click-through Wayland overlay.

Extracted from `watcher.py` as part of the modular split. The overlay runs
as a separate process - it needs a Qt event loop and a layer-shell
surface, neither of which belongs inside a polling loop, and a crash in
the GUI must not take the watcher down with it.

Every failure here is swallowed deliberately. The overlay is the fifth
delivery channel after the desktop banner, the sound, the alert log and
the console line; losing it degrades an alert, while raising would lose
the alert itself - mid-session, hours after the last green start.
"""

from __future__ import annotations

import atexit
import json
import subprocess
import sys
from pathlib import Path

from screen_watcher.kwin import _session_is_wayland


class OverlayChannel:
    """Delivers alerts to the click-through Wayland overlay, if running.

    The overlay stays a separate process for good reasons: it needs a Qt
    event loop and a layer-shell surface, neither of which belongs inside a
    polling loop, and a crash in the GUI cannot then take the watcher down.

    Every failure here is swallowed deliberately. The overlay is the fifth
    delivery channel after the banner, the sound, the alert log and the
    console line; losing it degrades an alert, while raising would lose the
    alert itself - mid-session, hours after the last green start.
    """

    # parents[1] because this module now lives one level down, in
    # screen_watcher/. Computing it from __file__ silently pointed at
    # screen_watcher/tools/ the moment the class was moved.
    SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "overlay.py"

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._failed = False

    def available(self) -> tuple[bool, str]:
        if not self.SCRIPT.exists():
            return False, f"{self.SCRIPT.name} not found"
        try:
            import PySide6                                  # noqa: F401
        except ImportError:
            return False, "PySide6 not installed"
        if not _session_is_wayland():
            return False, "overlay needs a Wayland session"
        return True, "ok"

    def start(self) -> bool:
        ok, _why = self.available()
        if not ok or self._failed:
            return False
        try:
            self._proc = subprocess.Popen(
                [sys.executable, str(self.SCRIPT)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, text=True)
        except OSError:
            self._failed = True
            return False
        atexit.register(self.stop)
        return True

    def send(self, rule: str, body: str, urgency: str = "normal") -> None:
        """Push one alert. Does nothing when the overlay is absent."""
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            return
        tone = "critical" if urgency == "critical" else "normal"
        try:
            proc.stdin.write(json.dumps(
                {"rule": rule, "body": body, "tone": tone}) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            # The overlay died or its pipe closed. Stop trying rather than
            # raising on every subsequent alert.
            self._proc = None

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass

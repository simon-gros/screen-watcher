#!/usr/bin/env python
"""Click-through KDE/Wayland overlay prototype.

Priority 0, step 9. A compositor-level overlay, never an injection into
RuneScape's own GL/Vulkan context: Screen Watcher stays an observer.

The mechanism is `wl-layer-shell` through KDE's `layer-shell-qt`, which the
roadmap's reference projects (poe2-overlay, PathofTrading) identify as the
one approach that works on native KWin Wayland where X11 and Electron
overlay shims fail.

There is no Python binding for LayerShellQt, but the package ships a QML
module (`org.kde.layershell`), so the whole surface is configured from QML
and driven from Python - no C++ build step.

Run it standalone with a demo alert:

    python tools/overlay.py --demo

Or feed it JSON alerts on stdin, one per line, which is how a watcher would
drive it:

    {"rule": "pack_nearly_full", "body": "5 slots left", "tone": "critical"}

Read-only and click-through: it never accepts keyboard focus and its input
region is empty, so clicks pass through to the game underneath.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

QML = Path(__file__).with_name("overlay.qml")


def ensure_wayland_env() -> bool:
    """Recover WAYLAND_DISPLAY/XDG_RUNTIME_DIR from the running session.

    Same problem `ensure_x_env` solves for X11: a process started outside the
    desktop session (a service, an agent shell) inherits neither variable, and
    Qt then aborts - it does not fall back, it dumps core. Reading them out of
    a live session process is the only reliable source.
    """
    if os.environ.get("WAYLAND_DISPLAY") and os.environ.get("XDG_RUNTIME_DIR"):
        return True
    for proc in ("plasmashell", "kwin_wayland"):
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
                ("WAYLAND_DISPLAY", "XDG_RUNTIME_DIR"))
            if found.get("WAYLAND_DISPLAY"):
                os.environ.update(found)
                return True
    return False


def make_click_through(window) -> None:
    """Empty input region: pointer events fall through to the game.

    `Qt.WindowTransparentForInput` alone is an X11-era hint that the Wayland
    backend does not honour on its own. Setting an empty mask is what actually
    produces an empty `wl_surface` input region.
    """
    from PySide6.QtGui import QRegion
    window.setMask(QRegion())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--demo", action="store_true",
                    help="show sample alerts instead of reading stdin")
    ap.add_argument("--compact", action="store_true",
                    help="titles only, no bodies")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="exit after N seconds (0 = run until closed)")
    args = ap.parse_args()

    if not ensure_wayland_env():
        print("no Wayland session found (this overlay is Wayland-only)",
              file=sys.stderr)
        return 1
    os.environ.setdefault("QT_QPA_PLATFORM", "wayland")

    from PySide6.QtCore import QTimer, QObject, Signal
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine

    app = QGuiApplication(sys.argv)
    engine = QQmlApplicationEngine()
    engine.load(str(QML))
    roots = engine.rootObjects()
    if not roots:
        print(f"failed to load {QML}", file=sys.stderr)
        return 1

    window = roots[0]
    window.setProperty("detailed", not args.compact)
    make_click_through(window)

    def push(rule: str, body: str, tone: str = "normal") -> None:
        window.pushAlert(rule, body, tone)

    if args.demo:
        samples = [
            ("pack_nearly_full", "5 slots left. Bank soon.", "critical"),
            ("coin_milestone", "450,000 coins this session.", "good"),
            ("level_up", "Advanced to Thieving level 71.", "good"),
        ]
        for i, (rule, body, tone) in enumerate(samples):
            QTimer.singleShot(400 + i * 900,
                              lambda r=rule, b=body, t=tone: push(r, b, t))
    else:
        # stdin is read on a worker thread and marshalled onto the Qt thread;
        # touching QML objects from another thread is undefined behaviour.
        class Bridge(QObject):
            arrived = Signal(str, str, str)

        bridge = Bridge()
        bridge.arrived.connect(push)

        def reader() -> None:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                bridge.arrived.emit(str(msg.get("rule", "alert")),
                                    str(msg.get("body", "")),
                                    str(msg.get("tone", "normal")))

        threading.Thread(target=reader, daemon=True).start()

    if args.seconds:
        QTimer.singleShot(int(args.seconds * 1000), app.quit)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

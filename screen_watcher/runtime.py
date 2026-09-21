"""Watch-loop runtime: poll scheduling, window lifecycle, frame health.

Extracted from `watcher.py` as part of the modular split. These are the
pieces that keep a long run honest rather than the pieces that detect
anything: when to poll next, whether the window is still there, and
whether the pixels are still changing.

`find_window`, `window_size` and `kwin_find` are resolved through a late
import of `watcher` rather than imported directly. Six tests patch
`watcher.find_window` to drive WindowTracker through states that are
awkward to produce by hand - minimised, on another desktop, restarted
under a new id - and binding the names here would make those patches
reach nothing.
"""

from __future__ import annotations

import atexit
import os
import signal
import sys
import time

import numpy as np

from screen_watcher.capture import GameInstance   # noqa: F401


def _w():
    """The `watcher` module, imported late."""
    import watcher
    return watcher


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
    `_w().find_window`, which passes `--onlyvisible` and therefore reports
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
        self.use_kwin = (_w().kwin_available()[0] if use_kwin is None
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
        return _w().window_size(self.wid)

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
            win = _w().kwin_find(self.wm_class)
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

        new = _w().find_window(self.wm_class)
        if not new:
            # Not visible. Before declaring it gone, ask again including
            # unmapped windows - that is the minimised/other-desktop case.
            if _w().find_window(self.wm_class, visible_only=False):
                self.hidden = True
                self._wait = self._backoff
                self._backoff = min(self._backoff * 2, self.BACKOFF_MAX)
                return "hidden", self.describe_hidden()
            return "gone", None

        size = _w().window_size(new)
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


#: Defined in `watcher` so a test redirecting `watcher.PID_FILE` reaches
#: the singleton helpers here. Binding it at import time would freeze the
#: repository path before any redirect could apply.


def claim_singleton() -> None:
    """Refuse to start if another watcher is already running.

    Two instances double every alert, which is indistinguishable from a rule
    being mistuned - and sends you tuning thresholds that were never the
    problem. A stale pid file (crash, SIGKILL) is reclaimed rather than
    treated as fatal, so a hard kill cannot lock the watcher out.
    """
    _w().STATE_DIR.mkdir(exist_ok=True)
    if _w().PID_FILE.exists():
        try:
            old = int(_w().PID_FILE.read_text().strip())
        except (ValueError, OSError):
            old = None
        if old and old != os.getpid():
            if _w()._process_identity(old) is not None:
                sys.exit(f"watcher already running (pid {old}) - "
                         f"stop it first, or delete {_w().PID_FILE}")
    _w().PID_FILE.write_text(str(os.getpid()))
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
        if _w().PID_FILE.exists() and _w().PID_FILE.read_text().strip() == str(os.getpid()):
            _w().PID_FILE.unlink()
    except OSError:
        pass

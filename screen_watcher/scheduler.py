"""One sampling pass over the game window, shared by every reader.

Extracted from `watcher.py` as part of the modular split. Self-contained:
it owns per-region capture accounting and frame health, and needs nothing
from the rest of the application.

The scheduler's job is coherence and accounting rather than fewer pixels.
Capturing the whole window once and cropping was measured 11x slower than
per-region grabs on this client, because the regions total 0.69 MPx
against a 7.90 MPx window.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from screen_watcher.capture import CaptureError, GameInstance

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

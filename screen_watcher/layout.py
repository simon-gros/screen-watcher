"""Find where the game's interface panels actually are.

Regions in a profile are fixed pixel offsets, which assumes the player
never rearranges their interface. The RuneScape wiki is explicit that
they can: "every window on the screen may be moved and resized, and
almost all of them can be removed."

When that assumption breaks the failure is silent and confident rather
than loud. Observed live: with the Skills panel docked beside the
Backpack, the `backpack` region read SKILLS and `count_occupied`
returned 29/30 from skill-level digits while the pack held four items.
`doctor` reported 0 fail throughout.

This module locates panels from the screen itself, so one profile works
across different layouts and across a layout changed mid-session.

Two detectors, because no single signal is reliable:

- **Title bar.** RS3 draws one above most movable windows, so OCR of a
  narrow strip identifies the panel by name. Cheap and exact when
  present.
- **Content signature.** Title bars can be switched off - the wiki lists
  "Hide title bars when locked" as a setting, and this player's chat
  panel has none. Chat is instead identified by its timestamps, which no
  other panel produces.

Scanning is deliberately not free, so it runs at startup and on demand
rather than every cycle, and the result is cached to disk keyed by
window size.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np


def _w():
    """The `watcher` module, imported late (circular at module scope)."""
    import watcher
    return watcher


#: Panel titles RS3 draws in a window's title bar. Searching for a known
#: set rather than "any text" keeps a scene full of signposts and NPC
#: names from being mistaken for interface chrome.
KNOWN_TITLES = (
    "METRICS", "MINIMAP", "BACKPACK", "SKILLS", "EQUIPMENT",
    "PRAYER", "MAGIC", "NOTES", "FRIENDS", "QUEST", "ALL CHAT",
)

#: A chat line always carries a timestamp. Nothing else on screen does,
#: which makes this a reliable content signature for a panel whose title
#: bar may be hidden.
_TIMESTAMP = re.compile(r"\d\d[:;.]\d\d[:;.]\d\d")


@dataclass
class PanelBox:
    """Where a panel was found, in window pixels."""

    name: str
    x: int
    y: int
    w: int
    h: int

    def as_region(self, win: tuple[int, int]) -> dict:
        """A profile region spec anchored to the nearest window corner.

        Anchoring rather than storing absolutes is what lets the same
        detection survive a window resize, which is the behaviour the
        rest of the profile format already relies on.
        """
        width, height = win
        left = self.x < width - (self.x + self.w)
        top = self.y < height - (self.y + self.h)
        anchor = f"{'top' if top else 'bottom'}-{'left' if left else 'right'}"
        return {
            "anchor": anchor,
            "dx": self.x if left else self.x + self.w - width,
            "dy": self.y if top else self.y + self.h - height,
            "w": self.w,
            "h": self.h,
        }


def _bands(frame: np.ndarray, x0: int, width: int, step: int,
           height: int) -> tuple[list, list]:
    """Horizontal strips down a column, with their y offsets."""
    strips, ys = [], []
    limit = frame.shape[0] - height
    for y in range(0, max(1, limit), step):
        strips.append(frame[y:y + height, x0:x0 + width])
        ys.append(y)
    return strips, ys


def find_titles(frame: np.ndarray, step: int = 24) -> dict[str, tuple[int, int]]:
    """Locate title bars by OCR'ing strips down the left and right edges.

    Panels dock to the window edges, so scanning two columns rather than
    the whole screen roughly halves the work. Batched through `ocr_many`
    because each separate tesseract call pays a ~72 ms startup floor -
    measured, batching 34 strips took 1.0 s against 12.4 s unbatched.
    """
    height, width = frame.shape[:2]
    columns = ((0, min(760, width)), (max(0, width - 820), min(820, width)))
    strips, coords = [], []
    for x0, band_w in columns:
        band, ys = _bands(frame, x0, band_w, step, 40)
        strips += band
        coords += [(x0, y) for y in ys]
    if not strips:
        return {}
    texts = _w().ocr_many(strips)
    found: dict[str, tuple[int, int]] = {}
    rows: dict[str, tuple[int, int, int]] = {}
    for (x0, y), text in zip(coords, texts):
        upper = text.upper()
        for title in KNOWN_TITLES:
            if title in upper and title not in rows:
                rows[title] = (x0, y, band_width(columns, x0))
    # Two panels can share a strip - Backpack and Skills dock side by
    # side, and a single wide OCR pass reports the same x for both,
    # which is exactly the confusion this module exists to remove. Narrow
    # down to the column each title really occupies.
    for title, (x0, y, band_w) in rows.items():
        found[title] = (_locate_x(frame, title, x0, band_w, y), y)
    return found


def band_width(columns, x0: int) -> int:
    for start, width in columns:
        if start == x0:
            return width
    return 0


def _locate_x(frame: np.ndarray, title: str, x0: int, band_w: int,
              y: int) -> int:
    """Which sub-column of a strip actually contains `title`.

    Bisecting the strip is cheaper than scanning it: two extra OCR
    passes place a title within a quarter of the band, which is enough
    to tell neighbouring panels apart.
    """
    left, width = x0, band_w
    for _ in range(2):
        half = max(40, width // 2)
        first = frame[y:y + 40, left:left + half]
        text = _w().ocr_array(first).upper()
        if title in text:
            width = half
        else:
            left, width = left + half, width - half
        if width <= 40:
            break
    return left


def find_chat(frame: np.ndarray, step: int = 60) -> PanelBox | None:
    """Locate the chat panel by its timestamps rather than its title.

    This player's chat panel has no title bar at all - the wiki lists
    "Hide title bars when locked" as a setting - so a header-only
    detector finds nothing. Every chat line carries a timestamp, and no
    other panel does, which makes the content the stronger signal.

    Returns the full vertical span of consecutive timestamped bands, so
    a resized chat window is followed rather than assumed.
    """
    height, width = frame.shape[:2]
    band_w = min(700, width)
    strips, ys = _bands(frame, 0, band_w, step, step)
    if not strips:
        return None
    texts = _w().ocr_many(strips, sauvola=True)
    rows = [y for y, text in zip(ys, texts) if _TIMESTAMP.search(text)]
    if not rows:
        return None
    top, bottom = min(rows), max(rows) + step
    return PanelBox("chat", 0, top, band_w, min(bottom, height) - top)


def scan(frame: np.ndarray) -> dict:
    """One full layout scan of a captured window.

    PRECISION: positions are accurate to about one scan step, so ~24px
    vertically. Measured against an interface-scaling change, which
    moves and resizes every panel while the window stays the same size:
    the Backpack header was reported at y=1584 against a true 1560.

    That is fine for the job this does - deciding whether the layout
    CHANGED - and not fine for calibrating a region, where 24px is
    enough to clip a row of chat or straddle two panels. Use `shot` to
    measure a region; use this to learn that measuring is needed.

    Interface scaling is the case that motivates scanning at all. The
    window stays 3840x2058 and KWin still reports ui_scale 1.75, so the
    profile fingerprint sees nothing wrong, while every panel has moved
    and shrunk. Nothing else in the application can notice that.
    """
    height, width = frame.shape[:2]
    titles = find_titles(frame)
    panels = {name: {"x": x, "y": y} for name, (x, y) in titles.items()}
    chat = find_chat(frame)
    return {
        "window": [width, height],
        "scanned_at": round(time.time(), 1),
        "titles": panels,
        "chat": asdict(chat) if chat else None,
    }


def fingerprint(layout: dict) -> str:
    """A short stable key for "is this the same layout as last time".

    Only the panel positions matter, not when they were scanned, so the
    timestamp is excluded - otherwise every scan would look like a
    change.
    """
    titles = layout.get("titles") or {}
    parts = [f"{k}@{v['x']},{v['y']}" for k, v in sorted(titles.items())]
    chat = layout.get("chat")
    if chat:
        parts.append(f"chat@{chat['x']},{chat['y']},{chat['w']},{chat['h']}")
    parts.append("win=" + "x".join(str(n) for n in layout.get("window", [])))
    return "|".join(parts)


def cache_path(state_dir: Path) -> Path:
    return Path(state_dir) / "layout.json"


def load(state_dir: Path) -> dict | None:
    path = cache_path(state_dir)
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save(state_dir: Path, layout: dict) -> None:
    """Remember a layout so the next start can tell whether it changed."""
    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        cache_path(state_dir).write_text(json.dumps(layout, indent=2) + "\n")
    except OSError:
        # A read-only state directory must not stop the watcher running;
        # the cost is re-scanning next time, not a failure.
        pass


def changed_since(state_dir: Path, layout: dict) -> bool:
    """True when the interface has moved since the last remembered scan."""
    previous = load(state_dir)
    if not previous:
        return False
    return fingerprint(previous) != fingerprint(layout)

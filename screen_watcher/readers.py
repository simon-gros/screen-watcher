"""Reusable interface readers: pixels in, normalized events out.

Extracted from `watcher.py` as part of the modular split. A reader turns a
region into events several rules can consume, rather than each rule owning
its own OCR loop - five rule kinds were re-implementing chat reading,
deduplication and capping before `ChatReader` existed.

`ocr`, `ocr_cached` and `norm_line` are resolved through a late import of
`watcher`, both to avoid a circular import and so a test patching them
still reaches the readers that call them.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from screen_watcher.scheduler import FrameScheduler    # noqa: F401


def _w():
    """The `watcher` module, imported late."""
    import watcher
    return watcher


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
        text = _w().ocr_cached(sched.game.handle, box, sched.cycle, psm)
        fresh: list[ChatLine] = []
        for raw in text.splitlines():
            line = raw.strip()
            key = _w().norm_line(line)
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

"""Reading text and numbers out of captured frames.

Extracted from `watcher.py` as part of the modular split. Two layers:
Tesseract for general chat, and a sprite-template reader for the fixed
numeric font RuneScape uses in its interface, which is both faster and
steadier than OCR on those readouts.

`capture_array` and `norm_line` resolve through a late import of
`watcher`. Fifty tests patch the OCR entry points here and the capture
below them; binding either would make those patches reach nothing while
still passing.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

from screen_watcher import capture as capture_mod
from screen_watcher.capture import CaptureError, capture


def _w():
    """The `watcher` module, imported late."""
    import watcher
    return watcher


_OCR_CACHE: dict = {}

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
    Tesseract is not byte-stable on this font, hence `_w().norm_line` rather than
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
    seen = {_w().norm_line(ln) for ln in old_lines[-12:] if _w().norm_line(ln)}
    fresh = [ln for ln in new_lines
             if _w().norm_line(ln) and _w().norm_line(ln) not in seen
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
    frame = _w().capture_array(wid, box, None, cycle=cycle)
    prev = _SCROLL_CACHE.get(key)

    if prev is not None:
        shift = detect_scroll(prev[0], frame)
        if shift == 0:
            # Nothing moved; the cached text still describes this frame.
            _SCROLL_CACHE[key] = (frame, prev[1])
            return prev[1]
        if shift is not None:
            strip = frame[max(0, frame.shape[0] - shift - _SCROLL_OVERLAP):]
            text = _stitch(prev[1], ocr_array(strip, psm, sauvola=True))
            _SCROLL_CACHE[key] = (frame, text)
            return text

    text = ocr_array(frame, psm, sauvola=True)
    _SCROLL_CACHE[key] = (frame, text)
    return text


class OcrError(RuntimeError):
    """Tesseract could not be run, or did not finish.

    Distinct from `CaptureError`: the pixels arrived, so the window is fine
    and the watcher must not treat this as a lost window and start trying to
    reacquire it. The per-rule guard in the poll loop catches this and drops
    that one rule for the cycle.
    """


#: Sauvola adaptive thresholding (`thresholding_method=2`). RS3 draws chat
#: over a semi-transparent panel, so the background brightness varies with
#: whatever 3D scene is behind it - exactly the case global Otsu handles
#: worst and Sauvola is designed for. MEASURED over three fixture frames
#: and 63 known lines: 0.06 character errors per line against Otsu's 0.14,
#: a further 57% reduction, for 22 ms (5%). LeptonicaOtsu (method 1) was
#: catastrophic here - 10.20 errors per line and 3.6x slower - so the
#: choice is specifically Sauvola, not "any non-default method".
_THRESHOLD_ARGS = ("-c", "thresholding_method=2")


def _run_tesseract(path: str, psm: int, env: dict | None = None,
                   sauvola: bool = False) -> str:
    """One tesseract pass over a PNG on disk."""
    try:
        r = subprocess.run(["tesseract", path, "stdout", "--psm", str(psm),
                            *(_THRESHOLD_ARGS if sauvola else ())],
                           capture_output=True, text=True, timeout=30,
                           env=env)
    except subprocess.TimeoutExpired:
        raise OcrError("tesseract timed out")
    except OSError:
        raise OcrError("tesseract not installed")
    return r.stdout.strip()


def _prepare(img: np.ndarray) -> np.ndarray:
    """Upscale and invert a region before Tesseract reads it.

    RS3 chat is bright text on a dark, semi-transparent panel at a glyph
    height of ~17px - roughly 70 DPI against the 300 DPI Tesseract is
    trained for. Two cheap corrections, both MEASURED against the replay
    fixture rather than assumed:

    - **2x bicubic.** Gives Tesseract the stroke detail it expects. On
      its own it is a wash for accuracy and costs 35% more time.
    - **Invert to dark-on-light.** Matches the polarity of the training
      data. On its own it is accuracy-neutral but 40% FASTER, because
      Tesseract's internal binarisation stops fighting the panel.

    Together they are worth much more than either alone: measured over
    all three fixture frames and 63 lines of known chat, character
    errors fell from 0.54 to 0.14 per line - a 74% reduction - while the
    pass got *faster*, 452 ms against 558 ms.

    What this does NOT fix, checked explicitly rather than claimed: the
    leading '[' of a chat timestamp is still lost (23 of 23 lines), and
    dropped-l artefacts like 'oot' for 'loot' survive. Those are clipped
    at the region edge and too thin respectively, so the profile
    patterns still carry their [lI1|] character classes. The gain is in
    the sentence body - for instance '20;32:57' now reads '20:32:57'.
    """
    height, width = img.shape[:2]
    if height == 0 or width == 0:
        return img
    big = Image.fromarray(img).resize((width * 2, height * 2), Image.BICUBIC)
    return 255 - np.asarray(big, dtype=np.uint8)


def ocr_array(frame: np.ndarray, psm: int = 6,
              sauvola: bool = False) -> str:
    """Tesseract over an in-memory frame.

    `OMP_THREAD_LIMIT=1` is deliberate. Tesseract's OpenMP parallelism is a
    net loss on this workload: measured on a 24-core host, the default took
    1181 ms against 790 ms pinned to a single thread, because the region is
    small enough that thread coordination costs more than it saves.

    `sauvola` selects adaptive thresholding, which is a large win on prose
    and a REGRESSION on the numeric bars - see `_THRESHOLD_ARGS`. It is off
    by default so the gauge rules keep Otsu; `ocr_scrolling` turns it on
    for chat.
    """
    if frame.ndim == 3:
        frame = frame[..., :3].mean(axis=2)
    img = _prepare(np.clip(frame, 0, 255).astype(np.uint8))
    env = dict(os.environ, OMP_THREAD_LIMIT="1")
    with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
        Image.fromarray(img).save(tmp.name)
        return _run_tesseract(tmp.name, psm, env, sauvola=sauvola)


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
        # Read through the module, not a from-import: set_active_game
        # rebinds screen_watcher.capture.ACTIVE_GAME, and a copy taken at
        # import time would stay None for the life of the process.
        game = capture_mod.ACTIVE_GAME
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
            frame = _w()._capture_array_uncached(wid, box)
        except CaptureError:
            return ocr(wid, box, psm)
    text = read_numeric(frame)
    if text is not None:
        return text
    return ocr(wid, box, psm)

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image


class CaptureError(RuntimeError):
    pass


_FRAME_CACHE: dict[tuple[str, int], np.ndarray] = {}


def capture(
    wid: str,
    box: tuple[int, int, int, int] | None,
    out: Path,
    resize: str | None = None,
) -> Path:
    """Capture a window or crop using ImageMagick import."""
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
        result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired as exc:
        raise CaptureError("import timed out") from exc
    if result.returncode != 0 or not out.exists():
        raise CaptureError(f"import failed: {result.stderr.strip()[:200]}")
    return out


def capture_frame(wid: str, cycle: int | None = None) -> np.ndarray:
    """Capture one immutable full-window frame.

    All detectors in the same polling cycle crop this shared frame, so OCR,
    inventory, idle, and visual rules observe the same instant.
    """
    if cycle is not None:
        key = (wid, cycle)
        hit = _FRAME_CACHE.get(key)
        if hit is not None:
            return hit

    with tempfile.NamedTemporaryFile(suffix=".ppm") as tmp:
        capture(wid, None, Path(tmp.name))
        try:
            with Image.open(tmp.name) as image:
                frame = np.asarray(image.convert("RGB"), dtype=np.int16)
        except (OSError, ValueError) as exc:
            raise CaptureError(f"unreadable capture: {exc}") from exc

    if cycle is not None:
        # Retain only the current cycle for this window.
        for old in [k for k in _FRAME_CACHE if k[0] == wid and k[1] != cycle]:
            _FRAME_CACHE.pop(old, None)
        _FRAME_CACHE[(wid, cycle)] = frame
    return frame


def capture_array(
    wid: str,
    box: tuple[int, int, int, int],
    mask: str | None = None,
    cycle: int | None = None,
) -> np.ndarray:
    frame = capture_frame(wid, cycle)
    x, y, w, h = box
    height, width = frame.shape[:2]
    x0 = max(0, min(x, width))
    y0 = max(0, min(y, height))
    x1 = max(x0, min(x + w, width))
    y1 = max(y0, min(y + h, height))
    crop = frame[y0:y1, x0:x1]
    if crop.size == 0:
        raise CaptureError(f"empty crop {box} for frame {width}x{height}")
    if mask == "bright":
        lum = crop.mean(axis=2)
        crop = (lum > 140).astype(np.int16) * 255
    return crop


def clear_frame_cache() -> None:
    _FRAME_CACHE.clear()

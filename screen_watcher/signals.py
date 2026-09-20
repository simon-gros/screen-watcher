from __future__ import annotations

import json
import re
import subprocess
import tempfile
import numpy as np
from PIL import Image

from .capture import capture_array
from .paths import STATE_DIR


def count_occupied(
    frame: np.ndarray,
    grid: tuple,
    pad: float = 0.22,
    threshold: float = 8.0,
) -> tuple[int, int]:
    x0, y0, cell_w, cell_h, cols, rows = grid
    occupied = 0
    for row in range(rows):
        for col in range(cols):
            x, y = int(x0 + col * cell_w), int(y0 + row * cell_h)
            px, py = int(cell_w * pad), int(cell_h * pad)
            patch = frame[y + py:y + cell_h - py, x + px:x + cell_w - px]
            if patch.size and float(patch.std(axis=(0, 1)).mean()) > threshold:
                occupied += 1
    return occupied, cols * rows


def slot_signatures(
    frame: np.ndarray,
    grid: tuple,
    pad: float = 0.22,
    threshold: float = 8.0,
) -> list[dict]:
    x0, y0, cell_w, cell_h, cols, rows = grid
    output: list[dict] = []
    for row in range(rows):
        for col in range(cols):
            x, y = int(x0 + col * cell_w), int(y0 + row * cell_h)
            px, py = int(cell_w * pad), int(cell_h * pad)
            patch = frame[y + py:y + cell_h - py, x + px:x + cell_w - px]
            index = row * cols + col
            if patch.size == 0:
                output.append({"i": index, "occ": False, "rgb": (0.0, 0.0, 0.0), "cover": 0.0})
                continue
            std = float(patch.std(axis=(0, 1)).mean())
            lum = patch.mean(axis=2)
            icon = lum > 90
            cover = float(icon.mean())
            rgb = tuple(float(v) for v in patch[icon].mean(axis=0)) if icon.any() else (0.0, 0.0, 0.0)
            output.append({"i": index, "occ": std > threshold, "rgb": rgb, "cover": cover})
    return output


def diff_slots(prev: list[dict], cur: list[dict], colour_tol: float = 6.0) -> list[dict]:
    changes: list[dict] = []
    for before, after in zip(prev, cur):
        if before["occ"] and not after["occ"]:
            changes.append({"slot": after["i"], "kind": "emptied"})
        elif not before["occ"] and after["occ"]:
            changes.append({"slot": after["i"], "kind": "gained"})
        elif before["occ"] and after["occ"]:
            delta = max(abs(a - b) for a, b in zip(before["rgb"], after["rgb"]))
            if delta > colour_tol:
                changes.append({"slot": after["i"], "kind": "changed", "delta": round(delta, 1)})
    return changes


def count_by_colour(
    frame: np.ndarray,
    grid: tuple,
    min_blue: float,
    lum_floor: float = 90.0,
    pad: float = 0.22,
    min_cover: float = 0.30,
) -> int:
    x0, y0, cell_w, cell_h, cols, rows = grid
    count = 0
    for row in range(rows):
        for col in range(cols):
            x, y = int(x0 + col * cell_w), int(y0 + row * cell_h)
            px, py = int(cell_w * pad), int(cell_h * pad)
            patch = frame[y + py:y + cell_h - py, x + px:x + cell_w - px]
            if patch.size == 0:
                continue
            lum = patch.mean(axis=2)
            icon = lum > lum_floor
            if icon.mean() < min_cover:
                continue
            red, _green, blue = patch[icon].mean(axis=0)
            if float(blue) - float(red) >= min_blue:
                count += 1
    return count


OCCUPANCY_LOG = STATE_DIR / "occupancy.jsonl"


def log_occupancy(wall_time: float, occupied: int, profile: str = "") -> None:
    """Persist wall-clock timestamps only; monotonic values never cross boots."""
    try:
        OCCUPANCY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with OCCUPANCY_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "t": round(wall_time, 3),
                "occ": occupied,
                "profile": profile,
            }) + "\n")
    except OSError:
        pass


def load_cycles(capacity: int, gap: float = 300.0) -> list[dict]:
    if not OCCUPANCY_LOG.exists():
        return []
    rows: list[dict] = []
    for line in OCCUPANCY_LOG.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except (ValueError, TypeError):
            continue
    cycles: list[dict] = []
    current: dict | None = None
    for index, row in enumerate(rows):
        timestamp, occupied = row.get("t"), row.get("occ")
        if not isinstance(timestamp, (int, float)) or not isinstance(occupied, int):
            continue
        prev = rows[index - 1] if index else None
        if prev and isinstance(prev.get("t"), (int, float)) and timestamp - prev["t"] > gap:
            current = None
        if prev and isinstance(prev.get("occ"), int) and occupied < prev["occ"] - 2:
            if current and current.get("first_gain") is not None:
                current["banked_at"] = timestamp
                current["peak"] = current.get("peak", prev["occ"])
                current["emptied_to"] = occupied
                cycles.append(current)
            current = {"start": timestamp, "first_gain": None, "peak": occupied}
            continue
        if current is None:
            current = {"start": timestamp, "first_gain": None, "peak": occupied}
        if prev and isinstance(prev.get("occ"), int) and occupied > prev["occ"]:
            if current["first_gain"] is None:
                current["first_gain"] = timestamp
            current["last_gain"] = timestamp
            current["peak"] = max(current.get("peak", 0), occupied)
            if occupied >= capacity and "full_at" not in current:
                current["full_at"] = timestamp
    return cycles


def fill_rate(history: list[tuple[float, int]], window: float = 180.0) -> float:
    if len(history) < 4:
        return 0.0
    now = history[-1][0]
    points = [(timestamp, value) for timestamp, value in history if now - timestamp <= window]
    if len(points) < 4:
        return 0.0
    ts = np.array([point[0] for point in points]) - points[0][0]
    values = np.array([point[1] for point in points], dtype=float)
    if ts[-1] <= 0 or values.max() == values.min():
        return 0.0
    return max(0.0, float(np.polyfit(ts, values, 1)[0]))


def mean_abs_diff(a: np.ndarray | None, b: np.ndarray | None) -> float:
    if a is None or b is None or a.shape != b.shape:
        return float("nan")
    return float(np.mean(np.abs(a.astype(np.float32) - b.astype(np.float32))))


def norm_line(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


class OCRError(RuntimeError):
    pass


_OCR_CACHE: dict[tuple[str, tuple, int], tuple[int, str]] = {}


def ocr(wid: str, box: tuple, psm: int = 6, cycle: int | None = None) -> str:
    """Run Tesseract over a crop of the cycle's shared full-window frame."""
    array = capture_array(wid, box, cycle=cycle)
    with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
        Image.fromarray(array.astype(np.uint8), "RGB").save(tmp.name)
        try:
            result = subprocess.run(
                ["tesseract", tmp.name, "stdout", "--psm", str(psm)],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            raise OCRError("tesseract timed out") from exc
    if result.returncode != 0:
        raise OCRError(f"tesseract failed: {result.stderr.strip()[:200]}")
    return result.stdout.strip()


def ocr_cached(wid: str, box: tuple, cycle: int, psm: int = 6) -> str:
    key = (wid, tuple(box), psm)
    hit = _OCR_CACHE.get(key)
    if hit is not None and hit[0] == cycle:
        return hit[1]
    text = ocr(wid, box, psm, cycle=cycle)
    _OCR_CACHE[key] = (cycle, text)
    return text

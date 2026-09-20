from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


def ensure_x_env() -> None:
    """Recover DISPLAY/XAUTHORITY from the active desktop session when needed."""
    if os.environ.get("DISPLAY") and os.environ.get("XAUTHORITY"):
        if Path(os.environ["XAUTHORITY"]).exists():
            return
    for proc in ("plasmashell", "kwin_wayland", "gnome-shell"):
        result = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), proc],
            capture_output=True,
            text=True,
        )
        for pid in result.stdout.split():
            try:
                env = Path(f"/proc/{pid}/environ").read_bytes().decode("utf-8", "replace")
            except OSError:
                continue
            found: dict[str, str] = {}
            for item in env.split("\0"):
                if "=" not in item:
                    continue
                key, value = item.split("=", 1)
                if key in {"DISPLAY", "XAUTHORITY"}:
                    found[key] = value
            if found.get("DISPLAY") and Path(found.get("XAUTHORITY", "")).exists():
                os.environ.update(found)
                return


def _xdo(*args: str) -> str:
    result = subprocess.run(["xdotool", *args], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def window_size(wid: str) -> tuple[int, int] | None:
    match = re.search(r"Geometry:\s*(\d+)x(\d+)", _xdo("getwindowgeometry", wid))
    return (int(match.group(1)), int(match.group(2))) if match else None


def find_window(wm_class: str, min_area: int = 100_000) -> str | None:
    best: str | None = None
    best_area = 0
    for wid in _xdo("search", "--onlyvisible", "--class", wm_class).splitlines():
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


def resolve_window(cfg: dict) -> tuple[str, tuple[int, int]]:
    wid = find_window(cfg["window"]["wm_class"])
    if not wid:
        raise RuntimeError(
            f"window not found (class={cfg['window']['wm_class']!r}) - is the game running?"
        )
    size = window_size(wid)
    if not size:
        raise RuntimeError(f"could not determine geometry for window {wid}")
    return wid, size

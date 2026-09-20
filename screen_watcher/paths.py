from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_PROFILE_DIR = PROJECT_ROOT / "profiles"

CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "screen-watcher"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "screen-watcher"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "screen-watcher"
CAPTURE_DIR = CACHE_DIR / "captures"

DEFAULT_PROFILE = BUNDLED_PROFILE_DIR / "thieving.json"


def ensure_runtime_dirs() -> None:
    for path in (CONFIG_HOME, STATE_DIR, CACHE_DIR, CAPTURE_DIR):
        path.mkdir(parents=True, exist_ok=True)

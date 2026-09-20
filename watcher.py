#!/usr/bin/env python3
"""Compatibility entry point for Screen Watcher.

New code should import from the screen_watcher package or use the installed
`screen-watcher` command. Selected historical symbols are re-exported so
existing local tests and scripts do not break abruptly.
"""

from screen_watcher.capture import CaptureError, _FRAME_CACHE, capture, capture_array
from screen_watcher.cli import main
from screen_watcher.config import ConfigError, load_config, validate_config
from screen_watcher.events import Alert
from screen_watcher.regions import Region
from screen_watcher.rules import Rule, RuleBase, evaluate
from screen_watcher.signals import mean_abs_diff, norm_line

__all__ = [
    "Alert",
    "CaptureError",
    "ConfigError",
    "Region",
    "Rule",
    "RuleBase",
    "_FRAME_CACHE",
    "capture",
    "capture_array",
    "evaluate",
    "load_config",
    "mean_abs_diff",
    "norm_line",
    "validate_config",
    "main",
]


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")

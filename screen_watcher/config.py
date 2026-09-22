"""Profile loading, validation, and compatibility metadata.

Extracted from `watcher.py` as part of the modular split. Validation runs
before any window lookup or capture side effect, so a malformed profile is
rejected while it is still cheap to say why.

`Region`, `Rule` and the kind/type tables are resolved through a late
import of `watcher`: they are the vocabulary a profile is written in, and
they live with the rules that consume them.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from screen_watcher.windows import find_window, window_size


def _w():
    """The `watcher` module, imported late to avoid a circular import."""
    import watcher
    return watcher


def load_config(path: Path | None = None) -> dict:
    # Resolved at call time, not as a default argument: evaluating
    # _w() at import time would trigger the circular import this
    # module exists to avoid.
    path = Path(path if path is not None else _w().CONFIG_PATH)
    if not path.exists():
        sys.exit(f"no config at {path}")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"invalid JSON in {path}: {exc}")
    try:
        validate_config(cfg)
    except ValueError as exc:
        sys.exit(f"invalid configuration: {exc}")
    cfg["_regions"] = {k: _w().Region.parse(v) for k, v in cfg["regions"].items()
                       if not k.startswith("_")}
    return cfg


#: Profile schema this build understands. Bump the MAJOR part when a change
#: makes older profiles misbehave rather than merely lack a feature.
SCHEMA_VERSION = 1


def validate_schema_version(cfg: dict) -> None:
    """Reject a profile written for a schema this build cannot honour.

    A profile missing the field is accepted as version 1: every profile
    predates the field, and refusing them would break working setups to
    enforce bookkeeping.

    A *newer* major version is refused outright. Silently ignoring fields
    it does not understand is the failure mode worth preventing - a
    profile that relies on a detector this build lacks would run with that
    protection quietly absent.
    """
    raw = cfg.get("schema_version", SCHEMA_VERSION)
    if isinstance(raw, str):
        raw = raw.split(".")[0]
    try:
        version = int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"schema_version must be a number, got {cfg.get('schema_version')!r}")
    if version > SCHEMA_VERSION:
        raise ValueError(
            f"profile needs schema version {version}, this build supports "
            f"{SCHEMA_VERSION} - update Screen Watcher")
    if version < 1:
        raise ValueError(f"schema_version must be 1 or greater, got {version}")


def check_fingerprint(cfg: dict, size: tuple[int, int],
                      backend: str = "") -> list[str]:
    """Compare a profile's calibration assumptions against reality.

    Regions are pixel offsets measured on one window size. At any other
    size they resolve somewhere else entirely, and the failure is silent:
    OCR returns nothing or, worse, reads a neighbouring panel. This
    already happened during development - a gold row measured from a
    scaled screenshot landed 80px off and read as garbage for hours.

    Returns human-readable mismatches rather than raising, because a
    mismatch is a warning: the profile may still work, and refusing to
    start would be worse than reporting a risk.
    """
    fp = cfg.get("fingerprint")
    if not isinstance(fp, dict):
        return []
    problems = []

    expected = fp.get("window_size")
    if isinstance(expected, list) and len(expected) == 2:
        if tuple(expected) != tuple(size):
            problems.append(
                f"window is {size[0]}x{size[1]}, profile calibrated at "
                f"{expected[0]}x{expected[1]} - regions may resolve wrongly")

    expected_backend = fp.get("capture_backend")
    if expected_backend and backend and expected_backend != backend:
        problems.append(
            f"capture backend is {backend}, profile recorded "
            f"{expected_backend}")

    return problems


def validate_config(cfg: object) -> None:
    """Validate configuration before any window or capture side effects."""
    if not isinstance(cfg, dict):
        raise ValueError("root must be an object")
    validate_schema_version(cfg)
    fp = cfg.get("fingerprint")
    if fp is not None and not isinstance(fp, dict):
        raise ValueError("fingerprint must be an object")
    window = cfg.get("window")
    if not isinstance(window, dict) or not isinstance(window.get("wm_class"), str):
        raise ValueError("window.wm_class must be a string")
    if "skill" in cfg and (
            not isinstance(cfg["skill"], str) or not cfg["skill"].strip()):
        raise ValueError("skill must be a non-empty string")
    profile_type = cfg.get("profile_type", "skill")
    if profile_type not in _w().PROFILE_TYPES:
        raise ValueError(
            f"profile_type must be one of {sorted(_w().PROFILE_TYPES)}")
    interval = cfg.get("interval", 1.0)
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
        raise ValueError("interval must be a positive number")

    raw_regions = cfg.get("regions")
    if not isinstance(raw_regions, dict) or not raw_regions:
        raise ValueError("regions must be a non-empty object")
    regions = {}
    for name, spec in raw_regions.items():
        if name.startswith("_"):
            continue
        if not isinstance(name, str):
            raise ValueError("region names must be strings")
        try:
            region = _w().Region.parse(spec)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"region {name!r}: {exc}") from exc
        if region.w <= 0 or region.h <= 0:
            raise ValueError(f"region {name!r} dimensions must be positive")
        if region.grid:
            x0, y0, cw, ch, cols, rows = region.grid
            if x0 < 0 or y0 < 0 or min(cw, ch, cols, rows) <= 0:
                raise ValueError(f"region {name!r} grid values must be positive")
            if x0 + cw * cols > region.w or y0 + ch * rows > region.h:
                raise ValueError(f"region {name!r} grid exceeds region bounds")
        regions[name] = region

    rules = cfg.get("rules")
    if not isinstance(rules, list):
        raise ValueError("rules must be an array")
    names = set()
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"rule {index} must be an object")
        name = rule.get("name")
        kind = rule.get("kind")
        region = rule.get("region")
        if not isinstance(name, str) or not name:
            raise ValueError(f"rule {index} needs a non-empty name")
        if name in names:
            raise ValueError(f"duplicate rule name {name!r}")
        names.add(name)
        if kind not in _w().RULE_KINDS:
            raise ValueError(f"rule {name!r} has unknown kind {kind!r}")
        if region not in regions:
            raise ValueError(f"rule {name!r} references unknown region {region!r}")
        for body_key in ("alert_body", "out_alert_body"):
            if body_key in rule and not isinstance(rule[body_key], str):
                raise ValueError(
                    f"rule {name!r}: {body_key} must be a string")
        unknown = sorted(
            key for key in rule
            if not key.startswith("_") and key not in _w().Rule.__dataclass_fields__)
        if unknown:
            raise ValueError(
                f"rule {name!r} has unknown option(s): {', '.join(unknown)}")
        if kind in {"activity", "ocr", "supply", "counter"} and not rule.get("pattern"):
            raise ValueError(f"rule {name!r} requires pattern")
        if kind == "loot" and not rule.get("item_pattern"):
            raise ValueError(f"rule {name!r} requires item_pattern")
        if kind in {"inventory", "item_count", "stack"} and not regions[region].grid:
            raise ValueError(f"rule {name!r} requires a region grid")
        if kind == "inventory" and rule.get("mode", "lead") not in {"lead", "overflow"}:
            raise ValueError(
                f"rule {name!r}: mode must be 'lead' or 'overflow'")
        corroborate_region = rule.get("corroborate_region")
        corroborate_pattern = rule.get("corroborate_pattern")
        if (corroborate_region is None) != (corroborate_pattern is None):
            raise ValueError(
                f"rule {name!r} requires corroborate_region and "
                "corroborate_pattern together")
        if corroborate_region is not None:
            # item_count joined presence here: both ask "is the activity
            # actually running?" before reporting, so a shortage nothing is
            # consuming stays quiet the same way a blackout does.
            if kind not in ("presence", "item_count"):
                raise ValueError(
                    f"rule {name!r}: corroboration is only valid for "
                    "presence and item_count rules")
            if corroborate_region not in regions:
                raise ValueError(
                    f"rule {name!r} references unknown corroborate_region "
                    f"{corroborate_region!r}")
        items_region = rule.get("require_items_region")
        if items_region is not None:
            # Only `activity` reads these fields; elsewhere they would be
            # silently ignored, which is worse than refusing the profile.
            if kind != "activity":
                raise ValueError(
                    f"rule {name!r}: require_items_region is only valid for "
                    "activity rules")
            if items_region not in regions:
                raise ValueError(
                    f"rule {name!r} references unknown require_items_region "
                    f"{items_region!r}")
            if not regions[items_region].grid:
                raise ValueError(
                    f"rule {name!r}: require_items_region {items_region!r} "
                    "needs a grid to count items")
        for key in ("pattern", "suppress_pattern", "trip_pattern", "out_pattern",
                    "item_pattern", "ignore_pattern", "corroborate_pattern"):
            pattern = rule.get(key)
            if pattern is not None:
                try:
                    compiled = re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"rule {name!r} has invalid {key}: {exc}") from exc
                if kind == "counter" and key == "pattern" and compiled.groups < 1:
                    raise ValueError(
                        f"rule {name!r}: counter pattern needs a capture group")
        for key in ("cooldown", "idle_seconds", "threshold", "lead_seconds",
                    "overflow_seconds", "stop_seconds", "confirm_seconds",
                    "repeat_seconds", "absent_seconds", "present_above",
                    "stack_tolerance", "cell_threshold", "step"):
            value = rule.get(key)
            if value is not None and (
                    not isinstance(value, (int, float)) or isinstance(value, bool)
                    or value < 0):
                raise ValueError(f"rule {name!r}: {key} must be non-negative")


def resolve_window(cfg: dict) -> tuple[str, tuple[int, int]]:
    wid = find_window(cfg["window"]["wm_class"])
    if not wid:
        sys.exit(f"window not found (class={cfg['window']['wm_class']!r}) - is the game running?")
    size = window_size(wid)
    if not size:
        sys.exit(f"could not determine geometry for window {wid}")
    return wid, size

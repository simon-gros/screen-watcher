from __future__ import annotations

import json
import re
from dataclasses import fields
from pathlib import Path

from .paths import BUNDLED_PROFILE_DIR, DEFAULT_PROFILE
from .regions import Region
from .rules import RULE_CLASS, rule_from_dict, RuleBase

SCHEMA_VERSION = 1
PROFILE_TYPES = {"skill", "quest", "activity", "boss"}
RULE_KINDS = set(RULE_CLASS)

_TOP_LEVEL_FIELDS = {
    "schema_version",
    "profile_version",
    "profile_type",
    "activity_type",
    "skill",
    "activity",
    "name",
    "window",
    "interval",
    "regions",
    "rules",
}


class ConfigError(ValueError):
    pass


def _non_negative_number(value, label: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ConfigError(f"{label} must be a non-negative number")


def validate_config(cfg: object) -> None:
    if not isinstance(cfg, dict):
        raise ConfigError("root must be an object")

    unknown_top = {
        key for key in cfg
        if not key.startswith("_") and key not in _TOP_LEVEL_FIELDS
    }
    if unknown_top:
        raise ConfigError(f"unknown top-level fields: {', '.join(sorted(unknown_top))}")

    if cfg.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(
            f"schema_version must be {SCHEMA_VERSION}; got {cfg.get('schema_version')!r}"
        )
    version = cfg.get("profile_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ConfigError("profile_version must be a positive integer")

    profile_type = cfg.get("profile_type")
    if profile_type not in PROFILE_TYPES:
        raise ConfigError(f"profile_type must be one of {sorted(PROFILE_TYPES)}")
    if profile_type == "activity":
        activity_type = cfg.get("activity_type")
        if not isinstance(activity_type, str) or not activity_type.strip():
            raise ConfigError("activity profiles require a non-empty activity_type")
    if profile_type == "skill":
        skill = cfg.get("skill")
        if not isinstance(skill, str) or not skill.strip():
            raise ConfigError("skill profiles require a non-empty skill")

    window = cfg.get("window")
    if not isinstance(window, dict) or not isinstance(window.get("wm_class"), str):
        raise ConfigError("window.wm_class must be a string")
    extra_window = {
        key for key in window
        if not key.startswith("_") and key not in {"wm_class"}
    }
    if extra_window:
        raise ConfigError(f"unknown window fields: {', '.join(sorted(extra_window))}")

    interval = cfg.get("interval", 1.0)
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
        raise ConfigError("interval must be a positive number")

    raw_regions = cfg.get("regions")
    if not isinstance(raw_regions, dict) or not raw_regions:
        raise ConfigError("regions must be a non-empty object")

    regions: dict[str, Region] = {}
    for name, spec in raw_regions.items():
        if str(name).startswith("_"):
            continue
        if not isinstance(name, str) or not name:
            raise ConfigError("region names must be non-empty strings")
        try:
            region = Region.parse(spec)
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"region {name!r}: {exc}") from exc
        if region.w <= 0 or region.h <= 0:
            raise ConfigError(f"region {name!r} dimensions must be positive")
        if region.grid:
            x0, y0, cell_w, cell_h, cols, rows = region.grid
            if x0 < 0 or y0 < 0 or min(cell_w, cell_h, cols, rows) <= 0:
                raise ConfigError(f"region {name!r} grid values must be positive")
            if x0 + cell_w * cols > region.w or y0 + cell_h * rows > region.h:
                raise ConfigError(f"region {name!r} grid exceeds region bounds")
        regions[name] = region

    raw_rules = cfg.get("rules")
    if not isinstance(raw_rules, list):
        raise ConfigError("rules must be an array")

    names: set[str] = set()
    for index, spec in enumerate(raw_rules):
        if not isinstance(spec, dict):
            raise ConfigError(f"rule {index} must be an object")
        name = spec.get("name")
        kind = spec.get("kind")
        region_name = spec.get("region")

        if not isinstance(name, str) or not name:
            raise ConfigError(f"rule {index} needs a non-empty name")
        if name in names:
            raise ConfigError(f"duplicate rule name {name!r}")
        names.add(name)

        if kind not in RULE_KINDS:
            raise ConfigError(f"rule {name!r} has unknown kind {kind!r}")
        if region_name not in regions:
            raise ConfigError(f"rule {name!r} references unknown region {region_name!r}")

        cls = RULE_CLASS[kind]
        allowed = {field.name for field in fields(cls) if field.init}
        unknown = {
            key for key in spec
            if not key.startswith("_") and key not in allowed
        }
        if unknown:
            hint = ""
            if "cooldwon" in unknown:
                hint = "; did you mean 'cooldown'?"
            raise ConfigError(
                f"rule {name!r} has unknown fields: {', '.join(sorted(unknown))}{hint}"
            )

        if kind in {"activity", "ocr", "supply"} and not spec.get("pattern"):
            raise ConfigError(f"rule {name!r} requires pattern")
        if kind in {"inventory", "item_count"} and not regions[region_name].grid:
            raise ConfigError(f"rule {name!r} requires a region grid")

        for key in ("pattern", "suppress_pattern", "trip_pattern", "out_pattern"):
            pattern = spec.get(key)
            if pattern is not None:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ConfigError(f"rule {name!r} has invalid {key}: {exc}") from exc

        for key in (
            "cooldown",
            "idle_seconds",
            "threshold",
            "lead_seconds",
            "overflow_seconds",
            "stop_seconds",
            "confirm_seconds",
            "repeat_seconds",
            "cell_threshold",
            "min_blue",
        ):
            if key in spec:
                _non_negative_number(spec[key], f"rule {name!r}: {key}")

        if kind == "inventory":
            if spec.get("mode", "lead") not in {"lead", "overflow"}:
                raise ConfigError(f"rule {name!r}: mode must be 'lead' or 'overflow'")
            if int(spec.get("capacity", 28)) <= 0:
                raise ConfigError(f"rule {name!r}: capacity must be positive")

        try:
            rule_from_dict(spec)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"rule {name!r}: {exc}") from exc


def load_config(path: Path = DEFAULT_PROFILE) -> dict:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"no profile at {path}")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc
    validate_config(cfg)
    cfg["_regions"] = {
        name: Region.parse(spec)
        for name, spec in cfg["regions"].items()
        if not name.startswith("_")
    }
    cfg["_path"] = str(path)
    return cfg


def build_rules(cfg: dict) -> list[RuleBase]:
    return [
        rule_from_dict(spec)
        for spec in cfg["rules"]
        if spec.get("enabled", True)
    ]


def list_profiles() -> list[Path]:
    return sorted(BUNDLED_PROFILE_DIR.glob("*.json"))


def profile_identity(cfg: dict) -> str:
    if cfg.get("profile_type") == "skill":
        return cfg.get("skill", "unnamed")
    return cfg.get("activity") or cfg.get("name") or cfg.get("activity_type") or "unnamed"


def resolve_profile_path(profile: str | None, config: Path | None) -> Path:
    if profile and config:
        raise ConfigError("use either --profile or --config, not both")
    if config:
        return Path(config)
    if profile:
        candidate = BUNDLED_PROFILE_DIR / f"{profile}.json"
        if not candidate.exists():
            available = ", ".join(path.stem for path in list_profiles())
            raise ConfigError(f"unknown profile {profile!r}; available: {available}")
        return candidate
    return DEFAULT_PROFILE

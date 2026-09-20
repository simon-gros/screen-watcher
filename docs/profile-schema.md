# Profile schema

Screen Watcher profile schema version 1 is JSON. Every profile begins with:

```json
{
  "schema_version": 1,
  "profile_version": 1,
  "profile_type": "skill",
  "skill": "fishing"
}
```

`schema_version` describes compatibility with the application. Increment it
only for a breaking format change. `profile_version` belongs to the individual
profile and should increase when calibration, rules, or verified cues change.

## Profile types

- `skill` requires `skill`.
- `quest` may use `activity` or `name` for identity.
- `activity` requires `activity_type`, such as `boss` or `minigame`.
- `boss` remains accepted as a legacy migration type.

## Regions

A region is anchored to the game window:

```json
{
  "anchor": "bottom-left",
  "dx": 5,
  "dy": -57,
  "w": 500,
  "h": 375
}
```

Inventory-like regions may add a grid:

```json
{
  "grid": {
    "x0": 17,
    "y0": 86,
    "cell_w": 61,
    "cell_h": 55,
    "cols": 5,
    "rows": 6
  }
}
```

Unknown region and grid fields are rejected.

## Rules

All rules share:

- `name`
- `kind`
- `region`
- `message`
- `cooldown`
- `sound`
- `urgency`
- `timeout_ms`
- `enabled`

Additional fields are permitted only when they belong to the selected rule
kind. Unknown fields are rejected before screen capture begins.

### change / idle

Visual rules can use `mask`, `threshold`, and `idle_seconds`.

### ocr

Requires `pattern`.

### activity

Requires `pattern`; may use `suppress_pattern` and `stop_seconds`.

### inventory

Requires a grid region. Supports `capacity`, `lead_seconds`, `warn_free`,
`cell_threshold`, `mode`, `overflow_seconds`, and `log_occupancy`.

### item_count

Requires a grid region. Supports `min_blue`, `warn_below`, `out_below`,
`confirm_seconds`, `repeat_seconds`, `item`, and `out_message`.

### supply

Requires `pattern`; may use `item`, `trip_pattern`, `out_pattern`,
`out_message`, and `warn_streak`.

## Verification policy

Bundled profiles should enable only rules whose signal wording, timing, or
visual threshold has been verified for that profile. A detector may be reused
across activities, but calibration data must not be treated as transferable
without evidence. Experimental rules should remain `"enabled": false` and state
what still needs to be measured or captured in an underscore-prefixed note.

CI includes a regression check that rejects enabled bundled rules explicitly
marked as unverified.

## Comments

Keys beginning with `_` are reserved for human-readable notes and are ignored
by runtime construction. This allows measured thresholds and calibration notes
to live beside the fields they explain without weakening strict validation.

# Screen Watcher application outline

## Purpose

Screen Watcher is a read-only observability application for RuneScape. It
observes the client, extracts reusable signals, evaluates an explicitly chosen
activity profile, and tells the player when attention is needed. It does not
perform gameplay actions.

## Product boundary

The application may observe:

- pixels and frame changes;
- OCR text;
- inventory occupancy and visual item signatures;
- XP, health, prayer, adrenaline, timers, targets, phases, and interface state;
- elapsed time and transitions derived from those observations.

It must not generate mouse, keyboard, movement, combat, banking, item-use, or
other synthetic game input.

## Profile families

New profiles should use one of these families:

- `skill` — a RuneScape skill or concrete training activity;
- `quest` — a quest or quest-stage workflow;
- `activity` — bosses, minigames, repeatable encounters, and other activities.

Activity profiles add `activity_type`, for example `boss` or `minigame`.
The older top-level `boss` type is accepted only for migration.

Profiles are versioned independently from the application schema.

## Implemented architecture

```text
Profile manager
    -> platform/window discovery
    -> one-frame-per-cycle capture
    -> reusable signal extraction
    -> typed rule evaluation
    -> structured Alert + evidence
    -> terminal / JSONL / desktop backends
```

The runtime is now split across the `screen_watcher` package rather than a
single application module. `watcher.py` is only a compatibility entry point.

### Capture

Each polling cycle captures one full game-window frame. Region consumers crop
that shared frame. OCR and image detectors therefore observe the same instant
and repeated ImageMagick launches are avoided.

### Time

Duration logic uses a monotonic clock. Persistent logs use wall-clock Unix
timestamps. Missed polling deadlines are skipped rather than replayed.

### Signals

Reusable signals currently include:

- OCR and normalized line identity;
- mean absolute frame differences;
- inventory occupancy;
- per-slot signatures;
- simple item-colour counts;
- fill-rate estimates.

Tesseract failure is represented as an OCR error, not an empty text result.

### Rules

Runtime rules are typed by detector family rather than one dataclass containing
every possible detector field. Profile validation derives the allowed fields
from those rule classes and rejects unknown properties before capture begins.

Current rule families are:

- visual change;
- visual idle;
- OCR match;
- activity stop;
- inventory lead/overflow;
- item count;
- supply streak.

### Events and outputs

Rule evaluation produces a structured `Alert` containing presentation fields,
profile identity, confidence when available, and machine-readable evidence.

Delivery is independent of evaluation. Current backends are:

- terminal;
- local JSONL history;
- Linux desktop notifications and optional sounds.

### Replay

Sanitized observation files can exercise text-rule logic without a live game
client. Replay and live OCR share the same pure evaluation functions.

### Runtime state

State and captures use XDG directories rather than the source checkout.
Repository screenshots must be deliberate sanitized fixtures.

## Reliability requirements

- profile validation precedes capture;
- stale or PID-reused singleton files must not signal unrelated processes;
- one broken rule must not terminate all other rules;
- capture failure and OCR failure are distinct;
- resize or window reacquisition resets detector observation state;
- alert history contains profile identity and evidence;
- health telemetry reports capture state and rule errors;
- CI compiles the package, validates profiles, runs tests, lints, type-checks,
  and builds the distributable package.

## Current coverage

Bundled, calibrated profiles:

- Fishing
- Thieving / pickpocketing

The project should not claim coverage for activities that have only planning
notes. New activities require verified cues, thresholds, and regression
fixtures.

## Next coverage work

With the architecture stabilized, additional skills should mostly be profile
and calibration work. Good next candidates are activities with clear recurring
signals, such as Mining, Woodcutting, Archaeology, Divination, Cooking, or
Smithing.

Boss and quest support should begin only with a specific encounter or quest,
not a generic "boss" or "quest" detector. Their profiles should model
activity-specific phases, objectives, supplies, suppressions, and failure
states.

## Non-goals

Screen Watcher is not:

- a bot or macro;
- an input automation framework;
- an autonomous combat system;
- a credential, clipboard, browser-history, or network-data collector;
- a storage location for unsanitized personal screenshots.

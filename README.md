# Screen Watcher

Screen Watcher is a read-only RuneScape observability and notification tool for
Linux. It watches configured regions of the game client, extracts signals from
pixels and OCR, evaluates profile-specific rules, and tells the player when
something needs attention.

> **Read-only by architecture.** Screen Watcher does not click, type, move the
> mouse, press keys, bank, fight, or otherwise send input to the game. It only
> observes the screen and produces local output.

The project currently ships calibrated Fishing and Thieving profiles. The core
architecture supports reusable visual, OCR, activity, inventory, item-count,
and supply detectors; additional skills, quests, bosses, minigames, and other
activities should be added as versioned profiles and fixtures rather than
hard-coded into the runtime.

## Current architecture

The application is split into explicit layers:

```text
profile/configuration
        ↓
window + one-frame-per-cycle capture
        ↓
signal extraction (OCR, image, inventory)
        ↓
typed rule evaluation
        ↓
Alert + evidence
        ↓
terminal / JSONL / desktop notification backends
```

Key modules:

- `screen_watcher/platform.py` — desktop session and game-window discovery
- `screen_watcher/capture.py` — full-window capture and per-cycle frame reuse
- `screen_watcher/signals.py` — OCR, frame differences, inventory signals
- `screen_watcher/rules.py` — typed rule classes and pure evaluation logic
- `screen_watcher/config.py` — schema/version validation and profile loading
- `screen_watcher/events.py` — structured alerts and evidence
- `screen_watcher/notifications.py` — replaceable output backends
- `screen_watcher/replay.py` — deterministic replay of sanitized observations
- `screen_watcher/cli.py` — operator commands and watch loop

The historical `watcher.py` command remains as a small compatibility entry
point.

## Requirements

Runtime Python dependencies are declared in `pyproject.toml`. The current
Linux capture stack also expects:

- `xdotool`
- ImageMagick (`import`)
- Tesseract OCR
- `notify-send`
- `paplay` for optional per-rule sounds

On Arch/CachyOS:

```bash
sudo pacman -S xdotool imagemagick tesseract libnotify libpulse
```

On Debian/Ubuntu:

```bash
sudo apt install xdotool imagemagick tesseract-ocr libnotify-bin pulseaudio-utils
```

## Installation

For development:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

This installs the `screen-watcher` command.

## Quick start

List bundled profiles:

```bash
screen-watcher list-profiles
```

Verify all bundled profiles:

```bash
screen-watcher validate-profiles
```

Inspect resolved regions before enabling alerts:

```bash
screen-watcher --profile fishing regions
screen-watcher --profile fishing shot chat_tail
```

Run the watcher:

```bash
screen-watcher --profile fishing watch
```

Run without desktop popups or sounds while tuning rules:

```bash
screen-watcher --profile fishing watch --dry-run
```

The old entry point still works:

```bash
python watcher.py --profile fishing watch
```

## Profile selection

Bundled profiles live under `profiles/`. Select one by name:

```bash
screen-watcher --profile thieving watch
```

or pass an explicit file:

```bash
screen-watcher --config /path/to/custom-profile.json watch
```

Profiles are validated before capture begins. Unknown fields are rejected so a
typo such as `cooldwon` cannot silently become a runtime failure.

Every profile declares:

```json
{
  "schema_version": 1,
  "profile_version": 1,
  "profile_type": "skill"
}
```

`profile_type` is normally `skill`, `quest`, or `activity`. Activity
profiles can add `activity_type`, such as `boss` or `minigame`. The legacy
`boss` profile type is still accepted for migration, but new profiles should
use `activity`.

See `docs/profile-schema.md` for the schema and detector-specific fields.

## Commands

Common commands:

```text
list-profiles       list bundled profiles
validate-profiles   validate every bundled profile
regions             show resolved regions and active rules
calibrate           capture a scaled full-window calibration image
shot                capture one configured region
probe               measure frame-to-frame visual noise
inv                 inspect inventory slot changes
watch               run the observer
status              show process status and recent health telemetry
pause               SIGSTOP the running watcher
resume              SIGCONT the running watcher
stats               summarize inventory fill/bank cycles
alerts              summarize alert history
replay              replay sanitized observations through live rule logic
```

## One frame per polling cycle

A watch cycle captures the game window once. Every detector receives a crop of
that same immutable frame. This prevents one rule from seeing a different
moment than another and avoids launching ImageMagick separately for each
region.

OCR uses the same captured frame. A Tesseract failure raises an OCR-specific
error and is treated as an unavailable detector, not as an empty chat box.
Consequently, OCR failure does not advance an activity-stop timer.

## Timing model

Screen Watcher deliberately uses two clocks:

- `time.monotonic()` for cooldowns, inactivity windows, polling deadlines, and
  other durations;
- `time.time()` for persisted JSONL timestamps.

Monotonic timestamps are never written to long-lived history because they are
not meaningful across reboots.

If a detector cycle takes longer than the configured interval, missed polls are
skipped. The watcher does not issue a burst of catch-up captures.

## Evidence and health

Alerts carry structured evidence in addition to their human-readable message.
Examples include the OCR line that matched, visual-difference magnitude,
inventory occupancy, fill rate, elapsed inactivity, or item count.

Alert history is stored as JSONL. `screen-watcher status` also reads the
latest health snapshot and reports capture health and rule-specific errors.

## Runtime data and privacy

Runtime state no longer lives inside the Git repository.

Screen Watcher follows the XDG directory convention:

```text
~/.config/screen-watcher/
~/.local/state/screen-watcher/
~/.cache/screen-watcher/
```

Calibration and diagnostic screenshots go under the cache directory unless an
explicit `--out` path is supplied. Screenshots and live runtime logs should
not be committed. Test fixtures must be deliberately sanitized.

## Replay testing

Text-based detectors can be replayed without RuneScape, X11, or Tesseract:

```bash
screen-watcher replay tests/fixtures/activity-replay.json --evidence
```

Replay uses the same text-rule evaluation functions as the live watcher. This
makes detector changes regression-testable without reproducing the gameplay
session.

## Development

Run the local checks with:

```bash
python -m compileall -q screen_watcher watcher.py
screen-watcher validate-profiles
pytest
flake8 screen_watcher watcher.py tests
mypy screen_watcher
python -m build
```

GitHub Actions performs compilation, profile validation, tests with coverage,
linting, type checking, and package building.

## Extending Screen Watcher

Do not create a new detector when an existing signal extractor and typed rule
can express the behavior. New activity coverage should normally be introduced
as:

1. a versioned profile;
2. measured thresholds or verified OCR patterns;
3. sanitized replay/fixture data;
4. tests describing the expected alert sequence.

New capture or detector code belongs in the package layer, not inside the
profile.

See `docs/application-outline.md`, `docs/profile-schema.md`,
`docs/detector-notes.md`, and `docs/engineering-notes.md` for further
design notes.

## License

MIT.

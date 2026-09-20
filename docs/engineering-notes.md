# Engineering notes

This file records implementation constraints that remain relevant after the
0.2 architecture refactor. Historical notes about the former monolithic
`watcher.py`, shared scratch files, and repository-local runtime state are no
longer current.

## Runtime layout

`watcher.py` is a compatibility entry point. The implementation lives in the
`screen_watcher` package:

- `platform.py` handles X11/XWayland session and window discovery.
- `capture.py` handles ImageMagick capture and the per-cycle full-frame cache.
- `signals.py` contains OCR and reusable image/inventory measurements.
- `rules.py` contains typed rule state and evaluation.
- `config.py` handles schema validation and profile construction.
- `events.py` defines structured alert values.
- `notifications.py` provides terminal, JSONL, and desktop outputs.
- `replay.py` provides deterministic text-observation replay.
- `cli.py` owns commands, singleton handling, polling, and health telemetry.

## Capture consistency

One polling cycle owns one full-window frame. Region reads crop that frame in
memory. OCR uses a crop from the same frame instead of launching a second
capture. Multiple rules evaluating one cycle therefore observe the same moment.

The frame cache is bounded to the current cycle for each window ID.

## OCR failure semantics

Tesseract non-zero exit codes and timeouts raise `OCRError`. They are not
converted to empty strings. Activity-stop rules therefore distinguish valid OCR
with no matching activity from unavailable OCR.

Rule-specific OCR errors are recorded in health telemetry and do not terminate
unrelated rules.

## Timekeeping

Detector duration state uses `time.monotonic()`. Persistent logs use
`time.time()`.

Monotonic values are never persisted because they are only meaningful for the
current boot.

Polling uses deadlines, but missed deadlines are skipped. A slow OCR or capture
cycle does not create a burst of immediate catch-up polls.

## Process ownership

The singleton file is stored under the XDG state directory. It records both PID
and Linux process start time. Before signalling a process, Screen Watcher
re-checks command identity and start time, using pidfd signalling where
available.

## Runtime data

Runtime data is outside the source checkout:

- state: `$XDG_STATE_HOME/screen-watcher`
- cache/captures: `$XDG_CACHE_HOME/screen-watcher`
- user configuration: `$XDG_CONFIG_HOME/screen-watcher`

Generated captures are not repository assets. A screenshot belongs in Git only
when deliberately sanitized and introduced as a test fixture.

## Profile compatibility

Profiles carry both `schema_version` and `profile_version`.

Unknown rule fields are rejected before capture begins. Rule-specific allowed
fields are derived from the typed runtime rule classes, preventing configuration
typos from surfacing later as constructor failures.

New non-skill/non-quest profiles use `profile_type: activity` and an
`activity_type` such as `boss` or `minigame`.

## Evidence and replay

Alerts carry machine-readable evidence such as matched OCR text, visual
difference, item count, inventory occupancy, fill rate, or elapsed inactivity.

Text detector replay uses the same pure evaluation functions as live OCR. This
is the preferred regression path for chat-driven rules.

## CI acceptance

CI gates Python 3.11, 3.12, and 3.13 installation, compilation, bundled profile
validation, pytest with coverage, Flake8, MyPy on Python 3.12, and a package
build on Python 3.12.

Detector-behavior changes should add or update a replay/unit fixture rather than
relying only on live-game verification.

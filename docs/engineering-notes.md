# Engineering notes

This document collects implementation history, reliability fixes, benchmarks,
and architectural limitations that are useful to contributors but too detailed
for the main README.

## Current architecture

The current application is implemented primarily in `watcher.py`.

The long-term architecture is documented in
[application-outline.md](application-outline.md) and separates:

```text
profile management
    -> capture and observation
    -> signal extraction
    -> rule evaluation
    -> evidence/events
    -> outputs
```

That modular split is planned work, not the current code layout.

## Reliability fixes already implemented

### First-alert cooldown

Rules track the last time they fired. A never-fired rule must be treated
specially; otherwise a default timestamp of zero can accidentally behave like a
real timestamp in cooldown arithmetic.

The current `Rule.ready()` explicitly returns ready when `_last_fired == 0.0`.

### Stale PID handling

`state/watcher.pid` is a hint, not proof that the recorded process is still
the watcher. Linux can reuse PIDs after a crash.

The singleton/status/pause/resume code verifies the recorded process through
`/proc`, checking its command line and process start identity before treating
it as the active watcher.

Where supported, signaling uses a pidfd after identity verification.

### Truncated captures

ImageMagick can occasionally leave an incomplete image while a window is
repainting. Pillow may then raise an `OSError` or `ValueError`.

`capture_array()` converts those failures to `CaptureError`, allowing the
main loop to use its normal capture-miss/reacquisition path instead of
terminating unexpectedly.

### Startup OCR history

OCR regions contain scrollback when Screen Watcher starts. Existing lines are
primed into rule state before notifications are allowed, preventing stale
events from firing immediately at startup.

### OCR normalization

Minor Tesseract punctuation changes can make the same visible line look
different across passes. `norm_line()` reduces text to lowercase
alphanumerics for deduplication.

### OCR per-cycle reuse

OCR results are cached for a polling cycle by region and Tesseract page
segmentation mode. Several OCR rules watching the same chat region therefore
reuse one Tesseract result.

## Capture benchmark

An earlier 4K-window measurement produced approximately:

| method | time |
|---|---:|
| full frame -> PNG | 1.46 s |
| full frame -> PPM | 0.10 s |
| cropped region -> PNG | 0.11 s |

The experiment indicated that PNG encoding, rather than raw capture alone, was
a major source of cost. `capture_array()` therefore uses a temporary PPM file
before converting the image to a NumPy array. The temporary path is unique per
capture and is removed automatically.

These numbers are environment-specific benchmarks, not performance guarantees.

## Current architectural limitations

### Region-level capture consistency

`capture_array()` caches an identical `(window, region, mask)` request for the
current polling cycle. Rules that read the same region therefore reuse the same
pixels.

Different regions are still captured independently, however, so one polling
cycle does not yet represent one immutable full-window frame. A future capture
backend should capture once per cycle and crop all detector regions in memory.

### Poll scheduling

The watch loop already uses `time.monotonic()` and a deadline rather than
sleeping for a fixed interval after processing. This prevents ordinary
processing time from being blindly added to every cycle.

The current `main` implementation does not skip deadlines that have already
been missed. A sufficiently slow capture/OCR cycle can therefore be followed by
back-to-back polling iterations while the deadline catches up. The planned
scheduler should advance directly to the next future deadline.

### Timekeeping

Cooldowns, inactivity windows, overflow durations, and other elapsed detector
state use monotonic time. Persisted alert and occupancy logs continue to use
Unix wall-clock timestamps, which is appropriate for cross-process history.

### Monolithic module

At roughly 1,500+ lines, `watcher.py` now contains window lookup, capture,
image processing, OCR, rule implementations, persistence, notification output,
CLI commands, and process management.

The planned split is approximately:

```text
screen_watcher/
    cli.py
    runtime.py
    capture.py
    windows.py
    profiles.py
    models.py
    signals/
    rules/
    notifications.py
    history.py
    diagnostics.py
```

The important boundary is conceptual rather than file count: observation,
signal extraction, rule decisions, events, and side effects should be separable
and independently testable.

## Configuration validation

Configuration is validated before window lookup or capture side effects.

Current validation checks include:

- root/window structure;
- non-empty skill identity when supplied;
- supported profile types;
- positive polling interval;
- region definitions and anchors;
- grid dimensions and bounds;
- duplicate rule names;
- supported rule kinds;
- rule-to-region references;
- required patterns/grids for relevant rule kinds;
- regular-expression compilation;
- non-negative timing/threshold values.

A future typed profile model or generated JSON Schema would make configuration
errors even earlier and more precise.

## Testing

The current pytest suite covers configuration-independent behaviour such as:

- region anchoring and clamping;
- OCR normalization;
- frame difference calculation;
- alert creation without notification side effects;
- profile identity in alert logs;
- singleton/PID behaviour;
- configuration validation;
- loading the included profiles.

The next major testing improvement should be recorded, sanitized detector
fixtures: inventory screenshots, overlays, representative OCR output, and
replayable sequences.

## CI

`.github/workflows/ci.yml` currently runs on pushes and pull requests using
Python 3.12. It installs `requirements-dev.txt`, compiles `watcher.py` and
`tests/` with `compileall`, runs pytest, and checks `watcher.py` and
`tests/` with Flake8.

Live screen capture is intentionally absent from CI because the hosted runner
does not provide the required RuneScape client, X/XWayland session, or desktop
notification environment.

## Runtime data

Local history and diagnostic images live under `state/`, which is ignored by
Git except for `state/.gitkeep`.

Runtime files should not be committed unless a deliberately sanitized sample is
being promoted to a test fixture.

## Release archives

Release archives can be built from tracked files only:

```bash
git archive --format=tar.gz --prefix=screen-watcher/ \
  -o screen-watcher.tar.gz HEAD
```

This avoids accidentally bundling virtual environments, runtime logs, caches,
or local calibration screenshots.

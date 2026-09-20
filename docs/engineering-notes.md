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
a major source of cost. `capture_array()` therefore uses a PPM scratch file
before converting the image to a NumPy array.

These numbers are environment-specific benchmarks, not performance guarantees.

## Current architectural limitations

### Shared scratch files

`capture_array()` currently writes `state/_scratch.ppm`, and OCR uses
`state/_ocr.png`.

That keeps the implementation simple but means concurrent diagnostic capture
commands can contend for the same files. A future capture backend should use
unique temporary paths or stream image bytes directly through the subprocess.

### Duplicate image captures within a cycle

OCR is cached, but ordinary image captures are not yet shared across every rule.
Two image-based rules watching the same region may capture it independently
during one polling iteration.

A per-cycle frame context/cache is a planned improvement.

### Poll scheduling

The current loop performs all rule work and then calls `time.sleep(interval)`.
Processing time therefore adds to the effective period.

For example, a configured 1.5-second interval plus 0.8 seconds of processing
produces roughly a 2.3-second cycle.

A future scheduler should use a monotonic deadline so processing time is
accounted for separately.

### Wall-clock timers

Rule timestamps currently use `time.time()`. Monotonic time would be more
appropriate for cooldowns, inactivity, overflow durations, and other elapsed
intervals, while Unix wall-clock timestamps can remain appropriate for logs.

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
Python 3.12. It installs `requirements-dev.txt`, runs pytest, and checks
`watcher.py` with Flake8.

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

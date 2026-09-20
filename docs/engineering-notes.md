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


## Priority 0, step 1 — capture backend abstraction

`CaptureBackend` and `GameInstance` separate *which window and how do we get
pixels* from *what those pixels mean*.

```text
GameInstance(wm_class, backend)
    .acquire()            find the window
    .refresh_size()       notice a resize
    .begin_cycle(n)       start a sampling pass
    .frame(box, mask)     cached RGB array
    .save(box, path)      write a capture to disk
```

`X11ImageMagickBackend` wraps the existing xdotool/ImageMagick path unchanged.
It stays the default deliberately: a native XCB/XShm backend (step 3) has to be
benchmarked against a known quantity, so the current behaviour must remain
measurable rather than being rewritten at the same time.

### Why introduce the seam before the backends exist

Retrofitting an interface underneath a dozen call sites is the expensive part.
Adding it first means the Wayland/PipeWire backend, the replay backend, and the
`doctor` command each plug into a defined surface instead of each one
re-plumbing capture.

It also makes capture testable without a desktop. `RecordingBackend` in the
test suite runs with no X11, no ImageMagick, and no game, which is what lets
these paths run in CI at all.

### Shared frames

`GameInstance` owns a per-cycle frame cache keyed on `(box, mask)`.

- Several detectors reading one region in one cycle cost **one** capture.
- A masked view is derived from the cached raw frame, so asking for both a
  plain and a `bright`-masked view of the same region still costs one capture.
- `begin_cycle` keys the cache on an explicit cycle number rather than a
  timestamp, so every detector in a pass sees identical pixels and two rules
  cannot disagree about a frame that changed between them.
- A detected resize clears the cache, so stale geometry cannot be served.

### Not yet done

The rule evaluators still call `capture_array`/`ocr_cached` with a raw window
id. Migrating them onto `GameInstance` is step 10 of the Priority 0 order and
is what finally removes the direct ImageMagick coupling from profile code.


## Priority 0, step 2 — shared frame scheduler

`FrameScheduler` drives one sampling pass over a set of named regions. It owns
the cycle counter, resolves region names against the current window size,
serves every reader in a pass from one coherent set of frames, and records
per-region timing, reuse, and failure counts.

```text
sched = FrameScheduler(game, cfg["_regions"])
sched.begin()                    # open a pass
sched.prefetch(["chat_tail"])    # capture up front, collecting failures
sched.frame("backpack")          # cached within the pass
sched.report()                   # [(region, PASS/WARN/FAIL, reason)]
```

### Full-window capture was measured and rejected

The architecture note asks for the window to be treated as "a continuously
sampled data source, not a sequence of unrelated screenshot subprocesses", and
the README previously proposed cropping every region out of one immutable
full-window frame.

Measured at the real window size, that is much worse:

| strategy | cost |
|---|---|
| 4 cropped captures | **4.2 ms/cycle** |
| 1 full-window capture + numpy crops | 48.1 ms/cycle |

The thieving profile's four live regions total **0.69 MPx** against a
**7.90 MPx** window, so a full grab moves ~11x more pixels and the
encode/decode cost tracks that ratio exactly (11.4x slower).

Capture strategy therefore stays per-region. The scheduler's value is
coherence and accounting, not fewer pixels. This is worth remembering before
the native XCB/XShm backend lands in step 3: shared memory changes the
constant factor, not the pixel ratio.

### Failure isolation

`prefetch` collects failures rather than raising. A region caught mid-repaint
must not cost the pass its other regions - the same reasoning that already
isolates one broken rule from the rest of the loop.

### Frame health

The scheduler hashes each unmasked frame and counts how many consecutive
cycles a region's pixels stay identical. A region unchanged for 20+ cycles is
reported as `WARN ... frozen or occluded?`.

This matters because a frozen or blank capture does not look like an error to
a detector - it looks like confident, stable input. Health tracking is what
turns that into a visible degraded state, and it is the data `doctor` reports
in step 4.

### Not yet wired in

`cmd_watch` still runs its own cycle counter and calls `capture_array`
directly. Moving the live loop onto the scheduler belongs with step 10, when
rules stop owning their own capture/OCR calls.

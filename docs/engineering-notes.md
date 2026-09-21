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


## Priority 0, step 3 — native XCB capture backend

`X11XcbBackend` captures through XCB `GetImage` over a persistent X
connection, replacing a per-region ImageMagick subprocess.

### Measured against ImageMagick

Same 3840x2058 RuneScape window, same four live regions, 15 iterations each:

| region | ImageMagick | XCB | speedup |
|---|---|---|---|
| `chat_tail` | 308.9 ms | 11.3 ms | 27x |
| `backpack` | 178.5 ms | 3.5 ms | 51x |
| `session_timer` | 80.1 ms | 0.2 ms | 387x |
| `activity_icon` | 81.2 ms | 0.3 ms | 242x |
| **full cycle** | **648.6 ms** | **15.3 ms** | **42x** |

Through the real `FrameScheduler` the end-to-end figure is **639.2 ms ->
17.2 ms per cycle (37x)**.

The gap is dominated by fixed cost, not pixels: ImageMagick pays process
spawn, PPM encode, and PPM decode *per region*, which is why the smallest
regions show the largest speedup. This is the same effect measured in step 2
from the other direction - there, moving 11x more pixels cost 11x more time;
here, removing per-region fixed cost is worth far more than any pixel saving.

A 649 ms capture cycle against a 1.5 s poll interval left very little
headroom. 17 ms leaves the interval essentially free for detection work.

### Correctness

XCB output is **byte-identical** to the ImageMagick path on the same window:
mean absolute difference 0.000, maximum 0. X11 ZPixmap at depth 24 is BGRX on
little-endian hosts, so the channel reorder is `[2, 1, 0]`.

Byte-identical output is what makes the swap safe. Every threshold in every
profile - ring pixel counts, stack-digit counts, occupancy standard
deviations - was tuned against ImageMagick pixels and keeps its meaning.

### Robustness

- The X connection is opened lazily and reused; a per-capture connection
  would reintroduce the fixed cost this backend exists to remove.
- A protocol error (resize or unmap between geometry lookup and capture)
  drops the connection so the next attempt reconnects rather than reusing a
  poisoned one. Verified: a grab after an induced failure succeeds.
- Degenerate and out-of-bounds boxes raise `CaptureError` rather than
  crashing.
- Window discovery still uses xdotool. It runs once per acquire rather than
  per region, so it is not on the hot path, and reimplementing WM_CLASS
  matching over raw XCB would add risk for no measurable gain.
- `grab_file` delegates to ImageMagick when a `resize` is requested, because
  that is a one-off calibration path.

### Selection and fallback

`make_backend()` picks the default and falls back to ImageMagick when
python-xcffib is unavailable, so a host without it still runs - just slower.
An explicitly requested backend is returned even when unusable, so `doctor`
can report precisely why it will not work.

```bash
python3 watcher.py backends                        # what works here
python3 watcher.py --backend x11-imagemagick watch # force the old path
```


## Priority 0, step 4 — `doctor` diagnostics

`screen-watcher doctor` reports PASS/WARN/FAIL across the whole stack, with a
remediation line on anything that is not passing.

```bash
python3 watcher.py --config profiles/thieving.json doctor
```

It exits non-zero when any check fails, so it can gate a script.

### Why this command exists

Silent capture failure is indistinguishable from "nothing happened in game".
The watcher keeps polling, reports no alerts, and looks healthy. Every check
below corresponds to a confusion that has already cost time during
development: a rotated X cookie, a region calibrated at a different window
size, OCR reading noise, a frozen frame.

### What it checks

| area | catches |
|---|---|
| session | no `DISPLAY`, stale `XAUTHORITY` |
| tools | missing xdotool/tesseract/notify-send (FAIL), import/paplay (WARN) |
| backends | which capture paths work, and which is selected |
| profile | identity, enabled-rule count, unused regions, duplicate sounds |
| window | discovery failure, implausibly small geometry |
| regions | degenerate boxes, regions clamped by a smaller window |
| grids | an inventory grid larger than its own region |
| capture | per-region timing, and flat frames that indicate a blank capture |
| OCR | tesseract present, and whether a text region reads as tokens |
| outputs | notification binary, sound theme, `state/` writability |

### Two checks worth explaining

**Clamped regions.** `Region.resolve` clamps to the window rather than
returning an out-of-bounds box, so a region can never *report* itself as
outside the window. The only visible sign of drift is that the resolved size
differs from the configured size. An earlier version of this check tested
`x + w > W`, which was unreachable dead code.

**Numeric OCR.** Judging OCR health by word count alone reported a healthy
session timer (`00:32:19`) as noise, because it legitimately contains no
words. Digit groups count as readable tokens too.

### Findings from the first live run

Running it against the thieving profile immediately surfaced two real issues:

- `metrics_xp` and `orbs` are captured but used by no enabled rule;
- `coin_milestone` and `session_hour` shared `complete-media-burn`, so a coin
  milestone and an hour milestone were indistinguishable by ear - which
  defeats the purpose of per-rule sounds. `session_hour` now uses
  `completion-rotation`, and a regression test asserts the shipped profiles
  never reuse a tone.

### Not yet implemented from the specification

The full spec in `future-implementation-ideas.md` also asks for template/icon
anchor confidence, profile schema versioning, locale and UI-scale assumptions,
drift against a saved calibration, and `--profile` readiness requirements.
Those depend on calibration baselines and profile metadata that do not exist
yet.


## Priority 0, step 5 — interface readers

A reader turns one region's pixels into normalized events that any number of
rules can consume.

```text
ReaderRegistry
    .chat(region) -> ChatReader        # one reader per (kind, region)
        .read(scheduler) -> [ChatLine(text, key, cycle)]
```

### The duplication it removes

Five rule kinds (`ocr`, `activity`, `supply`, `loot`, `counter`) each carried
their own copy of: OCR the region, split lines, normalize a dedup key, skip
keys already seen, cap the seen-set at 400. The copies had drifted, and a fix
to one never reached the others.

Sharing matters beyond tidiness. When every rule kept its own seen-set, each
one independently consumed the same line, which meant N copies of the same
400-entry set and N chances for the capping logic to differ. The registry is
keyed on `(kind, region)` so two rules watching one chat region get the same
reader and therefore one authoritative dedup set.

### Fuzzy deduplication, and the regression it caused

Exact-key dedup is not sufficient. Tesseract mis-reads this font differently
on each pass, so one unchanging chat line yields a stream of distinct keys and
a rule can fire several times for one game event.

Adding a similarity check fixed that and immediately broke something worse:
repeated catches a second apart differ **only** by their timestamp, so fuzzy
matching collapsed them. That would silently break `fishing_stopped`,
`coin_milestone`, and every other rule that counts occurrences.

`_split_stamp` separates the leading `HHMMSS` from the wording. A different
in-game timestamp now means a different event regardless of similarity, while
same-stamp variants still collapse.

Measured on live chat over 6 cycles:

| measure | value |
|---|---|
| events emitted | 46 |
| distinct timestamps | **30** (real events) |
| same-stamp duplicates | 6 (true OCR variants) |
| variants suppressed | 11 |

An earlier measurement reported "67% near-duplicates" and looked alarming. It
was wrong: those were 12 genuine catches with 12 distinct timestamps. Judging
similarity without separating the clock measured real gameplay as noise.

### Not yet wired in

The rule evaluators still call `ocr_cached` directly. Moving them onto readers
is step 10. `InventoryReader` and `BuffBarReader` are not implemented.


## Priority 0, step 6 — layered OCR

RuneScape renders its UI numbers from a fixed sprite font, so a template
match beats general OCR on exactly the values that matter most: timers,
stack counts, resource numbers.

```text
read_numeric(frame)        sprite matching, or None when unsure
    -> ocr_numeric(...)    falls back to Tesseract on None
```

### Why this was worth doing

After step 3 made capture fast, Tesseract became the dominant cost in the
pipeline: **165.7 ms per read** on the session timer, roughly ten times the
entire four-region XCB capture cycle.

| path | cost |
|---|---|
| Tesseract | 165.5 ms |
| sprite matching | **0.066 ms** |
| speedup | **2516x** |

### Returning None is the point

`read_numeric` returns None rather than a guess when any glyph fails to
match confidently, and the caller falls back to Tesseract. A confident wrong
number is much worse than admitting the match failed - a misread timer would
fire an hour milestone at the wrong moment and look authoritative doing it.

Verified to return None for chat text, blank frames, and random noise.

### Single-sample templates were not good enough

The first template set was harvested one sample per digit. It read `0` as
`6` and agreed with Tesseract **0 times out of 10**.

The templates were not wrong - `0` genuinely scored highest, 0.821 against
0.684 - but a single sample sits too close to the decision boundary, so a
marginal frame flips or returns None.

The shipped templates are a pixel-wise majority vote over **564 live
samples** harvested by sampling the timer and cross-checking each frame
against Tesseract's reading. Self-match scores are 0.88-0.97 mean, minimum
0.718, which is why the threshold sits at 0.80 rather than higher.

After that change: **14/14 agreement with Tesseract at every threshold from
0.60 to 0.80**.

### Where the remaining time goes

`_eval_timer` now costs ~83 ms per evaluation, essentially all of it the
ImageMagick capture inside `capture_array`. The OCR portion is 0.066 ms.
Once step 10 moves rules onto `GameInstance`, the same evaluation becomes
~1 ms capture plus 0.07 ms OCR.

### Not yet applied

Stack-count reading still uses pixel counting rather than digit recognition.
The live backpack had only two visible stack counts while this was built,
which is not enough to validate a classifier - and the existing pixel-count
approach is already reliable for the question `item_gained` asks.


## Priority 0, step 10 — rules consume the capture stack

`set_active_game()` binds a `GameInstance` for the process. `capture_array`
and `ocr` route through it when the handle matches, so all fourteen call
sites in the rule evaluators inherit the selected backend without changing
a single signature.

`cmd_watch` now builds a backend via `make_backend(args.backend)`, acquires
the window through a `GameInstance`, binds it, and prints which backend is
in use. Resize and reacquire keep the instance in sync, so the frame cache
can never serve pixels from stale geometry or a dead window id.

### Measured result, and what it did not fix

| path | cycle cost |
|---|---|
| before (ImageMagick, unbound) | 1432.6 ms |
| after (XCB via GameInstance) | 1292.0 ms |
| speedup | **1.1x** |

That is far below the 37x the scheduler benchmark suggested, and profiling
says why:

```text
pack_nearly_full   inventory      6.6 ms
level_up           ocr         1132.4 ms   <-- 99% of the cycle
target_alerted     ocr            0.1 ms
...
TOTAL                          1141.3 ms
```

`level_up` is simply the first OCR rule to run, so it pays for the Tesseract
pass over `chat_tail` that the other four then reuse from cache. Breaking
that single call down:

| stage | cost |
|---|---|
| XCB capture | 17.8 ms |
| PNG encode to disk | 41.9 ms |
| **Tesseract** | **1121.1 ms** |

So capture is now ~1.5% of the cycle and Tesseract is ~98%. Step 10 removed
the capture bottleneck completely; it simply revealed a larger one behind
it.

### Why the earlier benchmarks were not wrong

Step 3 measured *capture* in isolation (42x) and step 6 measured *numeric
OCR* in isolation (2516x). Both hold. The end-to-end figure is small because
chat OCR - which neither step addressed - dominates everything else.

The sprite OCR from step 6 cannot help here: chat is variable-width
proportional text, not a fixed numeric readout. Making chat OCR cheaper is
a separate problem, and the realistic options are reducing the region,
running Tesseract less often than every poll, or a RuneScape-specific
chat-font reader of the kind Alt1 uses.


## Post-step-10 validation fixes

Validation of the integrated Priority-0 stack exposed several defects that unit
tests written during the individual steps had not yet covered.

The XCB backend now validates an empty window id and degenerate capture boxes
before importing the optional `xcffib` binding. This preserves deterministic
`CaptureError` behaviour on hosts that intentionally run without xcffib and
use the ImageMagick fallback. A live `watch` session also verifies that the
selected backend is actually usable before entering the polling loop. Automatic
selection may still fall back, while an explicitly requested unusable backend
fails immediately with its availability reason.

`doctor` now sends OCR through the exact `GameInstance` and backend it has
selected instead of silently falling back to the legacy ImageMagick path. The
frame scheduler also distinguishes a region that was never sampled from one
whose every capture attempt failed; the latter is a diagnostic failure rather
than a warning.

OCR encoding now reuses the `GameInstance` frame for the current cycle. This
closes the remaining coherence gap between pixel rules and OCR rules: if a
pixel detector has already sampled a region, Tesseract is fed those same pixels
instead of triggering a second capture.

The step-5 `ChatReader` is now part of the live rule path rather than dormant
infrastructure. OCR, activity, supply, loot, counter, and presence-corroboration
rules consume one deduplicated event list per region and cycle. The reader
caches that list so every rule sees the same events, uses timestamp-aware fuzzy
matching to suppress OCR variants without collapsing repeated game events, and
retains the current viewport when compacting history so visible scrollback
cannot become new again after the dedup set is capped.

Regression coverage was added for each of these cases, including clean hosts
without xcffib, all-failed frame health, explicit backend rejection, doctor
backend routing, same-cycle reader fan-out, history compaction, and OCR/pixel
frame reuse.

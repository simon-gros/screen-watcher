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

### OCR cost breakdown (measured 2026-09-22, tesseract 5.5.3)

Measured against the replay fixture at 3840x2058, so it reproduces with
no game running:

| region | size | median |
|---|---|---:|
| `chat_tail` | 580x660 | 570 ms |
| `vitals` | 800x70 | 136 ms |
| `gold_row` | 420x46 | 90 ms |
| `session_timer` | 210x50 | 78 ms |

Two findings that shape where optimisation is worth spending effort.

**Process startup is a hard floor of ~72 ms.** A blank 60x20 image costs
that much, which is why the three small regions all land near 80-140 ms
regardless of their size. Four regions per cycle therefore pay roughly
290 ms in subprocess overhead alone, before any recognition happens.

**Tesseract flags barely move the number.** `--oem 1` (LSTM only) saved
2%, `--psm 4` saved 13% but dropped six lines, and `--psm 11` produced
105 lines against 52. `--oem 0` (legacy) was 4.5x *slower*. None of
these is worth taking; the cost is startup plus the size of the image,
not engine configuration.

The optimisation that actually works is the one already shipped:
`ocr_scrolling` re-reads only the rows that moved. Measured end to end
on real captured pixels, a two-line scroll costs **123 ms against 582 ms**
for a full read - 4.7x - because it OCRs 68px instead of 660px. That is
now guarded by `test_scroll_optimiser_saves_real_time_on_real_pixels`,
which runs the real binary rather than a mock: every other scroll test
fakes `ocr_array`, so they prove the strip is smaller but never that
tesseract costs less.

**Unclaimed saving: batch the regions into one Tesseract call.** It
accepts an `imagelist` - a text file of image paths - and processes them
in a single process. Measured 865 ms for four separate calls against
632 ms batched, a 27% saving with byte-identical output (221 words both
ways). Not implemented, because the evaluators currently pull regions
independently and batching needs the watch loop to know every region
wanted this cycle before any of them is read. Worth doing when the
scheduler owns the cycle (foundation step 2).

An in-process binding (`tesserocr`) would remove the startup floor
entirely, but it is not installed here and adding a dependency is a
decision for the operator, not a silent one.

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

## Focus loss must not look like a closed game

Reported from live use: the game going in and out of focus killed the
watcher. Step 10 made this visible rather than causing it - the loop had
always exited, but it now runs long enough in real sessions to hit it.

The mechanism: three consecutive `CaptureError`s triggered reacquisition
through `find_window`, which passes `--onlyvisible`. A minimised client, or
one on another virtual desktop, returns nothing from that search - exactly
like a closed one. The loop could not tell the two apart, so it took the
destructive reading and called `sys.exit("game window gone")`, mid-session,
after roughly three seconds of alt-tab.

The fix is one extra question on the failure path. `find_window` grew a
`visible_only` flag; when the visible search comes back empty, ask again
including unmapped windows. A hit means hidden - wait. A miss means gone -
stop. The distinction costs one `xdotool` call, and only on a path that is
already failing.

### Why this became a class

`WindowTracker` exists because the interesting states are all awkward to
produce by hand: minimised, on another desktop, restarted under a new
window id, or caught mid-resize between `window_size` and the capture.
Inline in `cmd_watch`, none of them could be tested - which is why the
original bug shipped. The class has seven tests; the loop has none.

While hidden, the loop skips rule evaluation entirely. Every rule would
fail against an unmapped window, and the log would fill with misses. It
also backs off exponentially (1 to 30 cycles) between attempts, so a long
alt-tab does not spawn an `xdotool` pair every second for its duration.

### Two faults found by writing the tests

Both were pre-existing, and neither was what I set out to fix:

- `reacquire` adopted `size=None` when a window vanished between the find
  and the geometry call. Every later resize check then compared against
  nothing, reporting a phantom resize - and re-anchoring every region - on
  every single cycle. It now keeps the last real measurement.
- A window resize changes the UI scale, so digits render a pixel taller and
  shift inside their fixed cell. The second digit of each pair scored
  0.75-0.78 against the 13px templates, below the 0.80 threshold, silently
  disabling the step 6 sprite fast path and falling back to Tesseract.
  Glyphs are now cropped to their own ink and matched over +/-1 offsets.

The second one is the more instructive: it was a *silent* regression. The
fast path degraded to the slow path and everything kept working, just
2500x slower on numeric reads. Nothing failed, so nothing reported it.

### Verified against the live client

Minimising the real RS3 window (`steam_app_1343400`, 3840x2058) and
restoring it confirms the premise the whole fix rests on:

| state | `--onlyvisible` search | plain search | `reacquire` |
|---|---|---|---|
| mapped | `100663297` | `100663297` | `ok` |
| minimised | `None` | `100663297` | `hidden` |
| restored | `100663297` | `100663297` | `ok` |

The middle row is the bug: the visible search returns nothing for a window
that is still very much running, and that `None` used to mean `sys.exit`.
Restoring re-points cleanly at the same id and geometry.

### A trap worth recording

The first live check appeared to show the game was closed - both searches
returned `None`. The game was running. RS3 under Steam has WM_CLASS
`steam_app_1343400`, not `RuneScape`, which is exactly what the shipped
profiles configure and what I failed to use in the ad-hoc check. Verify
against the configured `window.wm_class`, never a guessed name, or a
working window looks like an absent one.

## Attacking the chat-OCR bottleneck

Step 10 left Tesseract at ~98% of the poll cycle. The obvious readings were
all wrong, and measuring beat each of them:

| idea | measured | verdict |
|---|---|---|
| binarise before OCR | 687 ms vs 766 ms | loses a line; not worth it |
| downscale to half | 316 ms | 0 lines read at threshold; useless |
| `OMP_THREAD_LIMIT=1` | 1181 -> 790 ms | **kept** |
| crop the region | 170 ms | rejected - see below |

The threading result is the surprising one. Tesseract's OpenMP parallelism
is a *net loss* here: on a 24-core host the default took 1181 ms against
790 ms pinned to one thread, because the region is small enough that thread
coordination costs more than the work it splits. `OMP_THREAD_LIMIT=4` was
no better than the default.

Cropping looked like the winner at 4.3x, but `chat_tail`'s `_note` already
explains why `h=650`: at `h=375` a line's median visible lifetime was 11s,
and rare messages scrolled past between OCR passes. Cropping would trade a
missed-alert bug for speed. Rejected.

### The actual insight

Looking at the captured region rather than the numbers: it holds **32 lines
of scrollback, and between two 1.5s polls only the newest one or two are
new**. The program was re-reading ~30 already-parsed lines every cycle,
forever.

So `ocr_scrolling` detects how far the text scrolled, OCRs only the newly
exposed strip, and stitches it onto the cached text. Live, over ten cycles:
**8 took the fast path, mean 297 ms against ~750 ms** - 2.5x, with no
region shrunk and no line given up.

Scroll offset comes from per-row ink counts, which is cheap (10 ms) and
needs no OCR. Chat scrolls in whole lines, so the offset lands exactly on
the line pitch: 42px, or 84px when two lines arrived at once.

### Two things the live test caught that the prototype did not

The prototype reported 3.7x and looked finished. Running it through the
real call path exposed both of these:

- **Unbounded growth.** Stitching climbed 31 -> 52 lines over six cycles,
  so a rule asking for "the last N lines" would eventually read text that
  had scrolled off screen. `_stitch` now caps its output.
- **Garbage lines.** The strip's top edge cuts a line through its glyphs,
  and Tesseract renders that as `g e e P e S e e oL ARl Ty presaetle`.
  Deduplication cannot catch it - it matches nothing, precisely because it
  is garbage - so it was being stitched in as real chat.

`is_readable` rejects those on the share of word-shaped tokens. Real lines
scored 0.50-0.91 and noise 0.08-0.31, no overlap. Mean token length, the
first thing I tried, was not enough: noise like `T14- 94321 ACE rrire
fanses nnry aeirdart` averages a respectable 3.54 characters.

### Acceptance by margin, not by threshold

The first scroll detector accepted a match when its error fell below 3.0.
That rejected valid scrolls: measured over live cycles, true matches scored
2.45-4.60 while their runners-up scored 5.35-7.62. The ranges overlap, so
no fixed cutoff works. What separates them is that the correct offset is
*distinctly* better than the next best, so acceptance now requires winning
by 1.5x - ignoring near neighbours of the winner, which align almost as
well by construction.

## The newest chat line was never readable

Found while measuring the above, and the more serious bug of the two.

`chat_tail` used `dy=-57`, which put the region boundary in the middle of a
text line. The bottom row was clipped to ~10px of its 17px height, so the
**newest message - the one every rule cares about most - was OCR'd as
garbage on every single cycle**:

```text
dy=-57   E 8 AN AT ATE ook St P TN s At -
dy=-36   [14:15:21] 455 coins have been added to your money pouch.
```

`-36` is the largest offset that still keeps the chat input bar out of the
region. Fixed in `config.json`, both profiles, and `config.fishing.json`,
whose drift from `profiles/fishing.json` the test suite caught immediately.

This had been present the whole time and no test could have found it: every
check asserted that OCR returned *lines*, and it did - the last one was
simply always wrong. It took looking at the actual pixels.

## Priority 0, step 7 — KWin read-only window discovery

Native Wayland deliberately stops applications from enumerating each other's
windows, so `xdotool` sees only what XWayland exposes. KWin's scripting API
is KDE's supported route to that metadata.

This session is genuinely Plasma 6 Wayland (`kwin_wayland` on the session
bus) with the game running as an XWayland client inside it - which is why
both discovery paths exist and why they disagree in useful ways.

### Getting an answer back out of KWin

KWin scripts cannot return a value to their caller. The documented options
are `console.info` into the journal, or `callDBus` out to a service you own.
This uses the latter: a temporary D-Bus service that the injected script
calls back into. Journal scraping needs log access, races with rotation, and
parses human-readable output - all avoidable.

The script is loaded under a PID-unique plugin name, because KWin keys
loaded scripts by name alone and concurrent runs would otherwise unload each
other's script.

### What KWin knows that X11 does not

| state | X11 `--onlyvisible` | KWin |
|---|---|---|
| mapped | window id | `minimized=false active=false` |
| minimised | **nothing** | `minimized=true` |
| focused | window id | `active=true` |

The middle row is the ambiguity behind the focus-loss bug fixed earlier: X11
returns the same silence for "minimised" and "closed". KWin states it
outright, so `window hidden (minimised or off-desktop), waiting` became
`window hidden (minimised), waiting` - verified live.

Focus is the other gap. X11 discovery cannot distinguish a window that has
keyboard focus from one merely mapped; KWin reports `active` directly, which
the acceptance criteria require `doctor` to surface.

### Logical coordinates are not pixels

KWin reports **logical** geometry. On this display that is 2194x1204 against
a physical 3840x2058 - a factor of 1.75. Feeding KWin geometry to XCB would
capture the wrong region entirely, so `KWinWindow.geometry` is deliberately
never consumed by capture code; `scale_to_pixels` converts explicitly.

Scale is derived from **width only**. Height disagrees by more than rounding
(2107 computed against 2058 measured) because KWin reports *frame* geometry
including decoration while the X11 client area excludes it. Averaging the
two axes would have quietly encoded that error into the scale factor.

### Deliberately advisory

Capture still runs through XWayland. KWin discovery adds state, not pixels,
so a session without it is fully supported: `kwin_available()` reports why,
`doctor` records a WARN rather than a FAIL, and `WindowTracker` keeps its
existing X11 wording instead of inventing a reason.

Every D-Bus import is function-local behind that check. Verified by importing
`watcher` with `dbus` and `gi` forced to fail: the module loads, discovery
reports `missing python-dbus`, and the X11 path is unaffected - which is what
CI and any non-KDE machine will do.

Cost is 14-37 ms per query against 38 ms for the `xdotool` pair it
supplements, and it runs only on the capture-failure path.

### A test that was quietly wrong

Adding this made seven existing `WindowTracker` tests start probing the real
session bus, because the constructor resolves KWin availability. They passed
locally and would have failed in CI. The `windows` fixture now stubs
`kwin_available`, which is the correct place - the fixture already exists to
isolate these tests from the desktop.

Strictly read-only throughout, per the architecture notes: discovery,
identity, geometry, focus, health. No input injection.

## Priority 0, step 8 — Wayland capture via portal + PipeWire

`tools/portal_poc.py` is a standalone probe, deliberately not wired into
`watcher.py`. Native Wayland has no `XGetImage` equivalent - a client cannot
capture a window it does not own - so the supported route is the XDG
ScreenCast portal, which returns a PipeWire node id to read frames from.

Environment: `xdg-desktop-portal-kde` 6.7.5, PipeWire 1.6.8,
`gst-plugin-pipewire`, portal ScreenCast **version 5**, source types 7
(monitor, window, virtual). Window capture is supported, which matters -
a monitor-only portal would have forced cropping and broken on occlusion.

### It works, on live game pixels

| result | value |
|---|---|
| frame | 3840x2107, RGB, uint8 |
| frame time | min 4.6, **median 16.8**, max 34.1 ms |
| restore token | issued |

The frame is full-colour real game content with legible chat text, captured
with no X11 involvement whatsoever.

The height, 2107, is the number predicted by the step 7 scale analysis:
KWin's *frame* geometry including decoration, not the 2058 X11 client area.
Two independent subsystems agreeing on an unusual number is good evidence
neither is being misread.

### The comparison inverts step 2's conclusion

| path | pixels | median |
|---|---|---|
| XCB, six regions | 0.69 MPx | 35.0 ms |
| portal, full frame | 8.09 MPx | **16.8 ms** |

The portal is ~2x faster while delivering ~12x the pixels. Step 2 measured
full-window capture as 11.4x *slower* than cropped regions and rejected it
on that basis; that finding was correct for XCB, where each capture is a
separate synchronous round trip whose cost scales with area. PipeWire is a
continuous stream - the compositor is compositing those pixels anyway, and
a frame is already waiting when asked for. Area stops being the dominant
term, so "capture once, crop in memory" becomes the cheaper design exactly
as the architecture target assumed.

### A benchmark that lied first

The initial per-region figures were 80-307 ms, which would have meant a
catastrophic regression against step 3's 15.3 ms. It was the benchmark:
varying `cycle` per region defeated the frame cache and forced repeated
backend work. Measured through `backend.grab_array` directly, one small
region costs **0.3 ms**. Step 3's numbers stand.

Worth recording because the wrong number was plausible and pointed at a
real-sounding conclusion. The tell was that it contradicted an earlier
careful measurement, which is a reason to re-measure rather than to write
up a regression.

### Consent is the design, not an obstacle

The portal shows a KDE dialog and cannot be bypassed - that user grant is
what makes arbitrary window capture safe on Wayland. KDE issued a
**restore token**, so a later run can reuse the grant without prompting,
which is what an unattended watcher would need. Any future adoption must
treat token absence as normal: the portal is entitled to decline.

### Not yet adopted, and why

This stays a probe. Switching the watcher over needs: a persistent session
held across the whole run rather than per capture, restore-token storage,
handling the user revoking the grant mid-session, and a `CaptureBackend`
whose model is "subscribe to a stream" rather than "request a rectangle" -
`grab_array(handle, box)` assumes the latter. That is a step 11 concern.

The value delivered here is the proof the acceptance criteria asked for:
a working portal/PipeWire path on KDE/CachyOS, benchmarked against the
current backend, with the pixels verified as real game content.

## Priority 0, step 9 — click-through Wayland overlay

`tools/overlay.py` plus `tools/overlay.qml`. A compositor-level overlay,
never an injection into RuneScape's GL/Vulkan context: Screen Watcher stays
an observer.

The mechanism is `wl-layer-shell` via KDE's `layer-shell-qt`, which the
roadmap's reference projects identify as the one approach that works on
native KWin Wayland where X11 and Electron overlay shims fail.

### No C++ needed

There is no Python binding for LayerShellQt. The package does ship a QML
module, `org.kde.layershell`, exposing the full API - `layer`, `anchors`,
`keyboardInteractivity`, `exclusionZone`, `margins`. So the surface is
configured in QML and driven from Python, with no build step.

The three properties that make this an overlay rather than a window:

| property | value | effect |
|---|---|---|
| `layer` | `LayerOverlay` | draws above normal windows, including the game |
| `keyboardInteractivity` | `None` | never takes focus |
| input region | empty | clicks pass through to the game |

`exclusionZone: -1` is the fourth: without it the compositor reserves screen
space for the surface and shrinks a maximised game window.

### Verified against the live game

A compositor-level screenshot through the step 8 portal shows the alert
cards rendering over the running client, with the minimap and game UI
visible through the translucent backgrounds. That screenshot was the only
way to prove it: XCB captures the *game's own surface*, so it cannot show an
overlay composited above it by definition.

Measured with KWin discovery from step 7:

| check | result |
|---|---|
| surface geometry | 380x165 at (1782, 16), matching the 16px margin |
| `active` while showing alerts | `false` - never steals focus |
| window mask | empty region - click-through |
| compact vs detailed | 66px vs 108px for the same two alerts |

Feeding it JSON on stdin is the intended integration path. A deliberately
malformed line is skipped rather than crashing the surface, confirmed by the
overlay still rendering exactly two cards afterwards.

### Qt does not fall back, it aborts

The first run dumped core with no message. The cause was an empty
`WAYLAND_DISPLAY`: this shell runs outside the desktop session, and Qt
responds to a missing compositor by aborting the interpreter rather than
raising something catchable.

`ensure_wayland_env` recovers `WAYLAND_DISPLAY` and `XDG_RUNTIME_DIR` from a
live `plasmashell`/`kwin_wayland` process, exactly as `ensure_x_env` already
does for X11, and it must run *before* Qt is imported. This is the same
class of problem twice now, and both times the symptom was a hard crash
rather than an error message.

A second failure worth recording: the QML refused to load with no diagnostic
from `QQmlApplicationEngine.load`. Loading the same file through
`QQmlComponent` and reading `component.errors()` named it immediately -
`implicitWidth` is read-only on `Column`. Use `QQmlComponent` when QML fails
silently.

### Not yet wired to the watcher

The overlay is a separate process reading stdin, which matches the reference
projects' "keep the application persistent" lesson - no process spawn per
alert. Connecting it to `notify()` is deliberately left out: that is a
product decision about whether alerts should appear on-screen at all, not a
Priority 0 primitive.

Wayland-only by nature. `tools/overlay.py` reports that plainly on an X11
session, and every Qt import is function-local so the module - and its
tests - load on a machine without PySide6.

## Replay backend, and the focus check

The last two open Priority 0 acceptance criteria.

### Replay

`ReplayBackend` implements the same `CaptureBackend` interface as the live
paths, so everything above it - scheduler, readers, OCR, rules, events,
notifications - runs unchanged against recorded frames. `watcher.py record`
captures them:

```bash
python watcher.py record --out state/recording --frames 20
SCREEN_WATCHER_REPLAY=state/recording python watcher.py --backend replay doctor
```

Recordings are **full-window**, not per-region, so they outlive the region
layout they were made under: a later calibration change can be tested
against frames captured before it existed.

The backend **holds on the last frame** rather than looping. Looping would
replay a one-off event - a level-up, a stun - forever, which is precisely
the false positive a replay harness must never manufacture.

### It immediately found a real bug

`doctor --backend replay` reported `ocr:chat_tail FAIL CaptureError: import
timed out`. ImageMagick was being handed the replay backend's fake window
id, because `_check_ocr` calls `ocr()` directly and `ocr()` only routes
through a `GameInstance` when `ACTIVE_GAME` is bound - which `doctor` never
did. So `doctor` had been reporting on the ImageMagick path regardless of
`--backend` for every check that captures directly.

Binding the instance fixes it, in a `try/finally` so the global does not
leak into tests. Replay now reports **36 pass, 0 fail**, and the live
backends are unaffected at 35 pass.

This is exactly what the acceptance criterion was for: a replay harness
exercising the real code found a coupling bug that live capture hid, because
with a real window id the fallback silently worked.

### An empty path is a real directory

`Path("")` is `"."`. An unset `SCREEN_WATCHER_REPLAY` therefore scanned the
working directory and reported "no frames in ." rather than saying it was
unconfigured. The directory is now `None` when unset, so the two states are
distinguishable.

### Focus

The criteria ask `doctor` to identify focus, which X11 discovery cannot
answer - a mapped window and a focused one are identical through `xdotool
search`. The step 7 KWin `active` flag supplies it:

```text
PASS  focus   game visible but not focused
```

Minimised is reported as a WARN rather than a PASS, since capture will fail
until the window is restored.

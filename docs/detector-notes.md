# Detector and tuning notes

This document preserves the measurements, experiments, and detector-specific
reasoning that previously lived in the main README. The README is intentionally
shorter and user-oriented; this file is the technical reference for tuning
rules and understanding why the current defaults exist.

## Inventory rules

The `inventory` rule supports two modes.

### Overflow mode

`mode: "overflow"` waits until the inventory has remained full for
`overflow_seconds`, then fires once.

A single frame at capacity is deliberately insufficient because normal banking
can briefly pass through a full state. A persistent full state is a stronger
signal that the activity has stopped and needs attention.

In one measured fishing setup, 12 cycles produced a mean cycle time of about
73 seconds and a mean peak occupancy of 27/28 slots. Replaying several hours of
occupancy history showed that a predictive warning could degenerate into one
alert per bank trip, while a 20-second overflow rule remained quiet when the
player banked normally.

That distinction is important: a useful safety alert should detect an
exception, not narrate every routine cycle.

### Lead mode

`mode: "lead"` estimates recent fill rate and predicts time to full. The
current implementation uses a least-squares trend over recent occupancy
history.

Example:

```text
Bank soon: 10 slots left, filling at 20.2/min - full in ~30s. Head to the bank.
```

`lead_seconds` should roughly correspond to the travel/banking time when
predictive warning is actually useful. `warn_free` is a raw-slot backstop
before enough history exists for a stable rate estimate.

Both modes re-arm after the inventory empties.

### One occupancy-log writer

If two inventory rules watch the same pack, only one should use
`log_occupancy: true`.

Two writers would record duplicate transitions in `state/occupancy.jsonl`,
distorting the cycle analysis performed by `watcher.py stats`.

### Grid calibration

Inventory counting requires a grid such as:

```json
"grid": {
  "x0": 17,
  "y0": 86,
  "cell_w": 61,
  "cell_h": 55,
  "cols": 5,
  "rows": 6
}
```

Coordinates are relative to the capture region.

The original fishing calibration found a large separation between empty and
occupied slot texture: empty slots were near a standard-deviation score of
roughly 1-2, while occupied slots were typically tens of units higher. A
threshold around 8 therefore sat well between the observed populations.

Frames reporting more occupied cells than the configured inventory capacity
are discarded rather than clamped. Bank, loot, level-up, and other overlays can
cover the backpack and make many cells appear occupied. Feeding such a frame
into fill-rate history would corrupt the prediction even if the final count
were clamped to capacity.

## Activity rules

An `activity` rule is the inverse of a conventional OCR event rule. It watches
for a recurring line and alerts when that evidence stops arriving.

For fishing, the motivating case was a depleted/moved fishing spot. The game
does not necessarily provide a direct "spot moved" event, but normal catch
messages stop.

A measured sample of 166 catch intervals produced approximately:

| percentile | interval |
|---|---:|
| median | 3.7 s |
| p90 | 8.0 s |
| p95 | 10.0 s |
| p99 | 19.7 s |
| max | 21.3 s |

A `stop_seconds` value around 30 seconds therefore left margin above the
observed maximum for that specific setup.

These figures are empirical calibration data, not universal RuneScape
constants. Other activities and profiles should be measured independently.

### Startup scrollback

The first OCR pass sees old chat history. Activity and OCR rules therefore
prime themselves from the existing screen instead of treating those lines as
new events.

Without that behaviour, starting Screen Watcher after a level-up or other
message could immediately produce a stale alert.

### Suppression patterns

Normal transitions can temporarily stop the recurring activity signal. For
example, a full inventory can halt fishing while the player is about to bank.

A `suppress_pattern` lets the activity rule recognize an explained stop and
remain quiet until genuine activity resumes.

This prevents two rules from reporting the same event.

## OCR rules

OCR lines are normalized before deduplication because Tesseract can return
minor punctuation differences for the same rendered line.

For example:

```text
[11:03:15] You catch a fish!
(11:03:15] You catch a fish!
```

Both normalize to the same lowercase alphanumeric key.

OCR patterns should tolerate common recognition errors when the game font or
background makes exact transcription unreliable.

## OCR caching

Multiple rules can watch the same text region. Screen Watcher caches OCR by
region/PSM for the current polling cycle so one region is not passed through
Tesseract repeatedly for every OCR-based rule.

This optimization was introduced after measurements showed Tesseract dominating
poll latency when several rules independently read the same chat region.

## Item-count rules

The existing fishing example identifies decorated fishing urns through colour
rather than attempting to OCR tiny stack-count digits.

Observed "blueness" (mean blue minus mean red on bright icon pixels) in one
calibration was approximately:

| item | blueness |
|---|---:|
| decorated fishing urn | +104, +105 |
| coins | -174 |
| fish | -10 to -62 |

A `min_blue` value around 40 separated those samples comfortably.

This detector is profile- and item-specific. A different item should be
calibrated from its own screenshots rather than assuming the urn threshold will
generalize.

### Overlay confirmation

An overlay can temporarily hide backpack items and create a false low count.
`confirm_seconds` requires a low/out reading to persist before an alert is
accepted.

The "out" state can optionally re-arm through `repeat_seconds` because
continuing without a required item represents an ongoing loss rather than a
one-shot event.

## Supply rules

The `supply` detector watches evidence across repeated bank/preset trips.

The motivating fishing profiles showed that a single preset shortfall line was
not sufficient evidence of genuine depletion: routine preset behaviour could
produce shortfall-like lines repeatedly.

The rule therefore tracks a streak. `warn_streak` consecutive failures imply
"low", while a separate `out_pattern` can identify stronger exhaustion
wording and alert immediately.

Restocking resets the streak automatically after a clean trip.

## Idle/change thresholds

Use:

```bash
python3 watcher.py --config profiles/<profile>.json probe
```

to measure the visual noise floor and active-state differences for each region.

One Metrics-panel calibration measured approximately:

- static: 0.001
- XP movement: 1.3-4.0

A threshold of 0.5 therefore sat well above idle noise and below the observed
signal for that setup.

Brightness masks should not be applied automatically. On an opaque panel, a
luminance cutoff can make anti-aliased text flicker more than the unmasked
image. Masks are more useful where a transparent panel allows the moving 3D
scene behind it to dominate raw frame differences.

## Alert-rate analysis

Use:

```bash
python3 watcher.py alerts
```

to identify which rule generated notifications and how frequently.

Use:

```bash
python3 watcher.py stats
```

for inventory-cycle analysis.

The practical tuning rule is simple: compare the alert interval with the
activity's natural cycle. If they match closely, the rule may be reporting a
routine transition rather than a useful exception.

## Profile-specific evidence

Fishing measurements in this document explain the current example defaults but
must not be copied blindly into thieving, combat, boss, quest, or other skill
profiles.

Every new profile should establish its own evidence:

- verified OCR wording;
- measured timing distributions;
- visual thresholds;
- overlay/failure cases;
- expected transitions;
- false-positive suppression;
- inventory or resource signatures.

The goal is explainable detection built from observed signals rather than
assumptions about unrelated activities.

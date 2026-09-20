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

## Thieving: Menaphos market guards

Measured against a live session in the Menaphos Merchant district, 2026-09-20.
Drop rates are from the RuneScape Wiki; everything else was observed directly.

### Verified chat wording

All four lines were read from live OCR, not assumed:

```text
You pick the target's pocket.
455 coins have been added to your money pouch.
Your camouflage outfit keeps you hidden and you steal additional loot.
Your pickpocket target becomes aware of your presence.
```

The camouflage line matters: while the outfit is worn it replaces the plain
success message on some pickpockets. A pattern matching only `You pick the
target's pocket` therefore under-reports activity.

The stun notice is rendered in yellow rather than the usual blue-grey.

### Region sizing

Both chat dimensions were wrong when carried over from fishing.

| dimension | was | now | reason |
|---|---|---|---|
| `w` | 500 | 600 | text ends near x=560; 500 clipped lines mid-word (`you steal add...`), hiding item names and the stun notice entirely |
| `h` | 375 | 650 | ~18 lines vs ~32 |

Chat turnover is the binding constraint. Pickpocketing produces about two chat
lines every two seconds, and at `h=375` a line's measured visible lifetime was:

| statistic | value |
|---|---|
| median | 11.0s |
| max | 23.8s |

Rare drops and the stun notice were scrolling off between clean OCR passes.
Doubling the visible line count roughly doubles the number of chances to read
each line before it disappears.

### OCR degrades under a busy scene

The chat panel is semi-transparent, so OCR quality varies with what is rendered
behind it. The same panel that reads perfectly in one frame can return noise in
the next:

```text
ACE rrire aasws Bamrs ardddart S 3% 17 Metes Bes it .
```

A luminance mask does **not** help here — measured 8 clean matches at
`thr=110` versus 9 with no mask at all, consistent with the fishing finding
that masking hurts on opaque panels.

Two mitigations are used instead:

1. Match distinctive fragments (`aware of your presence`) rather than whole
   sentences, since a short anchor survives mangling better.
2. Give rules multiple independent lines to corroborate against.

### `thieving_stopped` needed corroborating evidence

With `pattern` limited to the success message, this rule fired **4 times in 3
minutes** while pickpocketing was demonstrably continuing — the coin counter
advanced throughout. The cause was garbled OCR passes that saw no success line
and started the stop timer.

The pattern now also accepts the money-pouch line and the stun notice. Any of
the four proves activity, and the money-pouch line is the most reliably read:
it is short, high-contrast, and appears on roughly 76% of successes.

| setting | was | now |
|---|---|---|
| evidence lines | 1 | 4 |
| `stop_seconds` | 20 | 45 |
| false alerts / 3 min | 4 | **0** |

`stop_seconds=45` allows for a stun pausing gains for ~3s plus several
unreadable polls, while staying under the fishing profile's 60s idle rule.

### Loot detection excludes currency

Coins land in the money pouch on ~76% of successes, roughly every two seconds.
Announcing them would bury the drops worth seeing, so `loot_drop` ignores the
currency and success lines and matches only named items from the drop table:

| item | rarity |
|---|---|
| Coins (455) | 758/1000 — *ignored* |
| Extra fine sand | 100/1000 |
| Acadia wood spirit | 50/1000 |
| Waterskin (4) | 50/1000 |
| Large bladed adamant salvage | 20/1000 |
| Sealed clue scroll (hard) | 6/1000 |
| Potato cactus | 5/1000 |
| Menaphite gift offering (small) | 4/1000 |
| Vital spark | 2/1000 |
| Menaphite gift offering (medium) | 2/1000 |
| Sealed clue scroll (elite) | ~495/100000 |
| Sealed clue scroll (master) | 5/100000 |

Pattern alternatives are ordered longest-first so `(elite)` and `(master)` win
over the bare `sealed clue scroll`.

### Coin milestones must accumulate, not react

Coins go straight to the money pouch, which the backpack grid cannot observe —
chat is the only evidence, so the total has to be summed from it.

At 455 coins per success and a ~76% success rate, one million coins is roughly
2,900 pickpockets. Per-gain alerting would fire every two seconds; a simulated
2,900-pickpocket run produced exactly **one** milestone alert.

The running total is persisted to `state/counters.jsonl` and restored at
startup. A milestone of this size takes hours, so an in-memory total would be
silently rewound by any restart or by the game window briefly disappearing —
verified by restarting mid-grind and seeing `resuming from 96,460`.

### Stun mechanics, and two events that are not the same

Per the wiki: a stun lasts 5 ticks (~3s) and deals 200 + 3% of base life
points. Success is 100% only for the first ~43s (25 attempts) before decaying,
so stuns are expected rather than exceptional.

The failure path is **two lines, one second apart**:

```text
[21:03:25] You fail to steal from the target.
[21:03:26] You've been stunned.
```

Both are matched, because OCR frequently catches only one of them.

An earlier *guessed* pattern used `you (have been |are )?stunned` and
`you fail to pick`. It missed **both** real lines: the game uses the
contraction `You've`, and says `fail to steal from the target` rather than
`fail to pick`. Only the yellow notice matched, and that was luck. This is the
clearest argument in this document for reading live OCR before writing a
pattern.

These are now two rules, because the consequences differ:

| rule | line | effect | sound |
|---|---|---|---|
| `target_alerted` | `becomes aware of your presence` | warning; pickpocketing continues | `dialog-warning-auth` |
| `stunned` | `You've been stunned` / `You fail to steal from the target` | **halts pickpocketing** | `dialog-error-serious` |

The stun lines were also **removed from `thieving_stopped`'s activity
pattern**. Being stunned is not evidence of activity but of the opposite, and
counting it kept the stop timer alive through a genuine halt.

### Detecting drops from the backpack instead of chat

`item_gained` (`kind: stack`) watches stack-count digits in the inventory. The
inventory is the stronger signal: a dropped item is simply *there*, while chat
lines survive a measured median of 11s and OCR poorly over a busy 3D scene.

**Reading the digit value does not work.** Tesseract resolved only **5 of 9**
slots correctly even after tuning the crop, and bled adjacent cells together
into values like `1271` (slot showing `10` next to a slot showing `22`).

**Counting digit pixels does.** The detector counts yellow-green pixels
(`R>120, G>120, B<120`) in each slot's top-left corner. It never learns the
quantity — only that it moved — which is all a drop alert needs.

| condition | signature movement |
|---|---|
| unchanging stack | ±2 (anti-aliasing) |
| real quantity change | 7–24 |

`stack_tolerance: 3` sits clear of the noise floor.

#### Tooltips are the false-positive case

Hovering the backpack draws a tooltip over neighbouring cells. Observed effects:
occupancy oscillating 9/10/11, and stack signatures jumping and reverting within
a few seconds.

`confirm_seconds: 4` measures how long the **new value has persisted** after the
slots stop moving, and the final check compares the settled value against the
pre-change baseline. A real drop holds; a tooltip reverts and is rejected.

An earlier version measured how long the values were *still changing*, which
inverted the logic and suppressed every real drop.

#### `new_slot_only` — why routine drops are silent

The first live run fired **5 alerts in 2 minutes, median gap 24s**. Those were
not false positives. At ~2s per pickpocket and a 10% extra-fine-sand rate, an
existing stack genuinely grows about every 20s — the measured 24s matches almost
exactly.

Correct but useless: common drops bury rare ones. With `new_slot_only: true`
only an item landing in a previously **empty** slot is announced, which is what
a first-of-its-kind drop looks like. Top-ups of a stack already carried are
tracked silently.

The chat-based `loot_drop` rule is retained but disabled. It can name the exact
item, which `item_gained` cannot — re-enable it if the item name matters more
than reliable detection.

### Session milestones from the in-game timer

`session_hour` (`kind: timer`) reads the elapsed-time counter at the bottom of
the Metrics panel — the `00:32:09` readout between the settings/reset buttons
and pause.

**Why the game's timer rather than wall-clock time in the watcher:**

- it is the number the player is already reading;
- it pauses when they pause it;
- it survives a watcher restart, because the game owns it.

Tracking elapsed time inside the watcher would drift from all three.

OCR is reliable here — measured **10/10 clean parses over 30s**, ticking
monotonically — because the readout is large, high-contrast and on an opaque
panel. Restricting the whitelist to `0123456789:` with `--psm 7` removes the
remaining ambiguity.

`parse_timer` anchors the minute and second fields on `[0-5]\d`, so a garbled
frame such as `00:82:09` is rejected rather than silently becoming a bogus
elapsed time. A reading that jumps backwards by more than 5s is treated as a
timer reset and rewinds the milestone counter, so a fresh session starts clean.

Milestones fire per `step` (3600s), so crossing an hour alerts once rather than
on every poll afterwards.

#### The Metrics panel carries other usable numbers

The same panel shows `Gain`, `Drops` and `GP/h`. During testing it read
`Gain 872,581` while the chat-derived `coin_milestone` counter stood at
527,345 — the panel is authoritative and counts from the session start, whereas
the chat counter only sees lines while the watcher is running. A future
milestone rule could read `Gain` directly instead of summing chat.

### Activity icon presence — the strongest stop signal

`activity_icon_gone` (`kind: presence`) watches the skill icon RS3 shows at the
top-centre of the screen while XP is accruing: a dark disc with an orange
progress ring. Its presence *is* the activity; it vanishes when the skill stops.

**This is a better stop detector than chat.** A chat rule must infer a stop from
the *absence* of messages, which fails exactly when OCR degrades over a busy 3D
scene — and `thieving_stopped` needed four corroborating message patterns to
stop false-firing (it fired 4 times in 3 minutes before that fix). Reading a
visual state needs none of that.

**Separation is absolute:**

| state | ring pixels |
|---|---|
| icon present | **328–331**, constant across 64 samples |
| surrounding scenery | 0–113 |

The ring's orange simply does not occur in the environment here, so
`present_above: 200` sits clear of both populations with no tuning required.
The count was also flat across `dx` −20…+10, giving the region ~30px of framing
tolerance.

`absent_seconds: 12` debounces the icon's own fade animation and the occasional
dropped frame. Threshold selection was trivial; debouncing is the real work.

The chat-based `thieving_stopped` rule is retained but disabled. Re-enable it
for a skill that has no activity icon.

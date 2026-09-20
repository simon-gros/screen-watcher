# Screen Watcher

Watches regions of a game window and notifies you when something needs attention.

**It only reads the screen. It never sends input.** No `ydotool`, no synthetic
clicks, nothing that touches the game. That is deliberate and is what keeps it
on the right side of Jagex's rules — the same line Alt1 sits on.

Currently configured for the RS3 NXT client (`WM_CLASS=steam_app_1343400`), but
nothing is RS-specific except `config.json`.

## Requirements

Already present on this machine:

- `xdotool` — window lookup (works because RS3 runs under XWayland)
- ImageMagick `import` — window capture
- `tesseract` — chat OCR
- Python 3 with `PIL` and `numpy`
- `notify-send` — desktop notifications

Install the system tools and Python libraries on a Debian/Ubuntu system with:

```bash
sudo apt install xdotool imagemagick tesseract-ocr libnotify-bin python3-pil python3-numpy
```

The watcher also requires access to the active XWayland session. See
[Running it](#running-it) below when launching it from a terminal, service, or
automation process.

## Configuration

The watcher always loads `config.json` from the directory containing
`watcher.py`; it does not accept a configuration path as a command-line
option. `config.json` is the active thieving configuration in this repository.
`config.fishing.json` is the fishing profile kept as a reference/backup.

To switch profiles, stop the watcher, preserve the current file, and copy the
desired profile into place:

```bash
cp config.json config.thieving.local.json
cp config.fishing.json config.json
```

Do not edit a profile while `watch` is running. Region coordinates are relative
to the detected game window, but rule changes are only read when the process
starts. Keep local experiments in an untracked file or restore `config.json`
before committing.

The top-level settings are:

| setting | purpose |
|---|---|
| `window.wm_class` | X11/ XWayland class used to find the game window |
| `interval` | seconds between watch-loop polls |
| `regions` | anchored capture rectangles and optional inventory grids |
| `rules` | enabled detectors, thresholds, cooldowns, and notification text |

Run `python3 watcher.py regions` after changing anchors or grids to verify the
resolved rectangles against the current window size.

## Usage

```bash
cd ~/.openclaw/workspace/screen-watcher

python3 watcher.py regions      # show regions resolved at the current window size
python3 watcher.py calibrate    # scaled full-window shot for picking coordinates
python3 watcher.py shot chat_tail   # capture one region to check framing
python3 watcher.py probe        # per-region frame-to-frame diff, for thresholds
python3 watcher.py inv          # live inventory slot-change feed
python3 watcher.py stats        # fill/bank cycle analysis from occupancy data
python3 watcher.py watch        # the actual loop
python3 watcher.py alerts       # per-rule alert rates - which rule is beeping
python3 watcher.py status       # report the watcher's process status
python3 watcher.py pause        # pause the running watcher
python3 watcher.py resume       # resume a paused watcher
```

Run it in the background so it survives the terminal:

```bash
nohup python3 watcher.py watch >> state/watch.log 2>&1 &
```

Use `python3 watcher.py --help` or `python3 watcher.py <command> --help` for
the complete option list. `shot` accepts a configured region name and can write
to a custom path with `--out`; `probe`, `inv`, and `alerts` also expose timing,
duration, or filtering options.

Only one watcher may run at a time. A second instance refuses to start, because
two watchers double every alert — which looks exactly like a mistuned rule and
sends you tuning thresholds that were never the problem. The pid lives in
`state/watcher.pid`; a stale file from a crash is reclaimed automatically.
`pause` and `resume` verify the recorded process command line and Linux process
start time before signaling it, so a reused PID cannot target an unrelated
process.

## Runtime files

The watcher creates or appends these files under `state/`:

| path | contents |
|---|---|
| `watch.log` | stdout/stderr when using the background command above |
| `alerts.jsonl` | one JSON record for each notification |
| `occupancy.jsonl` | inventory transitions used by `stats` |
| `watcher.pid` | temporary singleton lock, removed on normal exit |

Diagnostic captures such as OCR screenshots and temporary PPM files may also
appear in `state/`; they are local runtime artifacts and should not be treated
as source files. Do not commit new runtime captures or logs unless they are
deliberately being preserved as test fixtures; copy them separately when
transferring a working setup to another machine.

## Diagnosing "it beeps too much"

`watcher.py alerts` attributes every fired alert to its rule:

```
15 alerts over 4.57h  (3.3/hour)

rule                  count  per hour  median gap
pack_nearly_full         15       3.3         82s
```

Without this the only way to answer "which rule is beeping" is to re-run the
watcher live and hope the noise reproduces. Every alert is appended to
`state/alerts.jsonl` by `notify()`, which is the single choke point all rules
fire through.

Check the median gap against the grind's natural cycle. A rule whose gap matches
the cycle length is firing once per cycle — normal for an advance warning, and a
bug for anything else.

## How regions work

Regions are **anchored to a window corner**, not stored as absolute pixels:

```json
"chat_tail": { "anchor": "bottom-left", "dx": 5, "dy": -57, "w": 500, "h": 375 }
```

RS3 pins its panels to window edges at a fixed size rather than scaling them,
so a resize moves the chat box but does not resize it. Absolute coordinates
silently break the moment you resize the window — the watcher would keep
running and just never fire again. Anchors survive it, and the loop re-resolves
every region when it detects a size change.

Anchors: `top-left`, `top-right`, `bottom-left`, `bottom-right`, `top-center`,
`bottom-center`, `center`. Negative `dx`/`dy` measure inward from a right or
bottom edge.

## Rule types

| kind | fires when | use for |
|---|---|---|
| `inventory` | pack stays full (`overflow`) or fill projected (`lead`) | **bank before catches are lost** |
| `activity` | a recurring chat line **stops** arriving for `stop_seconds` | **fishing spot depleted and moved** |
| `item_count` | carried count of an item falls to `warn_below` / `out_below` | **"I only have one urn left"** |
| `supply` | bank comes up short `warn_streak` trips running, or item gone | **bank running low on bait or urns** |
| `idle` | region unchanged for `idle_seconds` | grind stopped, XP not ticking |
| `change` | region changed by more than `threshold` | something appeared |
| `ocr` | a **new** chat line matches `pattern` | out of supplies, level up |

`cooldown` (seconds) throttles repeat alerts per rule.

### The `inventory` rule

Two modes, set with `mode`. **Which one is right depends entirely on how long
your cycle is**, and getting it wrong is the difference between a useful alert
and 50 beeps an hour.

#### `mode: "overflow"` — fire only when catches are actually being lost

Waits until the pack has been *full* for `overflow_seconds`, then fires once.

```
Pack full: Pack full for 21s - activity has stopped. Check the game.
```

A full pack does not waste catches — RS3 stops fishing outright — so the cost is
**idle time**, not lost fish. Every second past full is a second not spent
fishing.

A single frame at capacity is not enough, because that happens on every normal
trip in the moment before banking. Requiring the state to persist is what
separates "about to bank" from "AFK".

This is the right mode for a short cycle. Measured here over 12 cycles with
`watcher.py stats`: mean cycle **73s**, mean peak **27.0 / 28 slots**. Replaying
4.23 hours of real occupancy log through both modes:

| mode | alerts | rate |
|---|---|---|
| `lead` (`lead_seconds: 10`) | 10 | ~50/hour |
| `overflow` (`overflow_seconds: 20`) | 0 | 0/hour |

Zero is the correct answer. Peak occupancy averaged 27.0/28, so the pack was
*already* being banked in time on every trip — the predictive alert was firing
once per bank run to announce something already handled.

#### Running both together

`pack_nearly_full` (lead) and `pack_filling` (overflow) are configured as a
pair: one warns you the pack is about to fill, the other catches the case where
you missed that warning and went idle. Replayed over 4.57h of real occupancy
log:

| rule | alerts | rate |
|---|---|---|
| `pack_nearly_full` (lead, 15s) | 15 | 3.3/h |
| `pack_filling` (overflow, 20s) | 0 | 0/h |
| **double-fires within 45s** | **0** | — |

The overflow rule reads zero because the lead warning works — you bank before
going idle. It only speaks up when the first alert was missed.

**Only one inventory rule may set `log_occupancy: true`.** Two writers double
every transition in `state/occupancy.jsonl`, which silently corrupts
`watcher.py stats`: duplicate rows inflate the cycle count and halve the
apparent fill rate.

#### `mode: "lead"` — warn ahead of time from the projected fill rate

Fits a least-squares fill rate over the last 180s and fires when projected
time-to-full drops below `lead_seconds`.

```
Bank soon: 10 slots left, filling at 20.2/min - full in ~30s. Head to the bank.
```

Set `lead_seconds` to however long the walk to the bank takes. This only earns
its noise when transit is long relative to the fill — a slow grind like mining
or woodcutting. On a fast one it degenerates into one alert per cycle.

`warn_free` is a backstop on raw free-slot count, for the window before the rate
estimate has its 4 samples. Expect it to fire late; it is the fallback, not the
feature.

Both modes re-arm automatically when the pack empties.

**Lesson worth keeping:** an alert that fires every cycle is not a safety net,
it is noise with extra steps. Check it against `watcher.py stats` — if mean peak
is already close to capacity, the grind does not need warning, and the rule
should only speak up when something actually goes wrong.

Slot counting needs a `grid` on the region:

```json
"grid": { "x0": 17, "y0": 86, "cell_w": 61, "cell_h": 55, "cols": 5, "rows": 6 }
```

Coordinates are relative to the region's top-left. Verify a change with
`python3 watcher.py shot backpack` and a hand count. An empty slot is flat
background (std 1.2-1.9); an item icon adds colour spread (std 28-48), so the
threshold at 8.0 sits far from both.

Capacity is 28, confirmed by watching the count plateau there. The grid is
5x6 = 30 cells because RS3 lays the 28 slots out 5 wide; the two extra cells
read empty.

**Frames reading above capacity are discarded, not clamped.** A bank, loot or
level-up interface drawn over the backpack makes every covered cell read as
occupied. Clamping `free` to zero hides the bad count but still feeds it to the
fill-rate history, which corrupts the ETA for minutes afterwards. Exceeding
capacity is the reliable tell for an overlay, so `_eval_inventory` drops those
frames outright. Measured separation on Menaphos fish is wide — occupied cells
score 28-45, empty ones 0.8-2.4 — so a genuine reading is never ambiguous;
anything above 28 is an overlay, not a full pack.

### Picking a threshold

Run `probe` and watch the numbers. Measured on the Metrics panel:

- static: **0.001**
- XP ticking: **1.3 – 4.0**

So `threshold: 0.5` sits ~500x above the noise floor and ~8x below real signal.

Do **not** add a brightness mask to an opaque panel — it was measured as worse
(floor 0.053 vs 0.001) because antialiasing flickers across the luminance
cutoff. The mask is only worth it on genuinely transparent overlays, where the
3D world bleeds through and creates a ~1.0 diff floor.

## Current rules

- **pack_nearly_full** — projected full in <15s → *"Bank soon"* (advance warning)
- **pack_filling** — pack full for 20s+ → *"Pack full"* (overflow backstop)
- **fishing_stopped** — no catch message for 30s → *"Fishing stopped"*
- **impling** — chat matches an impling spawn (`A creature is discovered while
  skilling`, confirmed against live chat)
- ~~**xp_stalled**~~ — disabled, superseded by `fishing_stopped`
- **urn_full** — fishing urn reaches three-quarters or full
- **bait_supply** — bait low (4 short trips) or out → *"Bait low"* / *"Out of bait"*
- **urn_supply** — empty urns low (4 short trips) or out → *"Urns low"* / *"Out of urns"*
- **out_of_supplies** — chat matches out-of-bait / full-inventory phrasing
- **level_up** — chat matches a level-up message

Every rule has a distinct sound, so alerts are identifiable without looking at
the screen. That is the point of `sound`: KDE's own notification blip fires for
every `notify-send` and tells you nothing about *which* rule tripped.

### The `activity` rule — detecting a depleted fishing spot

RS3 does not announce that a fishing spot has moved. There is no "the spot
disappears" message to match; the shoal simply stops producing and the only
evidence is that `You catch a ...` stops arriving.

So this rule is the **inverse of an `ocr` rule**: it fires on the *absence* of a
message rather than its presence.

```
Fishing stopped: No matching activity for 34s. Check the game.
```

**Threshold comes from measurement, not guesswork.** 166 real catch intervals
pulled from the occupancy log:

| percentile | interval |
|---|---|
| median | 3.7s |
| p90 | 8.0s |
| p95 | 10.0s |
| p99 | 19.7s |
| max | 21.3s |

`stop_seconds: 30` clears the observed maximum by ~9s, so ordinary bad luck at
the spot cannot false-fire.

The pattern also matches `You cast out your line...` and `You attempt to catch
a fish.`, so a slow first catch immediately after recasting does not count as
stopped.

**It only alerts if it saw activity first.** Starting the watcher while docked
at a bank would otherwise immediately claim fishing had stopped. The first OCR
pass primes the clock from scrollback without treating it as a live catch.

#### `suppress_pattern` — why a full pack must not count as "stopped"

A full pack does **not** waste catches. RS3 halts fishing outright:

```
You can't carry any more fish.
You do not have any space in your inventory to catch this item.
```

So catch messages stop on **every bank trip**, not just when a spot moves.
Without suppression this rule fires once per cycle — at the measured 73s cycle
that is ~50 alerts an hour, exactly the noise `pack_filling` was retuned to
avoid. Matching `suppress_pattern` disarms the rule: the stop is explained, and
the player already knows.

Disarming (rather than resetting the activity clock) means resuming still
requires a real catch to re-arm, so a genuine spot move immediately after
banking is still caught.

**Division of labour** — one alert per event, never two:

| situation | fires |
|---|---|
| spot depletes mid-fill | `fishing_stopped` |
| pack fills, banked promptly | *nothing* |
| pack fills, player AFK | `pack_filling` only |

#### Why this replaced `xp_stalled`

`xp_stalled` watched the Metrics panel for 60s of flat XP and detected the same
event — slower, and less reliably. Metrics XP stalls identically whether the
spot moved, the client was minimised, or the panel was closed. Two rules firing
for one spot move is just double beeping, so `xp_stalled` is now disabled. It is
still worth re-enabling for a grind with no per-action chat message, where XP is
the only evidence of progress.

### The `item_count` rule — urns carried right now

```
Urns low: 1 urn left. Restock on the next bank trip.
Urns low: No urns in the pack - urn XP is being lost. Grab more.
```

**This is a different question from `supply`.** `supply` watches the *bank*
coming up short across trips; `item_count` watches what is in the backpack right
now. They are independent — the bank can be well stocked while the pack is down
to its last urn, which is exactly the case that prompted this rule.

**Counted by colour, not by stack digits.** Reading the stack number was tried
and abandoned: the digits are small, anti-aliased and drawn over the icon, and
OCR of a stack that never changed was measured returning `6`, then `13`, then
`1`, then blank.

Blueness (mean B − mean R of the icon's bright pixels) separates cleanly:

| item | blueness |
|---|---|
| decorated fishing urn | **+104, +105** |
| coins | −174 |
| fish (all three types) | −10 … −62 |

`min_blue: 40` sits ~60 away from both populations. Verified against real
captures at every threshold from 20 to 80, and against known backpacks holding
2 urns, 1 urn and 0 urns — correct in all cases.

**`confirm_seconds` guards against overlays.** A bank or loot interface hides
backpack slots, which reads as a sudden drop to zero and would fire a false
"out of urns". A real drain persists; an overlay clears within a frame or two,
so the low reading must hold for 8s before it counts.

To watch a different item, measure its blueness with a captured backpack and set
`min_blue` between its value and everything else on the grind.

### The `supply` rule — bank running low on bait and urns

Four alerts, two consumables: **low** and **out**, for bait and for empty urns.

```
Bait low:  Fishing bait low - the bank has come up short 4 trips running.
Urns low:  Decorated fishing urns low - short 4 trips running. Restock soon.
Bait out:  Out of fishing bait - fishing will stop. Restock now.
Urns out:  Out of empty urns - urn XP is being lost. Restock now.
```

**Why not just count the stack?** RS3 never states how much bait or how many
urns remain, and reading the backpack stack digits does not work: they are
small, anti-aliased and drawn over the icon. OCR of a stack that never changed
was measured flickering between `6`, `13` and `1`. Alerts built on that number
would be worse than no alerts.

**Why not just match the shortfall message?** Because it fires on *every* bank
trip. Measured over 4 consecutive preset loads:

| preset loads | bait shortfalls | urn shortfalls |
|---|---|---|
| 4 | 5 | 6 |

The preset routinely requests more than the bank holds, so a single shortfall
carries no information. Matching it directly was tried in an earlier version and
fired once per bank trip.

**The signal is the streak.** A stocked bank satisfies the preset eventually; a
bank running dry fails trip after trip. `warn_streak: 4` consecutive failures
means low. The streak resets the moment a trip loads cleanly, so restocking
silences the rule with no manual action.

**"Out" is a different message.** The loader reporting it could not supply the
item *at all* — no `(empty)` qualifier, no `was loaded instead` fallback — means
exhausted, and fires immediately without waiting for a streak. Verified the
`out_pattern` matches **zero** of the seven routine lines observed every trip,
while still matching real exhaustion wording.

Patterns are deliberately loose (`quantity requ.{0,3}red`) because OCR mangles
this font: observed variants include `quantity reguired`, `Fishing|`,
`riot-be found` and `NEEH ot b found`.

### The `urn_full` rule

A fishing urn stops collecting once full, and every catch after that loses the
urn XP until it is teleported. RS3 announces four stages — one-quarter, half,
three-quarters, full — and the pattern deliberately matches only the last two.
Alerting at one-quarter would fire constantly for no decision.

## Running it

The watcher needs X access. RS3 runs under XWayland, so a shell without a
desktop session (a bare terminal, an agent, a systemd unit) has no `DISPLAY`
and `xdotool` fails with *"DISPLAY environment variable is empty"*:

```bash
export DISPLAY=:0
export XAUTHORITY=/run/user/1000/xauth_EJtUsQ   # varies per session
python3 watcher.py watch
```

The auth file is not at the traditional `~/.Xauthority` under KWin. Read the
current one out of a running session process:

```bash
tr '\0' '\n' < /proc/$(pgrep -u $USER plasmashell | head -1)/environ \
  | grep -E '^(DISPLAY|XAUTHORITY)='
```

## Implementation notes

**Cooldown swallowed the first alert.** `_last_fired` defaults to `0.0`, and
`ready()` compared `now - 0.0` against the cooldown — i.e. against the epoch. A
rule with `cooldown: 300` therefore stayed muted until 5 minutes after the
watcher started, silently dropping its first alert. For a supply rule that is
the alert that matters most. `ready()` now treats `0.0` as "never fired".

**Only one watcher may run.** Two instances double every alert, which is
indistinguishable from a mistuned rule and sends you tuning thresholds that were
never the problem. `state/watcher.pid` enforces it; stale files are reclaimed.

**A truncated capture used to kill the watcher.** `import` can leave a short PPM
when the window is repainting, and PIL raises `ValueError: not enough image
data`. The watch loop only caught `CaptureError`, so that escaped and took the
process down mid-session — which looks exactly like "it was never running".
`capture_array` now re-raises it as `CaptureError`, and the loop isolates each
rule so one bad rule cannot stop the rest.

Two bugs found during earlier testing, both fixed, both worth remembering:

1. **Startup scrollback.** The chat tail is full of history when the watcher
   starts. The first OCR pass records what is on screen *without* alerting,
   so it cannot fire on an event that predates the watcher.

2. **OCR is not deterministic.** The same chat line comes back as `[11:03:15]`
   on one pass and `(11:03:15]` on the next, which made dedup treat old lines
   as new. Dedup keys are normalised to lowercase alphanumerics
   (`norm_line`) so the wobble collapses.

Capture cost matters for poll rate. Measured on a 4K window:

| method | time |
|---|---|
| full frame → PNG | 1.46 s |
| full frame → PPM | 0.10 s |
| cropped region → PNG | 0.11 s |

PNG *encoding* is the bottleneck, not capture. Region captures go via PPM
(`capture_array`) to skip it entirely.

`capture()` refuses an empty window id — `import -window ""` drops into
interactive crosshair mode and hangs waiting for a click.

## Known gaps

- Notifications are desktop-only. Phone push needs an OpenClaw channel
  connected; `notify()` is the single place to extend.
- `backpack` and `orbs` regions are defined and framed but no rule uses them
  yet. Inventory-full detection would go on `backpack`.
- Chat must be visible and on the All Chat tab.

## Continuous integration

`.github/workflows/ci.yml` runs on pushes and pull requests. It installs
Flake8 on Python 3.12 and checks `watcher.py`, while excluding the existing
project formatting conventions (`E226`, `E501`, `E702`, `W503`, and `W504`).
The workflow does not launch the watcher because CI has no game window, X
session, or desktop notification service.

## Development and tests

Create the isolated development environment and install the Python
dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

Run the smoke tests and the same lint policy used by CI:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m flake8 watcher.py tests --ignore=E226,E501,E702,W503,W504
```

The tests exercise configuration-independent logic only. Commands that capture
the screen still require the external tools and active XWayland environment
listed above.

Configuration is validated before the watcher searches for a game window.
Validation checks required sections, region anchors and grids, rule kinds and
references, numeric thresholds, and regular-expression syntax. Rule evaluation
returns an `Alert` value; desktop notification, sound playback, and alert-log
writing happen afterward in the watch loop. This keeps detector decisions
testable without a live desktop notification service.

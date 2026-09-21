# Practical validation plan

Tracking issue: [#13 — Immediate priority: practical validation of the current Screen Watcher](https://github.com/simon-gros/screen-watcher/issues/13)

This document defines how the **currently implemented** Screen Watcher is
validated before development expands into more architecture, platforms, GUIs,
readers, or profiles.

The objective is not to prove that every roadmap idea is sound. The objective
is to discover whether the software that already exists behaves correctly,
reliably, and predictably on the real desktop.

## Priority rule

A reproducible defect in existing functionality has higher implementation
priority than a new roadmap feature of comparable importance.

Examples:

- a false "activity stopped" alert outranks adding a new skill profile;
- a lost/recreated RuneScape window that the watcher cannot reacquire outranks
  overlay work;
- corrupted or cross-profile history outranks analytics expansion;
- a backend-specific capture regression outranks an additional capture backend;
- excessive CPU/memory growth in the current watcher outranks GUI polish.

This does not mean all future work stops indefinitely. It means observed defects
are triaged first and are not allowed to disappear beneath theoretical plans.

## Evidence levels

### Level A — automated

Required on every relevant change:

- compile;
- unit/regression tests;
- lint;
- supported Python-version matrix;
- deterministic fake/replay tests where available.

Automated green status is necessary but does not imply real-world correctness.

### Level B — desktop smoke

5–15 minutes on the real development machine after runtime-affecting changes.

Confirm:

- process starts cleanly;
- correct backend selected;
- game window acquired;
- current profile loads;
- captures are nonblank and correctly aligned;
- OCR returns plausible text;
- expected notifications can fire;
- stop/pause/status/process lifecycle works;
- no immediate repeated errors or runaway resource use.

### Level C — activity session

At least 1–2 hours of ordinary affected gameplay.

Measure:

- expected versus observed alerts;
- false positives;
- false negatives;
- capture misses;
- OCR failures;
- window reacquisition;
- rule reset/re-arm behavior;
- persistence after restart;
- CPU/memory and latency at representative points.

### Level D — soak

Multi-hour or overnight operation after significant changes to:

- capture backend;
- scheduler/cache;
- OCR;
- persistence;
- window lifecycle/reacquisition;
- notification loop;
- state/history logging.

Soak validation looks for failures that short sessions hide:

- file/log growth;
- memory leaks;
- progressively slower OCR;
- stuck readers;
- stale cached frames;
- event dedup drift;
- counter/state drift;
- recurring capture disconnects;
- repeated notifications after long uptime.

## Current Linux validation matrix

### 1. Startup and environment

Test:

- normal launch with default Thieving config;
- explicit Fishing config;
- explicit `--backend x11-xcb`;
- explicit ImageMagick fallback;
- game absent at startup;
- invalid backend;
- missing optional sound output;
- missing Tesseract;
- missing xcffib with automatic fallback;
- malformed profile/config;
- second watcher instance while one already runs.

Expected:

- failures are explicit and actionable;
- no silent partial startup;
- fallback occurs only where documented;
- singleton behavior is reliable.

### 2. Window acquisition and lifecycle

Test while Screen Watcher is running:

- move RuneScape window;
- resize it;
- resize repeatedly;
- minimize;
- restore;
- obscure with another window where relevant;
- change focus;
- close RuneScape;
- reopen RuneScape;
- recreate the client window;
- restart the game while Screen Watcher remains running.

Observe:

- handle reacquisition;
- geometry refresh;
- region alignment;
- frame cache invalidation;
- rule reset behavior;
- whether old state produces false alerts after reacquisition.

### 3. Capture backend behavior

For XCB:

- full normal session;
- repeated small-region grabs;
- full-window calibration image;
- shot output;
- probe;
- inventory diagnostic;
- capture after resize;
- capture immediately around window recreation.

For ImageMagick fallback:

- same commands that claim fallback support;
- compare basic geometry and color correctness;
- ensure fallback errors surface through the same health path.

Record:

- mean/median representative capture time;
- failures;
- blank/flat frames;
- visibly wrong channel/order/crop results.

### 4. Chat/OCR and ChatReader

Test with RuneScape local chat timestamps enabled:

- startup with old scrollback;
- repeated identical catches/pickpockets;
- multiple events in one polling cycle;
- punctuation/OCR variation of one visible line;
- a line remaining visible for many cycles;
- line scrolling off then later recurring.

Test with timestamps disabled:

- repeated identical events;
- visible duplicate counts;
- lines disappearing/reappearing;
- assess false duplicate suppression and duplicate re-emission.

Test adverse OCR conditions:

- partially obscured chat;
- resized chat area;
- transient blank/flat capture;
- Tesseract failure;
- slow OCR;
- deliberately wrong region.

Record:

- expected event count;
- emitted event count;
- false merges;
- duplicate emissions;
- Tesseract latency.

### 5. Fishing profile

Validate every enabled rule through actual behavior where feasible.

Cases:

- watcher starts while already idle;
- watcher starts while fishing;
- catches continue normally;
- fishing genuinely stops;
- bank/inventory cycle;
- pack approaches full;
- pack fills;
- pack becomes empty/reduced after banking;
- relevant chat messages remain in scrollback;
- resize/reacquisition mid-session.

For each alert-producing rule, record:

- trigger condition;
- expected time;
- actual time;
- source evidence;
- whether any alert was early, late, duplicated, or missing.

### 6. Thieving profile

Cases:

- startup idle;
- startup during pickpocketing;
- sustained normal pickpocketing;
- genuine stop;
- stun/failed attempt;
- target awareness;
- activity-icon disappearance while chat confirms continued activity;
- genuine activity-icon disappearance without corroborating activity;
- ordinary coin gains;
- coin milestone accumulation;
- quantity-1/unstackable item appearing in a previously empty slot;
- existing stack increasing 1 -> 2;
- tooltip/overlay transient over backpack;
- supply warning/out condition where testable;
- watcher restart with persisted counter.

Specifically verify the corrected `item_gained` semantics:

- empty -> occupied is eligible;
- occupied -> larger stack is not a "new slot";
- a temporary visual overlay does not become a persistent item gain.

### 7. Persistence and restart behavior

Test:

- stop/start watcher during active activity;
- restart after counters have accumulated;
- switch Fishing -> Thieving -> Fishing;
- inspect occupancy/counter records for profile separation;
- simulate old legacy records without `skill`;
- append malformed/truncated JSONL rows;
- restart after malformed records;
- system reboot between recorded sessions if practical.

Verify:

- persisted timestamps are Unix wall time;
- no negative cross-reboot durations;
- no Fishing/Thieving history mixing;
- malformed rows do not crash startup/statistics;
- counters restore only to the intended profile.

### 8. Notifications and sounds

Test:

- standard notification;
- low/normal/critical urgency if used;
- configured sound;
- missing sound file;
- missing `paplay`;
- repeated cooldown behavior;
- two different rules close together;
- notification history matches what was actually delivered.

No sound failure should stop notification delivery.

### 9. Process lifecycle

Exercise:

- foreground `watch`;
- background launch procedure;
- `status`;
- `pause`;
- `resume`;
- stop/termination path;
- stale PID file;
- PID reuse identity protection;
- watcher crash and subsequent relaunch.

### 10. Diagnostic commands

Run and inspect output from:

- `doctor`;
- `calibrate`;
- `shot`;
- `regions`;
- `probe`;
- `inv`;
- `stats`;
- `alerts`.

A command counts as validated only if its output is not merely non-crashing but
actually consistent with the visible game state and documented semantics.

## Performance baseline

Maintain a small table of measured current-version behavior.

At minimum record:

- backend;
- window size;
- profile;
- capture time per active region;
- total capture/prefetch time;
- Tesseract time;
- whole polling-cycle time;
- CPU usage range;
- resident memory at start and after longer session;
- capture/OCR failure count.

Re-run the baseline whenever a change affects:

- capture implementation;
- number/size of regions;
- OCR preprocessing;
- scheduler/cache behavior;
- polling interval/deadline logic.

A "performance improvement" should not be accepted from code inspection alone.

## False-positive and false-negative accounting

For screen-reading software, "did not crash" is an insufficient quality metric.

For each practical session, record:

- true positives;
- false positives;
- known false negatives;
- ambiguous events;
- total expected observable events where countable.

Where enough events exist, derive:

- precision = true positives / all emitted positives;
- recall = true positives / expected positives.

Do not hide uncertain ground truth behind a fake percentage. For difficult
events, preserve annotated observations/replay fixtures instead.

## Bug severity for practical findings

### Critical

Examples:

- data corruption;
- runaway notification storm;
- watcher generates input or crosses the read-only boundary;
- persistent state causes materially wrong behavior across profiles/sessions;
- application cannot stop/recover safely.

Blocks unrelated roadmap implementation until resolved or deliberately
quarantined.

### High

Examples:

- common false positive/false negative;
- watcher dies during ordinary play;
- capture backend fails after normal resize/restart;
- counters/history become incorrect;
- major memory/CPU regression.

Normally fixed before moving to the next major feature.

### Medium

Examples:

- uncommon detector edge case;
- degraded diagnostic wording;
- one optional output/backend fails with a documented fallback.

Can coexist with unrelated work if tracked and bounded.

### Low

Examples:

- cosmetic output inconsistency;
- documentation wording;
- minor diagnostic presentation issue.

## Regression rule

Every practical bug fix should answer:

1. What real sequence exposed the bug?
2. Can the sequence be represented by a deterministic unit test?
3. If not, can it become a replay fixture?
4. If not, what exact manual reproduction procedure must be retained?
5. What nearby state transitions could the fix break?

Do not add tests that merely reimplement the production algorithm in the test.
Prefer tests that call the real evaluator/reader/backend interface.

## Validation report template

For each meaningful practical run, record something equivalent to:

```text
Date:
Software version:
Commit:
Machine/OS:
Kernel:
Desktop/session:
Python:
Backend:
RuneScape window size:
Profile:
Chat timestamps:
Duration:

Scenario:
Expected behavior:
Observed behavior:

Expected alerts/events:
Observed alerts/events:
False positives:
False negatives:
Capture errors:
OCR errors:

CPU:
Memory start/end:
Representative capture time:
Representative OCR time:
Representative cycle time:

Issues created:
Fixtures/tests added:
Result: PASS / PASS WITH ISSUES / FAIL
```

Reports may later be stored as structured files, issue comments, or release
validation records. The important requirement is reproducibility, not the final
storage format.

## Version milestone gate

Practical validation is part of the software-version decision.

A planned `0.X.0` milestone must not be declared merely because its coding
work is mostly present. The milestone version is assigned only after its
implementation and affected existing behavior have passed the validation
requirements in this document and no unresolved critical/high defect
contradicts the milestone claim.

Corrective releases within a milestone line use PATCH increments instead.

The formal rules are in [versioning-policy.md](versioning-policy.md).

## Development gate

Before starting a large roadmap feature, ask:

- Has the current main branch been practically exercised since the last runtime
  changes?
- Are there known high/critical defects in existing functionality?
- Does the proposed feature depend on a subsystem that has not been validated
  in real use?
- Would a smaller test/replay/diagnostic improvement reveal more useful
  information first?

When the answer indicates uncertainty in existing behavior, validation or bug
fixing comes first.

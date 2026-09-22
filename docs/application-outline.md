# Screen Watcher application outline

## Purpose

Screen Watcher should become a read-only observability and notification
application for RuneScape gameplay. It observes the client, interprets
skill-specific and activity-specific signals, and tells the player when
attention is needed. It should cover the whole game rather than being a
fishing/thieving tool built around urns and bait.

It must not play the game: no clicks, key presses, movement, combat actions,
banking, item use, or other synthetic input. The player remains responsible for
all decisions and actions.

## Scope

The final application should support three profile families:

1. **Skill/activity profiles** — coverage for all 29 RuneScape skills, with
   separate method/activity profiles where one skill contains materially
   different loops (for example Necromancy combat vs rituals).
2. **Quest profiles** — one profile per quest or quest activity where objective
   and progression cues matter.
3. **Boss/activity profiles** — one profile per boss, encounter, minigame, or
   other repeatable activity with its own phases and failure states.

Profiles should compose reusable global capabilities for events such as level-up,
inventory capacity, HP/Prayer thresholds, AFK/lobby warnings, familiar expiry,
porter depletion, and session milestones rather than duplicating those rules in
every activity file.

Fishing, thieving/pickpocketing, urns, and bait are initial examples only. They
are not the product boundary.

## Immediate implementation priority — Priority 0 Linux foundation

When development resumes, **implement the Linux technical foundation before
expanding broadly into additional skills**. The detailed research and package
choices are recorded at the top of
[`future-implementation-ideas.md`](future-implementation-ideas.md).

The immediate sequence is:

1. `GameInstance` / `CaptureBackend` abstraction.
2. Shared-frame scheduler/cache.
3. Native X11/XComposite/XShm backend and benchmark.
4. Backend/frame health integrated with `doctor`.
5. Reusable interface-reader registry beginning with `ChatReader`.
6. Layered OCR abstraction and specialized RuneScape numeric/sprite path.
7. Read-only KWin window-state integration.
8. XDG ScreenCast portal + PipeWire Wayland capture proof of concept.
9. PySide6/Qt + layer-shell read-only overlay proof of concept.
10. Migration of existing Fishing/Thieving rules to normalized reader/events.

This work is an implementation **gate**, not another equal-priority wishlist.
Skill-specific development during this stage should be limited to small cases
that validate a new reader/event primitive.

### Preferred Linux/CachyOS foundation

Use distro-supported components where possible:

- X11: XCB/xcffib + XComposite + XShm;
- Wayland capture: XDG Desktop Portal ScreenCast + PipeWire, KDE backend through
  `xdg-desktop-portal-kde`/KPipeWire;
- KDE window metadata: read-only KWin scripting/D-Bus integration;
- UI: PySide6/Qt 6;
- Wayland overlay: `layer-shell-qt` / `wl-layer-shell`;
- frame representation: NumPy;
- vision: OpenCV, with scikit-image only where it adds value;
- OCR: RuneScape-specialized readers plus Tesseract fallback;
- live persistence: SQLite;
- later analytics: DuckDB/Parquet over exported data;
- local APIs: authenticated WebSocket/HTTP/Unix-socket interfaces;
- tests: pytest, Hypothesis, replay fixtures, optional Xvfb/Gamescope harnesses.

Reference implementations worth consulting:
- RuneKit Reforged for Linux `GameInstance`, X11 capture, frame caching, and
  platform isolation;
- Arena Tracker for Wayland helper-process + shared-memory frame transfer;
- poe2-overlay and PathofTrading for native KDE/Wayland layer-shell overlays;
- GPU Screen Recorder for mature X11/Wayland/portal capture behaviour;
- MangoHud for compact configurable HUD/preset/config-reload ideas only, **not**
  its injection mechanism.

### Wayland technical rules

Native Wayland capture must respect compositor permission boundaries:
- create a ScreenCast portal session;
- select a monitor/window source;
- let the user grant access;
- consume the returned PipeWire stream;
- persist/restore portal selection only through supported restore tokens;
- prefer PipeWire serial/target-object semantics over assuming node IDs remain
  stable;
- begin with CPU-mappable buffers and treat DMA-BUF as an optimization requiring
  correct graphics-API synchronization/import.

Screen Watcher must never add RemoteDesktop/input-control permissions merely to
avoid capture limitations.

### Technical acceptance gate

Broader profile expansion should resume only after:
- existing reference profiles work through the backend abstraction;
- multiple detectors can consume one shared frame;
- X11 direct capture is stable and measured;
- diagnostics identify broken capture/session/geometry/OCR states;
- live and replay backends feed the same reader/event interfaces;
- a KDE Wayland portal/PipeWire capture proof of concept exists;
- a click-through layer-shell overlay proof of concept exists;
- no profile directly depends on an ImageMagick subprocess capture path.

## User experience

The player should be able to:

- select one explicit profile before starting;
- see the profile family, name, version, and monitored rules at startup;
- switch profiles using a visible stop–switch–start workflow;
- receive alerts with profile, activity, rule, severity, and recommended action;
- inspect why an alert fired and which evidence supported it;
- review alert rates, false-positive patterns, and recent activity history;
- run calibration and diagnostics without enabling notifications;
- run a `doctor` command that reports capture, OCR, region, template, profile,
  notification, sound, locale, and compatibility health as PASS/WARN/FAIL;
- see when a detector/profile is degraded instead of receiving confident alerts
  from bad input;
- disable individual rules or run in dry-run mode;
- correct/flag false detections so profile quality can be measured locally;
- manually resynchronise an inferred sequence/state machine when necessary;
- choose notification-only, compact status-strip, overlay, or detailed
  diagnostics presentation where supported;
- export sessions/events/alerts as stable CSV or JSON;
- maintain separate profiles for different activities within the same skill.

Automatic inference of the current activity should be optional and
conservative. A generic XP tick, inventory change, or open game window is not
enough evidence to silently switch from one profile to another.

## System layers

```text
Profile manager / compatibility fingerprint
    -> observation backend
    -> calibration + detector health
    -> signal extraction
    -> normalized event stream + provenance
    -> activity/session state
    -> rule evaluation / confidence fusion
    -> alert lifecycle
    -> history / analytics / outputs
    -> optional local extension API
```

The central architectural rule is that **observation is replaceable**. Screen
capture, OCR, image matching, replay fixtures, and a future sanctioned Jagex
API/plugin source should all be capable of producing the same normalized events.
Rules and profiles should not care which backend produced them. This is
important because Jagex's official 2026 plugin/API work explicitly demonstrates
the limitations of unreliable screen reading while opening richer sanctioned
data paths.

Reference:
- https://secure.runescape.com/m=news/api--plugins-september-preview

### Profile manager

Profiles should be versioned, validated JSON or a similarly portable format.
Each profile should declare:

- `profile_type`: `skill`, `quest`, or `boss`;
- activity and skill identity;
- required regions and calibration assumptions;
- observations and detectors;
- thresholds, cooldowns, and suppressions;
- supplies, resources, outputs, and failure conditions;
- profile-specific documentation and source links;
- locale/language assumptions or locale-pack requirements;
- compatibility fingerprint expectations such as UI scale/layout, capture
  backend, renderer, and calibration/schema versions;
- reusable base/global capabilities and explicit prerequisites;
- manual/degraded fallback and resynchronisation behaviour.

The manager should reject incomplete or ambiguous profiles before screen
capture begins. Profile identity should be present in startup output, logs,
alerts, and exported diagnostics. Resolved inheritance/composition should be
inspectable so the operator can see exactly why a rule exists.

### Observation backends

The current ImageMagick/X11 capture path is an acceptable prototype but should
not remain the long-term high-frequency architecture. The backend should resolve
the game window, maintain geometry, detect resize/reacquisition, expose frames,
and know nothing about Fishing, Thieving, bosses, or skill semantics.

Define a `GameInstance`/observation-backend interface before capture
assumptions spread further. Candidate backends:

- native X11/XComposite/XShm capture;
- XWayland-compatible capture where appropriate;
- Wayland portal/PipeWire capture;
- recorded fixture/replay input;
- future sanctioned RuneScape API/plugin observations.

The platform/backend layer should expose:
- window/process identity;
- position and size;
- scale/DPI;
- focus state;
- capture health;
- latest frame and frame timestamp;
- backend-advised minimum capture cadence;
- renderer/world/session metadata when reliably available.

Renderer and desktop differences should remain backend concerns. Capture tests
should explicitly cover layout scaling and, where relevant, different RuneScape
renderer paths such as Vulkan.

Use one documented internal pixel representation, preferably a NumPy uint8
array with explicit channel order, and keep format conversion at backend
boundaries.

The backend must never generate gameplay input.

### Calibration, structural location, and detector health

Calibration should evolve beyond saved coordinates alone. The system should be
able to locate repeated UI structure such as slot grids, rows, borders, bars,
and anchor clusters; estimate scale; and derive child regions relative to a
detected parent component.

Saved calibration should record a compatibility fingerprint:
- locale;
- interface and desktop scale;
- resolution/window geometry;
- capture backend and renderer;
- structural/template anchors;
- schema/profile version;
- last successful validation.

A detector-health service should continuously distinguish gameplay state from
observation failure. Examples include OCR readability collapsing, anchor
confidence disappearing, impossible inventory/resource values, geometry drift,
or one evidence source going silent while another proves the activity continues.

Health states should be explicit, for example:
- healthy;
- degraded;
- uncalibrated;
- backend failure;
- profile incompatible.

Low-confidence/degraded detectors should suppress or downgrade gameplay alerts
rather than generate misleading notifications.

Backend health should also include cheap frame sentinels such as:
- near-all-black frame;
- near-zero variance;
- impossible dimensions;
- implausibly frozen full-window hash;
- desktop/background capture instead of the RuneScape client.

### Capture scheduler and shared-frame cache

Do not let each detector own an independent capture loop.

A central scheduler should:
- gather reader cadence requirements;
- capture the RuneScape window once per necessary frame generation;
- cache the latest frame;
- give readers cheap region views into that frame;
- avoid duplicate full-window or overlapping-region captures;
- run interface reacquisition at a slower cadence than time-sensitive reads;
- record frame age so stale data cannot masquerade as current evidence.

Readers can request cadence classes such as very-fast, fast, medium, slow, or
scheduled. The scheduler decides the actual capture plan.

This follows patterns exposed by Alt1's `captureInterval`/bound-region API,
RuneKit's cached X11 frames, and modern Alt1 apps that separate read, process,
and retry intervals.

### Interface-reader registry

Reusable readers should own interface discovery, validation, parsing, health,
and relocation.

Initial registry:
- ChatReader;
- InventoryReader;
- BuffBarReader;
- ActionBarReader;
- TargetReader;
- RuneMetricsReader;
- ProgressReader;
- BossTimerReader;
- SlayerCounterReader;
- DialogReader.

Profiles consume normalized observations from these readers rather than
duplicating geometry and OCR logic.

A reader must periodically validate its anchor even when the game window has not
resized, because RuneScape interface panels can move independently.

### OCR stack

Use layered OCR rather than one universal engine:
1. specialized numeric/sprite OCR for known RuneScape fonts;
2. RuneScape chat-font OCR with supported size/color models;
3. general-purpose OCR such as Tesseract as fallback.

Known-font readers should expose confidence and reusable font definitions.
Dynamic timer/stack text should be parsed separately from stable icon artwork
before image matching.

### Signal extraction and normalized events

Reusable extractors should convert raw observations into backend-independent
events such as:

- `chat.message` and `chat.level_up`;
- `inventory.free_slots.changed`, `inventory.full`, and item gains;
- `activity.progress`, `activity.stopped`, and `activity.completed`;
- `resource.hp.changed`, `resource.prayer.low`, and other numeric resources;
- buff/debuff/status appearance, stacks, and expiry;
- target, boss-health, timer, phase, dialogue, and objective changes;
- temporary opportunities such as rockertunities, time sprites, or ritual
  disturbances;
- session milestones and inferred AFK/lobby risk.

Extractors should be testable with recorded frames and OCR fixtures, without a
live desktop session. The same event schema should also be usable by future
non-screen sources.

Each normalized event should carry provenance where possible:
- observation timestamp and source/game timestamp;
- backend/region;
- source hash/dedup key;
- confidence;
- corroborating event IDs;
- optional privacy-minimized evidence reference.

Freshness and deduplication belong in the event model rather than being
reimplemented independently by every OCR rule.

A persistent ChatReader should use overlap with prior reads and local timestamps
when available to determine which lines are genuinely new. It should also carry
chat/channel type and normalized text. Startup scrollback protection belongs in
the reader/event layer.

For important numeric resources, prefer dual-path observations: exact OCR/text
plus graphical bar proportion. Agreement increases confidence; disagreement is
diagnostic evidence rather than a reason to silently pick one value.

### Timers, schedules, and resynchronisation

Time-based features should use explicit timer classes rather than one generic
timer state:
- activity timers;
- event/manual countdowns;
- absolute deadlines;
- periodic schedules;
- daily/weekly/monthly reset timers.

A small RuneScape/UTC calendar service can calculate reset boundaries and
low-frequency reminders without continuous high-rate polling.

State machines for courses, trips, bosses, rituals, and other sequences must
support manual or landmark-based resynchronisation when inferred state drifts.

### Local game-knowledge datasets

Do not bury authoritative or research-derived RuneScape mechanics inside Python
constants.

Maintain versioned local datasets for concepts such as:
- status effects;
- trigger thresholds/lifetimes;
- item/resource metadata;
- skill/activity mechanics;
- optional cached values/prices.

A development/update tool may obtain structured data from the RuneScape Wiki,
recording source URL and revision/retrieval date. Runtime detection should use
the local snapshot rather than querying external services on every poll.

Status metadata can describe whether an effect has a timer, stacks, category,
display location, priority, and semantic ID. Trigger-reference data can preserve
facts such as 0-stamina Mining efficiency, 67% Smithing high-heat threshold,
6-second Fishing Frenzy inactivity, 30-second Seren spirit lifetime, and similar
mechanics discovered during research.

All Wiki/API acquisition must be cached and respectful of published usage
guidance.

### Rule evaluation

Rules should be pure decision logic where possible. An evaluator should return
an event containing the profile, rule, timestamp, evidence, severity, and
human-readable message. It should not directly call desktop notification,
sound, or filesystem APIs.

Rules should support:

- positive events, such as a kill, level-up, completion, or resource gain;
- negative events, such as inactivity, depletion, failure, or death;
- temporal windows, cooldowns, streaks, and re-arming;
- explicit suppression for expected transitions and overlays;
- multi-signal corroboration and confidence levels;
- ordered sequences/state machines for courses, trips, rituals, and encounters;
- dry-run evaluation and replay against historical observations.

A rule should be able to combine evidence instead of trusting one fragile
signal. For example, an activity-stop event may become high-confidence only
when an icon is absent, no new chat activity exists, and XP/progress has also
stalled.

Rules should also support mutual-exclusion/supersession groups. A combined
status, upgraded state, or new encounter phase may legitimately suppress
component or previous-state alerts without disabling those rules permanently.

Missing buff/status icons must be treated as conditional evidence because the
RuneScape UI has finite visible buff/debuff capacity and user-configurable
categories/icon sizes.

### Evidence, history, and analytics

Every alert should be explainable. Store bounded evidence such as the matched
OCR line, measured diff, item count, phase label, confidence, corroborating
signals, or elapsed interval. Runtime history must remain local and ignored by
Git by default. Sensitive screenshots should be opt-in fixtures, never
accidental repository content.

JSONL should remain available as a transparent debug/export format. If the
application begins storing months of multi-profile observations, add an
optional SQLite event store with separate concepts for raw observations,
normalized events, sessions, alerts, and persistent counters.

The same event history should support session analytics such as actions, XP,
items in/out, active vs idle time, bank/travel time, median/p95 cycle times,
failure counts, and estimated remaining time/actions.

Stable CSV and JSON exports should precede elaborate charting. Analytics should
report sample counts, medians, percentiles, variance, and confidence intervals
where meaningful rather than presenting skewed RuneScape outcomes as a single
average.

A generic resource ledger should represent item/resource gains and consumption
so Fishing, Herblore, Archaeology, combat, Slayer, Invention, Runecrafting, and
other activities can share profit/supply accounting.

### Alert lifecycle and outputs

Notification delivery should be replaceable and independent of rule logic.
Alerts should also have lifecycle state rather than relying only on cooldowns:

- enter warning/critical state once;
- repeat only while unresolved when configured;
- escalate after a configured duration;
- clear when evidence recovers;
- remember acknowledgement for the current state.

Possible outputs:
- desktop notifications;
- sound;
- text-to-speech;
- terminal/log output;
- compact always-on-top/click-through status strip;
- optional click-through overlays located near relevant RuneScape UI;
- persistent status/diagnostics view;
- optional edge/screen flash for accessibility;
- structured JSONL/SQLite history;
- stable CSV/JSON exports;
- optional future dashboards, phone alerts, or external channels.

Output combinations should be selectable by alert class so an opportunity,
progress notice, warning, and critical condition do not all behave identically.
All outputs should include profile/activity identity.

### Extension host, permissions, and local API

If Screen Watcher grows beyond one executable, extensions should consume a
controlled local API/event stream rather than arbitrary access to capture
internals.

Possible local interfaces:
- Unix socket;
- localhost HTTP;
- WebSocket;
- restricted subprocess protocol.

An extension manifest should request explicit capabilities such as event/history
read, evidence-region access, overlay drawing, network access, notification
emission, or settings access. Capabilities should be denied by default. Do not
load unrestricted third-party Python as the default extension model.

Localhost is not authentication. Generate a secret/token or use OS peer
credentials so unrelated local processes cannot silently subscribe to pixels,
history, or events. Tokens should be revocable/rotatable and scoped to extension
permissions.

This separation would let experimental dashboards, advisors, accessibility
outputs, or niche trackers evolve without weakening the trusted read-only core.

### Localization and status catalog

Semantic events must be independent of English text. Locale packs should hold
verified OCR/chat wording variants. Buff/status metadata should likewise be
data-driven, describing whether a status uses presence, duration, stacks, or a
combination rather than requiring bespoke Python for every effect.

### Client/session isolation

Avoid global process/profile state as the architecture matures. Each RuneScape
client should eventually own an independent window/backend, profile, event
stream, timer/state-machine set, history session, and output routing. This keeps
multi-client support possible without cross-contaminating alerts.

## Safety and reliability requirements

- Read-only operation must remain a hard architectural boundary.
- Profile changes must be explicit and visible.
- Stale PID files must never signal unrelated processes.
- Missing windows, X authentication failures, truncated captures, OCR errors,
  and malformed profiles must produce clear diagnostics.
- One broken rule must not silently disable every other rule.
- A broken/degraded detector must not masquerade as a real gameplay condition.
- Notifications must be stateful, rate-limited, explainable, and suppressible.
- User corrections/false-positive feedback must remain local unless explicitly
  exported.
- Evidence images must default to minimal relevant crops and opt-in retention.
- Third-party extensions must use explicit permissions and must not bypass the
  read-only boundary.
- Any future web/API enrichment must use sanctioned interfaces where available,
  cache results, and avoid excessive automated requests.
- Remote/mobile or party synchronization must be explicit opt-in and exchange
  normalized state rather than remote-control commands.
- Secrets, account data, screenshots, and runtime logs must not enter Git.
- CI must validate profiles, run unit tests, compile the application, and test
  non-desktop/replay paths.
- The read-only boundary must remain compatible with Jagex rules: no generated
  gameplay input, no direct unapproved game-world communication, and no client
  modification.
- Native Wayland support must use the ScreenCast capture path, not request
  RemoteDesktop keyboard/pointer permissions.
- Linux overlay implementation must remain compositor/desktop-level; do not
  inject Vulkan/OpenGL layers into RuneScape or preload libraries into the game.
- Third-party reference code must be checked for license compatibility before
  any implementation is copied; architectural ideas may be reimplemented
  independently.

## Delivery stages

### Current foundation

- Anchored region capture and calibration
- ImageMagick/X11 subprocess capture suitable for the prototype stage
- OCR, pixel, inventory, activity, and idle detectors
- Explicit skill profiles for fishing and thieving
- Profile validation and skill-aware alert logs
- Quest/boss profile type support and development tests

### Priority 0 engineering stage — implement before broad profile expansion

This is the next coding stage and should be read before the broader coverage
section.

- Split the monolithic script into observation backends, profiles, calibration/
  health, extractors, normalized events, activity/session state, rules, alerts,
  outputs, storage, and CLI modules.
- Define a `GameInstance` / `CaptureBackend` interface first.
- Add the shared-frame scheduler/cache so readers do not independently recapture
  the game window.
- Prototype native X11/XComposite/XShm capture using the Arch/CachyOS-friendly
  XCB/xcffib stack and benchmark it against ImageMagick.
- Implement `screen-watcher doctor` with PASS/WARN/FAIL diagnostics for session
  type, capture backend, window identity/geometry/focus, frame health, OCR,
  notifications, portal/PipeWire readiness, and profile assumptions.
- Add backend health sentinels for black, zero-variance, stale/frozen, invalid,
  or lost frames.
- Add an interface-reader registry with `ChatReader` as the first complex
  reader, then inventory/action-bar/buff readers.
- Define stable internal event names and provenance fields before adding many
  more detector kinds.
- Add a layered OCR abstraction with a path for RuneScape sprite/numeric OCR and
  Tesseract fallback.
- Add replay and fake backends that satisfy the same interface as live capture.
- Add a profile registry and `list-profiles` command.
- Add compatibility fingerprints and detector-health baselines to profiles.
- Add reusable stability gates: confirm frames/time, confidence, majority, and
  hysteresis.
- Add read-only KWin window metadata integration for native Plasma Wayland.
- Prototype Wayland ScreenCast portal + PipeWire capture, including restoration
  tokens and robust stream identity.
- Prototype PySide6/QML + `layer-shell-qt` click-through overlay output.
- Add profile composition/inheritance for global, combat, gathering, and
  production bases.
- Add dry-run and replay commands with sanitized fixture directories.
- Introduce fake OCR, clock, event-source, and notification interfaces.
- Add evidence/confidence to alert and event records.
- Add an alert lifecycle abstraction beyond simple cooldowns.
- Add local false-positive/correction feedback records and a basic quality
  report.
- Add a versioned local trigger-reference dataset and a Wiki-data update tool
  that records provenance.
- Keep SQLite as the live store; defer DuckDB/Parquet to analytical/export work.
- Keep optional OpenVINO/ML work deferred until classical OpenCV/template/sprite
  approaches are demonstrably insufficient.

### Broader coverage stage

- Prioritise reusable detectors that unlock several activities: resource bars,
  buff timers, progress bars, temporary opportunities, Make-X batches, target
  state, and multi-ingredient supplies.
- Use Archaeology, Mining, Necromancy rituals, Farming, Combat/Slayer, Agility,
  and Runecrafting as high-value experiments for those primitives.
- Build activity-specific profiles across the remaining skills from RuneScape
  Wiki research instead of forcing one monolithic profile per skill.
- Add quest and boss profile templates with encounter-specific state machines
  and explicit resynchronisation.
- Add recorded fixtures for common overlays, failures, deaths, and transitions.
- Add structural UI anchors, layout fingerprints, named calibrations, UI-scale
  metadata, compatibility warnings, and confidence reporting.
- Add locale packs and a data-driven status/buff catalog generated or enriched
  from structured Wiki data.
- Add buff-bar saturation/category awareness and timer-text masking to the
  BuffBarReader.
- Add dual-path ActionBar resource reading (numeric OCR plus bar proportion).
- Add timer taxonomy plus RuneScape reset/calendar support.
- Add stable CSV/JSON session/event exports and resource-ledger analytics.
- Add compact status-strip and optional click-through overlay output.

- Expand the reusable reader registry to cover target debuffs, channel bar,
  boss health/activity state, boss instance time, XP popups, loot/area status,
  and the Dungeoneering map where practical.
- Give readers explicit sampling-cost/cadence budgets so cheap numeric/icon
  sensors can run quickly while OCR/network work is change-driven or
  low-frequency.
- Add a live XP/session service with XP/hour, goal ETA, actions remaining where
  modelled, and automatic pause/resume semantics.
- Extend the resource ledger into source/encounter-grouped loot history and a
  recent-drop state model with explicit uncertainty.
- Extract a generic encounter timeline/phase/split engine with provenance,
  prediction-vs-observation labeling, variable windows, and manual/automatic
  resynchronisation.
- Add multi-scale/colour-tolerance calibration fixtures and accessibility
  overlays for low-contrast events, small timers/stacks, and selected
  maintainable statuses.

### Mature application stage

- Provide a stable CLI, normalized event schema, extension API, and
  configuration format.
- Offer an operator-facing status/diagnostics and session-analytics view.
- Support shareable profile packages, reusable presets, schema versions,
  permissioned extensions, and migration compatibility.
- Support X11/XWayland and native Wayland/PipeWire capture where practical.
- Support independent multi-client sessions.
- Add a sanctioned RuneScape API/plugin observation backend if Jagex exposes a
  suitable public path and its terms permit this use.
- Keep an optional advisor layer separate from the core observer; advisors must
  expose assumptions and never operate the game.
- Add opt-in phone notifications and, much later, authenticated shared/party
  normalized state where useful.
- Add optional dashboards and notification integrations without coupling them to
  game control.
- Publish releases with tested profile bundles, locale packs, fixture corpora,
  and migration notes.

## Non-goals

Screen Watcher should not become:

- a bot or macro;
- an input automation framework;
- a combat rotation assistant that chooses or performs actions;
- a credential, browser, clipboard, or indiscriminate network data collector;
- an unofficial direct game-world protocol client;
- an unrestricted third-party code host with implicit machine permissions;
- a remote-control mechanism for RuneScape;
- a repository of committed personal screenshots or runtime logs.

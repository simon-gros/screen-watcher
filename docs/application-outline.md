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

The current screen-capture backend should resolve the game window, maintain
anchored regions, detect resize/reacquisition, and expose frames without knowing
what a skill or boss means.

Define a backend interface before capture assumptions spread further. Candidate
backends:

- X11/XWayland screen capture;
- Wayland portal/PipeWire capture;
- recorded fixture/replay input;
- future sanctioned RuneScape API/plugin observations.

Renderer and desktop differences should be treated as backend concerns. Capture
tests should explicitly cover layout scaling and, where relevant, different
RuneScape renderer paths such as Vulkan.

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

## Delivery stages

### Current foundation

- Anchored region capture and calibration
- OCR, pixel, inventory, activity, and idle detectors
- Explicit skill profiles for fishing and thieving
- Profile validation and skill-aware alert logs
- Quest/boss profile type support and development tests

### Next engineering stage

- Split the monolithic script into observation backends, profiles, calibration/
  health, extractors, normalized events, activity/session state, rules, alerts,
  outputs, storage, and CLI modules.
- Implement `screen-watcher doctor` with PASS/WARN/FAIL diagnostics.
- Define stable internal event names and provenance fields before adding many
  more detector kinds.
- Add a profile registry and `list-profiles` command.
- Add compatibility fingerprints and detector-health baselines to profiles.
- Add profile composition/inheritance for global, combat, gathering, and
  production bases.
- Add reusable stability gates: confirm frames/time, confidence, majority, and
  hysteresis.
- Add dry-run and replay commands with sanitized fixture directories.
- Introduce fake capture, OCR, clock, event-source, and notification interfaces.
- Add evidence/confidence to alert and event records.
- Add an alert lifecycle abstraction beyond simple cooldowns.
- Add local false-positive/correction feedback records and a basic quality
  report.

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
- Add locale packs and a data-driven status/buff catalog.
- Add timer taxonomy plus RuneScape reset/calendar support.
- Add stable CSV/JSON session/event exports and resource-ledger analytics.
- Add compact status-strip and optional click-through overlay output.

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

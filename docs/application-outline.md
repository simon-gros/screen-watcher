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
- disable individual rules or run in dry-run mode;
- maintain separate profiles for different activities within the same skill.

Automatic inference of the current activity should be optional and
conservative. A generic XP tick, inventory change, or open game window is not
enough evidence to silently switch from one profile to another.

## System layers

```text
Profile manager
    -> observation backend
    -> signal extraction
    -> normalized event stream
    -> activity/session state
    -> rule evaluation
    -> alert lifecycle
    -> history / analytics / outputs
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
- profile-specific documentation and source links.

The manager should reject incomplete or ambiguous profiles before screen
capture begins. Profile identity should be present in startup output, logs,
alerts, and exported diagnostics.

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
- persistent status/diagnostics view;
- optional edge/screen flash for accessibility;
- structured JSONL/SQLite history;
- optional future dashboards or external channels.

Output combinations should be selectable by alert class so an opportunity,
progress notice, warning, and critical condition do not all behave identically.
All outputs should include profile/activity identity.

## Safety and reliability requirements

- Read-only operation must remain a hard architectural boundary.
- Profile changes must be explicit and visible.
- Stale PID files must never signal unrelated processes.
- Missing windows, X authentication failures, truncated captures, OCR errors,
  and malformed profiles must produce clear diagnostics.
- One broken rule must not silently disable every other rule.
- Notifications must be stateful, rate-limited, explainable, and suppressible.
- Any future web/API enrichment must use sanctioned interfaces where available,
  cache results, and avoid excessive automated requests.
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

- Split the monolithic script into observation backends, profiles, extractors,
  normalized events, activity/session state, rules, alerts, outputs, storage,
  and CLI modules.
- Define stable internal event names before adding many more detector kinds.
- Add a profile registry and `list-profiles` command.
- Add profile composition/inheritance for global, combat, gathering, and
  production bases.
- Add dry-run and replay commands with sanitized fixture directories.
- Introduce fake capture, OCR, clock, event-source, and notification interfaces.
- Add evidence/confidence to alert and event records.
- Add an alert lifecycle abstraction beyond simple cooldowns.

### Broader coverage stage

- Prioritise reusable detectors that unlock several activities: resource bars,
  buff timers, progress bars, temporary opportunities, Make-X batches, target
  state, and multi-ingredient supplies.
- Use Archaeology, Mining, Necromancy rituals, Farming, Combat/Slayer, Agility,
  and Runecrafting as high-value experiments for those primitives.
- Build activity-specific profiles across the remaining skills from RuneScape
  Wiki research instead of forcing one monolithic profile per skill.
- Add quest and boss profile templates with encounter-specific state machines.
- Add recorded fixtures for common overlays, failures, deaths, and transitions.
- Add layout fingerprints, named calibrations, UI-scale metadata, and confidence
  reporting.

### Mature application stage

- Provide a stable CLI, normalized event schema, and configuration format.
- Offer an operator-facing status/diagnostics and session-analytics view.
- Support shareable profile packages, reusable presets, schema versions, and
  migration compatibility.
- Support X11/XWayland and native Wayland capture where practical.
- Add a sanctioned RuneScape API/plugin observation backend if Jagex exposes a
  suitable public path and its terms permit this use.
- Add optional dashboards and notification integrations without coupling them to
  game control.
- Publish releases with tested profile bundles, fixture corpora, and migration
  notes.

## Non-goals

Screen Watcher should not become:

- a bot or macro;
- an input automation framework;
- a combat rotation assistant that chooses or performs actions;
- a credential, browser, clipboard, or indiscriminate network data collector;
- an unofficial direct game-world protocol client;
- a repository of committed personal screenshots or runtime logs.

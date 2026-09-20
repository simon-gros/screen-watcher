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

1. **Skill profiles** — one profile per each of RuneScape's 29 skills.
2. **Quest profiles** — one profile per quest or quest activity where objective
   and progression cues matter.
3. **Boss/activity profiles** — one profile per boss, encounter, minigame, or
   other repeatable activity with its own phases and failure states.

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
    -> capture and observation
    -> signal extraction
    -> activity/rule evaluation
    -> evidence and event model
    -> notification and history outputs
```

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

### Capture and observation

The capture layer should resolve the game window, maintain anchored regions,
detect resize/reacquisition, and expose frames without knowing what a skill or
boss means. It should support screenshots, frame sampling, OCR input, and
optional future observation backends.

### Signal extraction

Reusable extractors should convert raw observations into signals such as:

- OCR lines and normalized message events;
- frame differences and region state changes;
- XP, life-point, prayer, adrenaline, and resource-bar changes;
- inventory occupancy and item signatures;
- action-bar, target, boss-health, timer, and phase indicators;
- interface/objective/dialogue state;
- elapsed time since the last successful activity.

Extractors should be testable with recorded frames and OCR fixtures, without a
live X session.

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
- dry-run evaluation and replay against historical observations.

### Evidence and history

Every alert should be explainable. Store bounded evidence such as the matched
OCR line, measured diff, item count, phase label, or elapsed interval. Runtime
history must remain local and ignored by Git by default. Sensitive screenshots
should be opt-in fixtures, never accidental repository content.

### Outputs

Notification delivery should be replaceable and independent of rule logic:

- desktop notifications and sounds;
- terminal/log output;
- structured JSONL history;
- optional future integrations such as dashboards or external channels.

All outputs should include profile identity so events from adjacent activities
cannot be confused.

## Safety and reliability requirements

- Read-only operation must remain a hard architectural boundary.
- Profile changes must be explicit and visible.
- Stale PID files must never signal unrelated processes.
- Missing windows, X authentication failures, truncated captures, OCR errors,
  and malformed profiles must produce clear diagnostics.
- One broken rule must not silently disable every other rule.
- Notifications must be rate-limited, explainable, and suppressible.
- Secrets, account data, screenshots, and runtime logs must not enter Git.
- CI must validate profiles, run unit tests, compile the application, and test
  the non-desktop path.

## Delivery stages

### Current foundation

- Anchored region capture and calibration
- OCR, pixel, inventory, activity, and idle detectors
- Explicit skill profiles for fishing and thieving
- Profile validation and skill-aware alert logs
- Quest/boss profile type support and development tests

### Next engineering stage

- Split the monolithic script into capture, profiles, signals, rules, events,
  notifications, and CLI modules.
- Add a profile registry and `list-profiles` command.
- Add dry-run and replay commands.
- Introduce fake capture, OCR, clock, and notification interfaces.
- Add evidence to the `Alert` model and JSONL records.

### Broader coverage stage

- Build profiles for the remaining skills from RuneScape Wiki research.
- Add quest and boss profile templates with encounter-specific state machines.
- Add recorded fixtures for common overlays, failures, deaths, and transitions.
- Add profile-level calibration and confidence reporting.

### Mature application stage

- Provide a stable CLI and configuration format.
- Offer an operator-facing status/diagnostics view.
- Support profile packages and version compatibility.
- Add optional dashboards and notification integrations without coupling them to
  game control.
- Publish releases with tested profile bundles and migration notes.

## Non-goals

Screen Watcher should not become:

- a bot or macro;
- an input automation framework;
- a combat rotation assistant that chooses or performs actions;
- a credential, browser, clipboard, or network data collector;
- a repository of committed personal screenshots or runtime logs.

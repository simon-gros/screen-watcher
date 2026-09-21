# RuneScape skill profile specifications

This document turns the RuneScape Wiki's skill descriptions into a planning
map for Screen Watcher profiles. It covers all 29 current RuneScape skills.
Fishing and thieving/pickpocketing are the initial profiles; the other rows are
research-backed starting points, not finished detector configurations.

This is a profile-planning document, not a claim that the current application
already implements every skill. The broader application outline in
[`application-outline.md`](application-outline.md) defines the target
architecture for skills, quests, bosses, minigames, and other activities.

Profiles should describe the activity being monitored, not attempt to automate
gameplay. The watcher remains read-only: it observes game state and reports
information; it must not click, type, move the mouse, perform actions, or choose
gameplay for the player.

The current observation source is screen capture, OCR, colour/pixel analysis,
and local state. Profiles should not depend on that implementation detail. A
future sanctioned RuneScape API/plugin source should be able to produce the same
normalized events without rewriting profile logic. Jagex's September 2026 API
preview explicitly identifies unreliable screen reading as a limitation that
the official client API can avoid.

Reference:
- https://secure.runescape.com/m=news/api--plugins-september-preview

Questing and bossing are separate activity families, not additional skills.
They require their own profiles and cues because generic skill XP or inventory
changes may be indistinguishable during a transition. Use `profile_type:
quest` or `profile_type: boss` for those profiles; reserve `profile_type:
skill` for the 29 skill profiles.

## Implementation gate before broad profile expansion

The next development phase is **not** to create many more skill JSON files.
Before broadening coverage, complete the remaining Priority 0 Linux foundation.
The authoritative completion state is
[`priority-0-status.md`](priority-0-status.md); design rationale remains in
[`future-implementation-ideas.md`](future-implementation-ideas.md) and
[`application-outline.md`](application-outline.md).

Profiles created during Priority 0 should serve as **validation fixtures** for
generic infrastructure. Examples:
- Fishing validates inventory/activity/chat readers;
- Thieving validates chat/activity/counter/corroboration;
- Mining can validate resource/progress/opportunity readers;
- Archaeology can validate progress/opportunity/status composition;
- combat can validate action-bar resources and buff/debuff readers.

Do not add a profile-specific screenshot loop when a shared reader should own the
signal. Do not add an ImageMagick dependency to a new detector. New profiles
should consume normalized events from the new backend/reader architecture as it
comes online.

The minimum technical foundation expected before mass profile growth is:
- `GameInstance` / capture backend abstraction;
- shared-frame scheduler/cache;
- native X11 capture benchmark/implementation;
- detector/backend health and `doctor`;
- reusable `ChatReader` plus reader registry;
- replay backend;
- layered OCR;
- Wayland portal/PipeWire proof of concept on KDE/CachyOS.

Overlay support is also high priority, but lack of a polished overlay should not
block non-overlay normalized detector work once capture/readers are sound.

## Profile design

Each skill/activity profile should define:

- the skill name and concrete activity or training method;
- reusable global/base capabilities it composes rather than duplicates;
- the observations required (chat, XP/metrics, inventory, action bar, bank,
  activity-specific interface, or future non-screen backend);
- normalized events expected from those observations;
- recurring progress signals and stop/failure signals;
- supplies, tools, resources, and output items worth monitoring;
- overlay/interface states that must suppress false positives;
- confidence/corroboration rules where several signals can describe the same
  event;
- thresholds measured from captures or authoritative data rather than copied
  blindly between skills;
- alert lifecycle policy: one-shot, repeat-until-resolved, escalation, and clear
  conditions.

Global events such as level-up, inventory capacity, AFK/lobby warning, HP,
Prayer, familiar expiry, porter depletion, and generic session milestones
should live in reusable capability profiles rather than being copied into every
skill.

The RuneScape Wiki is the reference for current skill mechanics, item names,
training methods, unlocks, and likely supplies. Start from the skill page, then
follow its training, item, and activity links before adding OCR patterns.

For implementation-oriented alert ideas and basic profile seeds for all 29
skills, see [future-implementation-ideas.md](future-implementation-ideas.md).


## Cross-cutting requirements for every future profile

The mature Alt1/RuneApps ecosystem shows that a profile is not complete merely
because it can detect its happy path. Each profile should eventually describe
its operating assumptions, detector health, fallback behaviour, and evidence
quality.

### Compatibility fingerprint

Record, where relevant:
- game language/locale;
- interface scale;
- desktop scale;
- resolution/window geometry;
- named RuneScape layout or layout fingerprint;
- capture backend;
- renderer;
- profile/schema version;
- calibration version/date;
- expected structural anchors.

A profile should warn or enter degraded mode when the live environment differs
materially from the environment in which it was verified.

### Detector health

Every important detector should define what "healthy input" looks like.

Examples:
- expected OCR readability/confidence range;
- valid resource value range;
- expected inventory-grid geometry;
- minimum anchor/template confidence;
- physically possible occupancy/count limits;
- expected event cadence where the activity is running.

Health failure should be distinguishable from a real gameplay stop. A broken
chat OCR path must not automatically become "activity stopped".

### Stability and confirmation policy

Profiles should use reusable stability primitives rather than ad hoc sleeps.

Possible fields:
- `confirm_frames`;
- `confirm_seconds`;
- `minimum_confidence`;
- `majority_of_last_n`;
- enter/leave hysteresis.

The selected policy should be justified from live or replay measurements.

### Event provenance and freshness

Any event capable of generating an alert should be traceable to evidence:
- observation time;
- source/game timestamp when available;
- source backend and region;
- dedup/source hash;
- confidence;
- corroborating events;
- optional privacy-minimized evidence crop.

Profiles that read chat must explicitly handle startup scrollback, stale
messages, and duplicate OCR interpretations.

### Localization

Do not treat English wording as the semantic event itself. Where OCR/chat is
used, profile logic should refer to semantic events and locale packs should hold
verified message patterns for English, German, French, or other tested game
languages.

A wording change should require updating a locale pattern, not rewriting the
event state machine.

### Manual/degraded fallback

Document whether useful functionality remains when screen observation fails.

Examples:
- manually started timer;
- manual count;
- checklist;
- manual state selection;
- historical analytics;
- scheduled reminder;
- explicit `resync`.

A profile that depends on an inferred sequence should always have a recovery
path when inference drifts.

### Feedback and validation metrics

Where practical, track local user feedback on alerts:
- correct;
- false positive;
- wrong classification;
- useful but too noisy;
- missed event.

Profile tuning reports should use this evidence together with replay fixtures,
not only anecdotal impressions.

### Structural anchors before fixed coordinates

Prefer locating stable interface structure—slot grids, repeated frames, anchor
clusters, bars, borders—before falling back to fixed offsets. Store derived
scale and geometry as evidence so the doctor/calibration system can explain how
the region was found.

### Output class

A profile should classify events by purpose rather than giving every event the
same notification semantics:
- routine/progress;
- opportunity;
- warning;
- critical;
- hidden/log-only.

This allows desktop popup, sound, speech, overlay, status-strip, and history
outputs to be configured consistently across activities.

### Analytics contract

Profiles should identify which normalized events contribute to useful session
analytics:
- actions/laps/kills/trips/cycles;
- XP/output;
- resources consumed;
- active/idle time;
- failures/deaths;
- cycle/lap/kill duration;
- milestones and targets.

When outcomes are variable, prefer median, percentiles, sample counts, and
confidence intervals where meaningful rather than reporting an average alone.

References:
- https://runeapps.org/apps/alt1/help_alt1
- https://runeapps.org/forums/viewtopic.php?id=1913
- https://runeapps.org/forums/viewtopic.php?id=1324
- https://runeapps.org/forums/viewtopic.php?id=1822



## Interface-reader and trigger-data requirements

The third technical research pass adds several implementation requirements that
should apply before individual profiles proliferate.

### Reader ownership

Every screen-derived observation should have one clear reusable owner where
possible.

Examples:
- chat messages -> `ChatReader`;
- inventory state -> `InventoryReader`;
- buffs/debuffs -> `BuffBarReader`;
- HP/Prayer/Adrenaline/Summoning -> `ActionBarReader`;
- target name/HP -> `TargetReader`;
- RuneMetrics XP -> `RuneMetricsReader`;
- activity/crafting progress -> `ProgressReader`.

A profile should subscribe to normalized observations from these readers instead
of implementing its own copy of interface discovery and parsing.

### Profile readiness metadata

Profiles should distinguish hard requirements from reliability recommendations.

Possible fields:

```text
requires_game_settings:
  game_messages: true
  boss_kill_timer_visible: true

recommends_game_settings:
  local_chat_timestamps: true
  chat_font_min: 12
  interface_transparency: 0

requires_interfaces:
  - chat
  - boss_timer
```

The doctor command should report which conditions are satisfied, missing, or
not automatically verifiable.

### Shared-frame scheduling expectations

Profiles should not specify capture implementation details, but detectors/readers
may declare required update classes:
- very-fast;
- fast;
- medium;
- slow;
- scheduled.

The capture scheduler should merge these requests into the minimum number of
shared frames. A new profile must not introduce a separate full-window capture
loop merely because it needs a different poll interval.

### Chat-event contract

Chat-driven rules should consume a normalized chat event with:
- original text;
- normalized text;
- chatbox/channel type where known;
- source timestamp where available;
- observation timestamp;
- confidence;
- dedup key.

Local chat timestamps should be treated as a strongly recommended freshness
and identity signal. Timestamped events support safe same-event fuzzy OCR
deduplication. Without timestamps, repeated identical messages can be
intrinsically ambiguous while old lines remain visible, so profiles that depend
on counting repeated chat events should declare the timestamp recommendation
explicitly. Startup scrollback and duplicate reads must be handled by the
reader, not separately by each profile rule.

### Status/buff absence is conditional evidence

A missing status icon is only strong evidence when:
- the reader is healthy;
- the relevant category is enabled;
- the bar is not saturated;
- icon size/layout is supported;
- no mutually superseding status is active.

Profiles must not assume `missing icon == expired` without documenting those
conditions.

### Dual-path resources

For HP, Prayer, Adrenaline, Summoning, progress, and similar resources, prefer
two independent interpretations where practical:
- exact text/OCR value;
- graphical bar/proportion.

Agreement raises confidence. Disagreement should be logged and may place the
resource observation into an uncertain/degraded state.

### Wiki-derived trigger provenance

When a profile uses a game-mechanic number discovered from the RuneScape Wiki,
record:
- mechanic ID;
- value/unit;
- source URL;
- source revision or retrieval date;
- whether it is authoritative, inferred, or a user-facing default;
- whether the profile may override it.

Examples from the current research seed:
- Mining: 0 stamina is the major efficiency cliff;
- Smithing: 67% begins the high-heat band;
- Fishing Frenzy: 6-second inactivity deadline;
- Seren spirit: 30-second opportunity lifetime;
- Necromancy ritual disturbances: 12 ritual ticks apart;
- Divination chronicle enhanced catch: 6-second window;
- Memory Overflow: 10-minute duration;
- Bird's nest on ground: 2-minute lifetime.

These values should be reverified before production enablement and should not be
silently copied into unrelated activities.

### Dynamic-icon matching

If an icon contains changing timer/stack text, the reader should separate the
dynamic text from stable artwork before matching. This allows one icon template
to survive changing `59s -> 58s -> 57s` overlays.

Profiles should receive:
- semantic status ID;
- timer/stack argument;
- confidence;
- source icon position;

rather than performing raw icon-template matching themselves.

### Mutual-exclusion/supersession groups

Profiles may declare that one status or activity state supersedes others.

Examples:
- combination potions replacing component-effect monitoring;
- upgraded status replacing a weaker variant;
- one boss phase suppressing alerts for a previous phase;
- one activity mode disabling mutually incompatible rules.

This belongs in profile/state composition rather than bespoke `if` statements.


## All 29 skills

| Skill | Type | Profile focus | Likely signals and items to research | Wiki |
|---|---|---|---|---|
| Attack | Combat | melee combat progress and equipment | hit/XP changes, target state, weapon and ammunition/food supplies | [Attack](https://runescape.wiki/w/Attack) |
| Strength | Combat | melee damage training | XP changes, target/combat state, food and potion supplies | [Strength](https://runescape.wiki/w/Strength) |
| Defence | Combat | defensive combat training | XP changes, incoming damage, armour, food, and defensive supplies | [Defence](https://runescape.wiki/w/Defence) |
| Constitution | Combat | life-point and combat survivability | life-point changes, death/low-health state, food and healing items | [Constitution](https://runescape.wiki/w/Constitution) |
| Ranged | Combat | ranged combat progress | XP/target state, ammunition, bolts/arrows, food, and potions | [Ranged](https://runescape.wiki/w/Ranged) |
| Magic | Combat | spell and magic-combat progress | XP/spell feedback, runes, ammunition, food, and potions | [Magic](https://runescape.wiki/w/Magic) |
| Prayer | Combat | prayer-supported activity | prayer-point drain, active prayer icons, restoration potions, and bones/ashes | [Prayer](https://runescape.wiki/w/Prayer) |
| Summoning | Combat | familiar-supported activity | summoning points, familiar duration, pouches/scrolls, and replenishment items | [Summoning](https://runescape.wiki/w/Summoning) |
| Necromancy | Combat | necromancy combat and rituals | ability/XP feedback, conjuration state, necromancy supplies, and ritual materials | [Necromancy](https://runescape.wiki/w/Necromancy) |
| Mining | Gathering | resource extraction | rock/ore progress, XP ticks, depleted-rock state, pickaxe, ores, and stamina supplies | [Mining](https://runescape.wiki/w/Mining) |
| Fishing | Gathering | fishing spot activity | catch OCR, spot-stop timing, inventory capacity, bait, fish, and urns | [Fishing](https://runescape.wiki/w/Fishing) |
| Woodcutting | Gathering | tree resource extraction | tree/depletion feedback, XP ticks, hatchet, logs, and inventory capacity | [Woodcutting](https://runescape.wiki/w/Woodcutting) |
| Hunter | Gathering | creature trapping or catching | trap/creature state, catch OCR, bait/traps, and inventory output | [Hunter](https://runescape.wiki/w/Hunter) |
| Farming | Gathering | patch and crop cycles | patch growth/health, harvest state, seeds, compost, tools, and crop output | [Farming](https://runescape.wiki/w/Farming) |
| Archaeology | Gathering | excavation and artefact restoration | excavation progress, soil/material output, screening/restoration state, and tools | [Archaeology](https://runescape.wiki/w/Archaeology) |
| Divination | Gathering | divine-energy collection | spring/wisp state, XP ticks, energy and memory inventory, and depleted-node signals | [Divination](https://runescape.wiki/w/Divination) |
| Smithing | Artisan | bars and metal-item production | interface progress, heat/forge state, bars, ores, and output inventory | [Smithing](https://runescape.wiki/w/Smithing) |
| Crafting | Artisan | item production from materials | interface/action progress, material stacks, tools, and output inventory | [Crafting](https://runescape.wiki/w/Crafting) |
| Fletching | Artisan | bows, arrows, bolts, and crossbows | make-X interface/progress, logs, shafts, feathers, strings, and output | [Fletching](https://runescape.wiki/w/Fletching) |
| Firemaking | Artisan | fire and incense production | fire/action progress, logs, incense materials, XP ticks, and inventory movement | [Firemaking](https://runescape.wiki/w/Firemaking) |
| Cooking | Artisan | food preparation | cooking progress, burn/finish messages, raw food, tools, and output | [Cooking](https://runescape.wiki/w/Cooking) |
| Herblore | Artisan | potion production | make-X/progress interface, herbs, vials, secondary ingredients, and output | [Herblore](https://runescape.wiki/w/Herblore) |
| Runecrafting | Artisan | rune production | altar/action progress, essence, rune output, pouches, and familiar supplies | [Runecrafting](https://runescape.wiki/w/Runecrafting) |
| Construction | Artisan | player-owned-house building | build/remove interface, materials, noted items, and build-state changes | [Construction](https://runescape.wiki/w/Construction) |
| Agility | Support | course/lap and obstacle activity | lap/obstacle completion, XP ticks, stamina/run energy, and course failure | [Agility](https://runescape.wiki/w/Agility) |
| Thieving | Support | pickpocketing, stalls, chests, and traps | success/failure/stun OCR, target state, food, and loot inventory | [Thieving](https://runescape.wiki/w/Thieving) |
| Slayer | Support | assigned-monster combat | assignment/kill OCR, target and combat state, food, potions, and special supplies | [Slayer](https://runescape.wiki/w/Slayer) |
| Dungeoneering | Support | Daemonheim floor progression | floor/room completion, XP, deaths, supplies, and resource-dungeon state | [Dungeoneering](https://runescape.wiki/w/Dungeoneering) |
| Invention | Elite | disassembly, invention, and augmented gear | disassembly/XP feedback, charge/augmentation state, materials, and device output | [Invention](https://runescape.wiki/w/Invention) |

## Research and implementation order

Coverage should eventually span all 29 skills, but do **not** force each skill
into one giant JSON file. Prefer activity/method profiles such as
`profiles/necromancy-rituals.json`, `profiles/mining-core.json`, or
`profiles/agility-<course>.json` when the activity has distinct signals,
timers, or failure states.

Before expanding profile count, first verify that the Priority 0 Linux
foundation is sufficiently complete for the observation(s) required. Then favour
reusable detector/event primitives that unlock several skills at once:

1. Confirm the required signal belongs to an existing reusable interface reader,
   or extend that reader instead of creating a profile-local capture loop.
2. Define the normalized event(s) the activity needs.
3. Reuse or build a generic observation/extractor for those events.
4. Read the skill page and its current training/activity pages.
5. Identify one concrete activity and its expected cycle/state machine.
6. Capture representative chat, metrics, inventory, interface, and overlay
   states, or collect equivalent data through a sanctioned backend.
7. Record sanitized fixtures for normal progress, edge cases, and false-positive
   conditions.
8. Measure idle noise, progress intervals, layout/scaling behaviour, and
   corroborating signals.
9. Add the activity profile with conservative rules, explicit suppressions,
   confidence expectations, and alert lifecycle policy.
10. Run `doctor`/diagnostics against the live environment and record the
   compatibility fingerprint plus detector-health baseline.
11. Validate the resolved profile, replay fixtures, and inspect alert/event logs
    before relying on it live.
12. During live use, record false positives, missed events, and degraded-input
    incidents so the profile has measurable quality history.
13. Record which interface reader owns each observation and confirm that no
    duplicate capture/parser loop was introduced by the profile.
14. If numeric Wiki-derived triggers are used, record their provenance in the
    local trigger-reference data and reverify them before enabling alerts.

The current `profiles/fishing.json` and `profiles/thieving.json` remain the
reference examples for live measurement and evidence-driven tuning, but future
profiles should progressively use shared global/base capabilities rather than
copying those files wholesale. The root configuration files remain compatibility
copies for existing commands.

## Non-skilling activity profiles

Quest profiles should research the quest page and objective/step pages, then
observe objective text, dialogue, required-item checks, area transitions,
cutscenes, completion messages, and death or failure cues.

Boss profiles should research the boss page and encounter mechanics, then
observe phase transitions, enrage or timer indicators, target/health state,
kill and loot messages, death/wipe cues, and encounter-specific food, potions,
ammunition, prayer, or equipment.

Suggested names are `profiles/quest-<name>.json` and
`profiles/boss-<name>.json`. These profiles should be switched explicitly just
like skill profiles; a generic XP tick is not sufficient evidence that the
current activity is still the same.

## Sources

- [RuneScape Wiki: Skills](https://runescape.wiki/w/Skills) — 29-skill list,
  skill types, and high-level descriptions.
- Individual skill links in the table above — activity-specific mechanics,
  training methods, items, and unlocks.
- [RuneScape: API & Plugins September Preview](https://secure.runescape.com/m=news/api--plugins-september-preview)
  — official examples of Quest Helper, Clue Trainer, Drop Log, Ground Items,
  Job Gauges, and Rituals Helper, and evidence for keeping observation backends
  replaceable.
- [Jagex: Rules of RuneScape](https://legal.jagex.com/docs/rules/rules-of-runescape)
  and [Macro/client features not permitted](https://legal.jagex.com/docs/rules/macro-and-client-features-not-permitted)
  — read-only boundary and restrictions on generated input, direct game-world
  communication, client modification, and excessive automated website requests.
- [RuneScape Wiki: Interface](https://runescape.wiki/w/Interface) — customizable
  windows, layouts, and activity-specific interface areas that affect
  calibration.
- [Alt1 help](https://runeapps.org/apps/alt1/help_alt1) — mature capture,
  troubleshooting, and diagnostic patterns worth learning from.
- [RuneApps Prototyper discussion](https://runeapps.org/forums/viewtopic.php?id=1913)
  — structural UI location, scale detection, repeated-reading confirmation, and
  manual correction patterns.
- [RuneApps Better AoD discussion](https://runeapps.org/forums/viewtopic.php?id=1324)
  — evidence that localization and changing live game wording are long-term
  detector-maintenance concerns.
- [RuneApps Dungeoneering logger discussion](https://runeapps.org/forums/viewtopic.php?id=1822)
  — structured session history and CSV/Google Sheets export as useful outputs.
- [Alt1 public libraries](https://github.com/skillbert/alt1) — reusable chat,
  buff, action-bar, RuneMetrics, target, dialogue, tooltip, and other interface
  readers, plus RuneScape-specific sprite OCR.
- [RuneKit Reforged](https://github.com/Jcapehart2/RuneKit-Reforged) — Linux
  `GameInstance`, X11/XComposite/XShm capture, frame caching, scaling/focus
  state, overlays, and authenticated internal RPC patterns.
- [SusAlert](https://github.com/Raphire/SusAlert) — practical requirements for
  timestamps, visible boss timer, chat settings, and manual encounter resync.
- [RuneScape Wiki: Buffs and debuffs](https://runescape.wiki/w/Buffs_and_debuffs)
  — visible status-bar limits, categories, and icon-size considerations.
- [RuneScape Wiki: Infobox Buff](https://runescape.wiki/w/Template:Infobox_Buff/doc)
  — structured status-effect metadata suitable for a local generated catalog.
- [KWin scripting API](https://develop.kde.org/docs/plasma/kwin/api/) —
  read-only window lifecycle/focus/screen signals useful on KDE Wayland.
- [XDG ScreenCast portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html)
  — standard native Wayland capture-session flow and PipeWire stream metadata.
- [Arena Tracker](https://github.com/supertriodo/Arena-Tracker) — Linux/Wayland
  game companion using a Qt capture helper and shared-memory latest-frame path.
- [poe2-overlay](https://github.com/andre-lund/poe2-overlay) and
  [PathofTrading](https://github.com/brendancohan/PathofTrading) — KDE/Wayland
  game overlays demonstrating layer-shell approaches, including CachyOS-focused
  testing.
- [GPU Screen Recorder manual](https://man.archlinux.org/man/gpu-screen-recorder.1.en)
  — mature X11/Wayland/portal capture behaviour and portal-session restoration.
- [MangoHud](https://github.com/flightlessmango/MangoHud) — compact HUD,
  per-application configuration, presets, runtime reload, and logging patterns;
  graphics injection is explicitly not a Screen Watcher implementation path.

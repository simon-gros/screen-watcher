# Future implementation ideas

This document is an **idea backlog**, not a roadmap or commitment. Items here are
possible future directions discovered while comparing the current Screen Watcher
profiles with RuneScape mechanics and the RuneScape Wiki. Some ideas may never
be implemented because they depend on equipment, activities, UI layouts, or
play styles that are not currently relevant.

An item should move from this document into the active roadmap only after it has
a concrete use case, a reproducible in-game signal, and enough live measurements
to tune it without creating noisy or unreliable alerts.

## Fishing

### Seren spirit event

Possible alert for the Grace of the Elves Seren spirit spawn. A Seren spirit is
short-lived and therefore well suited to an attention notification.

**Current status:** vague future possibility only. The current user does not
have Grace of the Elves, so there is no immediate reason to implement or tune
this detector.

Possible implementation:
- OCR the relevant game/chat message announcing the spirit;
- optionally create a generic short-lived skilling-event rule with a lifetime;
- clear or mark the event as handled after the collection message is observed.

Reference:
- https://runescape.wiki/w/Grace_of_the_elves

### Blessing from the gods

Possible alert for Brooch of the Gods divine blessings. These are temporary
skilling events and fit the same general event model as Seren spirits.

**Current status:** conditional idea. Only useful if the relevant equipment is
being used.

Possible implementation:
- OCR start/collection messages;
- use a generic timed-event detector rather than a one-off special case.

Reference:
- https://runescape.wiki/w/Brooch_of_the_Gods

### Fishing potion / skilling buff expiry

Detect warnings or expiry of useful fishing buffs, such as juju or perfect juju
fishing effects.

Possible implementation:
- use exact chat messages when the game provides reliable warning text;
- later replace one-off OCR rules with a generic buff/debuff status detector.

Reference:
- https://runescape.wiki/w/Juju_fishing_potion
- https://runescape.wiki/w/Perfect_juju_fishing_potion

### Porter charge warnings

Grace of the Elves / sign-of-the-porter monitoring could eventually warn before
automatic banking runs out rather than only after inventory behaviour changes.

**Current status:** equipment-dependent and therefore not currently required.

Possible implementation:
- first support the zero-charge game message if useful;
- later read the porter buff/status stack count and provide configurable warning
  thresholds such as 100, 50, 10, and 0 charges.

Reference:
- https://runescape.wiki/w/Sign_of_the_porter
- https://runescape.wiki/w/Grace_of_the_elves

### Deep Sea Fishing events

A dedicated Deep Sea Fishing profile could monitor temporary events and special
spots rather than overloading the general fishing profile.

Candidate events include:
- travelling merchant;
- treasure turtle;
- sea monster;
- jellyfish;
- whale;
- whirlpool;
- Arkaneo;
- other event announcements exposed by public or game chat.

Possible implementation:
- OCR event announcements;
- separate sounds/priorities by event;
- optional visual detection of special fishing spots or the D&D/event icon;
- keep Deep Sea Fishing in its own profile because its useful alerts differ
  substantially from ordinary fishing.

Reference:
- https://runescape.wiki/w/Deep_Sea_Fishing

### Fishing Frenzy streak protection

Fishing Frenzy rewards uninterrupted activity and penalises inactivity quickly.
A dedicated profile could use a much shorter activity watchdog than normal
fishing.

Possible implementation:
- detect XP/activity ticks;
- warn shortly before the streak is lost;
- calibrate against live Fishing Frenzy measurements rather than reusing the
  generic fishing stop threshold.

Reference:
- https://runescape.wiki/w/Fishing_Frenzy

### Fishing pet acquisition

Log or notify when Bubbles is obtained.

**Current status:** low implementation priority because it is a one-time,
extremely rare event, but it would be useful as a permanent event record.

Reference:
- https://runescape.wiki/w/Bubbles

## Thieving

### Crystal Mask expiry

Warn when Crystal Mask expires so it can be refreshed before thieving
efficiency or success rate deteriorates.

Possible implementation:
- initially match the exact expiry game/chat message;
- later monitor the buff icon directly.

Reference:
- https://runescape.wiki/w/Crystal_Mask

### Suspicion / pre-caught state

The newer high-level pickpocketing mechanics include an earlier warning state
before the target fully reacts or the player is stunned. Screen Watcher already
has target-alerted and stunned alerts; a pre-failure warning would be more
actionable.

**Current status:** requires live capture before implementation. Do not invent a
regex from Wiki prose. Record the actual in-game wording/UI signal and measure
its reliability first.

Possible implementation:
- OCR the verified warning text;
- or detect a dedicated status/icon if the UI exposes one;
- use a different sound from the later target-alerted/stunned states.

Reference:
- https://runescape.wiki/w/Thieving

### Thieving buff expiry

Monitor success-rate buffs such as Crystal Mask and Five-finger Discount aura.

Possible implementation:
- generic buff icon detector;
- optional countdown/expiry warning;
- use profile-specific thresholds rather than hard-coding the behaviour into
  the thieving evaluator.

Reference:
- https://runescape.wiki/w/Five-finger_discount_aura
- https://runescape.wiki/w/Crystal_Mask

### Ralph pet acquisition

Log or notify when the Thieving pet is obtained.

**Current status:** low priority, one-time rare event.

Reference:
- https://runescape.wiki/w/Ralph

### Target-specific thieving profiles

As more thieving methods are tested, avoid turning one profile into a collection
of incompatible NPC-specific rules.

Possible future profile split:
- thieving-menaphos.json;
- thieving-elves.json;
- thieving-fairies.json;
- thieving-heists.json;
- other target-specific profiles as needed.

Shared detector behaviour should remain in the engine or a future reusable
profile/base layer rather than being copied indefinitely.

## Cross-profile engine ideas

### Generic buff/debuff detector

A major future detector family could observe the RuneScape buff/debuff bar and
identify configured icons and possibly their remaining stacks or duration.

Potential uses:
- Crystal Mask;
- porter charges;
- juju/perfect juju effects;
- Five-finger Discount;
- Call of the Sea;
- other skilling buffs.

Possible rule concepts:

```text
kind: status
region: buff_bar
icon: crystal_mask
warn_remaining: 30
missing_message: "Crystal Mask expired"
```

and:

```text
kind: status_stack
region: buff_bar
icon: sign_of_porter
warn_below: 50
critical_below: 10
```

This should be implemented generically rather than as many unrelated
skill-specific image detectors.

Reference:
- https://runescape.wiki/w/Buff_bar

### HP and resource monitoring

The existing profiles already define an `orbs` region. A future generic
resource detector could turn that region into useful alerts.

Candidate resources:
- hitpoints;
- prayer;
- summoning points;
- adrenaline where relevant.

Thieving is a particularly obvious early use because failed pickpockets can
cause damage.

Possible configuration:

```text
kind: resource
resource: hp
warn_below_percent: 40
critical_below_percent: 20
```

### Inventory item recognition

The current stack detector can reliably notice that an item appeared or changed
without identifying the item. A future visual classifier could combine that
reliability with item-specific alerts.

Possible pipeline:
1. detect a new/changed inventory slot;
2. crop only that slot;
3. compare against known item templates or another lightweight classifier;
4. report the identified item with an appropriate alert priority.

Potential uses:
- clue scrolls;
- rare thieving drops;
- activity-specific important items;
- suppression of routine/common gains.

This is preferable to relying entirely on chat OCR for rare-drop identification.

### Generic short-lived skilling events

Several RuneScape mechanics follow the same lifecycle: an event appears, remains
available briefly, can be collected, and disappears if ignored.

Possible generic rule:

```text
kind: event
start_pattern: ...
success_pattern: ...
expired_pattern: ...
lifetime: ...
priority: ...
sound: ...
```

Potential users:
- Seren spirits;
- blessings from the gods;
- selected Deep Sea Fishing events;
- future temporary skilling events.

A generic implementation would be preferable to a separate detector function
for every event.

### RuneMetrics performance monitoring

The current thieving profile already reads the RuneMetrics session timer. Future
work could also read:
- session XP;
- XP/hour;
- GP/hour;
- Gain;
- Drops where useful.

This could support alerts for degraded performance rather than only complete
stoppage, for example:
- rate significantly below a rolling baseline;
- unexpected loss of XP gain;
- milestone totals based on RuneMetrics rather than reconstructed chat events.

Any rate-based alert should be based on live measurements and smoothing so
normal short-term variance does not become notification noise.

## Promotion criteria

Before implementing an idea from this file:

1. Confirm that the mechanic is actually relevant to the current play style or
   equipment.
2. Capture the exact live game message, icon, colour range, or other signal.
3. Measure false-positive and false-negative behaviour over a meaningful live
   session.
4. Prefer an authoritative visual/status signal over inferred absence when one
   exists.
5. Avoid adding alerts that fire routinely without requiring action.
6. Add production-path tests for the detector, not tests that merely duplicate
   its algorithm.
7. Document why the chosen threshold/message is reliable.

Items can remain in this backlog indefinitely. Presence here means only that the
idea may be useful later.

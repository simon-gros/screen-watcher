# RuneScape skill profile specifications

This document turns the RuneScape Wiki's skill descriptions into a planning
map for Screen Watcher profiles. It covers all 29 current RuneScape skills.
Fishing and thieving/pickpocketing are the initial profiles; the other rows are
research-backed starting points, not finished detector configurations.

Profiles should describe the activity being monitored, not attempt to automate
gameplay. The watcher remains read-only: it observes regions, OCR text,
inventory state, XP changes, and notifications.

## Profile design

Each skill profile should define:

- the skill name and activity variant;
- the regions required (chat, XP/metrics, inventory, action bar, bank, or
  activity-specific interface);
- recurring progress signals and stop/failure signals;
- supplies, tools, resources, and output items worth monitoring;
- overlay/interface states that must suppress false positives;
- thresholds measured from captures rather than copied blindly between skills.

The RuneScape Wiki is the reference for current skill mechanics, item names,
training methods, unlocks, and likely supplies. Start from the skill page, then
follow its training, item, and activity links before adding OCR patterns.

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

Create a separate JSON file under `profiles/` for each skill, for example
`profiles/mining.json` or `profiles/herblore.json`. Do not assume that a rule
from one skill transfers unchanged to another:

1. Read the skill page and its current training/activity pages.
2. Identify one concrete activity and its expected cycle.
3. Capture representative chat, metrics, inventory, and interface regions.
4. Measure idle noise, progress intervals, and overlay behavior.
5. Add the profile with conservative rules and explicit suppressions.
6. Validate the JSON, replay captures where available, and observe alert logs
   before enabling unattended use.

The current `profiles/fishing.json` and `profiles/thieving.json` are the
reference examples for this process. The root configuration files remain
compatibility copies for existing commands.

## Sources

- [RuneScape Wiki: Skills](https://runescape.wiki/w/Skills) — 29-skill list,
  skill types, and high-level descriptions.
- Individual skill links in the table above — activity-specific mechanics,
  training methods, items, and unlocks.

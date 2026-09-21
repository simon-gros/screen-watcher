# Practical validation session — 21 September 2026

Level C evidence (activity session) against the
[practical validation plan](practical-validation-plan.md), covering live
Menaphos Fishing and a live Arch-Glacor bossing session on CachyOS/KDE
Wayland with the RS3 NXT client at 3840x2058.

This is the first record of the validation stage the
[Priority 0 status](priority-0-status.md) names as the immediate priority:
*reproduce and measure the current implementation in ordinary sessions,
record failures, and fix confirmed current-version defects before adding
more speculative architecture.*

Every defect below was found by running the application against the real
game, not by reading code or by automated tests. All are fixed on `main`
with regression tests; the test references are given so the evidence and
the guard stay connected.

## Defects found in live use

### Critical — false alerts on correct game state

**Failed pickpockets reported as stuns.** The `stunned` rule matched
`You fail to steal from the target`, which is a different event. With a
Fingerfeather necklace the steal fails and no stun occurs, so the alert
fired while chat plainly said `You nimbly avoid getting stunned`.

`state/alerts.jsonl` showed this was not an edge case: **every stun alert
ever logged had fired on the fail line**, never on a real stun. Pairing
the two lines hid it, because a failed steal usually *was* followed by a
stun, so the alert looked correct. The necklace broke the correlation.

Fixed by removing the fail line from the pattern and adding a
`suppress_pattern` for avoidance wording.
Tests: `test_avoided_stun_never_alerts`,
`test_ocr_rule_suppression_beats_the_pattern`.

**Critical health alert at full health.** `Life 4/10,597 (0%)` reported
while health was ~99%. A leading digit lost to a hitsplat drawn over the
readout turns 8,000 into 8, and that parses as a valid number.

Three distinct causes, each found by replaying the logged `source_text`:

| cause | example | effect |
|---|---|---|
| period as thousands separator | `2.309/10,597` | read 2,309 as 309 |
| leading icon noise at full health | `I 6 10,597/10,597` | read health as 6 |
| no previous reading at startup | `4/10597` first frame | nothing to contradict it |

Tests: `test_parse_gauge_accepts_a_period_thousands_separator`,
`test_parse_gauge_handles_a_full_gauge_after_leading_noise`,
`test_gauge_rejects_a_bad_first_reading`.

### High — alerts that never fire

**Kill detection never worked.** The `boss_defeated` pattern was a guess
(`kill count is|completed ... in |defeated`) and matched none of the real
lines. The game says `You have killed 626 Arch-Glacor in normal mode`, so
every kill in the session went unreported until the pattern was corrected
against live chat.

Test: `test_kill_pattern_matches_the_real_wording`.

**Coin counter lost income silently.** When the dedup set passed 400
entries the counter called `_seen.clear()` and re-primed, which treats
every line then on screen as pre-existing scrollback. At roughly two coin
lines a second that set fills every few minutes, so the total drifted
further below reality the longer the watcher ran.

Test: `test_counter_keeps_counting_across_seen_overflow`.

### High — repeated alerts for unchanged state

**Prayer re-announced on every restart.** Ten prayer alerts in the log,
in five identical pairs, one pair per watcher start. The rules behaved
correctly within each run; the state simply did not survive a restart, so
each start treated twenty-minute-old news as new.

Fixed with an opt-in `prime_on_start`, deliberately **not** applied to
`low_health`: starting the watcher while already at 5% health is exactly
when an alert is most needed.

**A single misread revived it.** One frame decoding `0/780` as `780/780`
re-armed the rule, so the next reading fired again — two alerts over 51
polls of a state that never changed. Re-arming now needs the same
confirmation that firing does.

Tests: `test_gauge_does_not_alert_on_a_state_that_predates_the_watcher`,
`test_a_single_misread_does_not_revive_a_low_alert`,
`test_priming_is_set_on_the_nagging_rules_only`.

### Medium — missing information

**Wrapped chat lines lost their content.** The drop announcement splits
across two rendered lines, with the quantity at the end of the first and
the item name on the second:

```text
15:41:55] ... You receive: 12 x
slacor remnants.
```

Rules evaluate line by line, so neither half alone carried both facts.
`join_wrapped_lines` rejoins them using the leading chat timestamp, which
every real line has and no continuation does. This is likely to affect
any long chat message, not only drops.

Test: `test_join_wrapped_lines_uses_the_timestamp_as_the_anchor`.

**Every kill was not every kill.** A 20s cooldown on `boss_defeated`
dropped one of three kills spaced 15s apart. Removed; per-line dedup
already prevents repeats.

Test: `test_every_kill_fires_even_in_a_fast_streak`.

### Critical — every rule broken while the suite was green

Found by running `watch` against the live game immediately after the
module split, with 319 tests passing:

```text
rule 'low_health' failed: NameError: name 'ACTIVE_GAME' is not defined
...and the same for all fifteen rules, every cycle
```

`ACTIVE_GAME` moved into `screen_watcher/capture.py`, but `set_active_game`
in `watcher.py` still said `global ACTIVE_GAME`. That declares a *second*
name in `watcher` which nothing ever assigns before the first read, so
`capture_array` raised on every rule on every cycle. The watcher started,
printed its rule table, and then did nothing at all.

**No test could have caught it.** Thirty-two tests patch `capture_array`
itself — they replace the exact line the bug was on. The failure only
appears when the real function runs, which only happens live.

Fixed by routing both the write and the read through `capture_mod`, and
by pointing `ocr.py` at the live module attribute rather than a value
copied at import time.

Tests: `test_set_active_game_is_visible_to_the_extracted_readers`,
`test_capture_array_reads_active_game_without_a_nameerror` — both
verified to fail against the reintroduced bug, not merely to pass now.

### The dead-patch class, and a guard for it

The split moved functions into submodules while tests kept patching them
through `watcher`. Where the new home calls a name module-locally, such a
patch reaches nothing — and the test usually *still passes*, because the
real function returns the same answer the fake would have.

Four such patches were found, three of them silently passing:

| name | call site | why it still passed |
|---|---|---|
| `count_by_colour` | `rules.py` | real counter returned 0 on the blank test frame, same as the fake |
| `ensure_x_env` | `capture.py` | real one is harmless on a host with no tools |
| `X11ImageMagickBackend` | `capture.py` | the real backend happened to be available |
| `ocr` / `ocr_array` | `ocr.py` | failed loudly — the only one the suite caught |

`test_no_test_patches_a_name_its_target_cannot_see` now walks the test
file for `monkeypatch.setattr(watcher, ...)` and flags any name whose
defining module calls it bare. Names verified as genuinely undriven are
listed in `_BARE_CALLS_NO_TEST_DRIVES` with the reason — each confirmed
by spying the implementation across the whole suite to see which tests
actually reach it, rather than by reading the code.

## Cross-cutting lesson: guessed wording is reliably wrong

Five rule patterns were written from assumption rather than observation.
**Four were wrong** when finally checked against live chat:

| rule | guessed | actual |
|---|---|---|
| `stunned` | matched the fail line | fail and stun are different events |
| `boss_defeated` | `kill count is` | `You have killed N Arch-Glacor` |
| `level_up` | required `Congratulations` | RS3 splits notices across lines |
| `impling` | `an impling appears` | `You manage to catch the impling` |
| `fishing_stopped` | *(correct)* | verified against Menaphos chat |

Any remaining unverified pattern should be treated as probably broken
rather than probably fine. `player_died`, `food_gone` and `rare_drop`
are currently in that category and are marked as such in the profile.

## Observations that were not defects

Recorded because each initially looked like one:

- **Kill numbers jump 627 → 631.** Kills 628–630 were not missed by the
  rule; no watcher was running. Each test run watched for about thirty
  seconds.
- **"Bank soon: 5 slots left" with 10 items visible.** The occupancy log
  records `occ=23` at exactly that second, then the bank trip twelve
  seconds later. The alert was right; the screenshot was later.
- **A false health alert with no watcher running.** A lingering desktop
  notification from an earlier run, eight minutes after the last log
  write.

The recurring theme is that alerts only exist while the watcher runs, and
that stale notifications outlive the process that made them.

## Coverage gaps this session could not close

- `player_died` and `food_gone` wording remains unverified — neither
  event occurred.
- `rare_drop` is wiki-verified but not live-verified.
- ~~`gold_row` and `metrics_panel` OCR unverified~~ — **closed.** Both
  read correctly at native resolution once the game was running with
  real figures: the gold row returns `& 855,446 0 1,505,183` and the
  panel `71,148 877,167 5h52m`. The original `gold_row` offset was 80px
  too low, estimated from a downscaled screenshot; measuring a panel
  from a scaled capture is not reliable.
- An independent check on the health reading was prototyped from the red
  fill bar and **discarded**: the track bounds cannot be established
  without the live game, and the prototype read 24.6% on a frame whose
  true value was 87.6%. A veto that wrong would suppress real alerts.

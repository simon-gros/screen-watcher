# Validation session — 22 September 2026

This record summarizes practical validation and corrective work performed against the live RuneScape client during the 24-hour development window ending 22 September 2026. It complements the 21 September validation record and is intended to distinguish behavior confirmed in real play from features that exist only in unit tests or roadmap documentation.

## Scope

The session exercised the current Linux/CachyOS implementation with live skill and boss activity, with particular attention to OCR accuracy, numeric HUD parsing, notification attribution, profile semantics, persistence, and false-positive control.

Profiles materially exercised or expanded during this period included Fishing, Thieving/Pickpocketing, Woodcutting, Firemaking, Arch-Glacor, and Giant Mole.

## Confirmed implementation changes

### OCR and chat reconstruction

- Chat OCR now uses 2x bicubic upscaling plus inversion before recognition. Measured character errors on the retained real fixture fell by about 74% compared with the earlier path.
- Chat-only Sauvola adaptive thresholding reduced the remaining measured error rate further, from about 0.14 to 0.06 errors per line in the measured sample.
- Sauvola is intentionally not used for numeric gauges because live tests showed that it can corrupt values severely enough to generate false critical alerts.
- Wrapped RuneScape chat lines are rejoined before ordinary OCR-rule evaluation, not only for item-drop rules. This fixed truncated loot names such as quantity-only or partial-name notifications.
- Quantity parsing was broadened for OCR-damaged separators observed in live chat.

### Numeric HUD and false-emergency protection

- The Arch-Glacor vitals, gold row, and session timer were sampled live rather than inferred from screenshots alone.
- Live numeric testing confirmed that the gauge path must remain separate from the text-optimized chat preprocessing path.
- The sudden-collapse guard was widened so a dropped leading digit such as 8,899 -> 899 is rejected instead of being interpreted as a real fall to roughly 10% health.
- A sustained genuine low reading still becomes eligible on the following confirmed sample.

### Arch-Glacor profile corrections

- Rare-drop broadcasts are now attributed to the configured player rather than accepting every server-wide "has received" broadcast.
- The Creeping Ice knockdown line is no longer classified as player death.
- The profile gained a Marks of War near-cap warning based on the remaining allowance reported by the game.
- Wrapped loot messages and Glacor remnants quantities are recovered more reliably.
- Duplicate suppression was tested against timestamp anchoring; timestamp-only matching was rejected because distinct drops in the same second can be more textually similar than a damaged duplicate. Preserving real drops was preferred over over-aggressive suppression.

### New and expanded profiles

- Woodcutting was trained against a live wood-box session and received additional practical alerts and persistence fixes.
- Firemaking was added and then trained against a live bonfire session, including fire-spirit, level-up, impling, and burnt-out-fire behavior.
- A Giant Mole boss profile was added after wiki research and live validation. Confirmed live lines include calls for aid, stun windows, rockfall warnings, rockfall/stun landing feedback, and kill messages.
- Giant Mole rare-drop handling uses the same own-player attribution rule from the start to avoid server-wide broadcast noise.

### Architecture and platform foundation

During the same 24-hour window, the implementation moved from a largely monolithic layout to focused modules under `screen_watcher/` for capture, window/KWin handling, diagnostics, configuration, readers/scheduling, runtime, rules, persistence, OCR, and overlay support. `watcher.py` remains the CLI and compatibility surface.

The production Linux foundation now includes native XCB capture, an XDG ScreenCast portal + PipeWire backend for Wayland, KWin read-only window-state discovery, runtime blank/frozen-region health checks, a click-through Wayland overlay, replay support with sanitized fixtures, profile schema/fingerprint validation, and graceful degradation when optional external tools are unavailable.

## Automated and live evidence

The Giant Mole addition concluded with 368 automated tests passing. Earlier checkpoints in the same session recorded 359–366 passing tests while individual OCR, gauge, attribution, and boss-profile fixes were being introduced.

Live `doctor` runs reported all configured checks passing in the exercised Arch-Glacor and Giant Mole sessions. Live combat runs also confirmed that corrected rules could fire without the previously observed false death and other-player rare-drop alerts.

The exact test count is a point-in-time measurement and should not be treated as a permanent project invariant; the important requirement is that later development preserves or extends regression coverage for the behaviors above.

## Remaining known issue

Heavy OCR damage on a wrapped trailing fragment can still produce a second alert for the same underlying drop. A timestamp-anchored deduplication strategy was measured and rejected because it could suppress genuinely different drops that happen within the same second. This remains an open reliability problem requiring a better identity/evidence strategy rather than a more aggressive fuzzy-text threshold.

## Validation conclusion

The last 24 hours materially strengthened the existing implementation through live measurement rather than roadmap-only expansion. The project now has broader profile coverage and a more mature observation stack, but practical validation remains the immediate priority: existing profiles and backends should continue to be exercised in ordinary play, and any reproducible defect should become a regression test or replay fixture before additional speculative architecture takes precedence.

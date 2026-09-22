# Priority 0 implementation status

This document is the operative status checklist for the Linux/CachyOS
foundation. The longer architecture and research documents explain the design
rationale; this page records what is actually implemented on `main` and what
still gates broad profile expansion.

Status meanings:

- **Complete** — implemented in the live path and covered by automated tests.
- **Partial** — useful infrastructure exists, but the acceptance criteria are
  not yet fully satisfied.
- **Not started** — documented design only.

## Current software version

Screen Watcher is currently **0.0.2**.

Version numbers are governed by
[versioning-policy.md](versioning-policy.md). During initial development,
PATCH releases represent corrective/smaller declared snapshots and MINOR
releases are reserved for completed, practically validated major coding
milestones. Priority 0 roadmap progress does not automatically change the
version number.

## Immediate validation priority

The **already implemented Linux version must continue to be exercised in real
use**. The last 24 hours moved several former Priority 0 gaps into production:
Wayland portal capture, overlay delivery, runtime frame-health checks, profile
fingerprints, sanitized replay fixtures, and the first large module split.
They also produced substantial live detector/profile validation: chat OCR
preprocessing was measured and improved; numeric gauges were audited separately;
wrapped loot reconstruction and own-player rare-drop attribution were corrected;
Arch-Glacor semantics were tightened; Woodcutting and Firemaking were trained
against live sessions; and Giant Mole was added as a live-trained boss profile.

Automated tests prove code paths under controlled inputs; they do not prove that
RuneScape capture, OCR, window lifecycle, notifications, timing, persistence,
and profile-specific semantics behave correctly together on the actual CachyOS
desktop. The 22 September session ended at a point-in-time total of 368 passing
tests, but that number is evidence of regression coverage, not a substitute for
continued live testing.

Current development priority is therefore:

1. reproduce and measure the current implementation in ordinary Fishing,
   Thieving, Woodcutting, Firemaking, Arch-Glacor, and Giant Mole sessions;
2. record false positives, false negatives, capture/OCR failures, lifecycle
   failures, persistence problems, performance regressions, and usability
   problems;
3. fix confirmed current-version defects before adding more speculative
   architecture;
4. convert reproducible practical failures into regression tests/replay fixtures;
5. rerun practical smoke/session/soak tests after material runtime fixes.

A green CI run is a baseline, **not** sufficient evidence that the current
application works correctly in practice.

See the "Immediate practical validation stage" in
[application-outline.md](application-outline.md), the detailed
[practical validation plan](practical-validation-plan.md), and tracking issue
[#13](https://github.com/simon-gros/screen-watcher/issues/13).

Session records:

- [22 September 2026](validation-session-2026-09-22.md) — live OCR, numeric HUD,
  Arch-Glacor, Woodcutting, Firemaking, and Giant Mole validation; 368-test
  checkpoint; remaining duplicate-drop edge case documented.
- [21 September 2026](validation-session-2026-09-21.md) — Level C, live
  Menaphos Fishing and Arch-Glacor. Eight defects found in live use and
  fixed, including false critical alerts on correct game state, a kill rule
  that had never fired, and a coin counter that silently lost income. Its
  main finding: of five rule patterns written from assumption, four were
  wrong when finally checked against real chat.

- **22 September 2026 live profile expansion** — Woodcutting and Firemaking
  were trained against ordinary live sessions, while Arch-Glacor received
  further live correction of gauge, loot, death/ice, broadcast, wrapping and
  quantity handling. Giant Mole was then added as a second boss profile with
  twelve rules derived from documented mechanics and checked against live chat.
  The recurring lesson is unchanged: profile rules are not considered reliable
  merely because a plausible pattern can be written; live evidence must confirm
  both the wording and the semantic meaning of the event.

## Ordered foundation

| Step | Status | Current state |
|---|---|---|
| 1. `GameInstance` / `CaptureBackend` abstraction | **Complete** | Live capture and interactive capture commands use the backend abstraction. |
| 2. Shared frame scheduler/cache | **Partial** | `GameInstance` owns per-cycle frame reuse, which is what makes one capture serve several rules, and `doctor` drives `FrameScheduler` directly. The watch loop does **not** build a scheduler - it calls `evaluate` and relies on the per-cycle cache - so scheduler-level prefetch and per-region isolation are not exercised during a real run. Corrected after reading the code: this row previously claimed `watch` let the scheduler own the cycle. |
| 3. Native X11 capture | **Complete for XCB GetImage** | `X11XcbBackend` is the default and is benchmarked against ImageMagick. The shipped implementation uses persistent XCB `GetImage`; XComposite/XShm remain optional future optimizations, not completed work. |
| 4. Backend/frame diagnostics | **Complete for current Linux backends** | `doctor` covers dependencies, backend selection, window/geometry, regions/grids, real captures, OCR, outputs, profile structure, KWin focus/window state, and detected logical-to-pixel scale. Compatibility fingerprints are validated, and `RegionHealth` now runs inside the live watch loop to report blank or frozen regions rather than leaving those checks confined to diagnostics. |
| 5. Interface-reader registry | **Partial** | `ChatReader` is live and shared by chat-driven rules. Inventory, buff/action-bar, target, RuneMetrics, and other reusable readers remain to be implemented. |
| 6. Layered OCR | **Complete for current numeric path** | RuneScape numeric/sprite OCR is used where applicable with Tesseract fallback. General chat OCR remains the dominant poll-cycle cost, now measured rather than asserted: 570 ms of a ~874 ms four-region cycle, against a ~72 ms process-startup floor that every call pays regardless of image size. Tesseract flag tuning was benchmarked and rejected - `--oem 1` saved 2%, faster page-segmentation modes changed the output, and legacy `--oem 0` was 4.5x slower. The shipped `ocr_scrolling` path is what carries the win: 123 ms against 582 ms on real pixels, now guarded by a timing test that runs the real binary. The remaining unclaimed saving is batching all regions into one Tesseract `imagelist` call - measured 27% - which needs the scheduler to own the cycle first (step 2). See [engineering notes](engineering-notes.md). |
| 7. Read-only KWin window metadata | **Complete** | KWin scripting/D-Bus discovery is implemented read-only, reports window state/focus/geometry, participates in diagnostics, and is used by runtime lifecycle handling when available. |
| 8. Portal/PipeWire Wayland capture | **Complete** | `WaylandPortalBackend` is a registered `CaptureBackend` with restore-token persistence. Verified live: `doctor --backend wayland-portal` reports 42 pass / 0 fail, reads 27 chat lines and the session timer through OCR, and a fresh process reused the stored grant with no picker. The portal delivers the framed window - 3840x2107 against XCB's 3840x2058 - so the backend measures and strips the titlebar, making an X11 calibration usable unchanged. It is not the default only because XCB is faster for small regions and needs no consent. |
| 9. KDE/Wayland layer-shell overlay | **Complete for alert delivery** | `tools/overlay.py` and `tools/overlay.qml` provide a click-through layer-shell surface, and `watch --overlay` wires it into `notify()` as an additional delivery channel. Verified live: the surface appears at the anchored position, renders alerts pushed through the real `notify()` path, and exits cleanly. Overlay-specific controls and a compact/detailed toggle at runtime remain future work. |
| 10. Existing rules consume normalized readers/events | **Partial** | Chat-driven rules consume shared `ChatReader` events and live pixel capture is scheduler-backed, but inventory/buff/resource observations are still implemented directly inside rule evaluators rather than reusable readers/events. |

## Additional acceptance-gate work

The Priority 0 foundation is **not complete** until the following are also
addressed:

- replay coverage is expanded with sanitized fixtures that exercise
  representative normal, overlay, resize, OCR-failure, and false-positive
  states through the same application path used by live capture.
  **Partly addressed:** `tests/fixtures/glacor` holds three masked frames
  from a live session, built by `tools/make_fixture.py`, and `doctor`
  runs the full stack against them with no game present (38 pass, 0 fail).
  Frames stay full-size with everything outside the read regions blanked,
  so region geometry is still exercised and the capture path is unchanged;
  that also strips display names, clan and private chat. Overlay, resize
  and deliberate OCR-failure states are not yet captured.

  **Gap found and partly closed, 22 September 2026.** Benchmarking the
  fixture showed `ocr_scrolling` never engaging against it - every cycle
  paid the full ~580 ms. The cause was the fixture, not the optimiser:
  `make_fixture.py` spaces frames evenly across a long recording, so
  consecutive fixture frames scroll far past `_SCROLL_MAX` (200px) and
  force a full re-read. The single most valuable optimisation in the
  program was therefore untestable through the replay path, and a
  regression that silently disabled it would have looked identical.
  `make_fixture.py` now takes `--consecutive` (and `--start`) to capture
  adjacent frames, and a timing test exercises the optimiser against real
  captured pixels with a synthetic two-line scroll. Recording a genuine
  consecutive-frame fixture still needs a live session;
- profile/schema versions and compatibility metadata are defined and validated. **Addressed:** every shipped profile declares `schema_version` and a `fingerprint` recording the window size, capture backend, UI scale and last validation. A profile written for a newer schema is refused outright rather than silently ignoring fields it needs; a fingerprint mismatch is reported by `doctor` as a warning, since the profile may still work. Detector-health baselines and structural anchors are not yet recorded;
- the portal/PipeWire proof of concept is integrated as a production
  `CaptureBackend` with persistent session/restore-token handling.
  **Addressed:** `WaylandPortalBackend` holds the newest streamed frame
  and crops from it, which bridges PipeWire's push model to the
  request-a-rectangle contract and is also cheaper - a full portal
  frame measured 16.8 ms against 35.0 ms for six XCB region requests.
  The restore token is stored 0600 in `state/portal-token` and verified
  to skip the picker in a fresh process. The framed-versus-client
  geometry is reconciled by measuring and stripping the decoration, so
  an existing calibration transfers unchanged;
- the click-through Wayland overlay proof of concept is integrated with the
  normal notification/application-service path. **Addressed:**
  `watch --overlay` starts the layer-shell overlay and `notify()` sends
  each alert to it as a fifth delivery channel, after the banner, the
  sound, the alert log and the console line. It stays a separate process
  - it needs a Qt event loop and a layer-shell surface, and a GUI crash
  must not take the watcher down - and every failure in that channel is
  swallowed, because losing the overlay degrades an alert while raising
  would lose the alert itself. Opt-in, since it is a visible change;
- long-duration frame-health assumptions and compatibility fingerprints are
  surfaced by diagnostics where they can be measured. **Addressed:**
  `RegionHealth` runs inside the watch loop and reports a region that has
  frozen or gone blank, which previously nothing watched for - the
  scheduler tracked it but only `doctor` ever built a scheduler. It reads
  the per-cycle frame cache, so monitoring costs no extra capture, and
  reports once per episode rather than every cycle;
- the monolithic `watcher.py` is split into independently testable capture,
  runtime, profile, reader/signal, rule, persistence, notification, diagnostic,
  and CLI modules. **Addressed:** `watcher.py` is 1,233 lines, down from
  5,292, with twelve modules under `screen_watcher/`. Submodules reach
  back for watcher-owned names through a late `_w()` accessor, so the
  tests' existing patch points keep working.

  Notification (`play`, `notify`, `log_alert`) deliberately stays in
  `watcher.py`. It reads three heavily patched module globals -
  `STATE_DIR` and `ALERT_LOG` are redirected by the `isolate_state`
  conftest fixture that stops tests overwriting live counters - and
  moving mutable module state is precisely what caused the `ACTIVE_GAME`
  outage. Ninety-five cohesive lines is not worth that risk.

  Two hazards this split proved, both recorded in the
  [validation session](validation-session-2026-09-21.md): moving a
  mutable global silently breaks every reader, and a monkeypatch aimed
  at the old home usually keeps passing while testing nothing. Only a
  live run caught the first; a static guard now catches the second.

## Current architectural rules

Until the remaining work is complete:

1. New detector code must not introduce a direct ImageMagick dependency.
2. New screen-reading commands must acquire a `GameInstance` and honor the
   selected capture backend.
3. Chat-derived rules must use `ChatReader`; local RuneScape chat timestamps
   are strongly recommended because repeated identical unstamped messages are
   intrinsically ambiguous while old lines remain visible.
4. Persistent records must use Unix wall-clock timestamps and include profile
   identity. Monotonic time is for in-process cooldowns and elapsed-state logic
   only.
5. Reproducible defects found in the current working implementation take
   precedence over new architectural or profile work.
6. Broad skill/profile expansion remains evidence-led and secondary to
   practical validation, reusable readers/normalized events, replay/fixture
   coverage, and soak testing. Schema/versioning, production Wayland capture,
   overlay delivery, and the first module split have already landed and should
   be treated as the current baseline rather than future gates.

## Next major roadmap target — cross-platform desktop application

Priority 0 is the immediate Linux observation foundation, but it is not the
final platform boundary. The next major product stage is a shared
**PySide6/Qt 6 desktop application for Linux and Windows**, documented in
[cross-platform-gui-roadmap.md](cross-platform-gui-roadmap.md).

Architecture added during Priority 0 should therefore remain portable:

- detector/rule/profile/event logic must stay platform-neutral;
- Linux capture/window/overlay code remains behind platform interfaces;
- Windows will add its own capture/window/notification/overlay adapters;
- the CLI and future Qt GUI must consume the same application-service state;
- packaged state/config paths must eventually become OS-standard rather than
  repository-relative.

Linux GUI implementation and Windows GUI/platform parity are both major roadmap
targets.

## Deferred branch work

The historical `refactor/application-architecture` branch contains useful
ideas and code (modular package layout, replay support, packaging/schema work,
and stronger CI), but it predates the current XCB/GameInstance/ChatReader
architecture and diverges substantially from `main`. It should be mined or
rebased selectively; it must not be merged wholesale.

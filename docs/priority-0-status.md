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

Before additional Priority 0 architecture is allowed to dominate development,
the **already implemented Linux version must be exercised in real use**.
Automated tests prove code paths under controlled inputs; they do not prove that
RuneScape capture, OCR, window lifecycle, notifications, timing, and persistence
behave correctly together on the actual CachyOS desktop.

Current development priority is therefore:

1. reproduce and measure the current implementation in ordinary Fishing,
   Thieving, and Arch-Glacor sessions;
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

- [21 September 2026](validation-session-2026-09-21.md) — Level C, live
  Menaphos Fishing and Arch-Glacor. Eight defects found in live use and
  fixed, including false critical alerts on correct game state, a kill rule
  that had never fired, and a coin counter that silently lost income. Its
  main finding: of five rule patterns written from assumption, four were
  wrong when finally checked against real chat.

## Ordered foundation

| Step | Status | Current state |
|---|---|---|
| 1. `GameInstance` / `CaptureBackend` abstraction | **Complete** | Live capture and interactive capture commands use the backend abstraction. |
| 2. Shared frame scheduler/cache | **Partial** | `GameInstance` owns per-cycle frame reuse, which is what makes one capture serve several rules, and `doctor` drives `FrameScheduler` directly. The watch loop does **not** build a scheduler - it calls `evaluate` and relies on the per-cycle cache - so scheduler-level prefetch and per-region isolation are not exercised during a real run. Corrected after reading the code: this row previously claimed `watch` let the scheduler own the cycle. |
| 3. Native X11 capture | **Complete for XCB GetImage** | `X11XcbBackend` is the default and is benchmarked against ImageMagick. The shipped implementation uses persistent XCB `GetImage`; XComposite/XShm remain optional future optimizations, not completed work. |
| 4. Backend/frame diagnostics | **Partial** | `doctor` covers dependencies, backend selection, window/geometry, regions/grids, real captures, OCR, outputs, profile structure, KWin focus/window state, and detected logical-to-pixel scale. Long-duration frozen-frame diagnosis and compatibility fingerprints remain outstanding. |
| 5. Interface-reader registry | **Partial** | `ChatReader` is live and shared by chat-driven rules. Inventory, buff/action-bar, target, RuneMetrics, and other reusable readers remain to be implemented. |
| 6. Layered OCR | **Complete for current numeric path** | RuneScape numeric/sprite OCR is used where applicable with Tesseract fallback. General chat OCR remains the dominant poll-cycle cost. |
| 7. Read-only KWin window metadata | **Complete** | KWin scripting/D-Bus discovery is implemented read-only, reports window state/focus/geometry, participates in diagnostics, and is used by runtime lifecycle handling when available. |
| 8. Portal/PipeWire Wayland capture | **Partial** | A working XDG ScreenCast portal + PipeWire proof of concept exists in `tools/portal_poc.py`, including restore-token handling and frame acquisition. It is not yet integrated as a production `CaptureBackend`. |
| 9. KDE/Wayland layer-shell overlay | **Partial** | A PySide6/layer-shell click-through overlay proof of concept exists in `tools/overlay.py` and `tools/overlay.qml`; it is not yet wired into the normal notification path. |
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
  and deliberate OCR-failure states are not yet captured;
- profile/schema versions and compatibility metadata are defined and validated. **Addressed:** every shipped profile declares `schema_version` and a `fingerprint` recording the window size, capture backend, UI scale and last validation. A profile written for a newer schema is refused outright rather than silently ignoring fields it needs; a fingerprint mismatch is reported by `doctor` as a warning, since the profile may still work. Detector-health baselines and structural anchors are not yet recorded;
- the portal/PipeWire proof of concept is integrated as a production
  `CaptureBackend` with persistent session/restore-token handling;
- the click-through Wayland overlay proof of concept is integrated with the
  normal notification/application-service path;
- long-duration frame-health assumptions and compatibility fingerprints are
  surfaced by diagnostics where they can be measured. **Addressed:**
  `RegionHealth` runs inside the watch loop and reports a region that has
  frozen or gone blank, which previously nothing watched for - the
  scheduler tracked it but only `doctor` ever built a scheduler. It reads
  the per-cycle frame cache, so monitoring costs no extra capture, and
  reports once per episode rather than every cycle;
- the monolithic `watcher.py` is split into independently testable capture,
  runtime, profile, reader/signal, rule, persistence, notification, diagnostic,
  and CLI modules.

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
6. Broad skill/profile expansion remains secondary to practical validation,
   reusable readers, replay/fixtures, schema/versioning, Wayland support, and
   modularization.

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

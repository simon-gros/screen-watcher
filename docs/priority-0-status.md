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

Screen Watcher is currently **0.0.1**.

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

1. reproduce and measure the current implementation in ordinary Fishing and
   Thieving sessions;
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

## Ordered foundation

| Step | Status | Current state |
|---|---|---|
| 1. `GameInstance` / `CaptureBackend` abstraction | **Complete** | Live capture and interactive capture commands use the backend abstraction. |
| 2. Shared frame scheduler/cache | **Complete** | `watch` lets `FrameScheduler` own the cycle, prefetches required regions, and isolates failed regions. `GameInstance` owns per-cycle frame reuse. |
| 3. Native X11 capture | **Complete for XCB GetImage** | `X11XcbBackend` is the default and is benchmarked against ImageMagick. The shipped implementation uses persistent XCB `GetImage`; XComposite/XShm remain optional future optimizations, not completed work. |
| 4. Backend/frame diagnostics | **Partial** | `doctor` covers dependencies, backend selection, window/geometry, regions/grids, real captures, OCR, outputs, and profile structure. Long-duration frozen-frame diagnosis, focus state, UI-scale assumptions, and compatibility fingerprints remain outstanding. |
| 5. Interface-reader registry | **Partial** | `ChatReader` is live and shared by chat-driven rules. Inventory, buff/action-bar, target, RuneMetrics, and other reusable readers remain to be implemented. |
| 6. Layered OCR | **Complete for current numeric path** | RuneScape numeric/sprite OCR is used where applicable with Tesseract fallback. General chat OCR remains the dominant poll-cycle cost. |
| 7. Read-only KWin window metadata | **Not started** | No native KWin/D-Bus window-state backend exists yet. |
| 8. Portal/PipeWire Wayland capture | **Not started** | Native Wayland ScreenCast/PipeWire capture is design work only. |
| 9. KDE/Wayland layer-shell overlay | **Not started** | No click-through overlay proof of concept exists yet. |
| 10. Existing rules consume normalized readers/events | **Partial** | Chat-driven rules consume shared `ChatReader` events and live pixel capture is scheduler-backed, but inventory/buff/resource observations are still implemented directly inside rule evaluators rather than reusable readers/events. |

## Additional acceptance-gate work

The Priority 0 foundation is **not complete** until the following are also
addressed:

- recorded/replay input feeds the same observation/reader/event layer as live
  capture;
- profile/schema versions and compatibility metadata are defined and validated;
- detector fixtures cover representative normal, overlay, resize, OCR-failure,
  and false-positive states;
- native Wayland capture has a working KDE/CachyOS proof of concept;
- a read-only click-through overlay proof of concept exists;
- focus/session/UI-scale assumptions are surfaced by diagnostics where they can
  be measured;
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

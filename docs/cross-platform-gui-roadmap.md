# Cross-platform desktop and GUI roadmap

Screen Watcher is Linux-first today, but the intended mature application is a
**Windows + Linux desktop companion** with one shared observation/rule core and
one shared Qt-based user interface. Platform-specific capture, window-state,
notification, overlay, packaging, and lifecycle code must sit behind adapters
instead of leaking into profiles or detector logic.

This document defines the two major desktop targets and the cross-platform
requirements that should influence development before the first GUI ships.

## Product targets

### Major target A — Linux desktop GUI

Build a complete operator-facing Linux application around the existing core.

Acceptance target:

- PySide6/Qt 6 main window and system tray application;
- X11/XWayland and native Wayland capture backends selectable/diagnosable;
- start/stop/pause/resume without a terminal;
- profile browser and profile readiness/compatibility view;
- calibration wizard with live region preview;
- live detector-health dashboard;
- rule enable/disable and alert settings;
- recent events/alerts/session history;
- export and sanitized diagnostic-bundle workflow;
- notification, sound, status-strip, and optional click-through overlay output;
- KDE/Wayland overlay through supported layer-shell mechanisms;
- keyboard-complete and screen-reader-friendly navigation;
- high-DPI/fractional-scaling correctness;
- native Linux packaging suitable for CachyOS/Arch plus at least one broadly
  distributable Linux format after capture/sandbox compatibility is proven.

The main GUI must remain useful when overlay support is unavailable. Overlay is
an output surface, not the application itself.

### Major target B — Windows desktop + GUI parity

Port the same application core and Qt GUI to supported Windows releases rather
than creating a second Screen Watcher implementation.

Acceptance target:

- Windows window discovery/identity/geometry/DPI backend;
- native per-window capture based on Windows Graphics Capture where available;
- captured frames enter the same `GameInstance -> FrameScheduler -> Reader ->
  Event` pipeline as Linux frames;
- Windows notification backend;
- system-tray operation and single-instance activation behavior;
- Windows calibration/doctor pages exposing capture, DPI, minimized/cloaked
  state, OCR, notification, storage, and package version health;
- Windows-compatible transparent/click-through overlay implementation that
  remains desktop-level and never injects into RuneScape;
- the same profile format, event schema, history database, GUI models, and rule
  behavior as Linux;
- signed Windows distribution, with MSIX or another documented native installer
  path evaluated for stable releases;
- automated Windows CI plus a real-desktop smoke-test path for graphics capture.

Windows support is a **first-class roadmap target**, not a post-project port.

## Shared desktop architecture

The target architecture is:

```text
                         Screen Watcher Core
       profiles / readers / rules / events / history / analytics
                                  |
                         Application services
                                  |
             +--------------------+--------------------+
             |                                         |
       Linux platform                           Windows platform
             |                                         |
 XCB / Portal-PipeWire                  Windows.Graphics.Capture
 KWin / X11 window state                  Win32 / DWM / DPI state
 D-Bus / portal notify                  Windows app notifications
 layer-shell / X11 overlay              desktop-level Qt overlay
             |                                         |
             +--------------------+--------------------+
                                  |
                      Shared PySide6 / Qt 6 GUI
               dashboard / tray / settings / wizard
```

The GUI must consume application-service models/events. It must not call OCR,
capture backends, or rule functions directly.

## GUI implementation principles

### One Qt codebase, platform adapters underneath

PySide6/Qt 6 is the preferred desktop UI stack. Qt supplies a shared Windows and
Linux widget/Qt Quick layer while still allowing native platform integration
where Qt does not expose a required capability.

Prefer:

- shared application models/controllers;
- shared QML/Qt Quick or Qt Widgets pages;
- small platform-service interfaces for capture, notifications, overlay,
  startup/lifecycle, window metadata, and packaging integration;
- no `if sys.platform` conditionals scattered through detectors/rules.

Qt Quick/QML is a strong candidate for the main dashboard/status surfaces and
overlay presentation, while a small amount of Qt Widgets may still be useful
for system tray and conventional desktop dialogs. Prototype both before
freezing the UI stack.

### Keep expensive work off the GUI thread

Capture, Tesseract, image analysis, database queries, export, replay, and
network/update checks must not run synchronously on the Qt GUI thread.

Use worker objects/tasks plus queued signals to publish immutable state back to
the UI. The current observation loop should ultimately become a service that
can run headless or behind the GUI without changing its detector semantics.

### First-class high-DPI and coordinate mapping

Qt 6 is high-DPI aware, but Screen Watcher also works with *physical game
pixels*. Treat GUI logical coordinates and capture physical pixels as different
coordinate spaces.

Add explicit tests for:

- Windows 100/125/150/175/200% scaling;
- mixed-DPI multi-monitor setups;
- KDE fractional scaling;
- game-window movement between displays;
- overlay alignment after scale/DPI changes;
- screenshot/calibration coordinates versus Qt device-independent pixels.

Windows platform code should expose per-window DPI in addition to Qt's screen
device-pixel ratio so capture and overlay diagnostics can explain mismatches.

### Accessibility is an acceptance criterion

The GUI should:

- be fully operable by keyboard;
- expose accessible names/roles/descriptions for custom QML controls;
- never encode PASS/WARN/FAIL or alert severity by color alone;
- respect system font sizing and high-contrast choices where practical;
- provide reduced-motion/minimal-animation behavior;
- keep screen-reader labels concise for rapidly updating detector state.

### Internationalization from the beginning

Make GUI strings translatable before the first public GUI release. Keep three
separate concepts:

1. Screen Watcher UI language;
2. RuneScape client/OCR language;
3. profile locale/pattern pack.

Changing the application language must not silently change OCR rules.

## Core desktop pages

The first useful GUI should prioritize operational tooling over visual polish.

### Home / watcher dashboard

Show:

- selected profile;
- game/window/backend status;
- running/paused/stopped state;
- capture and OCR health;
- active rules and last event time;
- recent alerts;
- degraded readers/regions;
- session duration and basic performance.

Primary controls:
- Start;
- Pause/Resume;
- Stop;
- Open profile;
- Doctor;
- Calibrate.

### Profile manager

Support:

- bundled versus user profiles;
- search/filter by skill/activity/boss/quest;
- enabled/disabled/experimental status;
- schema/profile version;
- required interface regions/readers;
- compatibility warnings;
- per-rule enable/disable;
- clone bundled profile into a writable user override;
- validate before activation;
- import/export profile package.

Installed application resources must remain read-only. User overrides belong in
the platform's writable application-data/config location.

### Calibration wizard

Provide a guided workflow:

1. acquire/select RuneScape window;
2. show backend and DPI/scale information;
3. capture preview;
4. overlay named region boxes;
5. drag/adjust or enter region coordinates;
6. test OCR/reader output;
7. save named calibration;
8. run health checks before enabling notifications.

A "show exactly what Screen Watcher sees" view should be one of the most
important troubleshooting tools in the application.

### Doctor / diagnostics GUI

Wrap the CLI doctor checks in a structured page rather than replacing the CLI.

Include:
- platform/session;
- selected capture backend;
- game process/window identity;
- size/DPI/scale/focus/minimized/cloaked state where available;
- capture timing and failures;
- frame health;
- OCR readiness;
- profile assumptions;
- notification/sound readiness;
- persistence paths and database/schema versions;
- package/app version;
- export sanitized diagnostics.

### History / analytics

Start with:
- recent normalized events;
- alerts and alert rate;
- session/trip/cycle summaries;
- counters;
- false-positive/correction records;
- simple median/p90/sample-count statistics.

Keep CSV/JSON export stable before investing in elaborate charts.

### Settings

Organize by:
- General;
- Appearance/accessibility;
- Notifications/sounds;
- Capture/backend;
- OCR/language;
- Storage/history;
- Updates;
- Privacy/diagnostics;
- Advanced/developer.

Use Qt's platform-independent settings abstraction for ordinary preferences and
platform-standard data/config/state locations for files/databases.

## Platform services

### Windows capture backend

Primary research target: Windows Graphics Capture.

A Windows backend should:

- identify the RuneScape HWND;
- create a capture item for that specific HWND through
  `IGraphicsCaptureItemInterop::CreateForWindow`;
- use a D3D11 capture frame pool;
- prefer a free-threaded/frame-callback design so frame acquisition is not tied
  to the GUI dispatcher;
- convert/copy only the regions actually needed by current readers where
  practical;
- recreate frame resources when dimensions change;
- detect lost/closed/minimized/cloaked windows and report degraded state rather
  than gameplay inactivity;
- benchmark copy/conversion cost before adding GPU-resident vision paths.

Do not implement Windows capture as repeated external screenshot subprocesses.

### Windows window-state service

Expose at least:
- HWND/process identity;
- client/extended-frame bounds;
- visibility;
- minimized state;
- DWM cloaked state;
- foreground/focus state;
- per-window DPI;
- monitor identity;
- recreation/reacquisition.

### Windows notifications

Use the current Windows desktop notification API when native notifications are
enabled. Keep notification delivery behind the existing output interface so
profiles never know whether the output is Windows App Notifications, Linux
D-Bus/portal notifications, sound, speech, or GUI-only.

### Linux desktop services

Retain:
- XCB for current X11/XWayland direct capture;
- ScreenCast portal + PipeWire for native Wayland;
- KWin metadata where supported;
- D-Bus/portal notifications;
- layer-shell for supported Wayland overlays;
- X11 transparent/input-transparent overlay fallback where appropriate.

## Packaging and distribution

### Native per-platform builds

Desktop binaries/installers should be built on their target OS. Do not assume
one Linux CI runner can create a trustworthy Windows artifact or vice versa.

Evaluate `pyside6-deploy`/Nuitka first for the Qt application bundle. Keep
packaging replaceable behind release scripts so another freezer can be adopted
without changing application code.

CI should eventually produce:
- Linux artifact(s);
- Windows artifact(s);
- checksums;
- machine-readable version metadata;
- SBOM/dependency report where practical.

### Windows release path

Evaluate:
- signed MSIX for stable releases;
- Microsoft Store as optional distribution, not a requirement;
- direct signed installer/package for users who prefer website/GitHub releases.

Production Windows packages must be code-signed. Treat publisher identity,
certificate renewal, release signing, and SmartScreen reputation as release
engineering concerns from the start.

Do not design the update flow around the `ms-appinstaller:` URI protocol;
Microsoft documents it as disabled by default. A release channel should instead
use normal package downloads/Store/package-manager mechanisms or an explicitly
designed signed updater.

### Linux release path

Keep an Arch/CachyOS-native package as the reference development path.

Evaluate, separately:
- Arch/AUR-style package;
- portable AppImage or equivalent;
- Flatpak only after the Wayland/portal backend is mature enough for the
  sandbox.

Flatpak should not be assumed to work with the current X11/process-discovery
implementation: Flatpak intentionally prevents arbitrary host-process access.
A portal-centric capture design is the better prerequisite for a sandboxed
build.

### Release channels

Plan for:
- stable;
- beta;
- nightly/development.

Every release should carry:
- application version;
- profile bundle version;
- schema/database migration version;
- changelog/migration notes;
- checksums/signatures.

Update checks should be optional and must never be required for local watcher
operation.

## Storage and settings migration

The current repository-local `state/` model is development-friendly but is not
suitable as the final installed-application layout.

Before packaged GUI releases:

- use platform-standard writable locations for config/data/cache/state;
- keep bundled profiles/assets read-only inside application resources;
- store user-created profiles/calibrations separately;
- add schema/database migrations;
- make migrations transactional and back up before destructive changes;
- support a one-time import from the existing repository-local state/config;
- make portable/developer mode explicit rather than accidental.

## System tray and lifecycle

Screen Watcher is a long-running companion, so tray behavior is a major UI
surface.

Target tray actions:
- Open Screen Watcher;
- Start/Stop;
- Pause/Resume;
- Current profile;
- Health summary;
- Recent alert;
- Exit.

Closing the main window should have an explicit configurable meaning: minimize
to tray or quit. Avoid invisible zombie instances.

Keep one logical Screen Watcher instance per user session by default, with
activation routed to the existing GUI. Multi-client RuneScape support should be
implemented *inside* that application as multiple managed `GameInstance`
sessions rather than by launching unrelated copies.

## Testing strategy for the desktop application

### Shared tests

Run on Linux and Windows:
- profile/schema validation;
- reader/rule/event tests;
- replay fixtures;
- storage migrations;
- application-service/model tests;
- GUI model tests;
- settings/path tests;
- export/import.

### GUI tests

Add:
- Qt/QML unit tests;
- offscreen component tests where possible;
- keyboard navigation tests;
- accessible-name/role checks for key controls;
- high-DPI geometry tests;
- start/pause/stop state-machine tests;
- calibration workflow tests with replayed frames.

### Platform integration tests

Linux:
- Xvfb/X11 smoke tests;
- KDE/Wayland manual or dedicated-runner tests;
- portal permission/restore-token tests.

Windows:
- window discovery/DPI/unit tests in hosted CI;
- Windows Graphics Capture tests on a dedicated interactive desktop runner or
  manual release gate;
- multi-monitor/mixed-DPI smoke tests;
- minimized/cloaked/recreated-window tests.

Never make live RuneScape itself a CI dependency. Use a purpose-built test
window that changes colors/text/geometry predictably.

## Additional improvements from the September 2026 research pass

### Move OCR behind change detection

General chat OCR is the current dominant cost. Add an image-change/line-dirty
gate before invoking Tesseract:

- hash/diff the text region;
- identify changed horizontal bands/lines;
- OCR only changed lines when confidence permits;
- retain periodic full reads as a correctness backstop;
- record OCR skipped/full/incremental timing.

Benchmark this independently on Linux and Windows because capture/conversion
costs differ.

### Structured diagnostic bundle

Create a one-click, privacy-conscious support bundle containing:

- application/platform/backend versions;
- profile/schema versions;
- doctor report;
- recent structured logs/events;
- capture/OCR timing summary;
- resolved regions and DPI/scale metadata;
- optional user-approved screenshots with an explicit redaction preview.

Account names/chat screenshots must never be silently included.

### Crash-safe session recovery

Persist enough lightweight runtime state to explain and recover from an
unexpected application restart:

- selected profile;
- running/paused state;
- last clean shutdown marker;
- open session identity;
- persistent counters;
- database migration state.

Do not automatically restart monitoring after a crash if profile/capture health
cannot be revalidated.

### Capability model

Replace platform assumptions with explicit capabilities:

```text
capture.window
capture.portal
window.focus
window.dpi
overlay.click_through
notify.native
tray
speech
hotkeys.global
```

The GUI and profiles can then display "available / unavailable / degraded"
instead of assuming every Windows or Linux environment has identical features.

### Privacy and network policy

Default behavior should remain fully local.

Any future:
- update check;
- Wiki/profile download;
- telemetry;
- crash upload;
- cloud/mobile notification

must be separately identifiable and configurable. Telemetry/crash submission
should be opt-in unless a later public release policy clearly documents a
different privacy-preserving choice.

### Translation-ready UI and OCR locale separation

Use Qt translation tooling from the first GUI implementation. Store display
strings separately from detector regexes/OCR dictionaries so community UI
translations cannot accidentally change gameplay detection.

### Reusable GUI state models

The UI should receive stable models such as:

- `ApplicationState`;
- `GameSessionState`;
- `ProfileState`;
- `ReaderHealth`;
- `RuleState`;
- `AlertRecord`;
- `BackendCapabilitySet`.

This keeps QML/widgets free of direct runtime globals and makes replay,
headless CLI, and GUI tests consume the same state representation.

## Research references

Windows:
- https://learn.microsoft.com/windows/win32/api/windows.graphics.capture.interop/nf-windows-graphics-capture-interop-igraphicscaptureiteminterop-createforwindow
- https://learn.microsoft.com/uwp/api/windows.graphics.capture.direct3d11captureframepool
- https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-getdpiforwindow
- https://learn.microsoft.com/windows/win32/api/dwmapi/ne-dwmapi-dwmwindowattribute
- https://learn.microsoft.com/windows/apps/develop/notifications/
- https://learn.microsoft.com/windows/apps/package-and-deploy/packaging/
- https://learn.microsoft.com/windows/msix/package/sign-msix-package-guide
- https://learn.microsoft.com/windows/apps/package-and-deploy/distribution-feature-status

Qt/PySide:
- https://doc.qt.io/qtforpython-6/
- https://doc.qt.io/qtforpython-6/tools/index.html
- https://doc.qt.io/qtforpython-6.8/deployment/deployment-pyside6-deploy.html
- https://doc.qt.io/qt-6/highdpi.html
- https://doc.qt.io/qt-6/accessible.html
- https://doc.qt.io/qt-6/qsystemtrayicon.html
- https://doc.qt.io/qt-6/qsettings.html
- https://doc.qt.io/qt-6/qstandardpaths.html
- https://doc.qt.io/qt-6/internationalization.html
- https://doc.qt.io/qt-6/threads-qobject.html
- https://doc.qt.io/qt-6/qtquicktest-index.html

Linux/portal/distribution:
- https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html
- https://docs.flatpak.org/en/latest/portal-api-reference.html
- https://docs.flatpak.org/en/latest/basic-concepts.html
- https://docs.flatpak.org/en/latest/conventions.html

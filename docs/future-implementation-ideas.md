# Future implementation ideas

This document is primarily an **idea backlog**, but it now begins with one
explicit exception: the **Priority 0 Linux technical foundation**. That section is
the implementation gate for the next coding work and should be reviewed first
whenever development resumes.

All later sections remain research/backlog material unless promoted. Some ideas
may never be implemented because they depend on equipment, activities, UI
layouts, game changes, or play styles that are not currently relevant.

An ordinary backlog item should move into active implementation only after it
has a concrete use case, a reproducible in-game signal, and enough live
measurements to tune it without creating noisy or unreliable alerts. Priority 0
items are different: they are infrastructure required to make later detectors,
profiles, overlays, diagnostics, and Linux support reliable.

## PRIORITY 0 — Linux/CachyOS technical foundation

**This section must be served first when coding resumes.** Before adding another
large batch of skill-specific rules, the project should establish the Linux
observation foundation described here. New skill detectors may still be used as
small validation cases, but they should not postpone this work.

The target development environment is CachyOS/Arch Linux, especially KDE Plasma
6 with KWin, while keeping the architecture portable enough for other Linux
desktops.

### Priority 0 implementation order

Implement in this order unless live testing proves a dependency must move:

1. Split capture/platform code behind a `GameInstance` / `CaptureBackend`
   interface.
2. Add a central shared-frame scheduler and frame cache.
3. Implement/benchmark a native X11/XComposite/XShm backend against the current
   ImageMagick subprocess capture.
4. Add backend/frame health diagnostics and make them part of
   `screen-watcher doctor`.
5. Introduce a reusable interface-reader registry, beginning with `ChatReader`
   and then inventory/resource/buff readers.
6. Add a layered OCR abstraction: specialized numeric/sprite OCR first where
   appropriate, Tesseract as fallback.
7. Add read-only KWin window discovery/state support for native KDE Wayland.
8. Prototype native Wayland capture through XDG ScreenCast portal + PipeWire,
   first using the simplest reliable Python/Qt/GStreamer path.
9. Add a native KDE/Wayland overlay prototype with PySide6/Qt and
   `layer-shell-qt`, remaining click-through and read-only.
10. Convert current rules to consume normalized reader/events rather than owning
    capture/OCR loops directly.
11. Only after these primitives are stable, accelerate broader skill/profile
    implementation.

### Architecture target

```text
                         Screen Watcher
                              |
                     GameInstance / Backend
                              |
             +----------------+----------------+
             |                                 |
        X11/XWayland                      native Wayland
     XCB/XComposite/XShm          XDG ScreenCast + PipeWire
             |                                 |
             +----------------+----------------+
                              |
                    Shared Frame Scheduler
                              |
          +-------------------+-------------------+
          |                   |                   |
      ChatReader       InventoryReader       BuffBarReader
          |                   |                   |
          +-------------------+-------------------+
                              |
                    Normalized Event Stream
                              |
              Profiles / Rules / State Machines
                              |
             Alerts / History / Analytics / UI
```

The project should treat the RuneScape window as a continuously sampled data
source, not as a sequence of unrelated screenshot subprocesses.

### CachyOS/Arch toolchain: preferred building blocks

Prefer official Arch/CachyOS repository packages where practical.

**Platform/capture**
- `python-xcffib` — XCB bindings for native X11 work;
- XComposite + XShm — direct X11 window capture/frame transport;
- `pipewire` — native low-latency video/audio transport;
- `xdg-desktop-portal` + `xdg-desktop-portal-kde` — user-approved Wayland
  capture;
- `kpipewire` — KDE's PipeWire integration;
- `gst-plugin-pipewire` + GStreamer/PyGObject — practical Python-side
  PipeWire prototype route.

**Window state / KDE**
- KWin scripting API — read-only discovery of active/add/remove/move/focus
  window state;
- D-Bus via `python-dbus-next` for KDE/portal integration;
- techniques demonstrated by `kdotool` may be useful for querying KWin, but
  Screen Watcher must not use input-generation features.

**UI / overlay**
- `pyside6` / Qt 6 — preferred native Python GUI stack;
- `layer-shell-qt` — KDE/Qt integration with `wl-layer-shell`;
- QML/QtQuick may be appropriate for a compact status strip or overlay;
- X11 fallback: transparent frameless always-on-top input-transparent Qt window.

**Vision/OCR**
- NumPy — canonical image arrays;
- `python-opencv` — structural detection, template matching, bars, grids,
  morphology, histograms, frame health;
- Pillow — image interchange/debug output;
- `python-scikit-image` — SSIM and higher-level image metrics where useful;
- Tesseract + language packs — general OCR fallback;
- custom RuneScape sprite/numeric OCR — preferred for constrained known fonts;
- OpenVINO — optional later lightweight inference backend if classical vision
  becomes insufficient.

**Storage/analytics**
- SQLite — runtime sessions/events/alerts/calibration/counters;
- SQLAlchemy optional for models/migrations;
- DuckDB — later analytical querying over exports;
- PyArrow/Parquet — optional compact historical exports;
- CSV/JSON remain stable interchange formats.

**Local APIs/integration**
- `python-websockets` or `aiohttp` — authenticated local event API;
- Pydantic + JSON Schema — profile/event/extension schema validation;
- `python-watchdog` — development-time profile/template hot reload;
- desktop notifications/D-Bus and optional TTS output.

**Testing/development**
- pytest;
- Hypothesis for state-machine/config invariants;
- Xvfb for X11 test environments;
- Gamescope as an optional controlled nested-game/display test environment;
- GPU Screen Recorder/OBS for collecting replay material;
- Ruff as a future fast lint/format/check option;
- `uv` for Python environment/lock/workflow management;
- Graphviz/PlantUML for architecture/state-machine documentation.

These are not all required runtime dependencies. The production dependency set
must remain smaller than the development toolchain.

References:
- https://archlinux.org/packages/extra/any/python-xcffib/
- https://archlinux.org/packages/extra/x86_64/pyside6/
- https://archlinux.org/packages/extra/x86_64/layer-shell-qt/
- https://archlinux.org/packages/extra/x86_64/xdg-desktop-portal-kde/
- https://archlinux.org/packages/extra/x86_64/python-opencv/
- https://wiki.archlinux.org/title/PipeWire

### X11/XWayland foundation

Replace the current repeated ImageMagick hot path with a direct backend after
the abstraction exists.

Preferred design:
- discover and own the RuneScape window identity;
- capture through XComposite/XShm;
- cache the latest full-game frame;
- expose NumPy region views;
- listen for configure/geometry changes;
- invalidate/reacquire backing resources on resize/recreation;
- maintain a backend minimum refresh interval;
- benchmark CPU time, frame latency, allocations, and missed captures against
  the existing implementation.

RuneKit provides a particularly useful reference architecture: platform-specific
code is isolated inside its game layer, it caches recent captures, uses
XComposite/XShm on Linux/X11, and exposes generic position/scaling/focus/frame
state to higher layers.

Reference:
- https://github.com/Jcapehart2/RuneKit-Reforged

### Native KDE Wayland capture

Use the standard permission model rather than trying to bypass the compositor.

The XDG ScreenCast portal flow is:
1. `CreateSession`;
2. `SelectSources`;
3. `Start` (normally presents user selection/consent);
4. receive one or more PipeWire stream descriptors;
5. `OpenPipeWireRemote` and consume frames.

The current portal interface supports monitor, window, and virtual sources.
Persistent sessions may use restore tokens so the user does not necessarily
need to reselect the source every launch. Restore tokens are single-use and must
be replaced by the newly returned token after restoration.

For modern portal version 6 streams, prefer the stream's
`pipewire-serial`/target object mechanism rather than assuming a PipeWire node
ID remains stable across hotplug, suspend, or stream recreation.

Frame consumers must be prepared for different PipeWire buffer types:
- direct memory;
- memfd;
- DMA-BUF.

Do **not** assume DMA-BUF is safely linear-mappable; hardware tiling,
compression, and synchronization may require EGL/Vulkan/graphics-API handling.
Start with a correct CPU-mappable path and optimize only after profiling.

References:
- https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html
- https://docs.pipewire.org/group__spa__buffer.html
- https://docs.pipewire.org/1.4/page_dma_buf.html

### Arena Tracker as a Wayland capture reference

Arena Tracker is especially relevant because it supports Linux and documents
CachyOS use. Its Wayland path separates capture into a small `captureHelper`
process.

Useful patterns from its source:
- detect Wayland via `XDG_SESSION_TYPE`;
- launch a capture helper only when needed;
- exchange the latest frame through shared memory;
- keep a small header containing capture status, width, height, and screen index;
- attach read-only to the shared-memory segment in the main application;
- copy the newest frame into the application when requested;
- restart the helper on a defined restart exit code;
- terminate/kill it cleanly on shutdown;
- surface helper stdout/stderr for diagnostics.

This validates an important fallback architecture for Screen Watcher: if direct
Python/Qt PipeWire capture becomes cumbersome, a very small native Qt/C++ helper
can own the portal/PipeWire stream and publish frames through shared memory while
the rest of Screen Watcher remains Python.

Reference:
- https://github.com/supertriodo/Arena-Tracker

### KWin read-only window integration

Native Wayland intentionally prevents ordinary applications from enumerating and
capturing arbitrary windows the X11 way. On KDE, use KWin's supported scripting/
window-state mechanisms for metadata and the portal for pixel access.

Useful KWin state/signals include:
- active window;
- `windowAdded`;
- `windowRemoved`;
- `windowActivated`;
- screen/output changes;
- geometry/focus properties available on window objects.

Screen Watcher should use these only for discovery, identity, focus, geometry,
and health. Never use desktop APIs to inject RuneScape input.

References:
- https://develop.kde.org/docs/plasma/kwin/api/
- https://github.com/jinliu/kdotool

### Wayland overlay foundation

A compositor-level overlay is preferable to injecting into RuneScape's Vulkan or
OpenGL process.

For KDE/Qt, investigate:
- PySide6 + QML/QtQuick;
- `layer-shell-qt`;
- `wl-layer-shell` OVERLAY layer;
- empty input region / click-through by default;
- temporary opt-in interactivity only for Screen Watcher's own controls;
- correct monitor anchoring;
- focus-aware show/hide;
- compact and detailed modes.

The Path of Exile 2 Linux overlay ecosystem provides direct evidence that common
X11/Electron overlay-window techniques fail on native KWin Wayland, while
layer-shell surfaces work. One reference project explicitly targets
CachyOS/KDE Plasma 6/KWin Wayland and documents layer-shell as the working
mechanism.

References:
- https://github.com/andre-lund/poe2-overlay
- https://github.com/brendancohan/PathofTrading
- https://archlinux.org/packages/extra/x86_64/layer-shell-qt/

### Lessons from poe2-overlay and PathofTrading

Transferable ideas:
- isolate platform-specific overlay code;
- use a real Wayland layer-shell surface rather than an X11 overlay shim;
- use an empty input region for click-through behaviour;
- keep the application persistent rather than spawning a process per event;
- make fullscreen/borderless/exclusive-fullscreen behaviour an explicit test
  matrix;
- treat monitor selection and game focus as first-class state;
- maintain architecture-decision records for fragile compositor-specific
  choices.

Do **not** copy their input-synthesis features. Screen Watcher has no legitimate
need for `ydotool`, `uinput`, or synthetic game keypresses.

### GPU Screen Recorder as a capture/replay reference

GPU Screen Recorder is now available in Arch's official repositories and
supports X11 and Wayland across NVIDIA/AMD/Intel. Its CLI distinguishes X11
window/focused capture from Wayland portal capture and supports restoring portal
sessions.

For Screen Watcher its main uses are:
- study of production-grade portal/Wayland capture behaviour;
- development fixture/session recording;
- instant-replay-style collection of the last N seconds around rare events;
- testing capture behaviour on the user's NVIDIA system;
- a benchmark/reference point, not a runtime dependency.

The Arch manual documents `-w portal` for Wayland and an optional
`-restore-portal-session` path.

References:
- https://man.archlinux.org/man/gpu-screen-recorder.1.en
- https://archlinux.org/packages/extra/x86_64/gpu-screen-recorder/

### MangoHud lessons without graphics injection

MangoHud is useful as a design reference, not as Screen Watcher's overlay
mechanism.

Borrowable concepts:
- per-application configuration;
- layered configuration precedence;
- compact/horizontal HUD modes;
- runtime config reload;
- presets;
- low-overhead continuously updated presentation;
- optional metric logging.

Avoid:
- Vulkan/OpenGL layer injection;
- `LD_PRELOAD`-style coupling to the game process.

Screen Watcher's overlay should remain compositor/desktop-level.

References:
- https://github.com/flightlessmango/MangoHud
- https://github.com/flightlessmango/MangoHud/blob/master/data/MangoHud.conf

### Gamescope as a development harness

Gamescope should be optional and development-only.

Potential test uses:
- fixed nested resolutions;
- known aspect ratios;
- borderless/fullscreen transitions;
- XWayland behaviour;
- scaling/DPI experiments;
- reproducible capture/overlay compatibility checks.

Do not require players to launch RuneScape through Gamescope.

### Vision foundation

Make OpenCV the default computer-vision engine for reusable readers.

Candidate primitives:
- template matching;
- structural anchor detection;
- connected components;
- morphology;
- edge/line detection;
- colour/HSV segmentation;
- progress/resource bar estimation;
- grid detection;
- histogram comparison;
- absolute frame difference;
- crop normalization;
- feature matching where template scale varies.

Use scikit-image selectively for metrics such as SSIM or functionality that is
clearer there.

ML remains optional. If classical vision is insufficient, OpenVINO is a
CachyOS/Arch-friendly inference path and currently has packaged Python bindings.
It may later support tiny classifiers for a constrained inventory/status/event
problem without making a full deep-learning framework part of the core.

References:
- https://archlinux.org/packages/extra/x86_64/python-opencv/
- https://archlinux.org/packages/extra/x86_64/python-openvino/

### Storage and analytics foundation

Keep SQLite as the live application database.

Suggested separation:
- SQLite: observations, normalized events, sessions, alerts, counters,
  calibration, feedback, trigger references;
- JSONL: transparent debug/export stream;
- CSV/JSON: stable interchange;
- Parquet/PyArrow: optional efficient archival export;
- DuckDB: later analytical queries over historical exports.

Do not replace SQLite with a heavier analytics engine just because historical
analysis may eventually be large.

### Packaging/dependency policy for CachyOS

Prefer:
1. Arch/CachyOS official repositories;
2. bundled pure-Python dependencies through the project environment;
3. AUR only when a capability has no reasonable repository alternative.

Development setup should be reproducible with `uv` or an equivalent locked
environment, but Linux system libraries such as Qt, PipeWire, XCB, portals, and
Wayland should generally remain distro-managed.

Keep three dependency groups conceptually separate:
- required runtime;
- optional feature runtime;
- development/research tools.

This prevents the final application from inheriting the entire research stack.

### Priority 0 acceptance criteria

Do not call the Linux foundation complete until all of these are true:

- existing Fishing and Thieving behaviour still works through the new backend
  abstraction;
- one shared frame can feed multiple readers without repeated game-window
  screenshot subprocesses;
- X11/XWayland direct capture is benchmarked and stable;
- `doctor` can identify backend, session type, geometry, focus, frame health,
  OCR readiness, notification readiness, and missing dependencies;
- reader health degrades cleanly on blank/frozen/lost frames;
- the application can recover from RuneScape resize/recreation without restart;
- a replay backend exercises the same reader/event code as live capture;
- the Wayland design has at least a working portal/PipeWire proof of concept on
  KDE/CachyOS;
- overlay proof of concept can draw read-only click-through content above the
  game on supported KDE/Wayland configurations;
- no implementation path introduces synthetic RuneScape input;
- profile/rule code is no longer coupled directly to ImageMagick capture.

After these criteria are met, broader skill coverage becomes the main priority.

### Status: all Priority 0 criteria met (September 2026)

| criterion | evidence |
|---|---|
| fishing/thieving work through the abstraction | 148 tests; `doctor` 35 pass on both profiles |
| one shared frame feeds multiple readers | `FrameScheduler`, step 2 |
| X11/XWayland capture benchmarked and stable | 42x over ImageMagick, step 3 |
| `doctor` identifies backend/session/geometry/focus/health/OCR/outputs/deps | 36 checks; `focus` from KWin `active` |
| reader health degrades on blank/frozen/lost frames | `FrameScheduler.health`, step 2 |
| recovers from resize/recreation without restart | `WindowTracker`; 11 tests |
| replay backend exercises the same reader/event code | `ReplayBackend` + `watcher.py record` |
| portal/PipeWire proof of concept on KDE/CachyOS | `tools/portal_poc.py`, 16.8 ms median |
| click-through overlay above the game | `tools/overlay.py`, verified by compositor screenshot |
| no synthetic input | no input-injection call sites |
| rules not coupled to ImageMagick | routed through `GameInstance`, step 10 |

Steps 1-10 of the implementation order are complete. Step 11 - broader skill
and profile coverage - is now the priority.

Two things were deliberately **not** done, and should not be mistaken for
oversights:

- the portal/PipeWire path stays a proof of concept. Adopting it needs a
  persistent session, restore-token storage, revocation handling, and a
  `CaptureBackend` shaped around subscribing to a stream rather than
  requesting a rectangle;
- the overlay is not wired to `notify()`. Whether alerts belong on screen is
  a product decision, not a foundation primitive.

## September 2026 research pass: strategic implications

A broader research pass across Jagex's official RuneScape material, the
RuneScape Wiki, existing Alt1-style tools, GitHub projects, Steam/community
discussion, and RuneScape forums produced several architectural conclusions that
should influence future work before more skill-specific rules are added.

### Treat observation as replaceable

Jagex is actively developing an official RuneScape plugin/API ecosystem. Its
September 2026 preview explicitly contrasts the new API with the limitations of
simple screen reading and demonstrates plugins for drop tracking, ground-item
alerts, combat gauges, Quest Helper, Clue Trainer, and Necromancy rituals.

Screen Watcher should therefore preserve the value of the current OCR/pixel
work without binding the rest of the application to it. The long-term flow
should be:

```text
observation backend
    -> normalized signal/event
    -> profile/activity state
    -> rule
    -> alert/history/output
```

Possible observation backends:
- current X11/XWayland screenshots, OCR, colour and pixel analysis;
- future Wayland/PipeWire capture;
- recorded fixture/replay input;
- a future sanctioned Jagex API/plugin source if and when it is publicly
  available and appropriate for Screen Watcher.

The rule, alert, history, analytics, and profile layers should not need to know
which backend produced an event.

Reference:
- https://secure.runescape.com/m=news/api--plugins-september-preview

### Preserve the read-only boundary

The project should continue to observe and inform, never generate gameplay
input. Jagex's rules prohibit software that performs gameplay for the player,
generates mouse/keyboard input, communicates directly with the game worlds
outside approved mechanisms, modifies the client, or repeatedly makes
excessive automated requests to Jagex websites.

This means future enrichment such as price lookups should use sanctioned APIs
where available, be cached/rate-limited, and remain ancillary to gameplay
observation.

References:
- https://legal.jagex.com/docs/rules/rules-of-runescape
- https://legal.jagex.com/docs/rules/macro-and-client-features-not-permitted

### Prefer activity/method profiles over one profile per skill

A skill is often too broad to be the useful unit of monitoring. Necromancy
combat and rituals, ordinary Farming and Player-Owned Farm, traditional Hunter
and Big Game Hunter, or ordinary Construction and Fort Forinthry have different
signals and failure states.

Keep all 29 skills represented in the planning documents, but model concrete
activities as first-class identities beneath them. A future profile key should
be able to express both `skill` and `activity`/method.

### Separate global rules from activity rules

Many useful alerts are not skill-specific and should not be copied into every
profile:

- level-up;
- inventory full / nearly full;
- low HP / Prayer / Summoning;
- familiar expiry;
- aura/potion/incense expiry;
- porter depletion;
- lobby/AFK warning;
- Seren spirit / Blessing of the Gods;
- pet acquisition;
- general loot/value thresholds;
- session milestones.

A profile should compose reusable global capabilities with activity-specific
rules rather than duplicate them.

### Prioritise information quality over alert quantity

Official plugin previews and community feedback repeatedly emphasise filtering,
customisation, and avoiding screen/audio clutter. Future alerts should therefore
support stateful policies beyond a simple cooldown:

- `once_per_state`;
- `repeat_only_if_unresolved`;
- `escalate_after`;
- `clear_when`;
- `mute_when_game_focused`;
- separate opportunity / progress / warning / critical classes;
- user-selectable visual, sound, speech, terminal, or persistent outputs.

Reference:
- https://secure.runescape.com/m=news/api--plugins-september-preview


## Second ecosystem research pass: lessons from Alt1, RuneApps, and mature tools

This section records a second research pass focused less on individual
RuneScape mechanics and more on what mature companion tools have already
learned in production. The goal is not to clone Alt1 or any particular app.
Instead, reuse the engineering lessons that repeatedly appear across Alt1,
RuneApps applications, Linux alternatives, official Jagex plugin previews, and
long-running community feature requests.

Several ideas below overlap conceptually with the cross-profile detector ideas
later in this document. They are retained here because they add concrete
operational requirements learned from existing tools: diagnostics, degradation
detection, structural calibration, user correction, extension permissions,
localization, scheduling, historical export, multi-client state, and graceful
fallbacks.

### First-class self-diagnostics: `screen-watcher doctor`

Mature screen-reading tools expose capture troubleshooting, debug overlays,
scan/diagnostic modes, and explicit UI-region selection because silent capture
failure is otherwise extremely difficult to distinguish from "nothing happened
in game".

A future `screen-watcher doctor` command should test, at minimum:

- RuneScape process/window discovery;
- selected capture backend;
- window geometry and renderer visibility;
- each configured region resolving in bounds;
- OCR executable and representative OCR confidence/readability;
- template/icon anchor confidence;
- inventory-grid geometry;
- notification delivery;
- sound playback;
- state/log directory writability;
- profile schema/version compatibility;
- locale and UI scaling assumptions;
- optional network/API dependencies;
- whether expected anchors have drifted from the saved calibration.

Output should be concise PASS/WARN/FAIL with actionable remediation rather than
a raw stack trace. A graphical diagnostic mode should be able to draw or save
the exact regions and anchors Screen Watcher believes it is using.

References:
- https://runeapps.org/apps/alt1/help_alt1
- https://runeapps.org/apps/alt1/alt1

### Detector-health monitoring and degraded mode

A detector can be syntactically healthy while its inputs have become nonsense
after a RuneScape UI, font, renderer, scaling, or layout change. The application
should monitor its own confidence and explicitly enter a degraded state rather
than continue issuing authoritative-looking alerts from bad evidence.

Possible health signals:
- OCR confidence/readability collapses relative to the calibration baseline;
- expected anchor/template disappears for many consecutive samples;
- inventory occupancy becomes physically impossible;
- all slots or all resource bars suddenly report the same state;
- observed region geometry changes without a corresponding calibration change;
- a normally active signal family goes completely silent while independent
  evidence says the activity continues;
- one detector's false-positive/uncertainty rate rises sharply.

Possible states:
- `healthy`;
- `degraded`;
- `uncalibrated`;
- `backend_failure`;
- `profile_incompatible`.

Gameplay alerts from a degraded detector should be suppressed or clearly marked
low-confidence until health recovers.

Community history is important here: RuneScape UI and chat changes have broken
multiple Alt1 screen-reading applications at once, demonstrating that
self-health monitoring is a product requirement rather than a convenience.

Reference:
- https://www.reddit.com/r/runescape/comments/1ge0fgu/

### Structural UI detection instead of coordinates alone

Recent RuneApps work demonstrates a useful alternative to one fixed template:
detect the geometry of repeated UI structures, derive exact scale/position, and
then measure relative to that detected structure.

Future calibration should therefore support:
- repeated-slot/grid geometry detection;
- border/row/column structure detection;
- anchor clusters rather than one fragile pixel template;
- automatic scale estimation;
- relative region derivation from a detected parent component;
- fallback from structural detection to saved coordinates when confidence is
  insufficient.

This is especially promising for:
- backpack slots;
- action bars;
- buff/debuff rows;
- bank preset buttons;
- repeated resource/status modules;
- Make-X interfaces.

Reference:
- https://runeapps.org/forums/viewtopic.php?id=1913

### Generic stability gates for noisy observations

Some mature tools require several identical consecutive readings before
accepting a state change. Screen Watcher already uses confirmation windows in a
few places; this should become a reusable detector primitive.

Possible options:
- `confirm_frames`;
- `confirm_seconds`;
- `minimum_confidence`;
- `majority_of_last_n`;
- `require_consecutive`;
- hysteresis for numeric thresholds;
- separate enter/leave thresholds.

This primitive should be usable by OCR, item recognition, progress bars, buff
icons, resource values, temporary opportunities, and structural calibration.

Reference:
- https://runeapps.org/forums/viewtopic.php?id=1913

### User correction and local detector feedback

A mature detector should learn from operation even when it is not using machine
learning. The user should eventually be able to classify an alert or detected
state as:

- correct;
- false positive;
- wrong event/classification;
- useful but too noisy;
- event was missed;
- detector needs recalibration.

Store this locally with the original event/evidence reference. Session or
profile reports could then show:
- alerts fired;
- accepted/rejected alerts;
- estimated false-positive rate;
- rules most often rejected;
- contexts where errors cluster.

This gives future tuning work evidence instead of memory and makes live play a
repeatable detector-validation process.

Reference:
- https://runeapps.org/forums/viewtopic.php?id=1913

### Graceful manual fallback when observation fails

Not every companion feature needs a live pixel feed. Existing tools often retain
manual timers, browser/checklist views, or history features when screen reading
fails.

Profiles or capabilities should declare degraded/manual fallbacks where useful:
- manual counters;
- manually started absolute timers;
- checklists;
- stored farming/research schedules;
- historical reports;
- manual state selection for a state machine;
- manual "mark resolved" / "resynchronise" actions.

The application should degrade by capability rather than collapsing entirely
because one OCR or capture path failed.

### Extension manifests and a permission model

If Screen Watcher ever supports third-party extensions, do not load arbitrary
Python modules with unrestricted machine access by default. Alt1's permission
model is a useful design lesson.

A future extension manifest could request capabilities such as:
- `event_stream.read`;
- `history.read`;
- `history.write`;
- `screen_regions.read`;
- `ocr.request`;
- `overlay.draw`;
- `network`;
- `notifications.emit`;
- `settings.read`.

Permissions should be explicit, minimal, reviewable, and denied by default.
Core read-only/game-safety guarantees should apply to extensions as well.

Reference:
- https://runeapps.org/apps/alt1/alt1

### Local extension/event API

A controlled local API would let dashboards, trackers, and experimental tools
consume Screen Watcher's normalized events without linking directly against
capture internals.

Possible interfaces:
- local-only HTTP;
- WebSocket event stream;
- Unix domain socket;
- stdin/stdout subprocess protocol.

Example consumers:
- a web dashboard;
- a tiny always-on-top status strip;
- a personal analytics notebook;
- an accessibility output;
- a profile-specific advisor.

The trusted core should own observation, normalization, permissions, and
history. Extensions should normally consume events rather than raw desktop
pixels.

Alt1's web-app ecosystem demonstrates the scalability advantage of a stable host
API over putting every niche feature in the core application.

Reference:
- https://runeapps.org/forums/viewtopic.php?id=101

### Event freshness, provenance, and proof

Every normalized event should eventually carry enough provenance to answer
"why did Screen Watcher believe this was new and real?"

Candidate fields:
- `observed_at`;
- source/game timestamp where available;
- source backend;
- source region;
- source hash/dedup key;
- event age when detected;
- confidence;
- corroborating event IDs;
- profile/activity/session IDs;
- optional evidence-crop reference.

This generalizes the startup-scrollback protection already used for OCR. Mature
trackers reject stale chat events, deduplicate submissions, preserve proof, and
queue network work separately from detection.

Reference:
- https://runeapps.org/forums/viewtopic.php?pid=5879

### Minimal evidence crops and privacy-preserving diagnostics

When evidence images are useful, store only the smallest relevant region rather
than full desktop screenshots.

Potential policy:
- opt-in only;
- short retention by default;
- crop only the source region or event line;
- redact/avoid account names and unrelated chat where practical;
- attach hashes/metadata so an alert can be reproduced;
- fixtures committed to Git must be intentionally sanitized.

This improves false-positive analysis while minimizing privacy exposure and disk
usage.

Reference:
- https://runeapps.org/forums/viewtopic.php?pid=5879

### Localization packs for OCR/chat semantics

Long-running Alt1 apps show that language support and Jagex wording changes are
maintenance concerns. Do not permanently bind semantic rules to English regexes.

Possible layout:
- semantic event: `thieving.stunned`;
- locale packs: English, German, French, and others as verified;
- each locale contains known live wording variants and OCR-tolerant patterns;
- game-language detection/manual selection belongs to profile/session metadata.

This makes it possible to update one phrase without changing detector logic and
makes community contributions safer.

Reference:
- https://runeapps.org/forums/viewtopic.php?id=1324

### Compatibility fingerprints for profiles and calibrations

A saved profile/calibration should record the environment in which it was
verified:

- RuneScape UI generation/build where known;
- game language;
- interface scaling;
- desktop scaling;
- resolution/window geometry;
- capture backend;
- renderer;
- layout fingerprint/anchor confidence;
- profile/schema version;
- last successful validation time.

On startup, compare the live environment to this fingerprint. Significant drift
should trigger a warning or doctor scan instead of silently trusting old
coordinates and thresholds.

### Timer taxonomy: activity, absolute, reset, and schedule

Existing RuneScape companion tools solve several fundamentally different timer
problems. Screen Watcher should not force all of them through one `timer`
rule.

Future timer classes:
- `activity_timer` — resets or pauses based on detected activity;
- `countdown_timer` — manually/event-started duration;
- `absolute_deadline` — fires at a real timestamp and never resets from game
  interaction;
- `periodic_schedule` — recurring interval;
- `daily_reset`;
- `weekly_reset`;
- `monthly_reset`;
- game-tick/activity-specific timers where appropriate.

Examples include AFK warnings, Farming, Archaeology research, Bik troves,
D&Ds, familiar durations, aura expiry, and manually tracked long activities.

References:
- https://runeapps.org/forums/viewtopic.php?id=1736
- https://pc.runeapps.org/forums/viewtopic.php?id=1910

### RuneScape-time and reset calendar

A small `GameCalendar` service could calculate RuneScape/UTC reset boundaries
instead of continuously polling.

Potential uses:
- daily/weekly/monthly activities;
- farm/research schedules;
- D&D windows;
- shops or user-defined routines;
- "time until reset" status;
- low-frequency reminders that work even when no active skill detector needs a
  1.5-second loop.

This should remain a scheduling/information feature, never automatic gameplay.

### Compact status strip and presentation levels

Not every useful state should become a desktop popup. Existing timer/plugin
feedback shows demand for compact persistent status.

Possible presentation modes:
1. notification-only;
2. tiny click-through/status strip;
3. detailed dashboard/diagnostics.

A minimal strip might show:
`Fishing | 3 slots | urn x1 | active 00:42`

Only actionable or selected values should appear. Avoid turning the screen into
a wall of gauges.

References:
- https://runeapps.org/forums/viewtopic.php?id=1736
- https://secure.runescape.com/m=news/api--plugins-september-preview

### Optional click-through overlay

A read-only overlay can place information near the relevant RuneScape UI without
generating input.

Potential uses:
- outline a monitored resource/buff;
- mark the backpack slot that changed;
- show a countdown beside an existing status icon;
- show detector-health/calibration boxes;
- highlight a relevant interface region.

The overlay must be click-through by default and should never decide or execute
a game action.

Examples in the mature ecosystem include ability cues and bank-preset labels.

References:
- https://runeapps.org/forums/viewtopic.php?id=1840

### Manual state-machine resynchronisation

Sequence inference will sometimes drift. Boss timers, Agility courses,
Runecrafting trips, ritual phases, and similar state machines need an explicit
resynchronisation path.

Possible controls:
- `resync` command/hotkey;
- select current state manually;
- resync automatically when an authoritative landmark event appears;
- expose uncertainty rather than pretending the inferred state is exact.

Reference:
- https://runeapps.org/forums/viewtopic.php?id=1375

### Historical performance as a first-class product

Long-running companion tools record more than alerts: floor times, kill times,
party size, deaths, bonus percentages, success/failure history, and trends.

Screen Watcher should eventually maintain profile-appropriate session records
such as:
- lap/floor/kill/trip/cycle durations;
- difficulty/variant metadata;
- deaths/failures;
- resources consumed;
- loot/output;
- XP;
- idle time;
- personal bests and rolling baselines.

This complements, rather than replaces, the generic normalized event history.

Reference:
- https://runeapps.org/forums/viewtopic.php?id=1822

### Stable CSV/JSON export before elaborate dashboards

Structured exports are high value and low coupling. Before investing heavily in
charts, provide stable export schemas for:
- sessions;
- events;
- alerts;
- counters;
- resource ledger;
- performance summaries.

CSV enables spreadsheets; JSON preserves richer event structure. SQLite can
serve built-in querying later.

Reference:
- https://runeapps.org/forums/viewtopic.php?id=1822

### Statistical uncertainty rather than averages alone

RuneScape outcomes are often skewed by rare drops and variable action times.
Analytics should therefore expose:
- median;
- p90/p95;
- variance/standard deviation where meaningful;
- sample count;
- confidence intervals where the model justifies them;
- rolling baseline versus current session.

Do not let one rare outcome distort the meaning of "expected" performance.

References:
- https://pc.runeapps.org/forums/viewtopic.php?id=1850

### Generic resource gain/consumption ledger

Represent inputs and outputs as normalized resource events:

```text
resource.consume(item, quantity, value?)
resource.gain(item, quantity, value?)
```

The same ledger could power:
- Fishing bait/fish;
- Herblore ingredients/potions;
- Archaeology materials/artefacts;
- combat supplies/loot;
- Slayer profit;
- Invention components;
- Runecrafting essence/runes.

Jagex's official Drop Log preview validates the usefulness of jointly tracking
supplies consumed and loot obtained.

Reference:
- https://secure.runescape.com/m=news/api--plugins-september-preview

### Generic importance/value tiers and filters

Events should eventually support more than enabled/disabled.

Possible tiers:
- hidden;
- routine;
- informational;
- opportunity;
- warning;
- critical.

For loot/resources, optional value tiers can supplement explicit include/exclude
lists. Sounds and persistent presentation should be configurable by tier.

This generalizes the filtering concepts used by official Ground Items and other
loot tools.

Reference:
- https://secure.runescape.com/m=news/api--plugins-september-preview

### Data-driven buff/status catalog

Rather than hand-writing every buff detector, maintain a status catalog with
metadata such as:
- semantic ID;
- category;
- icon templates/official identifiers;
- uses duration, stack count, presence, or combination;
- default warning thresholds;
- relevant skills/activities;
- locale/display names.

Potential categories mirror RuneScape's actual status ecosystem: potions,
prayers, abilities, item effects, pets/familiars, skilling, Invention perks, and
boss-specific effects.

Reference:
- https://runescape.wiki/w/Settings/Interfaces/Buff_Bar

### Account-aware prerequisites and optional advisor layer

Some tools use account levels, quests, unlocks, prices, and goals to determine
which methods or equipment are relevant.

Screen Watcher could eventually resolve optional prerequisites such as:
- skill level;
- quest completion;
- equipment/unlocks;
- Grace of the Elves/Brooch requirements;
- manually declared capabilities;
- public/sanctioned account data where available.

The core observer should remain distinct from an optional advisor layer. An
advisor may calculate or suggest, but it must expose assumptions/data sources
and must never operate the game.

Reference:
- https://pc.runeapps.org/forums/viewtopic.php?id=1909

### Multi-client architecture

Alt1 eventually added explicit support for multiple RuneScape clients. Screen
Watcher should avoid global state that makes this difficult.

Each client/session should have independent:
- process/window identity;
- capture backend;
- active profile/activity;
- normalized event stream;
- timers/state machines;
- history/session ID;
- notification routing.

This is another reason to retire global variables such as a single
`ACTIVE_SKILL` as the architecture matures.

### Optional shared/party state

Much later, group activities could exchange selected normalized facts between
trusted Screen Watcher instances, analogous to party-oriented tools that share
keys or encounter state.

Possible uses:
- Dungeoneering;
- group bosses;
- shared timers/checklists.

This requires authentication, privacy controls, and explicit opt-in. It should
exchange normalized state, never game input.

### Optional remote/mobile notifications

Existing AFK tools demonstrate that phone notifications are useful when the
player steps away.

A future companion path could:
- pair explicitly via QR/token;
- send only normalized alerts/status;
- default to local network or privacy-preserving relay;
- never expose screenshots unless explicitly requested.

Reference:
- https://runeapps.org/forums/viewtopic.php?id=1144

### Linux-native operation as a differentiator

Linux alternatives to Alt1 exist partly because the mature ecosystem remains
Windows-centric. Current Linux implementations also identify native Wayland
capture as a major technical boundary.

Screen Watcher should treat:
- X11/XWayland capture;
- Wayland portal/PipeWire capture;
- desktop notifications/audio;
- renderer differences;
- packaging on Arch/CachyOS and other distributions

as first-class engineering concerns rather than compatibility afterthoughts.

Reference:
- https://github.com/Jcapehart2/RuneKit-Reforged

### Presets and progressive disclosure instead of settings overload

Official plugin development has already encountered the problem of too many
independently configurable widgets. Screen Watcher should avoid exposing every
threshold and internal option in one flat settings surface.

Possible UX:
- presets: `minimal`, `standard`, `diagnostic`, `accessibility`;
- profile-provided sensible defaults;
- grouped advanced detector settings;
- expert/raw configuration still available;
- import/export/share settings.

Reference:
- https://secure.runescape.com/m=news/api--plugins-september-preview



## Third technical research pass: capture, interface readers, and trigger data

This pass focuses on implementation details found in Alt1's public developer
libraries, RuneKit's Linux architecture, Better Better Buff Bars, SusAlert,
Clue Trainer, and structured RuneScape Wiki data. It complements the earlier
architectural research by recording concrete techniques that mature tools
already use successfully.

### Shared-frame capture scheduler

The current watcher may perform separate capture work for several regions during
one logical polling cycle. Mature tools instead treat the RuneScape window as a
continuously sampled source and let multiple readers consume one captured frame.

Future design:

```text
GameInstance
    -> CaptureScheduler
    -> one shared frame / frame cache
    -> interface readers
    -> normalized events
```

Requirements:
- capture the game window once per scheduler tick when possible;
- expose zero-copy or low-copy region views into the same frame;
- support detector-specific cadences rather than one global interval;
- avoid recapturing the same pixels for chat, inventory, buffs, resources, and
  progress within the same frame generation;
- expose capture timestamp and backend metadata with every frame;
- allow a backend to advertise a sensible minimum capture interval.

Alt1 exposes an advised `captureInterval`, bound regions, and multi-region
operations. RuneKit similarly caches the most recent frame and avoids a fresh
X11 capture when callers request another image sooner than the backend refresh
rate.

Possible initial cadence classes:
- very fast: combat resources / time-sensitive opportunities;
- fast: buffs, targets, action/progress bars;
- medium: chat and inventory;
- slow: interface relocation/health scans;
- scheduled: Farming, resets, long timers.

References:
- https://runeapps.org/apps/alt1/helpoutput.html
- https://github.com/skillbert/alt1
- https://github.com/Jcapehart2/RuneKit-Reforged

### Direct X11/XComposite/XShm backend

The current ImageMagick `import` subprocess is practical for the first version
but should not remain the long-term hot path.

RuneKit's Linux backend provides a useful architectural reference:
- platform-specific code is isolated behind a `GameInstance`;
- XComposite redirects the RuneScape window;
- XShm is used for efficient shared-memory capture;
- window configure events invalidate/rebuild the backing pixmap;
- the last image is cached;
- callers receive a platform-independent image object.

Screen Watcher should eventually implement a native X11 backend with the same
separation of concerns. A later Wayland backend can use PipeWire/portal capture
without changing detectors.

Do not copy GPL implementation code unless licensing compatibility has been
deliberately reviewed; use the architecture and public protocol concepts as
research guidance.

Reference:
- https://github.com/Jcapehart2/RuneKit-Reforged

### Stable internal pixel representation

Choose one canonical in-memory frame representation for all readers.

Candidate:
- NumPy `uint8` array, shape `(height, width, 4)`;
- document channel order explicitly;
- keep conversions at backend boundaries;
- provide cheap region slicing;
- interoperate efficiently with OpenCV where useful.

RuneKit standardizes on a NumPy BGRA32 representation to avoid repeated channel
swaps between capture and image-processing layers.

### Reusable interface-reader registry

Alt1's public libraries provide separate readers for chat, buffs, the action bar,
RuneMetrics XP counters, target information, boss timers, dialogue, tooltips,
drops, and other interfaces. Screen Watcher should adopt the same separation.

Candidate readers:
- `ChatReader`;
- `InventoryReader`;
- `BuffBarReader`;
- `ActionBarReader`;
- `TargetReader`;
- `RuneMetricsReader`;
- `ProgressReader`;
- `BossTimerReader`;
- `SlayerCounterReader`;
- `DialogReader`.

Each reader should own:
1. discovery/location;
2. structural validation;
3. capture rectangle;
4. parsing;
5. confidence/health;
6. relocation when the interface moves;
7. normalized observations.

Profiles should consume reader events rather than duplicate interface geometry.

Reference:
- https://github.com/skillbert/alt1

### Reader relocation independent of window resize

A RuneScape interface can move while the game window size remains unchanged.
Therefore "window did not resize" is not evidence that saved interface
coordinates are still correct.

Each interface reader should periodically validate its anchors. If confidence
falls below a threshold:
- mark the reader degraded;
- stop trusting its gameplay events;
- attempt reacquisition at a slower retry cadence;
- restore healthy state only after the interface is structurally validated.

This complements the global detector-health model from the second research pass.

### RuneScape-specific sprite OCR

Alt1 uses sprite/pixel-based OCR built around RuneScape font definitions rather
than only general-purpose OCR. This works well because many RuneScape UI fonts
are rendered from predictable sprites.

Future Screen Watcher OCR stack could be:

```text
specialized numeric/sprite OCR
    -> RuneScape chat-font OCR
    -> Tesseract/general OCR fallback
```

High-value first targets:
- resource numbers;
- timers;
- stack counts;
- chat timestamps;
- RuneMetrics numbers;
- progress percentages.

Advantages:
- lower ambiguity for known fonts;
- predictable character set;
- easier confidence scoring;
- easier regression fixtures.

Keep Tesseract for unknown/general text.

References:
- https://github.com/skillbert/alt1/blob/master/docs/ocr.md
- https://github.com/skillbert/alt1/tree/master/src/ocr

### Timestamp-aware differential chat reader

Alt1's chat reader does substantially more than OCR a rectangle. Its public
source demonstrates several techniques worth adapting:

- automatically testing multiple supported chat font sizes;
- identifying different chatbox types structurally;
- using RuneScape chat colours;
- preserving fragments/badges;
- comparing overlap with the previous read;
- using local chat timestamps to reject old lines;
- handling the midnight timestamp wrap;
- normalizing visually confusable characters before line comparison.

Screen Watcher should eventually maintain a persistent `ChatReader` state
instead of independently OCRing the current crop for each rule.

Recommended normalized output:

```text
ChatEvent {
  text
  normalized_text
  chat_type
  source_timestamp
  observed_at
  fragments
  confidence
  dedup_key
}
```

SusAlert independently recommends enabling local chat timestamps because they
improve event accuracy.

References:
- https://github.com/skillbert/alt1/tree/master/src/chatbox
- https://github.com/Raphire/SusAlert

### Profile readiness requirements checked by doctor

Profiles should be able to declare required or recommended RuneScape settings.

Examples:
- local timestamps enabled;
- Game Messages enabled;
- Boss Kill Timer visible;
- Slayer Counter visible;
- specific buff-bar category enabled;
- compatible buff icon size;
- minimum chat font size;
- interface transparency assumptions;
- RuneMetrics panel visible;
- expected UI scale/layout.

`screen-watcher doctor --profile ...` should distinguish:
- hard requirement missing;
- recommended reliability setting missing;
- requirement cannot be verified automatically.

This converts undocumented setup assumptions into machine-readable profile
metadata.

### Mask dynamic timer/stack text before icon matching

Alt1's buff reader removes the timer/count text pixels from an icon before
comparing the underlying artwork. Otherwise the same status icon changes every
second as `59s`, `58s`, and so on.

This should become a generic image-matching capability:
- identify a known dynamic-text subregion;
- OCR/read it separately;
- mask it before template comparison;
- compare only stable icon pixels;
- return both icon identity and dynamic argument/time.

Useful beyond buffs:
- stacks;
- cooldown icons;
- counters;
- other UI elements with stable artwork plus changing numeric overlays.

Reference:
- https://github.com/skillbert/alt1/tree/master/src/buffs

### Buff-bar saturation and category awareness

The RuneScape buff/debuff bars cannot display an unlimited number of effects,
and categories may be disabled. Therefore:

```text
icon missing != status definitely absent
```

A missing-icon alert should only be high-confidence when Screen Watcher knows:
- the relevant buff/debuff category is enabled;
- the bar reader itself is healthy;
- the visible bar is not saturated/overflowing;
- the icon size/layout is supported.

The Wiki documents current display limits of 18 buffs and 12 debuffs and
multiple icon sizes/categories.

Reference:
- https://runescape.wiki/w/Buffs_and_debuffs

### Dual-path resource reading

Alt1's action-bar reader attempts exact numeric OCR for HP, Prayer, Adrenaline,
and Summoning but can fall back to measuring the visual resource bar.

Screen Watcher should use the same principle:

```text
exact text value
    OR
bar proportion
    -> reconciled resource observation
```

If both agree, confidence rises. If OCR fails but the bar remains readable,
alerts still work. If they disagree materially, mark the observation uncertain
and retain both values in diagnostics.

Reference:
- https://github.com/skillbert/alt1/tree/master/src/ability

### Capture/backend context as event evidence

Observation context should include backend state, not only pixels.

Candidate fields:
- capture backend;
- advised/actual capture cadence;
- frame age;
- game-window focus;
- game-window scale;
- game-window geometry;
- renderer if known;
- world number if reliably available;
- time since last observed/user game interaction where permitted;
- frame hash/variance;
- backend health.

Alt1 and RuneKit both expose host/game state alongside pixels. This provides
stronger AFK diagnostics and helps explain detector failures.

### Blank/frozen-frame sentinels

Before sending frames into gameplay detectors, perform cheap sanity checks.

Examples:
- >95% black/near-black;
- zero or near-zero variance;
- impossible dimensions;
- identical full-frame hash for an implausibly long period while game activity
  is otherwise observed;
- capture returns the desktop/background instead of the game client.

A sentinel failure should degrade the backend rather than causing rules to
interpret blank pixels as real game state.

RuneKit already disables overlay behaviour in a detected black-screen condition.

### Authenticated local extension transport

The earlier extension/API plan should require authentication even on localhost.

Possible approach:
- generate a random per-run or persisted local token;
- require it during WebSocket/HTTP handshake;
- Unix-socket peer permissions where supported;
- scope token permissions to declared extension capabilities;
- rotate/revoke credentials.

RuneKit's internal browser/RPC layer uses a secret token and rejects requests
that do not present it.

Reference:
- https://github.com/Jcapehart2/RuneKit-Reforged

### Mutual-exclusion and supersession groups

AFKWarden/community preset ecosystems often need several related alerts where
one status supersedes another. Screen Watcher profiles should support explicit
relationships instead of implementing them through duplicated conditions.

Possible metadata:

```text
group: overload_family
supersedes:
  - overload
  - antifire
  - antipoison
```

Use cases:
- combined potions replacing component effects;
- upgraded familiars/statuses replacing weaker variants;
- mutually exclusive activity modes;
- boss phases where only one phase-specific alert family is valid.

### Wiki-derived local knowledge datasets

The RuneScape Wiki exposes structured data that can reduce manual maintenance.

A future development/update command could build local, versioned snapshots such
as:

```text
data/status-effects.json
data/trigger-reference.json
data/items.json
data/skill-mechanics.json
data/ge-values.json
```

The runtime should consume the local snapshot. Updating it should be explicit or
low-frequency, cached, and respectful of Wiki/Jagex request guidance.

For status effects, useful structured fields include:
- display location;
- buff/debuff classification;
- category;
- priority;
- timer presence;
- stack presence;
- exact description;
- internal/status identifier where exposed.

References:
- https://runescape.wiki/w/Template:Infobox_Buff/doc
- https://runescape.wiki/w/RuneScape:Grand_Exchange_Market_Watch/Usage_and_APIs

### Versioned trigger-reference dataset

Concrete mechanic thresholds discovered from the Wiki should not remain buried
in prose. Preserve them in a versioned local dataset with:
- mechanic ID;
- value/unit;
- source;
- source revision/retrieval date;
- profile(s) using it;
- whether the value is authoritative, inferred, or user-tunable.

Initial research seed:

| Mechanic | Reference fact | Future alert use |
|---|---|---|
| Mining stamina | 100 stamina gives full damage; 1-99 gives 90%; **0 gives 20%** | Strong efficiency warning at 0 stamina; lower thresholds optional/user-defined |
| Mining rockertunity | New rockertunity generally appears about **24-36 s** after one is used | Opportunity expectation/overdue diagnostics |
| Smithing heat | **67-100%** high heat = x2 progress; 34-66% = x1.6; 1-33% = x1.3; 0 = x1 | 67% is a meaningful efficiency-band warning default |
| Archaeology Sprite Focus | **40%** unlocks stronger XP/precision benefit; **100%** bonus action resets focus to 60% | Warn before falling below 40%; treat 100->60 as expected |
| Necromancy ritual disturbances | Eligible disturbances are separated by **12 ritual ticks** | Predict opportunity windows and detect suspiciously missed disturbances |
| Tier-3 ritual glyph lifecycle | Glyphs drawn/repaired together last **36 rituals** | Maintenance counter/warning |
| Fishing Frenzy | Streak is halved after **6 s** without another spot interaction | Warn shortly before the 6-second deadline |
| Seren spirit | Remains available for **30 s** | Immediate opportunity plus optional expiry escalation |
| Divination chronicle | Enhanced catch window is **6 s** | High-priority short countdown |
| Memory Overflow | Duration **10 min** | Status/buff countdown |
| Familiar duration | Standard duration families include **16/32/48/64/96/144 min** and can refresh | Visible-timer warning, with duration as fallback metadata only |
| Sign of the Porter | Uses **stacks/charges, no timer** | Charge thresholds, never expiry-time logic |
| Buff/debuff visible capacity | Up to **18 buffs / 12 debuffs** visible | Missing icon is weak evidence when saturated |
| Invention equipment targets | Level 10 for max disassembly XP; 12 for max siphon XP; 9 often best siphon XP/item-XP efficiency | Configurable target-level alerts |
| Bird's nest on ground | Ground nest disappears after **2 min** | Timed ground-item warning; banked nests can be log-only |

These values must be reverified before being promoted into enabled production
rules. The purpose of the dataset is to preserve research and provenance, not to
hard-code every Wiki number permanently.

### Official RuneScape plugin API: keep the boundary, do not guess the SDK

Jagex's public September 2026 material confirms an active plugin/API ecosystem,
but a complete public SDK/reference comparable to Alt1's developer API was not
identified during this pass.

Do not invent API calls or couple profiles to assumptions about unpublished
interfaces. Continue strengthening Screen Watcher's own observation/event
boundary so an official supported backend can be added later without changing
rule/profile semantics.

Reference:
- https://secure.runescape.com/m=news/api--plugins-september-preview


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

## Whole-game skill profile seeds (all 29 skills)

RuneScape currently has 29 skills. The notes below are deliberately **profile
seeds**, not commitments and not final detector configurations. They answer a
narrower question: if Screen Watcher eventually supports a particular skill,
what would be worth observing first?

A skill should not automatically map to one giant profile. Prefer
activity-specific profiles when the same skill has materially different loops,
interfaces, timers, or failure states. For example, Necromancy combat and
Necromancy rituals should almost certainly be separate profiles; the same is
true of ordinary Construction, Construction Contracts, and Fort Forinthry.

General source:
- https://runescape.wiki/w/Skills
- https://www.runescape.com/game-guide/skills

### Attack

Useful first profile: ordinary melee combat or a specific repeatable combat
encounter rather than "all Attack training".

Potential signals:
- target-information panel present/absent;
- hit chance / damage-potential readout where visible;
- combat XP continuing versus stalling;
- weapon state, hitpoints, prayer, adrenaline, and food supplies.

Possible alerts:
- combat stopped while a target should still be engaged;
- very low hit chance against the current target;
- low food / low hitpoints / low prayer;
- weapon or combat buff expired.

Implementation note: Attack itself is usually not the actionable state. A
generic combat profile should probably own target, resource, and survivability
signals, with Attack simply selecting melee-specific expectations.

References:
- https://runescape.wiki/w/Attack
- https://runescape.wiki/w/Hit_chance

### Strength

Strength training shares most observability with Attack because both normally
occur inside melee combat.

Potential signals:
- target state and combat continuity;
- melee XP gain;
- adrenaline and ability-state changes;
- food, prayer, potion, and weapon/buff state.

Possible alerts:
- target/combat loop stalled;
- consumable or boost expired;
- survivability threshold crossed.

Implementation note: avoid making an independent Strength detector if it would
duplicate the same target and resource checks as an Attack profile. Prefer one
melee-combat profile with configurable XP expectations.

Reference:
- https://runescape.wiki/w/Strength

### Defence

Defence can be trained through multiple combat styles, so its useful profile
signals are primarily defensive rather than style-specific.

Potential signals:
- player hitpoints;
- incoming-damage state;
- prayer points and protection-prayer status;
- defensive buff/debuff icons;
- target state and Defence XP.

Possible alerts:
- low or rapidly falling hitpoints;
- defensive prayer unexpectedly inactive;
- important defensive buff expired;
- combat ended or death/respawn interface appeared.

Implementation note: this is a strong use case for the future generic
resource-bar and buff/debuff detectors.

References:
- https://runescape.wiki/w/Defence
- https://runescape.wiki/w/Constitution

### Constitution

Constitution is the clearest initial use case for a generic life-points
detector because the action bar exposes current and maximum life points.

Potential signals:
- exact or approximate life-point value;
- percentage of maximum life points;
- rate of loss over a short window;
- death status / respawn interface.

Possible alerts:
- warning and critical HP thresholds;
- unusually fast HP loss;
- healing has stopped while damage continues;
- death detected.

Implementation note: make this a reusable `resource` detector rather than a
Constitution-only special case. The same primitive can later serve bosses,
Slayer, Thieving, Dungeoneering, and Necromancy.

Reference:
- https://runescape.wiki/w/Constitution

### Ranged

Useful variants should distinguish ammunition-using setups from weapons or
effects where ammunition monitoring is irrelevant.

Potential signals:
- target information and hit chance;
- combat XP / target continuity;
- ammunition or quiver quantity where visible;
- hitpoints, prayer, adrenaline, food, and potion buffs.

Possible alerts:
- ammunition running low or exhausted;
- combat stopped;
- low hit chance;
- key ranged buff expired.

Implementation note: ammunition rules should be explicitly optional and tested
against the actual weapon/quiver setup rather than assumed for every Ranged
profile.

Reference:
- https://runescape.wiki/w/Ranged

### Magic

Magic has a particularly useful supply-monitoring opportunity because combat
spells consume runes under many setups.

Potential signals:
- rune-pouch or inventory rune quantities;
- "not enough runes" / spell failure messages;
- current spell/buff status;
- target information, hit chance, prayer, adrenaline, and HP.

Possible alerts:
- one required rune type approaching exhaustion;
- spell can no longer be cast;
- combat stopped because a rune supply failed;
- temporary magic buff expired.

Implementation note: a future multi-item supply rule should understand recipes
such as "this action requires A + B + C" and warn on whichever ingredient will
run out first.

References:
- https://runescape.wiki/w/Magic
- https://runescape.wiki/w/Rune_pouch

### Prayer

Prayer points are already represented as a continuously changing UI resource,
making Prayer a natural first target for generic resource monitoring.

Potential signals:
- prayer-point value / percentage;
- drain rate;
- quick-prayer or individual prayer icons;
- restoration-potion inventory.

Possible alerts:
- prayer below warning / critical thresholds;
- prayer unexpectedly deactivated;
- restoration supplies low;
- drain rate unexpectedly high for the current profile.

Implementation note: rate-of-change can be more useful than a fixed threshold:
"45 seconds of prayer remaining at the current drain rate" is often more
actionable than "25% remaining".

Reference:
- https://runescape.wiki/w/Prayer_points

### Summoning

A summoned familiar already exposes a timed status effect, so this skill is a
strong candidate for generic status-timer support.

Potential signals:
- Familiar Summoned buff timer;
- familiar presence;
- Summoning points;
- familiar scroll / special-move points where relevant;
- Beast of Burden inventory state for profiles that depend on it.

Possible alerts:
- familiar expires in 5 / 1 minutes;
- familiar unexpectedly dismissed or killed;
- Summoning points too low to perform a planned action;
- Beast of Burden still contains items before banking or leaving.

Implementation note: familiar duration varies by familiar and can be extended,
so read the visible timer/status instead of assuming a duration from the item
name.

References:
- https://runescape.wiki/w/Summoning
- https://runescape.wiki/w/Summoning_familiars
- https://runescape.wiki/w/Familiar_Summoned

### Necromancy

Necromancy should be split into at least **combat** and **ritual** profiles.

Combat profile ideas:
- conjure presence and remaining duration;
- ectoplasm and necrotic-rune supply;
- target, HP, prayer, adrenaline, and combat continuity;
- important necromancy stack/buff states where visually reliable.

Ritual profile ideas:
- ritual progress;
- ritual disturbances appearing;
- disturbance type / opportunity window;
- glyph/light-source repair state;
- ritual completion and material depletion.

Possible alerts:
- conjure nearing expiry;
- ectoplasm/runes low;
- ritual disturbance appeared;
- ritual ended with an unresolved disturbance;
- glyph set approaching repair/replacement.

Implementation note: ritual disturbances are an ideal test case for a generic
temporary-opportunity detector because they appear during a timed process and
reward prompt attention.

References:
- https://runescape.wiki/w/Necromancy
- https://runescape.wiki/w/Necromancy_training
- https://runescape.wiki/w/Rituals

### Mining

Mining exposes two unusually clear mechanics for Screen Watcher: stamina and
rockertunities.

Potential signals:
- stamina bar;
- mining/progress bar;
- rockertunity highlight;
- ore-box / inventory state;
- current rock depleted or mining XP stopped.

Possible alerts:
- stamina reached zero or fell below an efficiency threshold;
- rockertunity appeared and has not been taken;
- current mining action stopped;
- ore box / inventory nearing capacity.

Implementation note: a visual opportunity detector should recognize
rockertunities without trying to click them. The Wiki notes that rockertunities
are player-specific and persist until consumed, the area is left, or a
different rock type is mined.

Reference:
- https://runescape.wiki/w/Mining

### Fishing

The current Fishing profile remains the reference implementation for catch
activity, inventory fill, urn state, and short-lived events.

Additional profile seeds beyond the detailed Fishing section above:
- separate Deep Sea Fishing profile;
- Fishing Frenzy streak profile;
- method-specific bait or resource rules;
- optional buff/status monitoring;
- method-specific spot-depletion timing rather than one universal timeout.

Reference:
- https://runescape.wiki/w/Fishing

### Woodcutting

Woodcutting has several events that are more actionable than merely watching XP.

Potential signals:
- chopping XP/activity continuity;
- tree depletion;
- bird's-nest chat messages;
- wood-box / inventory capacity;
- temporary Woodcutting buffs;
- special method timers such as elder-tree availability.

Possible alerts:
- tree depleted / chopping stopped;
- bird's nest found, especially special/enchanted nests;
- wood box or backpack nearly full;
- important temporary buff expiring.

Implementation note: nest handling should distinguish "fell to the ground",
"placed in backpack", and "sent to bank" messages so the alert can reflect
whether intervention is actually required.

References:
- https://runescape.wiki/w/Woodcutting
- https://runescape.wiki/w/Bird%27s_nest

### Hunter

Hunter now spans very different loops, especially after the 2026 Havenhythe
update. Do not make a single generic Hunter profile.

Potential profile families:
- traditional trap hunting;
- clockwork box traps;
- clockwork birdhouses;
- Big Game Hunter;
- special creature-specific methods.

Potential signals:
- trap empty / sprung / failed / collected;
- number of active traps;
- birdhouse ready state;
- Big Game Hunter encounter state and danger/failure cues;
- inventory capacity and bait/supply levels.

Possible alerts:
- trap requires collection/reset;
- one expected trap disappeared;
- birdhouse ready;
- BGH encounter changed phase or entered a dangerous state;
- bait/trap supplies low.

Reference:
- https://runescape.wiki/w/Hunter
- https://runescape.wiki/w/Hunter_update

### Farming

Farming profiles should distinguish ordinary patches from Player-Owned Farm /
Dinosaur Farm management.

Patch profile signals:
- growth/ready state;
- disease;
- harvest completion;
- seed, compost, treatment, or produce inventory.

Player-Owned Farm ideas:
- animal growth stage;
- breeding/birth message;
- health/happiness problems;
- food trough state;
- animals ready to gather/check.

Possible alerts:
- crop ready;
- crop diseased;
- patch left empty after harvest;
- animal reached desired stage;
- breeding event occurred;
- farm food or treatment supplies low.

Implementation note: many Farming states evolve while the watcher is not
running. A future scheduled/checklist mode may be more appropriate than a
continuous 1.5-second poll for some Farming profiles.

References:
- https://runescape.wiki/w/Farming
- https://runescape.wiki/w/Farming_training
- https://runescape.wiki/w/Player-owned_farm

### Archaeology

Archaeology is especially suitable for progress/opportunity monitoring.

Potential signals:
- excavation progress bar;
- time-sprite location/active hotspot;
- damaged artefact obtained;
- soil/material inventory;
- material-cache depletion;
- porter / material-storage related state where visible.

Possible alerts:
- time sprite moved;
- artefact completed;
- material cache depleted;
- inventory/soil box full;
- porter or other excavation support expired.

Implementation note: a time-sprite detector is conceptually similar to a
rockertunity detector, suggesting one reusable "active opportunity moved"
primitive for gathering skills.

References:
- https://runescape.wiki/w/Archaeology
- https://runescape.wiki/w/Archaeology_training

### Divination

Divination contains several short-lived opportunities and state changes that
fit Screen Watcher well.

Potential signals:
- chronicle fragment spawn;
- enriched wisp / enriched spring;
- Memory Overflow buff and remaining duration;
- spring depletion;
- memory inventory filling;
- energy availability when using enhanced conversion.

Possible alerts:
- chronicle fragment appeared;
- enriched wisp available;
- Memory Overflow about to expire;
- current spring depleted;
- memory inventory full;
- enhanced conversion fell back because energy ran out.

Implementation note: chronicle fragments and enriched wisps are good candidates
for the generic short-lived-event subsystem. The Wiki documents a 10-minute
Memory Overflow buff after a rift is fully empowered.

References:
- https://runescape.wiki/w/Divination
- https://runescape.wiki/w/Divination_training

### Smithing

Smithing has a highly visible heat/progress model.

Potential signals:
- heat percentage / heat band;
- item progress percentage;
- unfinished item completed;
- auto-heater / coal supply;
- bars/materials remaining.

Possible alerts:
- heat dropped below a configured efficiency band;
- item completed;
- auto-heater cannot reheat due to supply;
- next item cannot start because materials are missing.

Implementation note: this is a strong use case for a generic progress-bar
detector plus a second resource bar. Heat directly changes progress speed, so a
threshold alert can be tied to actual efficiency rather than arbitrary time.

Reference:
- https://runescape.wiki/w/Smithing

### Crafting

Crafting is less about rare events and more about batch state and material
balance.

Potential signals:
- Make-X / crafting progress;
- input stacks decreasing;
- output stacks increasing;
- portable crafter presence where relevant;
- inventory/bank preset failures.

Possible alerts:
- batch finished;
- one ingredient exhausted before the others;
- inventory contains outputs but no usable input combination;
- expected portable station disappeared.

Implementation note: a generic `batch` detector should understand "production
is expected to continue until input X is exhausted" and avoid alerting on every
individual item.

References:
- https://runescape.wiki/w/Crafting
- https://runescape.wiki/w/Pay-to-play_Crafting_training

### Fletching

Fletching shares the same Make-X/batch structure as Crafting but often has
multiple component ratios.

Potential signals:
- production progress;
- logs / shafts / feathers / bowstrings / unfinished items;
- output stack change;
- bank preset success/failure.

Possible alerts:
- batch finished;
- one component is the limiting reagent;
- required knife/tool/interface is no longer available;
- output milestone reached.

Implementation note: future supply rules should support ingredient ratios, e.g.
warn that feathers will run out before shafts even when both stacks are still
non-zero.

Reference:
- https://runescape.wiki/w/Fletching

### Firemaking

Firemaking profiles could cover ordinary log burning, bonfires, and incense
effects separately.

Potential signals:
- log supply and production continuity;
- bonfire/fire state;
- incense buff timer and potency;
- ashes/output inventory where relevant.

Possible alerts:
- incense effect about to expire;
- incense potency reached its desired level;
- log supply low;
- fire/bonfire interaction stopped unexpectedly.

Implementation note: incense is another reason to build a generic buff timer.
The Wiki documents timed incense effects whose potency can rise over time and
whose duration can be extended.

Reference:
- https://runescape.wiki/w/Firemaking

### Cooking

Cooking is an ideal Make-X profile with one extra useful metric: burn rate.

Potential signals:
- raw-food stack falling;
- cooked/burnt output stacks rising;
- Make-X progress;
- cooking station still available.

Possible alerts:
- batch finished;
- raw food exhausted;
- unexpectedly high burn rate over the last N items;
- inventory full or output routing failed.

Implementation note: a rolling ratio detector could report "burn rate changed
materially" without notifying on individual burnt items.

Reference:
- https://runescape.wiki/w/Cooking

### Herblore

Herblore profiles should be recipe-aware because several ingredients may be
consumed at different ratios and high-level potion chains can be multi-stage.

Potential signals:
- Make-X progress;
- herb / unfinished potion / secondary / flask quantities;
- completed potion output;
- portable station state;
- combat/skilling potion buff timers when the same profile also uses potions.

Possible alerts:
- batch finished;
- ingredient imbalance / limiting component;
- wrong intermediate item left after a recipe step;
- expected potion buff about to expire.

Implementation note: combination potions can produce shorter, more
attention-heavy inventories than ordinary two-component potions, so timeout
defaults should be method-specific.

References:
- https://runescape.wiki/w/Herblore
- https://runescape.wiki/w/Herblore_training

### Runecrafting

Runecrafting has strong supply/state opportunities beyond simple XP monitoring.

Potential signals:
- essence remaining;
- rune output;
- pouch fill state;
- pouch degradation/repair messages;
- altar or Runespan siphoning activity;
- current Runespan node/creature depleted.

Possible alerts:
- pouch degraded and lost capacity;
- essence source exhausted;
- expected pouch not filled before a trip;
- Runespan node depleted / siphoning stopped;
- rune-production milestone reached.

Implementation note: pouch state is persistent and method-specific, so a future
profile may need a small state machine rather than one independent regex per
message.

References:
- https://runescape.wiki/w/Runecrafting
- https://runescape.wiki/w/Runecrafting_pouches
- https://runescape.wiki/w/Runespan

### Construction

Construction should be split by training method: Player-Owned House, portable
workbench, Construction Contracts, and Fort Forinthry.

Potential signals:
- Make-X production at workbenches;
- contract objective/completion;
- materials remaining;
- Fort construction progress;
- Fort optimal/shiny hotspot movement;
- blueprint/build completion.

Possible alerts:
- contract completed / next contract needed;
- required material low;
- optimal Fort hotspot moved;
- building completed;
- production batch stopped.

Implementation note: Fort Forinthry is particularly attractive for a visual
opportunity detector because the optimal hotspot moves during construction and
gives better progress.

References:
- https://runescape.wiki/w/Construction
- https://runescape.wiki/w/Construction_Contracts
- https://runescape.wiki/w/Fort_Forinthry

### Agility

Agility profiles should be course-specific because obstacle order and lap time
vary.

Potential signals:
- obstacle completion XP;
- lap completion bonus;
- course position if a stable UI cue exists;
- obstacle failure message;
- run-energy state for methods where it matters.

Possible alerts:
- lap completed / lap milestone;
- expected next obstacle was not completed within a measured interval;
- obstacle failure;
- player appears stuck mid-course.

Implementation note: course profiles could learn a simple ordered event
sequence and detect a missing or repeated step without attempting to navigate
the course.

References:
- https://runescape.wiki/w/Agility
- https://runescape.wiki/w/Agility_training

### Thieving

The current Thieving profile remains the second reference implementation.

Additional profile seeds beyond the detailed Thieving section above:
- target-specific profiles rather than one universal pickpocket profile;
- earlier suspicion-state detection;
- Crystal Mask and aura timers;
- HP monitoring;
- inventory item classification for rare drops;
- method-specific state machines for stalls, chests, Heists, or other targets.

Reference:
- https://runescape.wiki/w/Thieving

### Slayer

Slayer has one of the best existing UI hooks for Screen Watcher: the desktop
interface can display a Slayer Counter with kills remaining.

Potential signals:
- assignment name;
- kills remaining;
- task complete message;
- target/combat continuity;
- HP, prayer, food, potion, ammunition/rune supply;
- task-specific required item or protection state.

Possible alerts:
- 10 / 5 / 1 kills remaining;
- assignment complete;
- required consumable/equipment missing;
- combat stopped unexpectedly;
- rare/high-value drop detected.

Implementation note: Slayer profiles should have a generic task layer plus
optional monster-specific overlays. Do not duplicate full combat logic into
every Slayer task.

References:
- https://runescape.wiki/w/Slayer
- https://runescape.wiki/w/Slayer_assignment
- https://runescape.wiki/w/Slayer_training

### Dungeoneering

Dungeoneering benefits from state-machine monitoring more than ordinary
resource alerts.

Potential signals:
- current floor;
- room/floor completion;
- boss state;
- death counter;
- party/interface state;
- floor timer where present;
- required key/resource or puzzle cues for specific profiles.

Possible alerts:
- death count increased;
- boss/critical room reached;
- floor completed;
- repeated idle/stuck state;
- completion milestone across a floor set.

Implementation note: deaths reduce floor reward unless mitigated, so a death
counter is meaningful evidence to log even if no immediate alert is needed.

Reference:
- https://runescape.wiki/w/Dungeoneering

### Invention

Invention is a cross-cutting skill and should probably supply reusable status
rules to many other profiles.

Potential signals:
- charge-pack level;
- augmented item level;
- item ready for siphoning/disassembly;
- invention material or device production;
- perk/buff state where visible.

Possible alerts:
- charge pack low / critically low;
- augmented item reached a configured level such as a siphon target;
- equipment siphon supply low;
- augmented gear stopped gaining item XP when it should be active.

Implementation note: item level is especially useful because siphoning and
disassembly have intentional level breakpoints. Prefer configurable target
levels rather than hard-coding one "correct" level.

References:
- https://runescape.wiki/w/Invention
- https://runescape.wiki/w/Equipment_level
- https://runescape.wiki/w/Divine_charge

### First-pass priority by reusable detector

When implementation resumes, it may be more efficient to build a reusable
detector and then unlock several skills at once instead of finishing skills one
by one.

High-leverage detector families:

1. **Resource bars / numeric resources** — Constitution, Prayer, Summoning,
   combat profiles, Thieving, Slayer, Dungeoneering.
2. **Buff/debuff icons and timers** — Summoning, Necromancy, Firemaking,
   Fishing, Thieving, combat, Invention.
3. **Progress bars** — Smithing, Archaeology, Necromancy rituals, Construction,
   selected gathering/minigame activities.
4. **Temporary opportunity detection** — Mining rockertunities, Archaeology time
   sprites, Necromancy disturbances, Divination chronicles/enriched wisps,
   Fort Forinthry optimal hotspots.
5. **Make-X / production batches** — Crafting, Fletching, Cooking, Herblore,
   Construction workbenches and selected Smithing workflows.
6. **Task / remaining-count OCR** — Slayer, Dungeoneering, production goals,
   selected minigames.
7. **Target/combat state** — Attack, Strength, Defence, Constitution, Ranged,
   Magic, Necromancy and Slayer.
8. **Inventory classification and multi-ingredient supply** — Magic,
   Runecrafting, Herblore, Fletching, Crafting, Cooking and rare-drop profiles.

## Cross-profile engine ideas

### Normalized observation -> event -> rule pipeline

Before adding many more detector kinds, introduce an internal event model that
decouples observations from decisions.

Example normalized events:

```text
inventory.free_slots.changed
inventory.full
activity.progress
activity.stopped
chat.level_up
resource.hp.changed
resource.prayer.low
status.familiar.expiring
opportunity.spawned
loot.item.gained
session.milestone
```

An OCR line, pixel detector, template match, official API event, or replay
fixture could all produce the same normalized event. Rules would subscribe to
events and maintain state rather than reaching directly into capture code.

Benefits:
- easier migration away from OCR when better signals exist;
- easier unit/replay testing;
- cleaner profile inheritance/composition;
- multi-signal confidence becomes possible;
- analytics can consume the same event stream as alerts.

### Multi-signal event fusion and confidence

The Thieving profile has already demonstrated that a single signal can be
misleading: chat OCR can fail while activity continues, and an activity icon can
temporarily disappear. Future detectors should be able to combine independent
evidence.

Possible rule semantics:

```text
event: thieving_stopped
require:
  any:
    - xp_stalled
    - activity_icon_absent
  and:
    - no_recent_chat_activity
confidence:
  high: 3 corroborating signals
  medium: 2
  low: 1
```

Confidence should be logged even when no notification is shown. A low-confidence
observation may still be useful for diagnostics or later model tuning.

### Global capabilities and activity composition

Instead of copying common rules into every skill profile, allow profiles to
compose reusable capabilities:

```text
extends:
  - global/level-up
  - global/inventory
  - global/afk
  - gathering/base
activity: archaeology-excavation
```

Useful reusable bases:
- `global/base`;
- `combat/base`;
- `gathering/base`;
- `production/base`;
- `timed-opportunity/base`;
- `course-sequence/base`.

Resolved configuration should remain visible in diagnostics so inheritance does
not make alerts mysterious.

### AFK and lobby subsystem

The RuneScape client has a finite inactivity/lobby timer, and AFK warnings are a
common use case in existing screen-reading companions.

Possible future behaviour:
- infer activity/interactions from Screen Watcher's existing signals;
- estimate time since the last credible player/gameplay action;
- configurable warnings before likely lobby;
- suppress warnings when the session is intentionally idle;
- never generate the input that resets the timer.

This should be a global subsystem rather than copied into skill profiles.

Reference:
- https://runescape.wiki/w/Lobby_timer

### Alert state machine and anti-noise policy

Cooldown alone cannot model all useful alert behaviour. Add explicit alert
lifecycle state:

```text
inactive -> warning -> critical -> resolved
```

Possible configuration:
- fire once when entering a state;
- escalate after N seconds;
- repeat only while unresolved;
- clear when evidence recovers;
- remember acknowledgement for the current state;
- mute selected classes while RuneScape is focused;
- allow profiles to set an expected-action window before escalation.

This should reduce notification fatigue as profile coverage expands.

### Configurable and accessibility-oriented outputs

A normalized alert should be routable independently to:
- KDE/desktop notification;
- sound;
- text-to-speech;
- terminal;
- persistent status panel;
- edge/screen flash;
- structured log;
- optional future external integrations.

Different output combinations should be configurable by alert class. This is
useful both for personal preference and for accessibility when an audio-only or
colour-only cue is insufficient.

### Session analytics and local event store

The fishing cycle statistics and thieving coin counter are early examples of a
larger analytics system.

Potential session metrics:
- actions/catches/kills/laps/cycles;
- XP and XP/hour;
- items gained and consumed;
- active vs idle time;
- bank/travel/production time;
- median, p95, best/worst cycle;
- stop/failure counts;
- alert frequency and acknowledgement;
- estimated time/actions remaining to a target.

Keep JSONL as a transparent debug/export format, but consider SQLite once
queries span many profiles and sessions. A future schema could separate raw
observations, normalized events, sessions, alerts, and persistent counters.

Jagex's official Drop Log preview reinforces the value of historical supplies,
drops, ledgers, and charts.

Reference:
- https://secure.runescape.com/m=news/api--plugins-september-preview

### Layout fingerprinting and resilient calibration

RuneScape's desktop UI is highly configurable: windows can be moved/resized,
multiple layouts can be saved, and interface scaling changes component sizes.
Hard-coded edge offsets should therefore remain a starting point rather than the
final calibration model.

Possible improvements:
- detect known interface anchors by template;
- save multiple named layout calibrations;
- store UI scale/resolution with calibration;
- fingerprint backpack/chat/buff-bar geometry and auto-select a matching layout;
- invalidate/recalibrate when confidence falls below a threshold.

References:
- https://runescape.wiki/w/Interface
- https://runescape.wiki/w/Loading_screen

### Capture backend abstraction

Current capture is X11/XWayland-oriented. Before capture assumptions spread
through more modules, define a backend interface.

Candidate backends:
- X11/XWayland;
- Wayland portal/PipeWire;
- replay/fixture backend;
- future sanctioned game/API backend.

The capture tests should also cover RuneScape rendering changes such as Vulkan
versus other renderer paths where those change capture behaviour.

### Replay fixture library

Extend the existing evidence-replay idea into a maintained regression corpus.

Examples:
- `fixtures/thieving/stunned/`;
- `fixtures/fishing/zero-urns/`;
- `fixtures/mining/rockertunity/`;
- `fixtures/archaeology/time-sprite/`;
- `fixtures/necromancy/ritual-disturbance/`.

Each fixture should contain only the smallest sanitized regions needed, plus
expected normalized events. This allows detector changes to be tested without
waiting for rare events to occur live.

### High-value next activity experiments

The research pass suggests several activities that exercise reusable engine
capabilities rather than only adding one more profile:

1. **Archaeology excavation** — progress, time sprite, artefacts, material
   caches, porters, inventory/storage.
2. **Mining** — stamina, rockertunities, progress, ore box, activity stop.
3. **Necromancy rituals** — progress, disturbances, depleted/missing glyphs,
   pedestal/material state. Jagex's own Rituals Helper targets many of these
   exact visibility problems.
4. **Farming / Player-Owned Farm** — persistent timers and low-frequency checks
   rather than continuous polling.
5. **Combat/Slayer base** — HP, Prayer, buffs, familiar, target, cooldowns,
   supplies and assignment state.
6. **Agility course analytics** — ordered obstacle sequence, lap timing,
   completion statistics and stuck detection.
7. **Runecrafting** — pouch/essence state, trip sequence, production rate and
   optional cached price enrichment.

Reference:
- https://secure.runescape.com/m=news/api--plugins-september-preview

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

### Generic progress-bar detector

Several skills expose progress directly rather than only through chat or XP.

Candidate uses:
- Smithing item progress;
- Archaeology excavation progress;
- Necromancy ritual progress;
- Fort Forinthry construction progress;
- boss/activity progress bars in future non-skill profiles.

Possible configuration:

```text
kind: progress
region: activity_progress
warn_below_rate: ...
complete_at: 100
stalled_seconds: ...
```

The detector should support both absolute percentage and rate-of-change. A
progress bar that is still moving slowly is different from one that has stopped.

Reference:
- https://runescape.wiki/w/Interface

### Generic production / Make-X batch detector

Crafting, Fletching, Cooking, Herblore, Construction workbenches, and several
other production methods share the same basic loop: open a production
interface, consume one or more inputs repeatedly, create outputs, then stop.

A reusable batch detector could model:
- expected input ingredients and ratios;
- current output quantity;
- production progress/interface presence;
- expected batch end;
- unexpected early stop;
- limiting ingredient.

Possible alerts:
- batch complete;
- production stopped early;
- ingredient X will run out first;
- bank preset did not restore the expected recipe.

This is preferable to implementing "Crafting stopped", "Cooking stopped",
"Herblore stopped", etc. as independent copies of the same logic.

### Ordered-sequence / lightweight state-machine rules

Some profiles are not well represented by independent thresholds.

Candidate uses:
- Agility obstacle order and lap completion;
- Dungeoneering floor stages;
- Runecrafting bank -> travel -> altar -> return loops;
- Construction Contracts;
- Big Game Hunter;
- multi-stage potion or production chains.

Possible concept:

```text
kind: sequence
states:
  - banked
  - travelling
  - producing
  - returning
timeout_by_state: ...
reset_on: ...
```

The goal is not automation. It is to recognize when an expected sequence has
stalled, skipped a necessary step, or completed.

### Profile composition / reusable bases

As profile coverage grows, copying common rules into every JSON file will
create configuration drift.

Possible future structure:
- reusable `combat-base` for HP/prayer/target state;
- reusable `production-base` for Make-X and inventory flow;
- reusable `gathering-base` for activity continuity and capacity;
- small activity profiles that override only method-specific regions and rules.

Any inheritance/composition system should remain explicit when loaded so the
operator can see the final resolved rules. Avoid hidden inheritance that makes
it difficult to explain why an alert fired.

### Profile prerequisites and optional capabilities

Profiles should be able to state that some rules only make sense when a
particular item, unlock, interface, or activity is present.

Examples:
- Seren spirit alert requires Grace of the Elves;
- divine blessing alert requires Brooch of the Gods;
- porter warnings require a porter source;
- Slayer Counter rules require that interface to be visible;
- Crystal Mask rules only apply when using Crystal Mask.

Possible metadata:

```text
requires:
  equipment: [...]
  unlocks: [...]
  visible_interfaces: [...]
optional_features:
  seren_spirit: false
```

This would help prevent the future-ideas backlog from becoming a collection of
alerts that are technically supported but irrelevant to the current setup.

### Evidence recording and detector replay

The current promotion criteria require live measurements. A future recording
mode should make that process reproducible.

Possible workflow:
1. record bounded, sanitized frames/OCR results for selected regions;
2. annotate important events and false positives;
3. replay them through detector code without RuneScape running;
4. compare old and new detector behaviour before changing thresholds.

This would be particularly useful for:
- rockertunities;
- time sprites;
- ritual disturbances;
- target/combat state;
- Make-X completion;
- tooltips and inventory overlays.

Recorded fixtures must remain opt-in and sanitized; account names, chat, or
other personal information should not be committed accidentally.

### Template/icon matching with per-layout calibration

Many future ideas depend on stable icons rather than OCR: buffs, debuffs,
rockertunities, time sprites, familiar state, activity opportunities, and
inventory items.

A generic image/template detector should:
- allow several scale variants or tolerate small UI scaling changes;
- use a confidence score rather than exact pixel equality;
- expose calibration/diagnostic output;
- define a minimum persistence time when flicker is possible;
- support profile-specific templates without hard-coding them into Python.

Do not assume one user's interface scale or theme will generalize to every
layout.

### Scheduled / low-frequency observation mode

Not every skill benefits from a 1.5-second polling loop. Farming in particular
contains real-time growth states that can change on much longer timescales.

A future profile could declare a slower observation cadence or scheduled checks
for:
- Farming patches;
- Player-Owned Farm animals;
- long cooldowns;
- daily/periodic activities.

This should remain separate from desktop automation: Screen Watcher would only
observe and notify when the game/interface is available, not log in or perform
the activity.

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
8. Prefer a reusable normalized event over a new evaluator when several profiles
   could consume the same signal.
9. Record whether the alert can be corroborated by a second independent signal
   and what confidence level should be assigned.
10. Confirm that any web/API integration respects Jagex rules, sanctioned
    interfaces, and conservative request rates.
11. Record detector-health expectations and the conditions that should mark a
    profile or observation source degraded.
12. Record the locale, UI scale/layout, capture backend, and other compatibility
    assumptions used when the detector was validated.
13. Prefer a reusable stability gate/confirmation primitive over detector-specific
    sleeps or repeated-frame hacks.
14. Define whether the capability has a manual/degraded fallback and how a user
    can resynchronise it if inferred state drifts.
15. Ensure newly persisted evidence follows the minimum-crop/privacy rule and
    can be exported without exposing unrelated desktop content.
16. Prefer one shared frame and scheduled reader cadences over repeated
    independent captures when several detectors inspect the same game state.
17. Document which reusable interface reader owns an observation; profiles
    should not duplicate interface-discovery logic.
18. For chat-triggered alerts, document freshness/dedup strategy and whether
    local timestamps materially improve reliability.
19. For buff/status absence, document saturation/category assumptions before
    treating a missing icon as proof of expiry.
20. If a rule uses a Wiki-derived numeric threshold, record the source and
    revision/retrieval date in the trigger-reference dataset.

Items can remain in this backlog indefinitely. Presence here means only that the
idea may be useful later.

"""PASS/WARN/FAIL diagnostics for the whole observation stack.

Extracted from `watcher.py` as part of the modular split. A leaf: nothing
in the application calls into it, so it can move without disturbing the
paths it inspects.

It imports `watcher` lazily, inside the functions that need it. Importing
at module scope would be circular - `watcher` re-exports these names - and
the lazy import has a second benefit: the checks resolve `watcher.ocr` and
`watcher.capture_array` at call time, so a test patching those still
reaches them.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess                                        # noqa: F401
import sys
from dataclasses import dataclass
from pathlib import Path

from screen_watcher.capture import (
    BACKENDS, CaptureBackend, CaptureError, GameInstance, ReplayBackend,
    make_backend,
)
from screen_watcher.kwin import (
    _session_is_wayland, kwin_available, kwin_find, kwin_scale, kwin_windows,
)
from screen_watcher.windows import ensure_x_env, find_window, window_size


def _w():
    """The `watcher` module, imported late.

    Importing at module scope would be circular, since `watcher`
    re-exports these checks. Resolving it per call also means a test
    patching `watcher.ocr` or `watcher.capture_array` still reaches the
    diagnostics that use them.
    """
    import watcher
    return watcher


# --------------------------------------------------------------------------
# doctor
#
# Priority 0, step 4. Silent capture failure is otherwise indistinguishable
# from "nothing happened in game" - the watcher keeps polling, reports no
# alerts, and looks healthy. Every check below exists because some form of
# that confusion has already cost time during development.
# --------------------------------------------------------------------------

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Check:
    """One diagnostic result, in the shape the spec asks doctor to print."""

    area: str
    verdict: str
    detail: str
    remedy: str = ""


def _check_session() -> list[Check]:
    """X/Wayland session reachability."""
    out = []
    ensure_x_env()
    display = os.environ.get("DISPLAY")
    xauth = os.environ.get("XAUTHORITY", "")
    # XDG_SESSION_TYPE is empty outside the desktop session (a service, an
    # agent shell), so fall back to detecting the compositor directly rather
    # than reporting "unknown" for a perfectly identifiable session.
    session = (os.environ.get("XDG_SESSION_TYPE")
               or ("wayland" if _session_is_wayland() else "unknown"))
    wayland = os.environ.get("WAYLAND_DISPLAY")
    if display:
        out.append(Check("session", PASS,
                         f"DISPLAY={display} session={session}"
                         + (f" wayland={wayland}" if wayland else "")))
    else:
        out.append(Check("session", FAIL, "no DISPLAY",
                         "start from a desktop session, or ensure a "
                         "plasmashell/kwin process is running so the "
                         "X cookie can be recovered"))
    if xauth and not Path(xauth).exists():
        out.append(Check("xauthority", WARN,
                         f"XAUTHORITY={xauth} does not exist",
                         "the cookie rotates on login; it is re-read "
                         "automatically from the running session"))
    elif xauth:
        out.append(Check("xauthority", PASS, xauth))
    return out


def _check_kwin(cfg: dict) -> list[Check]:
    """KWin read-only window discovery (Priority 0, step 7).

    Advisory, never fatal: capture still runs through XWayland, so a session
    without KWin scripting is fully supported. What KWin adds is state X11
    cannot express - an explicitly minimised window rather than an absent
    one, and real focus - which is why it is reported separately.
    """
    ok, why = kwin_available()
    if not ok:
        return [Check("kwin", WARN, why,
                      "optional; X11 discovery is used instead")]

    wm_class = cfg["window"]["wm_class"]
    try:
        windows = kwin_windows()
    except Exception as e:                       # noqa: BLE001 - diagnostic
        return [Check("kwin", WARN, f"{type(e).__name__}: {e}",
                      "optional; X11 discovery is used instead")]
    if not windows:
        return [Check("kwin", WARN, "script returned no windows",
                      "KWin scripting may be restricted on this session")]

    out = [Check("kwin", PASS, f"{len(windows)} windows via KWin scripting")]
    win = kwin_find(wm_class, windows)
    if win is None:
        out.append(Check("kwin:window", WARN,
                         f"no window for class {wm_class!r}",
                         "start RuneScape, or correct window.wm_class"))
        return out

    state = ("minimized" if win.minimized
             else "active" if win.active else "mapped")
    out.append(Check("kwin:window", PASS,
                     f"{win.caption or wm_class} {win.width}x{win.height} "
                     f"logical, {state}"))

    # Focus is an acceptance criterion in its own right, and X11 discovery
    # cannot answer it: a mapped window and a focused one look identical
    # through `xdotool search`.
    if win.minimized:
        out.append(Check("focus", WARN, "game window is minimised",
                         "capture will fail until it is restored"))
    else:
        out.append(Check("focus", PASS,
                         "game has focus" if win.active
                         else "game visible but not focused"))

    pixels = window_size(find_window(wm_class, visible_only=False) or "")
    scale = kwin_scale(win, pixels) if pixels else None
    if scale is not None:
        out.append(Check("kwin:scale", PASS,
                         f"{scale:.2f}x logical -> pixels "
                         f"({win.width} -> {pixels[0]})"))
    elif pixels:
        out.append(Check("kwin:scale", WARN,
                         f"logical {win.width} vs pixels {pixels[0]}",
                         "KWin and X11 may be describing different windows"))
    return out


def _check_tools() -> list[Check]:
    """External executables the current paths depend on."""
    out = []
    required = {
        "xdotool": "window discovery",
        "tesseract": "OCR rules",
        "notify-send": "desktop notifications",
    }
    optional = {
        "import": "ImageMagick fallback capture",
        "paplay": "per-rule alert sounds",
    }
    for tool, why in required.items():
        path = shutil.which(tool)
        out.append(Check(tool, PASS, path) if path else
                   Check(tool, FAIL, f"not found ({why} unavailable)",
                         f"install {tool}"))
    for tool, why in optional.items():
        path = shutil.which(tool)
        out.append(Check(tool, PASS, path) if path else
                   Check(tool, WARN, f"not found ({why} unavailable)",
                         f"install {tool} if you want {why}"))
    return out


def _check_backends(requested: str | None) -> tuple[list[Check], CaptureBackend]:
    """Which capture backends work, and which would actually be used."""
    out = []
    for name in sorted(BACKENDS):
        ok, why = BACKENDS[name]().available()
        # An unconfigured replay backend is the normal state, not a fault:
        # it is opt-in for reproducing detector bugs. Warning about it on
        # every run trains people to ignore the warning column.
        if not ok and name == ReplayBackend.name and requested != name:
            out.append(Check(f"backend:{name}", PASS, "not configured "
                             "(opt-in; see SCREEN_WATCHER_REPLAY)"))
            continue
        out.append(Check(f"backend:{name}", PASS if ok else WARN, why,
                         "" if ok else "this backend will not be used"))
    selected = make_backend(requested)
    ok, why = selected.available()
    if ok:
        out.append(Check("backend", PASS, f"using {selected.name}"))
    else:
        out.append(Check("backend", FAIL, f"{selected.name}: {why}",
                         "no usable capture backend; install python-xcffib "
                         "or ImageMagick"))
    return out, selected


def _check_window(cfg: dict, game: GameInstance) -> list[Check]:
    """Game window discovery and geometry."""
    wm_class = cfg["window"]["wm_class"]
    if not game.acquire():
        return [Check("window", FAIL, f"no window for class {wm_class!r}",
                      "start RuneScape, or correct window.wm_class "
                      "in the profile")]
    w, h = game.size
    out = [Check("window", PASS, f"{game.handle} {w}x{h} class={wm_class}")]
    backend = getattr(getattr(game, "backend", None), "name", "")
    for problem in _w().check_fingerprint(cfg, (w, h), backend):
        out.append(Check("fingerprint match", WARN, problem,
                         "recalibrate the regions for this window, or "
                         "restore the recorded size"))
    if w < 800 or h < 600:
        out.append(Check("geometry", WARN, f"window is small ({w}x{h})",
                         "regions were calibrated on a larger window and "
                         "may not resolve meaningfully"))
    return out


def _check_regions(cfg: dict, game: GameInstance) -> list[Check]:
    """Every configured region must resolve inside the window."""
    if not game.size:
        return [Check("regions", WARN, "skipped (no window)")]
    W, H = game.size
    out = []
    for name, region in cfg["_regions"].items():
        x, y, w, h = region.resolve(game.size)
        if w <= 0 or h <= 0:
            out.append(Check(f"region:{name}", FAIL,
                             f"degenerate box {(x, y, w, h)}",
                             "fix the region's w/h in the profile"))
        elif (w, h) != (region.w, region.h):
            # `resolve` clamps to the window rather than returning an
            # out-of-bounds box, so a size mismatch is the only visible sign
            # that the region no longer fits where it was calibrated.
            out.append(Check(f"region:{name}", WARN,
                             f"clamped to {w}x{h} from configured "
                             f"{region.w}x{region.h} in a {W}x{H} window",
                             "re-run calibrate at this window size"))
        else:
            out.append(Check(f"region:{name}", PASS, f"{(x, y, w, h)}"))
    return out


def _check_grids(cfg: dict, game: GameInstance) -> list[Check]:
    """Inventory grid geometry must fit inside its own region."""
    out = []
    for name, region in cfg["_regions"].items():
        if not region.grid:
            continue
        x0, y0, cw, ch, cols, rows = region.grid
        span_w, span_h = x0 + cols * cw, y0 + rows * ch
        if span_w > region.w or span_h > region.h:
            out.append(Check(f"grid:{name}", FAIL,
                             f"grid spans {span_w}x{span_h} inside a "
                             f"{region.w}x{region.h} region",
                             "recalibrate the grid origin or cell size"))
        else:
            out.append(Check(f"grid:{name}", PASS,
                             f"{cols}x{rows} cells, {cw}x{ch} px"))
    return out


#: Panel title expected above each region, keyed by region name. RS3 draws
#: a title bar on every movable window, so the header is the cheapest proof
#: that a region still points at the panel it was calibrated against.
#: Panel titles RS3 draws that a region might land on by mistake. Used to
#: say WHICH panel a misplaced region is reading, rather than only that
#: the expected one is absent.
_KNOWN_PANELS = ("SKILLS", "BACKPACK", "ALL CHAT", "EQUIPMENT", "PRAYER",
                 "MAGIC", "MINIMAP", "NOTES", "FRIENDS", "QUEST")

_EXPECTED_PANEL = {
    "backpack": ("BACKPACK",),
    "chat_tail": ("ALL CHAT", "CHAT"),
}


def _check_panel_identity(cfg: dict, game: GameInstance) -> list[Check]:
    """Is each region still looking at the panel it was calibrated on?

    Regions are fixed pixel offsets, and the wiki is explicit that
    "every window on the screen may be moved and resized, and almost all
    of them can be removed". So a rearranged interface silently points a
    region at whatever now occupies those pixels.

    That is the dangerous failure, not a crash. Observed live: with the
    Skills panel docked beside the Backpack, the `backpack` region read
    the SKILLS panel and `count_occupied` returned 28/30 - a confident
    "pack full" built from skill-level digits. Every inventory rule
    would have acted on it, and nothing in `doctor` said a word.

    RS3 draws a title bar above each movable window, so reading the
    strip just above a region is a cheap identity check. A missing
    header is a warning rather than a failure: the panel may simply be
    configured without title bars, which is a legitimate setting.
    """
    out = []
    if not game.handle or not game.size:
        # No window: `_check_window` already reports that, and every
        # region check below it is skipped for the same reason.
        return out
    for name, expected in _EXPECTED_PANEL.items():
        region = cfg["_regions"].get(name)
        if region is None:
            continue
        x, y, w, h = region.resolve(game.size)
        top = max(0, y - 46)
        if y - top < 8:
            continue                      # region is flush with the top edge
        try:
            strip = game.backend.grab_array(game.handle, (x, top, w, y - top))
        except (CaptureError, ValueError, OSError):
            continue
        if strip.size == 0:
            continue
        seen = _w().ocr_array(strip).upper()
        if any(title in seen for title in expected):
            out.append(Check(f"panel:{name}", PASS,
                             f"{expected[0]} header found above the region"))
            continue
        # Naming the panel actually found is the difference between a
        # warning someone can act on and one they learn to ignore. Live,
        # this read "SKILLS" above the backpack region - which is the
        # whole diagnosis in one word.
        other = [t for t in _KNOWN_PANELS
                 if t in seen and t not in expected]
        if other:
            detail = (f"reading the {other[0]} panel, not "
                      f"{expected[0]}")
            remedy = (f"the panels have been rearranged - re-measure "
                      f"{name} with `shot {name}`; its rules are acting "
                      f"on the wrong pixels")
        else:
            shown = " ".join(seen.split())[:40] or "nothing readable"
            detail = f"expected {expected[0]} header, read {shown!r}"
            remedy = (f"the {name} panel may have moved, or title bars "
                      f"are hidden - check with `shot {name}`")
        out.append(Check(f"panel:{name}", WARN, detail, remedy))
    return out


def _check_layout(cfg: dict, game: GameInstance) -> list[Check]:
    """Scan where the interface panels actually are, and remember it.

    A profile's regions are fixed offsets; the wiki is explicit that
    "every window on the screen may be moved and resized". Scanning the
    live window means one profile can serve several players with
    different layouts, and can notice when one person rearranges theirs
    mid-session.

    Reported, never enforced: the scan is evidence for the operator, and
    a failed scan must not stop a watcher whose regions are still fine.
    """
    from screen_watcher import layout as layout_mod
    if not game.handle or not game.size:
        return []
    try:
        frame = game.backend.grab_array(game.handle,
                                        (0, 0, game.size[0], game.size[1]))
    except (CaptureError, ValueError, OSError):
        return []
    found = layout_mod.scan(frame)
    names = sorted(found.get("titles") or {})
    if found.get("chat"):
        names.append("chat")
    if not names:
        return [Check("layout", WARN, "no interface panels recognised",
                      "title bars may be hidden; regions are unverified")]

    out = [Check("layout", PASS, f"{len(names)} panels found: "
                 + ", ".join(names))]
    state = Path(_w().STATE_DIR)
    if layout_mod.changed_since(state, found):
        out.append(Check(
            "layout:changed", WARN,
            "the interface has moved since the last scan",
            "re-measure any region whose panel moved - its rules are "
            "reading whatever now occupies those pixels"))
    layout_mod.save(state, found)
    return out


def _check_capture(cfg: dict, game: GameInstance) -> list[Check]:
    """Actually capture each region and judge the frames.

    A region that resolves in bounds can still come back blank, so this
    samples real pixels rather than trusting the geometry alone.
    """
    if not game.handle:
        return [Check("capture", WARN, "skipped (no window)")]
    sched = _w().FrameScheduler(game, cfg["_regions"])
    out = []
    for _ in range(2):                    # two passes to detect frozen frames
        sched.begin()
        sched.prefetch(list(cfg["_regions"]))
    for name in sorted(cfg["_regions"]):
        stat = sched.stats.get(name)
        if stat is None or stat.captures == 0:
            out.append(Check(f"capture:{name}", FAIL,
                             stat.last_error if stat else "not captured",
                             "check the region and backend"))
            continue
        try:
            frame = sched.frame(name)
        except CaptureError as e:
            out.append(Check(f"capture:{name}", FAIL, str(e)[:120]))
            continue
        spread = float(frame.std())
        if spread < 0.5:
            out.append(Check(f"capture:{name}", WARN,
                             f"frame is flat (std {spread:.2f}) - blank or "
                             f"occluded?",
                             "check the interface is open and visible"))
        else:
            out.append(Check(f"capture:{name}", PASS,
                             f"{stat.mean_ms:.1f} ms, std {spread:.1f}"))
    return out


def _check_ocr(cfg: dict, game: GameInstance) -> list[Check]:
    """Read a text region and judge whether OCR produced usable words.

    OCR that returns noise looks identical to a quiet chat log from the
    outside, which is exactly the failure this command exists to surface.
    """
    if not shutil.which("tesseract"):
        return [Check("ocr", FAIL, "tesseract not installed",
                      "install tesseract and English language data")]
    if not game.handle:
        return [Check("ocr", WARN, "skipped (no window)")]
    text_regions = sorted({r["region"] for r in cfg["rules"]
                           if r.get("enabled", True)
                           and r["kind"] in ("ocr", "activity", "supply",
                                             "loot", "counter", "timer")
                           and r["region"] in cfg["_regions"]})
    if not text_regions:
        return [Check("ocr", PASS, "no text rules in this profile")]
    out = []
    for name in text_regions:
        box = cfg["_regions"][name].resolve(game.size)
        try:
            text = _w().ocr(game.handle, box)
        except Exception as e:
            out.append(Check(f"ocr:{name}", FAIL, f"{type(e).__name__}: {e}"))
            continue
        words = re.findall(r"[A-Za-z]{3,}", text)
        # Numeric readouts (timers, counters) are legitimately word-free, so
        # digit groups count as readable tokens too. Judging them by word
        # count alone reports a healthy timer as noise.
        numbers = re.findall(r"\d{2,}", text)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        tokens = len(words) + len(numbers)
        if not lines:
            out.append(Check(f"ocr:{name}", WARN, "no text read",
                             "expected for an empty chat; suspicious if the "
                             "region should contain text"))
        elif tokens < 2:
            out.append(Check(f"ocr:{name}", WARN,
                             f"{len(lines)} lines but only {tokens} "
                             f"readable token(s)",
                             "OCR may be reading noise; verify the region "
                             "with `shot`"))
        else:
            out.append(Check(f"ocr:{name}", PASS,
                             f"{len(lines)} lines, {len(words)} words, "
                             f"{len(numbers)} numbers"))
    return out


def _check_outputs() -> list[Check]:
    """Notification, sound, and state-directory writability."""
    out = []
    if shutil.which("notify-send"):
        out.append(Check("notifications", PASS, "notify-send present"))
    else:
        out.append(Check("notifications", FAIL, "notify-send missing",
                         "install libnotify"))
    if _w().SOUND_DIR.exists():
        n = len(list(_w().SOUND_DIR.glob("*.oga")))
        out.append(Check("sounds", PASS, f"{n} sounds in {_w().SOUND_DIR}"))
    else:
        out.append(Check("sounds", WARN, f"{_w().SOUND_DIR} not found",
                         "per-rule sounds will be skipped; alerts still "
                         "deliver"))
    try:
        _w().STATE_DIR.mkdir(exist_ok=True)
        probe = _w().STATE_DIR / ".doctor-write-test"
        probe.write_text("ok")
        probe.unlink()
        out.append(Check("state dir", PASS, f"{_w().STATE_DIR} writable"))
    except OSError as e:
        out.append(Check("state dir", FAIL, f"{_w().STATE_DIR}: {e}",
                         "alert and occupancy history cannot be recorded"))
    return out


def _check_profile(cfg: dict, path: Path) -> list[Check]:
    """Profile identity, rule coverage, and region references."""
    out = [Check("profile", PASS,
                 f"{path} skill={cfg.get('skill', 'unnamed')!r} "
                 f"type={cfg.get('profile_type', 'skill')} "
                 f"schema={cfg.get('schema_version', _w().SCHEMA_VERSION)}")]
    fp = cfg.get("fingerprint")
    if not isinstance(fp, dict):
        out.append(Check("fingerprint", WARN, "profile records no calibration "
                         "assumptions",
                         "add a fingerprint so a resolution change is "
                         "reported rather than silently misreading regions"))
    else:
        recorded = fp.get("window_size")
        out.append(Check("fingerprint", PASS,
                         f"calibrated at {recorded[0]}x{recorded[1]}"
                         if isinstance(recorded, list) and len(recorded) == 2
                         else "recorded"))
    enabled = [r for r in cfg["rules"] if r.get("enabled", True)]
    disabled = len(cfg["rules"]) - len(enabled)
    if not enabled:
        out.append(Check("rules", FAIL, "no enabled rules",
                         "the watcher will refuse to start"))
    else:
        out.append(Check("rules", PASS,
                         f"{len(enabled)} enabled, {disabled} disabled"))
    # Name them. A disabled rule is silent by design, which is exactly what
    # makes an accidental one impossible to notice: the watcher runs, the
    # other alerts arrive, and the missing detector looks like an activity
    # that simply never happened.
    off = [r["name"] for r in cfg["rules"] if not r.get("enabled", True)]
    if off:
        out.append(Check("rules disabled", WARN, ", ".join(off),
                         "these detectors never fire; enable them in the "
                         "profile if that is not deliberate"))
    unused = sorted(set(cfg["_regions"])
                    - {r["region"] for r in enabled})
    if unused:
        out.append(Check("regions unused", WARN,
                         f"captured by no enabled rule: {', '.join(unused)}",
                         "harmless, but they cost nothing to remove"))
    sounds = [r.get("sound") for r in enabled if r.get("sound")]
    if len(sounds) != len(set(sounds)):
        dupes = sorted({s for s in sounds if sounds.count(s) > 1})
        out.append(Check("sounds distinct", WARN,
                         f"shared by several rules: {', '.join(dupes)}",
                         "distinct sounds let you identify an alert "
                         "without looking at the screen"))
    return out


def run_doctor(cfg: dict, path: Path, requested_backend: str | None = None
               ) -> list[Check]:
    """Collect every diagnostic. Pure enough to test without a desktop."""
    checks: list[Check] = []
    checks += _check_session()
    checks += _check_tools()
    backend_checks, backend = _check_backends(requested_backend)
    checks += backend_checks
    checks += _check_profile(cfg, path)
    checks += _check_kwin(cfg)
    game = GameInstance(cfg["window"]["wm_class"], backend=backend)
    # Bind it, or checks that call `ocr`/`capture_array` directly fall back
    # to ImageMagick against a handle the selected backend owns. The replay
    # backend exposed this: `doctor --backend replay` reported "import timed
    # out" because ImageMagick was being handed a fake window id.
    _w().set_active_game(game)
    try:
        checks += _check_window(cfg, game)
        checks += _check_regions(cfg, game)
        checks += _check_grids(cfg, game)
        checks += _check_panel_identity(cfg, game)
        checks += _check_layout(cfg, game)
        checks += _check_capture(cfg, game)
        checks += _check_ocr(cfg, game)
    finally:
        # Leave no global behind: `run_doctor` is called from tests, and a
        # stale instance would silently redirect their captures.
        _w().set_active_game(None)
    checks += _check_outputs()
    return checks


def cmd_doctor(args) -> None:
    cfg = _w().load_config(args.config)
    checks = run_doctor(cfg, args.config, args.backend)
    width = max(len(c.area) for c in checks)
    for c in checks:
        print(f"  {c.verdict:<4}  {c.area:<{width}}  {c.detail}")
        if c.remedy and c.verdict != PASS:
            print(f"  {'':<4}  {'':<{width}}  -> {c.remedy}")
    fails = sum(1 for c in checks if c.verdict == FAIL)
    warns = sum(1 for c in checks if c.verdict == WARN)
    passes = len(checks) - fails - warns
    print(f"\n{passes} pass, {warns} warn, {fails} fail")
    if fails:
        sys.exit(1)

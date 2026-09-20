from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import time
from pathlib import Path

from PIL import Image

from .capture import CaptureError, capture, capture_array
from .config import (
    ConfigError,
    build_rules,
    list_profiles,
    load_config,
    profile_identity,
    resolve_profile_path,
)
from .notifications import ALERT_LOG, build_default_notifier
from .paths import CAPTURE_DIR, STATE_DIR, ensure_runtime_dirs
from .platform import ensure_x_env, find_window, resolve_window, window_size
from .regions import ANCHORS, Region
from .replay import replay_file
from .signals import (
    OCRError,
    diff_slots,
    load_cycles,
    mean_abs_diff,
    slot_signatures,
)
from .rules import evaluate

PID_FILE = STATE_DIR / "watcher.pid"
HEALTH_FILE = STATE_DIR / "health.json"


def _cfg(args) -> dict:
    path = resolve_profile_path(getattr(args, "profile", None), getattr(args, "config", None))
    return load_config(path)


def cmd_calibrate(args) -> None:
    cfg = _cfg(args)
    wid, size = resolve_window(cfg)
    out = Path(args.out) if args.out else CAPTURE_DIR / "calibrate.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    capture(wid, None, out, resize=args.scale)
    with Image.open(out) as image:
        shot = image.size
    print(f"window {wid} actual {size[0]}x{size[1]}")
    print(f"wrote {out} ({shot[0]}x{shot[1]})")
    print(f"multiply coordinates read off that image by {size[0]/shot[0]:.4f}")


def cmd_shot(args) -> None:
    cfg = _cfg(args)
    wid, size = resolve_window(cfg)
    if args.box:
        parts = args.box.split(",")
        if len(parts) == 5:
            anchor, values = parts[0], parts[1:]
            if anchor not in ANCHORS:
                raise ConfigError(f"invalid --box anchor {anchor!r}")
        elif len(parts) == 4:
            anchor, values = "top-left", parts
        else:
            raise ConfigError("--box must be anchor,dx,dy,w,h or x,y,w,h")
        try:
            region = Region(anchor, *(int(value) for value in values))
        except ValueError as exc:
            raise ConfigError(f"invalid --box values: {exc}") from exc
        label = "custom"
    else:
        if not args.region:
            raise ConfigError("shot requires a region name or --box")
        if args.region not in cfg["_regions"]:
            raise ConfigError(f"unknown region {args.region!r}")
        region, label = cfg["_regions"][args.region], args.region

    box = region.resolve(size)
    out = Path(args.out) if args.out else CAPTURE_DIR / f"shot_{label}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    capture(wid, box, out)
    print(f"wrote {out} {label} anchor={region.anchor} -> box={box} (window {size[0]}x{size[1]})")


def cmd_regions(args) -> None:
    cfg = _cfg(args)
    wid, size = resolve_window(cfg)
    print(f"window {wid} {size[0]}x{size[1]}\n")
    print(f"{'region':<16} {'anchor':<14} {'resolved x,y,w,h'}")
    for name, region in cfg["_regions"].items():
        print(f"{name:<16} {region.anchor:<14} {region.resolve(size)}")
    print(f"\n{'rule':<18} {'kind':<12} {'region':<16} {'enabled'}")
    for rule in cfg["rules"]:
        print(
            f"{rule['name']:<18} {rule['kind']:<12} {rule['region']:<16} "
            f"{str(rule.get('enabled', True)):<7}"
        )


def cmd_probe(args) -> None:
    cfg = _cfg(args)
    wid, size = resolve_window(cfg)
    regions = cfg["_regions"]
    masks = {rule["region"]: rule.get("mask") for rule in cfg["rules"]}
    previous = {name: None for name in regions}
    print(f"window {wid} {size[0]}x{size[1]} - {args.count} samples @ {args.interval}s")
    print("frame-to-frame mean absolute difference\n")
    print(f"{'t':>6}  " + "  ".join(f"{name:>14}" for name in regions))
    for index in range(args.count):
        row: list[str] = []
        cycle = index + 1
        for name, region in regions.items():
            try:
                array = capture_array(wid, region.resolve(size), masks.get(name), cycle=cycle)
                diff = mean_abs_diff(array, previous[name])
                previous[name] = array
                row.append(f"{diff:14.2f}" if diff == diff else f"{'--':>14}")
            except CaptureError:
                row.append(f"{'ERR':>14}")
        print(f"{index*args.interval:6.1f}  " + "  ".join(row), flush=True)
        time.sleep(args.interval)


def cmd_inv(args) -> None:
    cfg = _cfg(args)
    wid, size = resolve_window(cfg)
    if args.region not in cfg["_regions"]:
        raise ConfigError(f"unknown region {args.region!r}")
    region = cfg["_regions"][args.region]
    if not region.grid:
        raise ConfigError(f"region {args.region!r} has no grid defined")
    cols = region.grid[4]
    previous = None
    seen_frames = 0
    change_count: dict[int, int] = {}
    warmup = 8
    cycle = 0
    print(f"watching {args.region} every {args.interval}s - ctrl-c to stop")
    started = time.monotonic()
    while time.monotonic() - started < args.duration:
        cycle += 1
        frame = capture_array(wid, region.resolve(size), cycle=cycle)
        current = slot_signatures(frame, region.grid)
        occupied = sum(1 for slot in current if slot["occ"])
        if occupied > args.capacity:
            previous = None
            time.sleep(args.interval)
            continue
        if previous is not None:
            changes = diff_slots(previous, current)
            seen_frames += 1
            for change in changes:
                if change["kind"] == "changed":
                    slot = change["slot"]
                    change_count[slot] = change_count.get(slot, 0) + 1
            if seen_frames > warmup:
                noisy = {
                    slot for slot, count in change_count.items()
                    if count / seen_frames > 0.5
                }
                changes = [
                    change for change in changes
                    if change["kind"] != "changed" or change["slot"] not in noisy
                ]
            else:
                changes = []
            if changes:
                stamp = time.strftime("%H:%M:%S")
                desc = ", ".join(
                    f"#{change['slot']}({change['slot'] // cols + 1},{change['slot'] % cols + 1}) "
                    f"{change['kind']}"
                    + (f" d{change['delta']}" if "delta" in change else "")
                    for change in changes
                )
                print(f"{stamp} occ={occupied:<3} {desc}", flush=True)
        previous = current
        time.sleep(args.interval)


def cmd_stats(args) -> None:
    cfg = _cfg(args)
    capacity = next(
        (rule.get("capacity", 28) for rule in cfg["rules"] if rule["kind"] == "inventory"),
        28,
    )
    cycles = load_cycles(capacity)
    if not cycles:
        raise RuntimeError("no complete cycles logged yet - run watch through a bank trip first")

    print(f"{'#':>2} {'items':>6} {'peak':>5} {'active_s':>8} {'transit_s':>10} {'cycle_s':>8} {'eff/hr':>8}")
    active: list[float] = []
    transit: list[float] = []
    totals: list[float] = []
    gained: list[int] = []
    peaks: list[int] = []
    for index, cycle in enumerate(cycles, 1):
        got = cycle["peak"] - cycle.get("emptied_to", 0)
        last = cycle.get("last_gain")
        if last is None:
            continue
        active_seconds = last - cycle["first_gain"]
        transit_seconds = cycle["banked_at"] - last
        total = cycle["banked_at"] - cycle["first_gain"]
        if total <= 0:
            continue
        active.append(active_seconds)
        transit.append(transit_seconds)
        totals.append(total)
        gained.append(got)
        peaks.append(cycle["peak"])
        print(
            f"{index:>2} {got:>6} {cycle['peak']:>5} {active_seconds:>8.0f} "
            f"{transit_seconds:>10.0f} {total:>8.0f} {got/total*3600:>8.0f}"
        )

    if not totals:
        raise RuntimeError("no usable cycles")
    count = len(totals)
    total_items, total_time = sum(gained), sum(totals)
    mean_transit = sum(transit) / count
    active_rates = [gained[i] / active[i] for i in range(count) if active[i] > 0]
    raw = (sum(active_rates) / len(active_rates) * 3600) if active_rates else 0
    print(f"\ncycles: {count}")
    print(f"  mean active   {sum(active)/count:6.0f}s")
    print(f"  mean transit  {mean_transit:6.0f}s")
    print(f"  mean cycle    {total_time/count:6.0f}s")
    print(f"  mean peak     {sum(peaks)/count:6.1f} / {capacity}")
    print(f"  raw rate      {raw:7.0f} items/hr")
    print(f"  effective     {total_items/total_time*3600:7.0f} items/hr")


def cmd_alerts(args) -> None:
    if not ALERT_LOG.exists():
        raise RuntimeError("no alerts logged yet")
    rows: list[dict] = []
    for line in ALERT_LOG.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    if args.hours:
        cutoff = time.time() - args.hours * 3600
        rows = [row for row in rows if row.get("t", 0) >= cutoff]
    if not rows:
        raise RuntimeError("no alerts in the requested interval")

    span = (rows[-1]["t"] - rows[0]["t"]) / 3600 or (1 / 3600)
    by_rule: dict[str, list[float]] = {}
    for row in rows:
        by_rule.setdefault(row["rule"], []).append(row["t"])

    print(f"{len(rows)} alerts over {span:.2f}h ({len(rows)/span:.1f}/hour)\n")
    print(f"{'rule':<24} {'count':>6} {'per hour':>9} {'median gap':>11}")
    for name, timestamps in sorted(by_rule.items(), key=lambda item: -len(item[1])):
        gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
        median = sorted(gaps)[len(gaps) // 2] if gaps else None
        gap = f"{median:>10.0f}s" if median is not None else f"{'-':>11}"
        print(f"{name:<24} {len(timestamps):>6} {len(timestamps)/span:>9.1f} {gap}")

    if args.tail:
        print(f"\nlast {args.tail}:")
        for row in rows[-args.tail:]:
            stamp = time.strftime("%H:%M:%S", time.localtime(row["t"]))
            profile = row.get("profile", row.get("skill", "?"))
            print(f"  {stamp} {profile:<12} {row['rule']:<20} {row['body'][:70]}")


def _process_identity(pid: int) -> tuple[str, int] | None:
    proc = Path(f"/proc/{pid}")
    try:
        cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        stat = (proc / "stat").read_text()
    except (OSError, UnicodeDecodeError):
        return None
    split = stat.rsplit(")", 1)
    if len(split) != 2:
        return None
    values = split[1].split()
    if len(values) <= 19:
        return None
    looks_like_watcher = (
        "watcher.py" in cmdline
        or "screen-watcher" in cmdline
        or "screen_watcher.cli" in cmdline
    ) and " watch" in f" {cmdline}"
    if not looks_like_watcher:
        return None
    try:
        return cmdline, int(values[19])
    except ValueError:
        return None


def _read_pid_record() -> tuple[int, int | None] | None:
    try:
        raw = PID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return int(payload["pid"]), int(payload["start_time"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        pass
    try:
        return int(raw), None
    except ValueError:
        return None


def _get_pid() -> int | None:
    record = _read_pid_record()
    if not record:
        return None
    pid, expected_start = record
    identity = _process_identity(pid)
    if identity is None:
        return None
    if expected_start is not None and identity[1] != expected_start:
        return None
    return pid


def claim_singleton() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    record = _read_pid_record()
    if record:
        old_pid, expected_start = record
        if old_pid != os.getpid():
            identity = _process_identity(old_pid)
            if identity is not None and (expected_start is None or identity[1] == expected_start):
                raise RuntimeError(f"watcher already running (pid {old_pid})")

    identity = _process_identity(os.getpid())
    start_time = identity[1] if identity else None
    PID_FILE.write_text(
        json.dumps({"pid": os.getpid(), "start_time": start_time}),
        encoding="utf-8",
    )
    atexit.register(_release_singleton)


def _release_singleton() -> None:
    try:
        record = _read_pid_record()
        if record and record[0] == os.getpid():
            PID_FILE.unlink()
    except OSError:
        pass


def _signal_watcher(sig: signal.Signals) -> int | None:
    pid = _get_pid()
    if pid is None:
        return None
    before = _process_identity(pid)
    if before is None:
        return None
    try:
        pidfd = os.pidfd_open(pid)
    except (AttributeError, OSError):
        pidfd = None
    try:
        after = _process_identity(pid)
        if after != before:
            return None
        if pidfd is not None and hasattr(signal, "pidfd_send_signal"):
            signal.pidfd_send_signal(pidfd, sig)
        else:
            os.kill(pid, sig)
        return pid
    except OSError:
        return None
    finally:
        if pidfd is not None:
            os.close(pidfd)


def next_poll_deadline(deadline: float, interval: float, now: float) -> float:
    """Return the next future deadline, skipping missed polls rather than bursting."""
    deadline += interval
    if deadline <= now:
        missed = int((now - deadline) // interval) + 1
        deadline += missed * interval
    return deadline


def _write_health(payload: dict) -> None:
    try:
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = HEALTH_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(HEALTH_FILE)
    except OSError:
        pass


def cmd_watch(args) -> None:
    claim_singleton()
    cfg = _cfg(args)
    profile = profile_identity(cfg)
    wid, size = resolve_window(cfg)
    rules = build_rules(cfg)
    if not rules:
        raise RuntimeError("no enabled rules")

    interval = float(cfg.get("interval", 1.0))
    wm_class = cfg["window"]["wm_class"]
    notifier = build_default_notifier(desktop=not args.dry_run)
    print(
        f"watching profile={profile!r} source={cfg['_path']} {wid} ({wm_class}) "
        f"{size[0]}x{size[1]} every {interval}s"
    )
    for rule in rules:
        print(f"  {rule.name:<20} {rule.kind:<12} -> {rule.region}")
    print("ctrl-c to stop", flush=True)

    misses = 0
    cycle = 0
    next_poll = time.monotonic()
    rule_errors: dict[str, str] = {}

    while True:
        cycle += 1
        mono_now = time.monotonic()
        wall_now = time.time()
        current_size = window_size(wid)
        if current_size and current_size != size:
            print(f"window resized {size} -> {current_size}; regions re-anchored", flush=True)
            size = current_size
            for rule in rules:
                rule.reset()

        capture_ok = True
        try:
            for rule in rules:
                try:
                    alert = evaluate(rule, wid, cfg["_regions"][rule.region], size, mono_now, cycle)
                    rule_errors.pop(rule.name, None)
                    if alert is not None:
                        notifier.deliver(alert.with_profile(profile))
                except OCRError as exc:
                    rule_errors[rule.name] = f"OCR: {exc}"
                    print(f"rule {rule.name!r} OCR unavailable: {exc}", flush=True)
                except CaptureError:
                    raise
                except Exception as exc:
                    rule_errors[rule.name] = f"{type(exc).__name__}: {exc}"
                    print(f"rule {rule.name!r} failed: {type(exc).__name__}: {exc}", flush=True)
            misses = 0
        except CaptureError as exc:
            capture_ok = False
            misses += 1
            if misses >= 3:
                new_wid = find_window(wm_class)
                if new_wid:
                    new_size = window_size(new_wid)
                    if not new_size:
                        raise RuntimeError(f"reacquired window {new_wid} without geometry")
                    print(f"reacquired window {wid} -> {new_wid}", flush=True)
                    wid, size, misses = new_wid, new_size, 0
                    for rule in rules:
                        rule.reset()
                else:
                    raise RuntimeError("game window gone") from exc
            else:
                print(f"capture miss: {exc}", flush=True)

        _write_health({
            "t": wall_now,
            "pid": os.getpid(),
            "profile": profile,
            "profile_path": cfg["_path"],
            "window_id": wid,
            "window_size": list(size),
            "cycle": cycle,
            "capture_ok": capture_ok,
            "rule_errors": rule_errors,
            "rules": [rule.name for rule in rules],
            "interval": interval,
        })

        # A slow cycle does not create a burst of catch-up polls. Missed
        # deadlines are skipped so the observer returns to its requested cadence.
        after = time.monotonic()
        next_poll = next_poll_deadline(next_poll, interval, after)
        time.sleep(max(0.0, next_poll - time.monotonic()))


def cmd_status(args) -> None:
    pid = _get_pid()
    if pid is None:
        print("stopped")
    else:
        print(f"running pid {pid}")
    if HEALTH_FILE.exists():
        try:
            health = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
            age = max(0.0, time.time() - float(health.get("t", 0)))
            print(f"profile: {health.get('profile', '?')}")
            print(f"health age: {age:.1f}s")
            print(f"capture: {'OK' if health.get('capture_ok') else 'DEGRADED'}")
            errors = health.get("rule_errors", {})
            print(f"rule errors: {len(errors)}")
            for name, error in errors.items():
                print(f"  {name}: {error}")
        except (OSError, ValueError, TypeError):
            print("health: unreadable")


def cmd_pause(args) -> None:
    pid = _signal_watcher(signal.SIGSTOP)
    print(f"paused pid {pid}" if pid else "no running watcher to pause")


def cmd_resume(args) -> None:
    pid = _signal_watcher(signal.SIGCONT)
    print(f"resumed pid {pid}" if pid else "no running watcher to resume")


def cmd_list_profiles(args) -> None:
    for path in list_profiles():
        cfg = load_config(path)
        print(
            f"{path.stem:<20} type={cfg['profile_type']:<9} "
            f"version={cfg['profile_version']} identity={profile_identity(cfg)}"
        )


def cmd_validate_profiles(args) -> None:
    failed = 0
    for path in list_profiles():
        try:
            cfg = load_config(path)
            print(f"OK {path.name} schema={cfg['schema_version']} profile={cfg['profile_version']}")
        except ConfigError as exc:
            failed += 1
            print(f"FAIL {path.name}: {exc}")
    if failed:
        raise RuntimeError(f"{failed} profile(s) failed validation")


def cmd_replay(args) -> None:
    result = replay_file(Path(args.file))
    if not result.alerts:
        print("replay produced no alerts")
        return
    for alert in result.alerts:
        print(f"{alert.profile} {alert.rule_name}: {alert.body}")
        if args.evidence:
            print(json.dumps(alert.evidence, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="screen-watcher",
        description="Read-only RuneScape screen observability and notifications.",
    )
    parser.add_argument("--profile", help="bundled profile name, e.g. fishing")
    parser.add_argument("--config", type=Path, help="explicit profile JSON path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    calibrate = sub.add_parser("calibrate")
    calibrate.add_argument("--scale", default="30%")
    calibrate.add_argument("--out")
    calibrate.set_defaults(func=cmd_calibrate)

    shot = sub.add_parser("shot")
    shot.add_argument("region", nargs="?")
    shot.add_argument("--box", help="anchor,dx,dy,w,h or x,y,w,h")
    shot.add_argument("--out")
    shot.set_defaults(func=cmd_shot)

    sub.add_parser("regions").set_defaults(func=cmd_regions)

    probe = sub.add_parser("probe")
    probe.add_argument("-n", "--count", type=int, default=12)
    probe.add_argument("-i", "--interval", type=float, default=1.5)
    probe.set_defaults(func=cmd_probe)

    inv = sub.add_parser("inv", help="live per-slot inventory change feed")
    inv.add_argument("region", nargs="?", default="backpack")
    inv.add_argument("-i", "--interval", type=float, default=1.0)
    inv.add_argument("-d", "--duration", type=float, default=60)
    inv.add_argument("-c", "--capacity", type=int, default=28)
    inv.set_defaults(func=cmd_inv)

    sub.add_parser("stats").set_defaults(func=cmd_stats)

    alerts = sub.add_parser("alerts")
    alerts.add_argument("-H", "--hours", type=float, default=0)
    alerts.add_argument("-t", "--tail", type=int, default=10)
    alerts.set_defaults(func=cmd_alerts)

    watch = sub.add_parser("watch")
    watch.add_argument("--dry-run", action="store_true", help="evaluate and log without desktop popups/sounds")
    watch.set_defaults(func=cmd_watch)

    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("pause").set_defaults(func=cmd_pause)
    sub.add_parser("resume").set_defaults(func=cmd_resume)
    sub.add_parser("list-profiles").set_defaults(func=cmd_list_profiles)
    sub.add_parser("validate-profiles").set_defaults(func=cmd_validate_profiles)

    replay = sub.add_parser("replay")
    replay.add_argument("file")
    replay.add_argument("--evidence", action="store_true")
    replay.set_defaults(func=cmd_replay)
    return parser


def main() -> None:
    ensure_runtime_dirs()
    args = build_parser().parse_args()
    if args.cmd not in {"status", "pause", "resume", "list-profiles", "validate-profiles", "replay"}:
        ensure_x_env()
    try:
        args.func(args)
    except (ConfigError, CaptureError, OCRError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")

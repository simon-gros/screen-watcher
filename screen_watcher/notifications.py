from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Protocol

from .events import Alert
from .paths import STATE_DIR

SOUND_DIR = Path("/usr/share/sounds/ocean/stereo")
ALERT_LOG = STATE_DIR / "alerts.jsonl"


class Notifier(Protocol):
    def deliver(self, alert: Alert) -> None:
        ...


class DesktopNotifier:
    def deliver(self, alert: Alert) -> None:
        command = ["notify-send", "-a", "Screen Watcher", "-u", alert.urgency]
        if alert.urgency != "critical":
            command += ["-t", str(alert.timeout_ms)]
        command += [alert.title, alert.body]
        subprocess.run(command, capture_output=True)
        if alert.sound:
            path = Path(alert.sound) if "/" in alert.sound else SOUND_DIR / f"{alert.sound}.oga"
            if path.exists():
                try:
                    subprocess.Popen(
                        ["paplay", str(path)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except OSError:
                    pass


class TerminalNotifier:
    def deliver(self, alert: Alert) -> None:
        stamp = time.strftime("%H:%M:%S")
        profile = f" [{alert.profile}]" if alert.profile else ""
        print(f"[{stamp}]{profile} {alert.rule_name}: {alert.body}", flush=True)


class JsonlNotifier:
    def __init__(self, path: Path = ALERT_LOG):
        self.path = path

    def deliver(self, alert: Alert) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "t": round(time.time(), 3),
                "profile": alert.profile or "unknown",
                "rule": alert.rule_name,
                "title": alert.title,
                "body": alert.body[:500],
                "urgency": alert.urgency,
                "confidence": alert.confidence,
                "evidence": alert.evidence,
            }
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass


class CompositeNotifier:
    def __init__(self, *backends: Notifier):
        self.backends = backends

    def deliver(self, alert: Alert) -> None:
        for backend in self.backends:
            try:
                backend.deliver(alert)
            except Exception as exc:
                print(f"notification backend {type(backend).__name__} failed: {exc}", flush=True)


def build_default_notifier(desktop: bool = True) -> CompositeNotifier:
    backends: list[Notifier] = [TerminalNotifier(), JsonlNotifier()]
    if desktop:
        backends.append(DesktopNotifier())
    return CompositeNotifier(*backends)

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .events import Alert
from .rules import evaluate_text, rule_from_dict, RuleBase


@dataclass
class ReplayResult:
    alerts: list[Alert]


def replay_document(document: dict) -> ReplayResult:
    """Replay sanitized OCR observations through the same pure text-rule logic."""
    rule_spec = document.get("rule")
    observations = document.get("observations")
    if not isinstance(rule_spec, dict):
        raise ValueError("replay document requires a rule object")
    if not isinstance(observations, list):
        raise ValueError("replay document requires an observations array")

    rule: RuleBase = rule_from_dict(rule_spec)
    profile = str(document.get("profile", "replay"))
    alerts: list[Alert] = []

    for index, observation in enumerate(observations):
        if not isinstance(observation, dict):
            raise ValueError(f"observation {index} must be an object")
        timestamp = observation.get("t")
        text = observation.get("text", "")
        if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
            raise ValueError(f"observation {index}.t must be numeric")
        if not isinstance(text, str):
            raise ValueError(f"observation {index}.text must be a string")
        alert = evaluate_text(rule, text, float(timestamp))
        if alert is not None:
            alerts.append(alert.with_profile(profile))

    return ReplayResult(alerts)


def replay_file(path: Path) -> ReplayResult:
    return replay_document(json.loads(Path(path).read_text(encoding="utf-8")))

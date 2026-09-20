from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class Alert:
    """Pure rule output; delivery is handled by notification backends."""

    rule_name: str
    title: str
    body: str
    urgency: str = "normal"
    sound: str | None = None
    timeout_ms: int = 8000
    profile: str = ""
    confidence: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def with_profile(self, profile: str) -> "Alert":
        return replace(self, profile=profile)

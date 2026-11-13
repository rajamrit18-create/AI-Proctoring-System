"""Warning policy for real-time meeting safety signals.

This module combines scores from separate audio/video detectors. It does not
transcribe audio or inspect images itself. Detector adapters should emit one
Signal per unique audio chunk or sampled video frame.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Deque, Dict, Optional


class SignalKind(str, Enum):
    POTENTIALLY_HARMFUL_LANGUAGE = "potentially_harmful_language"
    CHAT_MESSAGE = "chat_message"
    EXPLICIT_VISUAL = "explicit_visual"
    VISIBLE_INJURY = "visible_injury"


@dataclass(frozen=True)
class Signal:
    """A detector result; timestamp is monotonic seconds within the call."""

    kind: SignalKind
    confidence: float
    timestamp: float
    sample_id: str


@dataclass(frozen=True)
class Rule:
    """Calibrated policy for turning detector scores into a warning."""

    threshold: float
    min_hits: int
    window_seconds: float
    cooldown_seconds: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        if self.min_hits < 1:
            raise ValueError("min_hits must be at least 1")
        if self.window_seconds <= 0 or self.cooldown_seconds < 0:
            raise ValueError("window must be positive and cooldown nonnegative")


@dataclass(frozen=True)
class WarningEvent:
    kind: SignalKind
    message: str
    timestamp: float
    confidence: float


WARNING_COPY = {
    SignalKind.POTENTIALLY_HARMFUL_LANGUAGE: (
        "A phrase in your audio may conflict with this meeting's conduct "
        "guidelines. Please keep the conversation respectful."
    ),
    SignalKind.CHAT_MESSAGE: (
        "A message may conflict with this meeting's conduct guidelines. "
        "Please keep the conversation respectful."
    ),
    SignalKind.EXPLICIT_VISUAL: (
        "Your camera may be showing explicit content. Please adjust your "
        "camera or turn video off."
    ),
    SignalKind.VISIBLE_INJURY: (
        "Your camera may be showing an injury. If unintended, adjust your "
        "camera or turn video off."
    ),
}


class WarningEngine:
    """Apply temporal thresholds and cooldowns to detector signals.

    Pass calibrated rules for all signal kinds. On a trigger, the caller should
    show the returned message privately to the participant whose media was
    analyzed. This engine stores only short-lived scores and sample IDs, not
    audio, video, or transcripts.
    """

    def __init__(self, rules: Dict[SignalKind, Rule]) -> None:
        missing = set(SignalKind) - set(rules)
        if missing:
            raise ValueError(f"missing rules for: {', '.join(k.value for k in missing)}")
        self._rules = dict(rules)
        self._recent: Dict[SignalKind, Deque[Signal]] = defaultdict(deque)
        self._seen_ids: Dict[SignalKind, set[str]] = defaultdict(set)
        self._last_warning: Dict[SignalKind, Optional[float]] = {
            kind: None for kind in SignalKind
        }
        self._last_timestamp: Dict[SignalKind, Optional[float]] = {
            kind: None for kind in SignalKind
        }

    def ingest(self, signal: Signal) -> Optional[WarningEvent]:
        """Return a warning when enough high-confidence signals cluster in time."""
        if not isfinite(signal.confidence) or not 0.0 <= signal.confidence <= 1.0:
            raise ValueError("confidence must be a finite number between 0 and 1")
        if not isfinite(signal.timestamp) or signal.timestamp < 0:
            raise ValueError("timestamp must be finite and nonnegative")
        if not signal.sample_id:
            raise ValueError("sample_id must identify a unique chunk or frame")

        rule = self._rules[signal.kind]
        previous_timestamp = self._last_timestamp[signal.kind]
        if previous_timestamp is not None and signal.timestamp < previous_timestamp:
            raise ValueError("signals for each kind must arrive in timestamp order")
        self._last_timestamp[signal.kind] = signal.timestamp

        recent = self._recent[signal.kind]
        seen = self._seen_ids[signal.kind]

        # Expire old evidence before considering this sample.
        while recent and signal.timestamp - recent[0].timestamp > rule.window_seconds:
            expired = recent.popleft()
            seen.discard(expired.sample_id)

        # Avoid counting a detector retry for the same audio chunk/video frame.
        if signal.sample_id in seen:
            return None

        if signal.confidence < rule.threshold:
            return None

        recent.append(signal)
        seen.add(signal.sample_id)

        if len(recent) < rule.min_hits:
            return None

        last = self._last_warning[signal.kind]
        if last is not None and signal.timestamp - last < rule.cooldown_seconds:
            return None

        self._last_warning[signal.kind] = signal.timestamp
        confidence = sum(item.confidence for item in recent) / len(recent)
        recent.clear()
        seen.clear()
        return WarningEvent(
            kind=signal.kind,
            message=WARNING_COPY[signal.kind],
            timestamp=signal.timestamp,
            confidence=confidence,
        )

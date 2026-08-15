"""Low-cost progress snapshots and robust stage ETA estimates."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from statistics import median
from typing import Any

from shared.models import ProgressSnapshot


class ProgressEstimator:
    """Estimate remaining work from a bounded window of completed units."""

    def __init__(self, window_size: int = 20) -> None:
        self.window_size = max(3, int(window_size))
        self._samples: dict[str, deque[tuple[float, float]]] = {}
        self._started_at: dict[str, datetime] = {}

    def reset(self, key: str) -> None:
        self._samples.pop(key, None)
        self._started_at[key] = datetime.now(timezone.utc)

    def observe(self, key: str, completed_units: float, elapsed_seconds: float) -> None:
        if completed_units <= 0 or elapsed_seconds <= 0:
            return
        samples = self._samples.setdefault(key, deque(maxlen=self.window_size))
        samples.append((float(completed_units), float(elapsed_seconds)))

    def estimate(
        self,
        key: str,
        *,
        completed_units: float,
        total_units: float,
    ) -> tuple[float | None, str]:
        remaining = max(0.0, float(total_units) - float(completed_units))
        samples = list(self._samples.get(key, ()))
        if remaining <= 0:
            return 0.0, "high"
        rates = [elapsed / units for units, elapsed in samples if units > 0]
        if not rates:
            return None, "none"
        eta = remaining * median(rates)
        count = len(rates)
        confidence = "low" if count < 3 else "medium" if count < 8 else "high"
        return round(eta, 1), confidence

    def snapshot(
        self,
        key: str,
        *,
        stage: str,
        phase: str,
        message: str,
        completed_units: float,
        total_units: float,
        **details: Any,
    ) -> ProgressSnapshot:
        now = datetime.now(timezone.utc)
        started = self._started_at.setdefault(key, now)
        eta, confidence = self.estimate(
            key,
            completed_units=completed_units,
            total_units=total_units,
        )
        percent = (
            min(100.0, max(0.0, completed_units * 100.0 / total_units))
            if total_units > 0
            else 0.0
        )
        return ProgressSnapshot(
            stage=stage,
            phase=phase,
            message=message,
            completed_units=completed_units,
            total_units=total_units,
            percent=round(percent, 2),
            elapsed_seconds=max(0.0, (now - started).total_seconds()),
            eta_seconds=eta,
            eta_confidence=confidence,
            started_at=started,
            updated_at=now,
            **details,
        )

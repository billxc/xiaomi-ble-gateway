"""Pure per-device replay gate. Only authenticated, model-checked frames enter.

Counters are strictly increasing uint32, not a modular sequence: reset/wrap is
fail-closed. Persistence contains no measurements, keys or health payloads.
"""
from dataclasses import dataclass, field
from enum import Enum


def weight_time_is_fresh(device_time, enabled_at, received_at) -> bool:
    """No clock repair/offset guessing: require an enabled, recent UTC sample."""
    return (type(device_time) is int and device_time > 0
            and type(enabled_at) is int and enabled_at > 0
            and type(received_at) is int
            and device_time >= enabled_at - 5
            and abs(device_time - received_at) <= 120)


class Observation(Enum):
    BASELINE = "baseline"
    NEW = "new"
    OLD = "old"


@dataclass
class DeviceState:
    counter: int | None = None
    last_attempt: int | None = None
    cloud_status: str = "disabled"
    readings: dict[str, float] = field(default_factory=dict)
    # Per-profile audit (__nearest__ for automatic matching); the consumption
    # boundary is device-global across all modes. Never persist weight/BIA/user data.
    weight_attempts: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def last_weight_attempt_device_time(self) -> int | None:
        return max((attempt["last_weight_attempt_device_time"]
                    for attempt in self.weight_attempts.values()), default=None)

    def can_attempt_weight(self, profile_id: str, device_time: int) -> bool:
        # Switching profile never resets the device's consumed measurement time.
        previous = self.last_weight_attempt_device_time
        return previous is None or device_time > previous

    def mark_weight_attempt(self, profile_id: str, device_time: int,
                            record_time: int) -> None:
        if (not isinstance(profile_id, str) or not profile_id
                or type(device_time) is not int or device_time <= 0
                or type(record_time) is not int or record_time <= 0
                or not self.can_attempt_weight(profile_id, device_time)):
            raise ValueError("Invalid or consumed weight attempt")
        self.weight_attempts[profile_id] = {
            "last_weight_attempt_device_time": device_time,
            "record_time": record_time,
        }
        self.cloud_status = "unknown"

    def observe(self, counter: int, readings: dict[str, float]) -> Observation:
        if type(counter) is not int or not 0 <= counter <= 0xFFFFFFFF:
            raise ValueError("Invalid counter")
        if self.counter is not None and counter <= self.counter:
            return Observation.OLD
        result = Observation.BASELINE if self.counter is None else Observation.NEW
        self.counter = counter
        # Missing weight (including mass 0x7FF) never erases a known weight.
        self.readings.update(readings)
        return result

    def mark_attempt(self, counter: int) -> None:
        if self.counter is None or counter > self.counter:
            raise ValueError("Unobserved counter")
        if self.last_attempt is not None and counter <= self.last_attempt:
            raise ValueError("Attempt already consumed")
        self.last_attempt = counter
        self.cloud_status = "unknown"

    def snapshot(self) -> dict:
        return {"counter": self.counter, "last_attempt": self.last_attempt,
                "cloud_status": self.cloud_status,
                "weight_attempts": {profile: dict(attempt)
                                    for profile, attempt in self.weight_attempts.items()}}

    @classmethod
    def restore(cls, data: dict) -> "DeviceState":
        if not isinstance(data, dict) or not {"counter", "last_attempt"} <= data.keys():
            raise ValueError("Invalid stored state")
        counter, attempt = data["counter"], data["last_attempt"]
        for value in (counter, attempt):
            if value is not None and (type(value) is not int or not 0 <= value <= 0xFFFFFFFF):
                raise ValueError("Invalid stored counter")
        if attempt is not None and (counter is None or attempt > counter):
            raise ValueError("Invalid stored attempt")
        status = data.get("cloud_status", "disabled")
        if status not in {"disabled", "waiting", "baseline", "unknown", "success", "storage_error",
                          "requires_gatt", "skipped_overload", "skipped_stale",
                          "weight_upload_disabled", "waiting_weight", "time_unverified",
                          "verified", "existing_verified", "existing_conflict", "existing",
                          "conflict", "no_match", "ambiguous", "no_reference"}:
            status = "unknown"
        weight_attempts = data.get("weight_attempts", {})
        if not isinstance(weight_attempts, dict):
            raise ValueError("Invalid stored weight attempts")
        restored = cls(counter=counter, last_attempt=attempt, cloud_status=status)
        for profile, weight_attempt in weight_attempts.items():
            if (not isinstance(weight_attempt, dict)
                    or set(weight_attempt) != {"last_weight_attempt_device_time", "record_time"}):
                raise ValueError("Invalid stored weight attempt")
            # Validate each audit entry independently: dictionary order is not
            # measurement order. The global boundary is derived as their max.
            validated = cls()
            validated.mark_weight_attempt(profile,
                                           weight_attempt["last_weight_attempt_device_time"],
                                           weight_attempt["record_time"])
            restored.weight_attempts.update(validated.weight_attempts)
        restored.cloud_status = status
        return restored

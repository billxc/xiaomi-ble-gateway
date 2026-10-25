"""Pure, fail-closed MS110 native selection and v3 serialization.

Inputs are authenticated, durably stored parse_native_csv payloads, never
broadcast-derived composition. Time equality is only a conservative gate, NOT
a shared unique measurement ID. Status 1 preserves device results verbatim;
this makes no claim about their medical validity. No historical backfill.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
import math

from .scale_history import FIELDS, parse_native_csv
from .scale_weight import same_identity, validate_user


def _number(value, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("INVALID_NATIVE: numeric field required")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("INVALID_NATIVE: invalid numeric field") from None
    if not number.is_finite() or (integer and number != number.to_integral_value()):
        raise ValueError("INVALID_NATIVE: finite numeric field required")
    result = int(number) if integer or number == number.to_integral_value() else float(number)
    if isinstance(result, float) and not math.isfinite(result):
        raise ValueError("INVALID_NATIVE: numeric field out of range")
    return result


def _payload(record):
    """Check raw completeness AND that the decoded data really came from it."""
    if not isinstance(record, dict) or type(record.get("offline")) is not bool:
        raise ValueError("INVALID_NATIVE: native CSV payload required")
    raw = record.get("raw_fields")
    if not isinstance(raw, list) or len(raw) != 34 or not all(isinstance(v, str) for v in raw):
        raise ValueError("INVALID_NATIVE: all 34 raw fields required")
    parts = ([str(record["idx"])] if record["offline"] and "idx" in record else []) + raw
    parsed = parse_native_csv(",".join(parts), offline=record["offline"])
    if record.get("data") != parsed["data"]:
        raise ValueError("INVALID_NATIVE: raw and decoded fields disagree")
    return parsed


def native_measurement(native_record):
    """Validate/number the 34 supplied fields without defaults or algorithms."""
    parsed = _payload(native_record)
    data = {}
    for name in FIELDS:
        value = parsed["data"][name]
        # IDs must never pass through float (including IDs above 2**53).
        data[name] = value if name == "miid" else _number(
            value, integer=name in ("duid", "userType", "status", "time"))
    if data["status"] not in (0, 1) or data["time"] <= 0 or data["weight"] <= 0:
        raise ValueError("INVALID_NATIVE: status/time/weight not eligible")
    data["idx"] = _number(parsed["idx"], integer=True) if parsed["offline"] else -1
    data["reportFrom"] = "Offline" if parsed["offline"] else "Regular"
    return data


def select_native_record(records, *, device_time: int, received_at: int,
                         enabled_at: int, gatt_result: dict) -> dict:
    """Select a detached snapshot only; no IO, clock access or history replay.

    Older native times are previous weighings even inside the freshness window.
    A newer fresh result must not be attached to this broadcast or downgraded.
    """
    if any(type(v) is not int for v in (device_time, received_at, enabled_at)):
        raise ValueError("INVALID_VALUE: integer Unix seconds required")
    candidates = {}
    for record in records:
        try:
            timestamp = _number(record["data"]["time"], integer=True)
        except (KeyError, TypeError, ValueError):
            return {"status": "conflict", "reason": "invalid_native"}
        if timestamp < enabled_at or abs(timestamp - received_at) > 120 or timestamp < device_time:
            continue
        if timestamp > device_time:
            return {"status": "conflict", "reason": "newer_native"}
        try:
            parsed = _payload(record)
        except (KeyError, TypeError, ValueError):
            return {"status": "conflict", "reason": "incomplete_native"}
        key = tuple(parsed["raw_fields"])
        # Prefer the actual offline provenance when online/offline raw34 repeat.
        if key not in candidates or parsed["offline"]:
            candidates[key] = record
    complete = isinstance(gatt_result, dict) and gatt_result.get("complete") is True
    if not candidates:
        return {"status": "weight_only", "reason": "no_fresh_native" if complete else "gatt_failed"}
    if not complete:
        return {"status": "conflict", "reason": "gatt_incomplete"}
    if len(candidates) != 1:
        return {"status": "conflict", "reason": "ambiguous_native"}
    record = next(iter(candidates.values()))
    status = _number(record["data"]["status"], integer=True)
    if status == 2:
        return {"status": "weight_only", "reason": "native_unmatched"}
    try:
        native_measurement(record)
    except ValueError:
        return {"status": "conflict", "reason": "invalid_native"}
    return {"status": "native", "record": deepcopy(record)}


def build_native_record(model, did, user, native_record) -> dict:
    """Official initWithDeviceData -> WeightRecord, with no constructor zeros."""
    validate_user(user)
    data = native_measurement(native_record)
    if (str(data["miid"]) != str(user["uid"])
            or data["duid"] != user["accountCode"] or data["userType"] != user["type"]):
        raise ValueError("NATIVE_PROFILE_CONFLICT: native/profile identity mismatch")
    if not isinstance(model, str) or not model or not isinstance(did, str) or not did:
        raise ValueError("INVALID_VALUE: model and DID are required")
    sn = native_record.get("sn")
    if sn is None:
        sn = "NO_SN"
    if not isinstance(sn, str) or not sn:
        raise ValueError("INVALID_NATIVE: invalid SN")
    data["user"] = deepcopy(user)
    try:
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("INVALID_PROFILE: profile is not valid JSON") from None
    return {"data": encoded, "dataVersion": 3, "uid": data["miid"],
            "accountId": user["accountId"], "createTime": data["time"] * 1000,
            "model": model, "did": did, "sn": sn, "fromSource": 2}


def _equal(actual, expected):
    """Strict recursive JSON equality, permitting finite int/float equivalence."""
    if type(expected) in (int, float):
        return (type(actual) in (int, float) and
                (type(actual) is int or math.isfinite(actual)) and actual == expected)
    if isinstance(expected, dict):
        return (isinstance(actual, dict) and actual.keys() == expected.keys()
                and all(_equal(actual[k], v) for k, v in expected.items()))
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(_equal(a, b) for a, b in zip(actual, expected)))
    return type(actual) is type(expected) and actual == expected


def native_record_matches(actual, expected) -> bool:
    """All sent fields and user snapshot must survive cloud readback intact."""
    try:
        data, wanted = json.loads(actual["data"]), json.loads(expected["data"])
        if not isinstance(data, dict) or not isinstance(wanted, dict):
            return False
        if not same_identity(actual, expected):
            return False
        if not all(key in actual and _equal(actual[key], expected[key]) for key in (
                "dataVersion", "createTime", "fromSource", "sn")):
            return False
        if set(wanted) != set(FIELDS) | {"idx", "reportFrom", "user"}:
            return False
        # Identity serialization may be string/int, but never float or bool.
        miid = data.get("miid")
        if type(miid) not in (str, int) or str(miid) != str(wanted["miid"]):
            return False
        return all(key in data and _equal(data[key], value)
                   for key, value in wanted.items() if key != "miid")
    except (KeyError, TypeError, ValueError):
        return False

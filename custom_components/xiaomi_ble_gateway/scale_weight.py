"""Pure MS110 UserEditor weight records; no HA, clock, network or storage.

Static source: ms110-v54 11027.saveWeightData, 10634.WeightData and
10637.WeightRecord. Zero-valued composition/impedance fields below are the
OFFICIAL constructor's placeholders, NOT measured zero body fat or impedance.
This reproduces initWithWeight, not device/GATT data, OnlyWeight/status=1,
BMI calculation, bcalc, Offline or Regular uploads. The caller supplies a fresh
measurement's Unix seconds; this module never shifts time or backfills history.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json


_ZERO_FIELDS = (
    "heartRate", "bfp", "slm", "bwp", "bmc", "vfl", "pp", "smm", "bmi",
    "swt", "mc", "wc", "fc", "whr", "bmr", "bt", "ma", "sbc", "slp",
    "bmcp", "bfm", "ffm", "bwm", "pm", "bodyResOriginZ50",
    "bodyResOriginZ250", "bodyResSmoothZ50", "bodyResSmoothZ250",
)


def _identifier(value):
    return (isinstance(value, (str, int)) and not isinstance(value, bool)
            and bool(str(value).strip()))


def validate_user(user: dict) -> None:
    """Require real profile identity/types, never infer type or primary user."""
    if (not isinstance(user, dict) or not _identifier(user.get("accountId"))
            or not _identifier(user.get("uid"))
            or type(user.get("accountCode")) is not int
            or type(user.get("type")) is not int
            or not isinstance(user.get("name"), str)):
        raise ValueError("INVALID_PROFILE: missing or invalid profile fields")


def decimal_weight(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("INVALID_VALUE: weight must be a finite positive number")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("INVALID_VALUE: invalid weight") from None
    if not number.is_finite() or number <= 0:
        raise ValueError("INVALID_VALUE: weight must be a finite positive number")
    return number


def nearest_weight_reference(weight_kg, references, max_difference=2.5) -> dict:
    """Pure exact nearest selection; missing references never enter comparison."""
    weight = decimal_weight(weight_kg)
    if (isinstance(max_difference, bool)
            or not isinstance(max_difference, (int, float, Decimal))):
        raise ValueError("INVALID_VALUE: invalid maximum difference")
    limit = Decimal(str(max_difference))
    if not limit.is_finite() or limit < 0:
        raise ValueError("INVALID_VALUE: invalid maximum difference")
    candidates = [(abs(weight - decimal_weight(ref["weight"])), ref)
                  for ref in references if ref.get("weight") is not None]
    if not candidates:
        return {"status": "no_reference"}
    difference = min(diff for diff, _ in candidates)
    if difference > limit:
        return {"status": "no_match"}
    winners = [ref for diff, ref in candidates if diff == difference]
    if len(winners) != 1:
        return {"status": "ambiguous"}
    winner = winners[0]
    return {"status": "matched", "matched_profile_id": winner["profile_id"],
            "matched_profile_name": winner["name"], "difference": float(difference)}


def scale_history_reference(rows, identity, now_ms) -> dict | None:
    """Validate one newest-first page, then select by time, not row position.

    Times are cloud Unix milliseconds, data.time is Unix seconds. Unknown
    schema/identity/time or conflicting latest weights must fail closed; invalid
    weights and explicitly synthetic records do not supply a reference.
    """
    if not isinstance(rows, list) or len(rows) > 20:
        raise ValueError("UNKNOWN: invalid scale reference history page")
    valid = []
    for row in rows:
        data = record_data(row)
        if not same_identity(row, identity):
            raise ValueError("UNKNOWN: scale reference identity mismatch")
        timestamp = row["createTime"]
        if (not valid_reference_time(timestamp, now_ms)
                or data["time"] != timestamp // 1000):
            raise ValueError("UNKNOWN: inconsistent scale reference timestamps")
        if data.get("reportFrom") in ("TestCase", "TestCase_Unclaim"):
            continue
        try:
            weight = decimal_weight(data.get("weight"))
        except ValueError:
            continue
        valid.append((timestamp, weight))
    if not valid:
        return None
    latest = max(timestamp for timestamp, _ in valid)
    weights = {weight for timestamp, weight in valid if timestamp == latest}
    if len(weights) != 1:
        raise ValueError("UNKNOWN: conflicting latest scale reference weights")
    return {"weight": float(weights.pop()), "reference_time": latest, "source": "history"}


def valid_reference_time(value, now_ms) -> bool:
    """Explicit Unix milliseconds (2000 or later), never infer timestamp units."""
    return type(value) is int and 946684800000 <= value <= now_ms


def scale_cloud_baseline(user, now_ms) -> dict | None:
    """Editable recognition baseline, NOT a measured weight or a target goal."""
    if not valid_reference_time(user.get("weightUpdateTime"), now_ms):
        return None
    try:
        raw_weight = user.get("weightTarget")
        # The official account API serializes this field as a numeric string.
        if isinstance(raw_weight, str):
            raw_weight = Decimal(raw_weight)
        weight = decimal_weight(raw_weight)
    except (ValueError, InvalidOperation):
        return None
    return {"weight": float(weight), "reference_time": user["weightUpdateTime"],
            "source": "cloud_baseline"}


def build_weight_record(*, model: str, did: str, user: dict,
                        weight_kg: float, measurement_time: int) -> dict:
    """Faithful official new WeightData -> initWithWeight -> WeightRecord."""
    validate_user(user)
    weight = decimal_weight(weight_kg)
    if type(measurement_time) is not int or measurement_time <= 0:
        raise ValueError("INVALID_VALUE: measurement_time must be positive Unix seconds")
    if not isinstance(model, str) or not model or not isinstance(did, str) or not did:
        raise ValueError("INVALID_VALUE: model and DID are required")
    data = {"miid": user["uid"], "duid": user["accountCode"],
            "userType": user["type"], "weight": float(weight), "status": 0,
            "time": measurement_time, **dict.fromkeys(_ZERO_FIELDS, 0.0),
            "idx": -1, "reportFrom": "UserEditor", "user": deepcopy(user)}
    try:
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("INVALID_PROFILE: profile is not valid JSON") from None
    return {"data": encoded, "dataVersion": 3, "uid": data["miid"],
            "accountId": user["accountId"], "createTime": measurement_time * 1000,
            "model": model, "did": did, "sn": "NO_SN", "fromSource": 2}


def record_data(record: dict) -> dict:
    """Only the verified API shape: each record has a JSON string in data."""
    if not isinstance(record, dict) or not isinstance(record.get("data"), str):
        raise ValueError("UNKNOWN: invalid scale record shape")
    try:
        data = json.loads(record["data"])
    except (ValueError, TypeError):
        raise ValueError("UNKNOWN: invalid scale record JSON") from None
    if (not isinstance(data, dict) or type(data.get("time")) is not int
            or type(record.get("createTime")) is not int):
        raise ValueError("UNKNOWN: invalid scale record timestamps")
    return data


def same_identity(record: dict, expected: dict) -> bool:
    """Cloud identifiers may be serialized as strings or integers."""
    return all(_identifier(record.get(key)) and str(record[key]) == str(expected[key])
               for key in ("did", "model", "uid", "accountId"))


def same_measurement_time(record: dict, expected: dict) -> bool:
    data = record_data(record)
    return (record["createTime"] == expected["createTime"]
            or data["time"] == expected["createTime"] // 1000)


def record_matches(record: dict, expected: dict) -> bool:
    """Confirm actual server data, not add acknowledgements or invented rows."""
    data, wanted = record_data(record), record_data(expected)
    try:
        weights_match = decimal_weight(data.get("weight")) == decimal_weight(wanted["weight"])
    except ValueError:
        return False
    return (same_identity(record, expected)
            and record["createTime"] == expected["createTime"]
            and data["time"] == wanted["time"] and weights_match
            and _identifier(data.get("miid")) and str(data["miid"]) == str(wanted["miid"])
            and type(data.get("duid")) is int and data["duid"] == wanted["duid"]
            and type(data.get("userType")) is int and data["userType"] == wanted["userType"]
            and data.get("reportFrom") == "UserEditor")

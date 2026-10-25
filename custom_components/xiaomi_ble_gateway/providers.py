"""Adapters for *loaded* source integrations; no login or credential storage.

Account IDs are UID_region_xiaomiio, stable across source reloads and shared
with Home metadata for the same UID/region. ``auto`` requires an unambiguous
Miot account (or a single metadata-only Home account). No runtime objects or
bindkeys are cached here. Importing this module does not import HA.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import math
import re
import sys
import time
from typing import Any
from copy import deepcopy

from .const import CO2_MODEL, ENVIRONMENT_MODELS, SCALE_MODEL, T2_MODEL
from .scale_native import build_native_record, native_measurement, native_record_matches
from .scale_weight import (
    build_weight_record, decimal_weight, record_data, record_matches,
    same_identity, same_measurement_time, validate_user, nearest_weight_reference,
    scale_history_reference, scale_cloud_baseline,
)


_MODELS = ENVIRONMENT_MODELS | {SCALE_MODEL}
_ENVIRONMENT_PROPERTIES = {
    CO2_MODEL: {"co2": (8, 1029), "temperature": (7, 1001), "humidity": (7, 1002)},
    T2_MODEL: {"temperature": (2, 1), "humidity": (2, 2)},
}


class ProviderError(Exception):
    """Sanitized provider failure; UNKNOWN means never automatically resend."""


@dataclass(frozen=True)
class AccountRef:
    id: str
    label: str
    uid: str
    region: str
    can_upload: bool


@dataclass(frozen=True)
class DeviceRef:
    mac: str
    did: str
    model: str
    name: str


@dataclass(frozen=True)
class ScaleProfile:
    """Live DID-scoped profile; raw user data is RAM-only, never entry data."""

    account_id: str
    name: str
    uid: str | int
    account_code: int
    user_type: int
    user: dict = field(repr=False)


@dataclass
class _Source:
    uid: str
    region: str
    cloud: Any = field(default=None, repr=False)
    caches: list = field(default_factory=list, repr=False)
    homes: list = field(default_factory=list, repr=False)

    @property
    def id(self):
        return f"{self.uid}_{self.region}_xiaomiio"


def _identity(uid, region):
    if not isinstance(uid, (str, int)) or isinstance(uid, bool):
        return None
    if not isinstance(region, str) or not region.strip():
        return None
    uid = str(uid).strip()
    return (uid, region.strip().lower()) if uid else None


def _sources(hass):
    """Inspect only live objects. Never import/start an unloaded integration."""
    sources = {}

    def add_cloud(cloud):
        if cloud is None or getattr(cloud, "sid", None) != "xiaomiio":
            return None
        identity = _identity(getattr(cloud, "user_id", None),
                             getattr(cloud, "default_server", None))
        if identity is None:
            return None
        source = sources.setdefault(identity, _Source(*identity))
        source.cloud = cloud
        return source

    data = getattr(hass, "data", {})
    miot = data.get("xiaomi_miot", {})
    module = sys.modules.get("custom_components.xiaomi_miot.core.xiaomi_cloud")
    cls = getattr(module, "MiotCloud", None)
    if cls is not None and isinstance(miot, dict):
        for cloud in cls.all_clouds(hass):
            add_cloud(cloud)
    if isinstance(miot, dict):
        for runtime in miot.values():
            add_cloud(runtime.get("xiaomi_cloud") if isinstance(runtime, dict) else runtime)
    module = sys.modules.get("custom_components.xiaomi_miot.core.hass_entry")
    entries = getattr(getattr(module, "HassEntry", None), "ALL", {})
    for entry in entries.values():
        if getattr(entry, "hass", None) is not hass:
            continue
        source = add_cloud(getattr(entry, "cloud", None))
        cache = getattr(entry, "cloud_devices", None)
        if source is not None and isinstance(cache, dict):
            source.caches.append(cache)

    # Home OAuth clients supply metadata only, never legacy cloud transport.
    home = data.get("xiaomi_home", {})
    clients = home.get("miot_clients", {}) if isinstance(home, dict) else {}
    config_entries = getattr(hass, "config_entries", None)
    if isinstance(clients, dict) and config_entries is not None:
        for entry_id, client in clients.items():
            entry = config_entries.async_get_entry(entry_id)
            if entry is None or getattr(entry, "domain", "xiaomi_home") != "xiaomi_home":
                continue
            identity = _identity(entry.data.get("uid"), entry.data.get("cloud_server"))
            if identity is not None and client is not None:
                source = sources.setdefault(identity, _Source(*identity))
                source.homes.append(client)
    return sources


def available_accounts(hass) -> dict[str, AccountRef]:
    """Return loaded accounts, without checking auth or accessing storage."""
    return {
        source.id: AccountRef(
            source.id,
            f"{'xiaomi_miot' if source.cloud is not None else 'xiaomi_home (metadata only)'}"
            f" · {source.uid} · {source.region}",
            source.uid, source.region, source.cloud is not None,
        )
        for source in _sources(hass).values()
    }


def _resolve(hass, account_id):
    sources = list(_sources(hass).values())
    if account_id == "auto":
        candidates = [source for source in sources if source.cloud is not None] or sources
        if len(candidates) != 1:
            raise ProviderError("SOURCE_ACCOUNT_AMBIGUOUS: select one loaded source account")
        return candidates[0]
    for source in sources:
        if source.id == account_id:
            return source
    raise ProviderError("SOURCE_ACCOUNT_UNAVAILABLE: source integration is not loaded")


def _mac(value):
    if not isinstance(value, str):
        return None
    if re.fullmatch(r"[0-9a-fA-F]{12}", value):
        return value.upper()
    if re.fullmatch(r"[0-9a-fA-F]{2}([:-])[0-9a-fA-F]{2}(?:\1[0-9a-fA-F]{2}){4}", value):
        return value.replace(":", "").replace("-", "").upper()
    return None


def _metadata(rows):
    """Copy only the device whitelist, not arbitrary source metadata/secrets."""
    if isinstance(rows, dict):
        iterable = rows.items()
    elif isinstance(rows, list):
        iterable = ((None, row) for row in rows)
    else:
        return []
    result = []
    for key, row in iterable:
        if not isinstance(row, dict):
            continue
        did = row.get("did", key)
        if not isinstance(did, (str, int)) or isinstance(did, bool) or not str(did):
            continue
        # A cache key and its explicit DID must agree.
        if key is not None and str(key) != str(did):
            continue
        result.append({"did": str(did), "mac": _mac(row.get("mac")),
                       "model": row.get("model") if isinstance(row.get("model"), str) else None,
                       "name": row.get("name") if isinstance(row.get("name"), str) else None})
    return result


async def get_devices(hass, account_id) -> list[DeviceRef]:
    source = _resolve(hass, account_id)
    rows = []
    for cache in source.caches:
        rows.extend(_metadata(cache))
    if source.cloud is not None:
        if getattr(source.cloud, "service_token", None) and getattr(source.cloud, "ssecurity", None):
            # HassEntry caches are filtered by the source integration's config,
            # not a full account inventory. Merge the public account-wide read;
            # renew=False reuses its one-day cache, never forces a refresh.
            try:
                rows.extend(_metadata(await source.cloud.async_get_devices(renew=False)))
            except asyncio.CancelledError:
                raise ProviderError("UNKNOWN: source metadata request cancelled") from None
            except Exception:
                raise ProviderError("UNKNOWN: source metadata unavailable") from None
        elif not source.caches:
            _ready(source.cloud)
    for client in source.homes:
        rows.extend(_metadata(client.device_list))

    merged, conflicts = {}, set()
    for row in rows:
        did = row["did"]
        target = merged.setdefault(did, {"did": did})
        for key in ("mac", "model", "name"):
            value = row[key]
            if value:
                if key != "name" and target.get(key) and target[key] != value:
                    conflicts.add(did)
                target[key] = value
    devices = [DeviceRef(row["mac"], did, row["model"], row.get("name") or row["model"])
               for did, row in merged.items()
               if did not in conflicts and row.get("mac") and row.get("model") in _MODELS]
    # Never resolve one MAC to multiple DIDs, even when the model is identical.
    counts = {}
    for device in devices:
        counts[device.mac] = counts.get(device.mac, 0) + 1
    return sorted((device for device in devices if counts[device.mac] == 1),
                  key=lambda device: (device.name, device.did))


def _ready(cloud):
    if not getattr(cloud, "service_token", None) or not getattr(cloud, "ssecurity", None):
        raise ProviderError("SOURCE_ACCOUNT_NOT_READY: existing Miot session is not ready")


async def _request(cloud, model, api, payload):
    """One POST using Miot public crypto/session primitives; no login/retry/logging."""
    _ready(cloud)
    response = None
    try:
        import aiohttp
        url = cloud.get_api_url(api)
        params = cloud.rc4_params("POST", url, {"data": cloud.json_encode(payload)})
        session = cloud.api_session(**{"async": True})
        response = await session.post(
            url, data=params,
            headers={"MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4", "Accept-Encoding": "identity",
                     "MIOT-REQUEST-MODEL": model},
            timeout=aiohttp.ClientTimeout(total=25), allow_redirects=False)
        if response.status != 200:
            raise ProviderError(f"UNKNOWN: cloud HTTP {response.status}; do not resend")
        raw = await response.text()
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            body = json.loads(cloud.decrypt_data(cloud.signed_nonce(params["_nonce"]), raw))
        if not isinstance(body, dict) or type(body.get("code")) is not int:
            raise ProviderError("UNKNOWN: ambiguous cloud response; do not resend")
        if body["code"] != 0:
            raise ProviderError(f"CLOUD_REJECTED: business code {body['code']}")
        return body.get("result")
    except ProviderError:
        raise
    except asyncio.CancelledError:
        raise ProviderError("UNKNOWN: cloud request cancelled; do not resend") from None
    except Exception:
        raise ProviderError("UNKNOWN: cloud transport or response failure; do not resend") from None
    finally:
        if response is not None:
            response.release()


async def _target(hass, account_id, device):
    source = _resolve(hass, account_id)
    if source.cloud is None:
        raise ProviderError("SOURCE_METADATA_ONLY: xiaomi_home OAuth cannot use legacy BLE cloud APIs")
    if not isinstance(device, DeviceRef) or device.model not in _MODELS:
        raise ProviderError("UNSUPPORTED_DEVICE: model is not supported")
    mac = _mac(device.mac)
    if mac is None or not any(
        (row.mac, row.did, row.model) == (mac, device.did, device.model)
        for row in await get_devices(hass, source.id)
    ):
        raise ProviderError("DEVICE_NOT_VERIFIED: no reliable source MAC/DID/model association")
    # Metadata loading can yield to a source reload; resolve again rather than
    # keeping an old authenticated runtime object across that await.
    source = _resolve(hass, source.id)
    if source.cloud is None:
        raise ProviderError("SOURCE_METADATA_ONLY: Miot source is no longer loaded")
    return source.cloud


async def get_bindkey(hass, account_id, device: DeviceRef) -> bytes:
    cloud = await _target(hass, account_id, device)
    result = await _request(cloud, device.model, "v2/device/blt_get_beaconkey",
                            {"did": device.did, "pdid": 1})
    key = result.get("beaconkey") if isinstance(result, dict) else None
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", key):
        raise ProviderError("UNKNOWN: invalid beaconkey response")
    return bytes.fromhex(key)


async def get_scale_ble_token(hass, account_id, device: DeviceRef) -> bytes:
    """Read ONLY the verified scale's existing Standard Auth token into RAM."""
    if device.model != SCALE_MODEL:
        raise ProviderError("UNSUPPORTED_DEVICE: GATT history requires the supported scale")
    cloud = await _target(hass, account_id, device)
    rows = await cloud.async_get_devices(renew=False)
    iterable = rows.values() if isinstance(rows, dict) else rows or []
    targets = [row for row in iterable if isinstance(row, dict)
               and str(row.get("did")) == device.did
               and _mac(row.get("mac")) == device.mac
               and row.get("model") == device.model]
    tokens = {row.get("token") for row in targets if row.get("token")}
    if len(tokens) != 1:
        raise ProviderError("BLE_TOKEN_UNAVAILABLE: target metadata has no unique token")
    token = tokens.pop()
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-fA-F]{24}", token):
        raise ProviderError("BLE_TOKEN_UNAVAILABLE: expected a 12-byte Standard Auth token")
    return bytes.fromhex(token)


async def report_co2(hass, account_id, device: DeviceRef, value: int) -> None:
    if not isinstance(device, DeviceRef) or device.model != CO2_MODEL:
        raise ProviderError("UNSUPPORTED_UPLOAD: this property-report method only supports CO2")
    await report_environment(hass, account_id, device, {"co2": value})


async def report_environment(hass, account_id, device: DeviceRef, readings: dict) -> None:
    """Report selected fields from one verified broadcast in ONE non-retried POST.

    Temperature uses the official [-30, 100] Celsius range; three decimals
    remove IEEE754 tails without smoothing (the device spec resolution is 0.1C).
    New3pr humidity is integral uint8; T2 temperature/humidity are floats.
    Battery is local only; T2 has no CO2 upload property.
    None means every exact property was acked with integer code zero; UNKNOWN
    must never authorize an automatic resend.
    """
    if not isinstance(device, DeviceRef) or device.model not in ENVIRONMENT_MODELS:
        raise ProviderError("UNSUPPORTED_UPLOAD: environment reporting requires the verified model")
    properties = _ENVIRONMENT_PROPERTIES[device.model]
    if not isinstance(readings, dict) or not readings or readings.keys() - properties.keys():
        raise ProviderError("INVALID_VALUE: invalid environment selection")
    props = []
    for key, value in readings.items():
        if key == "co2":
            valid = type(value) is int and 0 <= value <= 65535
        else:
            valid = (type(value) in (int, float)
                     and (type(value) is int or math.isfinite(value)))
            if valid:
                if key == "temperature":
                    valid = -30 <= value <= 100
                else:
                    valid = (0 <= value <= 100
                             and (device.model == T2_MODEL
                                  or type(value) is int or value.is_integer()))
        if not valid:
            raise ProviderError("INVALID_VALUE: invalid environment measurement")
        if device.model == T2_MODEL:
            value = round(float(value), 3)
        elif key == "humidity":
            value = int(value)
        elif key == "temperature":
            value = round(value, 3)
        siid, piid = properties[key]
        props.append({"did": device.did, "siid": siid, "piid": piid, "value": value})
    cloud = await _target(hass, account_id, device)
    result = await _request(cloud, device.model, "v2/miotspec/prop_changed", {"props": props})
    if isinstance(result, dict):
        # Two possible wrappers are supported, but conflicting wrappers are not.
        wrappers = [result[key] for key in ("results", "result") if key in result]
        result = wrappers[0] if len(wrappers) == 1 else None
    if not isinstance(result, list) or len(result) != len(props):
        raise ProviderError("UNKNOWN: ambiguous per-property acknowledgement; do not resend")
    expected = {(prop["did"], prop["siid"], prop["piid"]) for prop in props}
    seen = set()
    for item in result:
        if (not isinstance(item, dict) or type(item.get("did")) is not str
                or type(item.get("siid")) is not int or type(item.get("piid")) is not int
                or type(item.get("code")) is not int):
            raise ProviderError("UNKNOWN: invalid per-property acknowledgement; do not resend")
        identity = (item["did"], item["siid"], item["piid"])
        if identity not in expected or identity in seen:
            raise ProviderError("UNKNOWN: unmatched per-property acknowledgement; do not resend")
        seen.add(identity)
    if seen != expected:
        raise ProviderError("UNKNOWN: incomplete per-property acknowledgement; do not resend")
    if any(item["code"] != 0 for item in result):
        raise ProviderError("CLOUD_REJECTED: property returned a nonzero business code")


def _scale_device(device):
    if not isinstance(device, DeviceRef) or device.model != SCALE_MODEL:
        raise ProviderError("UNSUPPORTED_UPLOAD: pure weight requires an MS110 scale")


async def get_scale_profiles(hass, account_id, device: DeviceRef) -> list[ScaleProfile]:
    """Read only indexInfo for this verified DID using the loaded Miot session.

    Do not choose a primary profile, infer a profile from a broadcast tag, cache
    personal data, or persist anything except the user's selected accountId.
    """
    _scale_device(device)
    cloud = await _target(hass, account_id, device)
    return await _scale_profiles(cloud, device)


async def _scale_profiles(cloud, device):
    uid = cloud.user_id
    result = await _request(cloud, device.model, "eco/common/scale/indexInfo", {
        "uid": uid, "accountId": uid, "deviceId": device.did, "model": device.model})
    if not isinstance(result, dict) or not isinstance(result.get("accountInfoList"), list):
        raise ProviderError("UNKNOWN: invalid indexInfo accountInfoList shape")
    profiles, seen = [], set()
    for user in result["accountInfoList"]:
        try:
            validate_user(user)
        except ValueError:
            raise ProviderError("UNKNOWN: invalid scale profile fields") from None
        # indexInfo already scopes the list to this DID. If a returned profile
        # explicitly names a different device, never accept or silently join it.
        if "deviceId" in user and str(user["deviceId"]) != device.did:
            raise ProviderError("UNKNOWN: scale profile belongs to another device")
        profile_id = str(user["accountId"])
        if profile_id in seen:
            raise ProviderError("UNKNOWN: duplicate scale profile accountId")
        seen.add(profile_id)
        profiles.append(ScaleProfile(profile_id, user["name"], user["uid"],
                                     user["accountCode"], user["type"], deepcopy(user)))
    return profiles


async def _scale_records(cloud, device, expected, *, native=False):
    timestamp = expected["createTime"]
    rows = await _request(cloud, device.model, "eco/common/scale/getUserDataByPage", {
        "model": device.model, "uid": expected["uid"], "did": device.did,
        "accountId": expected["accountId"], "beginTime": timestamp + 1000,
        "endTime": timestamp - 1000, "pageSize": 20})
    # Verified response.result is directly a list; no speculative wrappers.
    # A full page might hide another same-time row, so fail closed.
    if not isinstance(rows, list) or len(rows) >= 20:
        raise ProviderError("UNKNOWN: invalid or truncated scale history result")
    matches = []
    try:
        for row in rows:
            if native:
                if not isinstance(row, dict) or not isinstance(row.get("data"), str):
                    raise ValueError("UNKNOWN: invalid scale record shape")
                data = json.loads(row["data"])
                times = (row.get("createTime"), data.get("time") if isinstance(data, dict) else None)
                if any(type(value) not in (int, float) or
                       (type(value) is float and not math.isfinite(value)) for value in times):
                    raise ValueError("UNKNOWN: invalid scale record timestamps")
                same_time = (times[0] == expected["createTime"]
                             or times[1] == expected["createTime"] // 1000)
            else:
                record_data(row)
                same_time = same_measurement_time(row, expected)
            if not same_identity(row, expected):
                raise ValueError("UNKNOWN: scale history identity mismatch")
            if same_time:
                matches.append(row)
    except ValueError as err:
        raise ProviderError(str(err)) from None
    return matches


def _scale_existing(rows, expected, summary, matcher=record_matches):
    if len(rows) != 1:
        return {**summary, "status": "existing", "code": "EXISTING_AMBIGUOUS"}
    row = rows[0]
    if matcher(row, expected):
        return {**summary, "status": "existing_verified", "code": "EXISTING_VERIFIED"}
    try:
        different = (decimal_weight(record_data(row).get("weight"))
                     != decimal_weight(record_data(expected)["weight"]))
    except ValueError:
        different = False
    if different:
        return {**summary, "status": "existing_conflict", "code": "EXISTING_WEIGHT_CONFLICT"}
    return {**summary, "status": "existing", "code": "EXISTING_UNVERIFIED"}


async def _scale_reference(cloud, device, profile, now_ms):
    rows = await _request(cloud, device.model, "eco/common/scale/getUserDataByPage", {
        "model": device.model, "uid": profile.uid, "did": device.did,
        "accountId": profile.account_id, "beginTime": now_ms,
        "endTime": 0, "pageSize": 20})
    try:
        history = scale_history_reference(rows, {
            "model": device.model, "did": device.did,
            "uid": profile.uid, "accountId": profile.account_id}, now_ms)
        baseline = scale_cloud_baseline(profile.user, now_ms)
    except ValueError as err:
        raise ProviderError(str(err)) from None
    reference = history
    if baseline and (not history or baseline["reference_time"] > history["reference_time"]):
        reference = baseline
    return {"profile_id": profile.account_id, "name": profile.name,
            **(reference or {"weight": None, "reference_time": None,
                             "source": "no_reference"})}


async def _scale_references(cloud, device, profiles):
    # Each request owns its headers. One page per profile, no fallback endpoints
    # or persistent health cache; successful uploads become next call's history.
    now_ms = time.time_ns() // 1_000_000
    results = await asyncio.gather(
        *(_scale_reference(cloud, device, profile, now_ms) for profile in profiles),
        return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            if isinstance(result, ProviderError):
                raise result
            raise ProviderError("UNKNOWN: scale reference read failed; do not resend") from None
    return results


async def get_scale_weight_references(hass, account_id, device: DeviceRef) -> list[dict]:
    """Read-only, DID-scoped references for authorized UI/diagnostics (not logs).

    reference_time is Unix milliseconds; source is history, cloud_baseline or
    no_reference. Only profile_id/name/weight/reference_time/source are exposed.
    A failed history read is UNKNOWN, never an empty history/baseline fallback.
    """
    _scale_device(device)
    try:
        async with asyncio.timeout(180):
            cloud = await _target(hass, account_id, device)
            profiles = await _scale_profiles(cloud, device)
            return await _scale_references(cloud, device, profiles)
    except TimeoutError:
        raise ProviderError("UNKNOWN: scale reference budget exhausted; do not resend") from None


async def report_scale_measurement(hass, account_id, device, *, weight_kg,
                                   measurement_time, mode, profile_id=None,
                                   max_difference=2.5, native_record=None) -> dict:
    """One selected record kind, one write at most, never a downgrade/resend.

    Caller persists its unified attempt BEFORE calling, and owns new-measurement
    authorization/freshness. measurement_time is the canonical device timestamp.
    Native weight wins over broadcast weight, including nearest recognition.
    """
    _scale_device(device)
    kind = "native" if native_record is not None else "weight_only"
    try:
        if mode not in ("fixed", "nearest"):
            raise ValueError("INVALID_VALUE: invalid scale selection mode")
        if type(measurement_time) is not int or measurement_time <= 0:
            raise ValueError("INVALID_VALUE: measurement_time must be positive Unix seconds")
        if native_record is not None:
            data = native_measurement(native_record)
            if data["time"] != measurement_time:
                return {"status": "conflict", "code": "NATIVE_TIME_CONFLICT",
                        "record_kind": kind, "measurement_time": measurement_time}
            weight_kg = data["weight"]
        nearest_weight_reference(weight_kg, [], max_difference)
    except ValueError as err:
        raise ProviderError(str(err)) from None
    if mode == "fixed" and (not isinstance(profile_id, str) or not profile_id):
        raise ProviderError("SCALE_PROFILE_NOT_FOUND: explicitly select a current profile")
    try:
        async with asyncio.timeout(180):
            cloud = await _target(hass, account_id, device)
            profiles = await _scale_profiles(cloud, device)
            summary = {"measurement_time": measurement_time, "record_kind": kind,
                       "weight_kg": float(decimal_weight(weight_kg))}
            if mode == "nearest":
                references = await _scale_references(cloud, device, profiles)
                match = nearest_weight_reference(weight_kg, references, max_difference)
                summary["references"] = references
                if match["status"] != "matched":
                    return {**summary, **match, "code": match["status"].upper()}
                profile_id = match["matched_profile_id"]
                summary.update({k: v for k, v in match.items() if k != "status"})
            selected = [profile for profile in profiles if profile.account_id == profile_id]
            if len(selected) != 1:
                raise ProviderError("SCALE_PROFILE_NOT_FOUND: selected profile is no longer available")
            result = await _report_scale_for_profile(
                cloud, device, selected[0], weight_kg, measurement_time,
                native_record=native_record)
            if result["status"] in ("existing_conflict", "existing"):
                result = {**result, "status": "conflict"}
            return {**summary, **result}
    except TimeoutError:
        raise ProviderError("UNKNOWN: scale measurement budget exhausted; do not resend") from None


async def report_scale_weight_nearest(hass, account_id, device: DeviceRef,
                                     weight_kg: float, measurement_time: int,
                                     max_difference=2.5) -> dict:
    """All live DID profiles, exact unique nearest reference, ONE chosen upload.

    Runtime must supply a new weighing (no replay). UNKNOWN never authorizes a
    resend. The entire operation has a 180s budget, including parallel history
    reads, precheck, one add, and at most three readbacks.
    """
    _scale_device(device)
    try:
        # Validate even if there are no profiles/references; no network for bad
        # measurement input. Freshness itself remains exclusively runtime-owned.
        nearest_weight_reference(weight_kg, [], max_difference)
        if type(measurement_time) is not int or measurement_time <= 0:
            raise ValueError("INVALID_VALUE: measurement_time must be positive Unix seconds")
    except ValueError as err:
        raise ProviderError(str(err)) from None
    try:
        async with asyncio.timeout(180):
            cloud = await _target(hass, account_id, device)
            profiles = await _scale_profiles(cloud, device)
            references = await _scale_references(cloud, device, profiles)
            match = nearest_weight_reference(weight_kg, references, max_difference)
            summary = {"measurement_time": measurement_time,
                       "weight_kg": float(decimal_weight(weight_kg)),
                       "references": references}
            if match["status"] != "matched":
                return {**summary, **match, "code": match["status"].upper()}
            winner = next(profile for profile in profiles
                          if profile.account_id == match["matched_profile_id"])
            result = await _report_scale_for_profile(
                cloud, device, winner, weight_kg, measurement_time)
            # Keep the fixed-profile API's legacy existing* statuses unchanged.
            if result["status"] in ("existing_conflict", "existing"):
                result = {**result, "status": "conflict"}
            return {**summary, **{key: value for key, value in match.items()
                                  if key != "status"}, **result}
    except TimeoutError:
        raise ProviderError("UNKNOWN: scale weight budget exhausted; do not resend") from None


async def report_scale_weight(hass, account_id, device: DeviceRef, profile_id: str,
                              weight_kg: float, measurement_time: int) -> dict:
    """Precheck -> at most ONE official UserEditor add -> real readback.

    Only a fresh authorized weighing may reach this API (runtime owns freshness
    and validated device/receive time). No history replay, body composition or
    GATT. UNKNOWN must never trigger an automatic resend. Readback queries have
    GET semantics despite the cloud's POST transport; only these may repeat.
    """
    _scale_device(device)
    if not isinstance(profile_id, str) or not profile_id:
        raise ProviderError("SCALE_PROFILE_NOT_FOUND: explicitly select a current profile")
    cloud = await _target(hass, account_id, device)
    profiles = await _scale_profiles(cloud, device)
    selected = [profile for profile in profiles if profile.account_id == profile_id]
    if len(selected) != 1:
        raise ProviderError("SCALE_PROFILE_NOT_FOUND: selected profile is no longer available")
    return await _report_scale_for_profile(cloud, device, selected[0],
                                           weight_kg, measurement_time)


async def _report_scale_for_profile(cloud, device, profile, weight_kg, measurement_time,
                                    *, native_record=None):
    """Use the exact selected snapshot; never fetch/reselect a profile after match."""
    kind = "native" if native_record is not None else "weight_only"
    matcher = native_record_matches if native_record is not None else record_matches
    try:
        if native_record is not None:
            expected = build_native_record(device.model, device.did, profile.user, native_record)
            data = record_data(expected)
            weight_kg = data["weight"]
            if data["time"] != measurement_time:
                return {"status": "conflict", "code": "NATIVE_TIME_CONFLICT",
                        "record_kind": kind, "measurement_time": measurement_time}
        else:
            expected = build_weight_record(model=device.model, did=device.did,
                                          user=profile.user, weight_kg=weight_kg,
                                          measurement_time=measurement_time)
    except ValueError as err:
        if str(err).startswith("NATIVE_PROFILE_CONFLICT:"):
            return {"status": "conflict", "code": "NATIVE_PROFILE_CONFLICT",
                    "record_kind": kind, "measurement_time": measurement_time}
        raise ProviderError(str(err)) from None
    # Return only safe time/weight/status, never the embedded personal profile.
    summary = {"measurement_time": measurement_time, "record_kind": kind,
               "weight_kg": float(decimal_weight(weight_kg))}
    before = await _scale_records(cloud, device, expected, native=native_record is not None)
    if before:
        return _scale_existing(before, expected, summary, matcher)
    try:
        await _request(cloud, device.model, "eco/common/scale/add", expected)
    except ProviderError as err:
        if not str(err).startswith("UNKNOWN:") or asyncio.current_task().cancelling():
            # _request sanitizes cancellation as UNKNOWN. Do not suppress the
            # enclosing 180s deadline by starting more reads after it expires.
            raise
        # The write may have committed. Only query; NEVER resend the add.
    for attempt in range(3):
        if attempt:
            await asyncio.sleep(1)
        after = await _scale_records(cloud, device, expected, native=native_record is not None)
        if after:
            if len(after) == 1 and matcher(after[0], expected):
                return {**summary, "status": "verified", "code": "READBACK_VERIFIED"}
            raise ProviderError("UNKNOWN: scale readback conflict or ambiguity; do not resend")
    raise ProviderError("UNKNOWN: scale write not confirmed by readback; do not resend")

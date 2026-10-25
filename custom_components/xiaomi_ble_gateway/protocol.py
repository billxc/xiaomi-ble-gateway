"""Pure, authenticated MiBeacon V5 decoding for two explicitly supported PIDs.

Input is the FE95 service-data payload WITHOUT its 95fe UUID. No I/O or
replay/dedup state is kept here; callers must deduplicate only after MIC success.
Scale weights are community-protocol broadcast values, not screen/GATT-verified
measurements or a complete native cloud-v3 report.
"""
from dataclasses import dataclass
import math
import re
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESCCM


@dataclass
class DecodedFrame:
    counter: int
    product_id: int
    readings: dict[str, float]
    metadata: dict


_MODELS = {39609: "xiaomi.sensor_ht.new3pr", 19205: "xiaomi.scales.ms110"}
# Only EIDs already used by the target's researched decoder, not generic MiBeacon
# heuristics (in particular, do not interpret arbitrary battery-like objects).
_CO2_FIELDS = {
    0x601D: ("co2", "<H"),
    0x5C01: ("temperature", "<f"),
    0x4C01: ("temperature", "<f"),
    0x4801: ("temperature", "<f"),
    0x5C02: ("humidity", "<B"),
    0x4C02: ("humidity", "<B"),
    0x4802: ("humidity", "<B"),
    0x6012: ("humidity", "<B"),
    0x5403: ("battery", "<B"),
    0x5003: ("battery", "<B"),
    0x4C03: ("battery", "<B"),
    0x4803: ("battery", "<B"),
}


def decode_advertisement(
    service_data: bytes, address: str, bindkey: bytes
) -> DecodedFrame | None:
    """Return authenticated readings, None for basic/unsupported advertisements.

    Raises ValueError for malformed target frames, invalid key/MAC or bad MIC.
    counter is the full 32-bit (frame byte + three extension bytes) counter.
    CO2/temperature/humidity/battery units are ppm/°C/%/% respectively.
    Unknown, well-formed EIDs are ignored, never guessed as another model.
    """
    if len(service_data) < 5:
        raise ValueError("Truncated MiBeacon header")
    control, product_id, counter = struct.unpack_from("<HHB", service_data)
    if product_id not in _MODELS:
        return None
    if control >> 12 != 5 or control & 0x80:
        return None  # not a supported V5 non-mesh layout
    if not isinstance(address, str) or not re.fullmatch(
        r"(?:[0-9a-fA-F]{12}|(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})", address
    ):
        raise ValueError("Invalid source MAC")
    mac = bytes.fromhex(address.replace(":", ""))[::-1]
    position = 5
    if control & 0x10:
        if len(service_data) < position + 6:
            raise ValueError("Truncated embedded MAC")
        if service_data[position:position + 6] != mac:
            raise ValueError("Embedded/source MAC mismatch")
        position += 6
    if control & 0x20:
        if position >= len(service_data):
            raise ValueError("Missing capability")
        capability = service_data[position]
        position += 1
        if capability & 0x20:
            if position >= len(service_data):
                raise ValueError("Missing I/O capability")
            position += 1
    if control & 0x48 != 0x48:
        return None  # basic/no encrypted object data; never emit an empty update
    if len(bindkey) != 16:
        raise ValueError("Expected a 16-byte bindkey")
    if len(service_data) < position + 10:
        raise ValueError("Truncated encrypted object data")
    extension = service_data[-7:-4]
    nonce = mac + service_data[2:5] + extension
    try:
        plaintext = AESCCM(bindkey, tag_length=4).decrypt(
            nonce, service_data[position:-7] + service_data[-4:], b"\x11"
        )
    except InvalidTag:
        raise ValueError("MiBeacon MIC verification failed") from None

    readings: dict[str, float] = {}
    metadata = {
        "model": _MODELS[product_id],
        "mic_verified": True,
        "frame_control": control,
        "object_ids": [],
    }
    position = 0
    while position < len(plaintext):
        if position + 3 > len(plaintext):
            raise ValueError("Truncated object header")
        eid, size = struct.unpack_from("<HB", plaintext, position)
        position += 3
        if position + size > len(plaintext):
            raise ValueError("Truncated object value")
        value = plaintext[position:position + size]
        position += size
        metadata["object_ids"].append(eid)
        if product_id == 39609 and eid in _CO2_FIELDS:
            name, fmt = _CO2_FIELDS[eid]
            if size != struct.calcsize(fmt):
                raise ValueError("Invalid known CO2 sensor object length")
            number = float(struct.unpack(fmt, value)[0])
            if not math.isfinite(number):
                raise ValueError("Non-finite sensor measurement")
            readings[name] = number
        elif product_id == 19205 and eid == 0x6E16:
            if size != 9:
                raise ValueError("Invalid scale object length")
            tag, packed, device_time = struct.unpack("<BII", value)
            mass = packed & 0x7FF
            heart_rate = (packed >> 11) & 0x7F
            metadata.update(
                tag=tag,
                device_time=device_time,
                mass_raw=mass,
                impedance_ohm=(packed >> 18) / 10,
                weight_sentinel=mass == 0x7FF,
                weight_source="community_protocol_broadcast",
            )
            # Also remove an earlier value if a multi-object frame ends in a
            # sentinel. Never write None (or 204.7) over a caller's previous state.
            readings.pop("weight_kg", None)
            readings.pop("heart_rate_bpm", None)
            if mass not in (0, 0x7FF):
                readings["weight_kg"] = mass / 10
            if 0 < heart_rate < 127:
                readings["heart_rate_bpm"] = float(heart_rate + 50)
    return DecodedFrame(
        counter=int.from_bytes(bytes([counter]) + extension, "little"),
        product_id=product_id,
        readings=readings,
        metadata=metadata,
    )

"""Encode OS-visible BLE advertising data, not an RF packet capture.

DataSections are authoritative and retain their bytes, order and duplicates.
Parsed Windows properties only fill missing fields; nothing is decrypted or
filtered by vendor. No cross-packet state, platform handles or file IO is used.
"""

from collections.abc import Iterable, Mapping
from uuid import UUID

_BASE_SUFFIX = "-0000-1000-8000-00805f9b34fb"
_UUID_LISTS = {0x02: 2, 0x03: 2, 0x04: 4, 0x05: 4, 0x06: 16, 0x07: 16}
_SERVICE_DATA = {0x16: 2, 0x20: 4, 0x21: 16}


def _integer(value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError("invalid_integer")
    return value


def _bytes(value: object) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError("invalid_bytes")
    return bytes(value)


def _uuid(value: object) -> UUID:
    try:
        if isinstance(value, UUID):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return UUID(f"{_integer(value, 0, 0xffffffff):08x}{_BASE_SUFFIX}")
        if isinstance(value, str):
            if len(value) in (4, 8):
                return UUID(f"{int(value, 16):08x}{_BASE_SUFFIX}")
            return UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("invalid_uuid") from exc
    raise ValueError("invalid_uuid")


def _wire_uuid(value: UUID) -> tuple[int, bytes]:
    text = str(value)
    if text[8:] == _BASE_SUFFIX:
        short = int(text[:8], 16)
        width = 2 if short <= 0xffff else 4
        return width, short.to_bytes(width, "little")
    # BLE uses little endian for the ENTIRE UUID, not UUID.bytes_le's GUID layout.
    return 16, value.bytes[::-1]


def _decode_uuid(data: bytes) -> UUID:
    if len(data) == 16:
        return UUID(bytes=data[::-1])
    return UUID(f"{int.from_bytes(data, 'little'):08x}{_BASE_SUFFIX}")


def pack_advertisement(
    sections: list[tuple[int, bytes]],
    *,
    manufacturer_data: Mapping[int, bytes] | Iterable[tuple[int, bytes]] | None = None,
    service_uuids: Iterable[str | UUID | int] | None = None,
    local_name: str = "",
    flags: int | None = None,
    tx_power: int | None = None,
) -> bytes:
    """Return concatenated [length, AD type, data] structures.

    Each data value may contain at most 254 bytes. There is no 31-byte aggregate
    limit (Windows may expose extended advertising). Invalid input raises
    ValueError; nothing is truncated. Raw sections are not semantically repaired.

    Manufacturer properties accept a mapping or (company_id, bytes) pairs.
    A raw manufacturer field wins for its company, even if metadata differs.
    Existing UUID lists AND service-data UUID prefixes suppress duplicate UUID
    metadata. New lists use INCOMPLETE types: WinRT does not expose completeness.
    Existing short/complete names, flags and TX power win over parsed properties.
    """
    if not isinstance(sections, list):
        raise ValueError("invalid_sections")
    encoded: list[bytes] = []
    present_types: set[int] = set()
    companies: set[int] = set()
    known_uuids: set[UUID] = set()

    def append(ad_type: int, data: bytes) -> None:
        if len(data) > 254:
            raise ValueError("ad_too_long")
        encoded.append(bytes((len(data) + 1, ad_type)) + data)

    for section in sections:
        if not isinstance(section, tuple) or len(section) != 2:
            raise ValueError("invalid_section")
        ad_type = _integer(section[0], 0, 255)
        data = _bytes(section[1])
        append(ad_type, data)
        present_types.add(ad_type)
        if ad_type == 0xff and len(data) >= 2:
            companies.add(int.from_bytes(data[:2], "little"))
        width = _UUID_LISTS.get(ad_type)
        if width and len(data) % width == 0:
            known_uuids.update(_decode_uuid(data[i:i + width]) for i in range(0, len(data), width))
        width = _SERVICE_DATA.get(ad_type)
        if width and len(data) >= width:
            known_uuids.add(_decode_uuid(data[:width]))

    if manufacturer_data is not None:
        entries = manufacturer_data.items() if isinstance(manufacturer_data, Mapping) else manufacturer_data
        try:
            iterator = iter(entries)
        except TypeError as exc:
            raise ValueError("invalid_manufacturer_data") from exc
        metadata_seen: set[tuple[int, bytes]] = set()
        for entry in iterator:
            if not isinstance(entry, (tuple, list)) or len(entry) != 2:
                raise ValueError("invalid_manufacturer_data")
            company = _integer(entry[0], 0, 0xffff)
            data = _bytes(entry[1])
            if len(data) > 252:
                raise ValueError("ad_too_long")
            if company not in companies and (company, data) not in metadata_seen:
                append(0xff, company.to_bytes(2, "little") + data)
                metadata_seen.add((company, data))

    groups: dict[int, bytearray] = {2: bytearray(), 4: bytearray(), 16: bytearray()}
    if service_uuids is not None:
        if isinstance(service_uuids, (str, bytes)):
            raise ValueError("invalid_service_uuids")
        try:
            iterator = iter(service_uuids)
        except TypeError as exc:
            raise ValueError("invalid_service_uuids") from exc
        for item in iterator:
            value = _uuid(item)
            if value not in known_uuids:
                width, data = _wire_uuid(value)
                groups[width].extend(data)
                known_uuids.add(value)
    for width, data in groups.items():
        chunk_size = (254 // width) * width
        for start in range(0, len(data), chunk_size):
            append({2: 0x02, 4: 0x04, 16: 0x06}[width], bytes(data[start:start + chunk_size]))

    if not isinstance(local_name, str):
        raise ValueError("invalid_local_name")
    try:
        name_bytes = local_name.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("invalid_local_name") from exc
    if len(name_bytes) > 254:
        raise ValueError("ad_too_long")
    if name_bytes and not present_types.intersection((0x08, 0x09)):
        append(0x09, name_bytes)
    if flags is not None:
        value = _integer(flags, 0, 255)
        if 0x01 not in present_types:
            append(0x01, bytes((value,)))
    if tx_power is not None:
        value = _integer(tx_power, -128, 127)
        if 0x0a not in present_types:
            append(0x0a, bytes((value & 255,)))
    return b"".join(encoded)

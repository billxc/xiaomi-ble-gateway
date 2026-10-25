"""Pure native MS110 history codec and synchronous private SQLite store.

Field order/scales are protocol facts from ms110-v54 event 10.4 (online) and
9.1 (offline): 34 fields, or an idx prefix plus those same 34 fields. This is
an independent implementation, not vendor code. No body metrics are derived.

parse_native_csv returns {offline: bool, raw_fields: [34 verbatim strings],
data: {34 field names: exact decimal strings}, idx: str (offline only)}.
In particular data.weight is raw_fields[3] / 100, data.time is device Unix
seconds WITHOUT adjustment, and miid/duid/userType remain strings. Strings
are intentional: JSON numbers/JavaScript doubles cannot preserve all IDs or
native precision. This is a LOCAL payload, not a cloud record/upload schema.
Only complete numeric CSV is accepted; integer raw fields are required except
bmr/bt/ma/sbc, whose protocol mapping permits decimal values. No rounding,
missing-field defaults, user attribution, or authenticity claims are made.

HistoryStore methods are blocking: run in the caller's executor. Connections
are per operation; each merge is one BEGIN IMMEDIATE transaction, synchronous
FULL, returning counts only AFTER commit. Exceptions propagate (no false
persistence success). A dedicated private parent directory is required.
History is durable and has NO automatic TTL: raw-recorder retention does not
apply. The integration must document that distinction and any future explicit
history deletion policy. No cloud writes, device IO or Action9.2 confirmation.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import re
import sqlite3


FIELDS = (
    "miid", "duid", "userType", "weight", "heartRate", "status", "time",
    "bfp", "slm", "bwp", "bmc", "vfl", "pp", "smm", "bmi", "swt", "mc",
    "wc", "fc", "whr", "bmr", "bt", "ma", "sbc", "slp", "bmcp", "bfm",
    "ffm", "bwm", "pm", "bodyResOriginZ50", "bodyResOriginZ250",
    "bodyResSmoothZ50", "bodyResSmoothZ250",
)
_INTEGER = re.compile(r"[+-]?[0-9]+\Z")
_NUMBER = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?\Z")


def _shift(raw: str, places: int) -> str:
    # Decimal tuple construction, unlike division/scaleb, cannot round under
    # the caller's decimal context (including unusually long native integers).
    value = Decimal(raw)
    sign, digits, exponent = value.as_tuple()
    return format(Decimal((sign, digits, exponent - places)), "f")


def parse_native_csv(csv: str, *, offline: bool) -> dict:
    """Decode one CSV, NOT an underscore-separated offline event batch."""
    if not isinstance(csv, str) or type(offline) is not bool:
        raise ValueError("Native CSV string and explicit offline boolean required")
    parts = csv.split(",")
    if len(parts) != 34 + int(offline):
        raise ValueError("Complete native CSV requires 34 online / 35 offline fields")
    if offline and not _INTEGER.fullmatch(parts[0]):
        raise ValueError("Invalid native idx")
    raw = parts[1:] if offline else parts
    data = {}
    for index, (name, value) in enumerate(zip(FIELDS, raw)):
        pattern = _NUMBER if 20 <= index <= 23 else _INTEGER
        if not pattern.fullmatch(value):
            raise ValueError("Invalid native numeric field")
        places = 2 if index == 3 else 1 if 7 <= index <= 19 or index >= 24 else 0
        data[name] = _shift(value, places) if places else value
    result = {"offline": offline, "raw_fields": raw, "data": data}
    if offline:
        result["idx"] = parts[0]
    return result


def _decimal_key(value) -> str:
    """Exact numeric equality key; never apply a tolerance or binary arithmetic.

    Broadcast callers may supply Decimal/string/int, or an already-decoded
    float (interpreted via its decimal str, never rounded to native precision).
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Finite decimal value required")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Finite decimal value required") from None
    if not number.is_finite():
        raise ValueError("Finite decimal value required")
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if number == 0 else text.lstrip("+")


def _integer_key(value) -> str:
    text = _decimal_key(value)
    if "." in text:
        raise ValueError("Integer value required")
    return text


def _mac(value: str) -> str:
    text = value.replace(":", "").replace("-", "").upper()
    if not re.fullmatch(r"[0-9A-F]{12}", text):
        raise ValueError("MAC must contain 12 hex digits")
    return text


def _metadata(source, received_at):
    if not isinstance(source, str) or not source:
        raise ValueError("Nonempty source required")
    if (isinstance(received_at, bool) or not isinstance(received_at, (int, float))
            or not math.isfinite(received_at)):
        raise ValueError("Finite received_at Unix seconds required")


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS device_bindings (
        mac TEXT PRIMARY KEY, did TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS native (
        id INTEGER PRIMARY KEY, mac TEXT NOT NULL, raw_json TEXT NOT NULL,
        data_json TEXT NOT NULL, device_time TEXT NOT NULL, weight TEXT NOT NULL,
        UNIQUE(mac, raw_json))""",
    """CREATE INDEX IF NOT EXISTS native_exact ON native(mac, device_time, weight)""",
    """CREATE TABLE IF NOT EXISTS native_observations (
        native_id INTEGER NOT NULL REFERENCES native(id), source TEXT NOT NULL,
        offline INTEGER NOT NULL, idx TEXT NOT NULL, idx_key TEXT NOT NULL,
        first_received_at REAL NOT NULL, last_received_at REAL NOT NULL,
        PRIMARY KEY(native_id, source, offline, idx))""",
    """CREATE INDEX IF NOT EXISTS native_idx ON native_observations(idx_key, native_id)""",
    """CREATE TABLE IF NOT EXISTS broadcast (
        id INTEGER PRIMARY KEY, mac TEXT NOT NULL, device_time TEXT NOT NULL,
        weight TEXT NOT NULL, counter TEXT NOT NULL,
        native_id INTEGER REFERENCES native(id), candidate_count INTEGER NOT NULL DEFAULT 0,
        UNIQUE(mac, device_time, weight, counter))""",
    """CREATE TABLE IF NOT EXISTS broadcast_observations (
        broadcast_id INTEGER NOT NULL REFERENCES broadcast(id), source TEXT NOT NULL,
        first_received_at REAL NOT NULL, last_received_at REAL NOT NULL,
        PRIMARY KEY(broadcast_id, source))""",
)


class HistoryStore:
    """Small-batch history store, independent of HA and raw-recorder lifecycle.

    merge_native accepts an iterable of parse_native_csv payloads. Native
    identity is (MAC, all 34 verbatim raw fields), never idx or time/weight.
    Offline indices and per-source first/last receipt times are retained.
    Repeated reads do not add native rows. Even differing lexemes are preserved
    rather than silently choosing one representation of a complete record.

    Merge summaries: inserted, duplicates (input row counts); conflicts (number
    of distinct touched native records having a conflicting peer, or 0/1 for a
    broadcast with multiple candidates); linked/ambiguous/unmatched (current
    device-wide broadcast counts). linked means candidate_exact only: broadcast
    and native have no proven shared record ID. Exact time/weight is NOT proof
    of identity or the same measurement, even with a device binding. Both data
    records remain separate; native_id points to a candidate, never a verified
    same-measurement record. A peer conflicts if same time/weight but
    different full fields, OR shares a device-local offline idx. Replays can
    report existing conflicts again, without inserting rows or overwriting.
    totals returns native/broadcast/conflicts plus those association counts;
    its conflicts counts all native records with peers, NOT pairs of records.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with self._transaction() as connection:
            for statement in _SCHEMA:
                connection.execute(statement)

    def bind_device(self, mac, did) -> None:
        """Persist a caller-supplied MAC/DID binding, not authenticate a device.

        Root must call this in its executor before writing each device. Repeating
        the same binding is a no-op; a different DID raises ValueError without
        changing the binding or history. Returns None only after FULL commit.
        """
        mac = _mac(mac)
        if not isinstance(did, str) or not did.strip():
            raise ValueError("Nonempty DID string required")
        with self._transaction() as connection:
            connection.execute("""INSERT INTO device_bindings (mac, did) VALUES (?, ?)
                ON CONFLICT(mac) DO NOTHING""", (mac, did))
            bound_did = connection.execute(
                "SELECT did FROM device_bindings WHERE mac = ?", (mac,)).fetchone()[0]
            if bound_did != did:
                raise ValueError("Device binding conflict: MAC already bound to another DID")

    def _connect(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = ON")
        except Exception:
            connection.close()
            raise
        return connection

    @contextmanager
    def _transaction(self):
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _relink(connection, mac):
        # Recompute ALL observations: a later complete native record can make a
        # previously unique candidate ambiguous. No association is verified proof
        # of identity or the same measurement; native and broadcast stay separate.
        connection.execute("""UPDATE broadcast SET candidate_count = (
            SELECT COUNT(*) FROM native n WHERE n.mac = broadcast.mac
            AND n.device_time = broadcast.device_time AND n.weight = broadcast.weight
        ), native_id = (
            SELECT CASE WHEN COUNT(*) = 1 THEN MIN(n.id) ELSE NULL END FROM native n
            WHERE n.mac = broadcast.mac AND n.device_time = broadcast.device_time
            AND n.weight = broadcast.weight
        ) WHERE mac = ?""", (mac,))

    @staticmethod
    def _conflicted(connection, mac) -> set:
        return {row[0] for row in connection.execute("""SELECT n.id FROM native n
            WHERE n.mac = ? AND (
                EXISTS (SELECT 1 FROM native other WHERE other.mac = n.mac
                    AND other.id != n.id AND other.device_time = n.device_time
                    AND other.weight = n.weight)
                OR EXISTS (SELECT 1 FROM native_observations a
                    JOIN native_observations b ON a.idx_key = b.idx_key
                    JOIN native other ON other.id = b.native_id
                    WHERE a.native_id = n.id AND a.offline = 1 AND b.offline = 1
                    AND other.mac = n.mac AND other.id != n.id)
            )""", (mac,))}

    @staticmethod
    def _associations(connection, mac):
        linked, ambiguous, unmatched = connection.execute("""SELECT
            COALESCE(SUM(candidate_count = 1), 0),
            COALESCE(SUM(candidate_count > 1), 0),
            COALESCE(SUM(candidate_count = 0), 0)
            FROM broadcast WHERE mac = ?""", (mac,)).fetchone()
        return {"linked": linked, "ambiguous": ambiguous, "unmatched": unmatched}

    def merge_native(self, mac, records, source, received_at) -> dict:
        """Atomically persist one small batch; malformed rows abort the batch.

        data is re-decoded from raw_fields and must match; callers cannot replace
        native body metrics with a broadcast value while keeping the same raw ID.
        """
        mac = _mac(mac)
        _metadata(source, received_at)
        inserted = duplicates = 0
        touched = set()
        with self._transaction() as connection:
            for record in records:
                raw = record["raw_fields"]
                offline = record["offline"]
                csv = ",".join(([record["idx"]] if offline else []) + raw)
                parsed = parse_native_csv(csv, offline=offline)
                if parsed != record:
                    raise ValueError("Native payload differs from raw fields")
                raw_json = json.dumps(parsed["raw_fields"], separators=(",", ":"))
                data = parsed["data"]
                cursor = connection.execute("""INSERT INTO native
                    (mac, raw_json, data_json, device_time, weight) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(mac, raw_json) DO NOTHING""", (
                        mac, raw_json, json.dumps(data, separators=(",", ":")),
                        _integer_key(data["time"]), _decimal_key(data["weight"])))
                inserted += cursor.rowcount
                duplicates += 1 - cursor.rowcount
                native_id = connection.execute(
                    "SELECT id FROM native WHERE mac = ? AND raw_json = ?",
                    (mac, raw_json)).fetchone()[0]
                touched.add(native_id)
                idx = parsed.get("idx", "")
                connection.execute("""INSERT INTO native_observations VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(native_id, source, offline, idx) DO UPDATE SET
                    first_received_at = MIN(first_received_at, excluded.first_received_at),
                    last_received_at = MAX(last_received_at, excluded.last_received_at)""",
                    (native_id, source, int(offline), idx,
                     _integer_key(idx) if offline else "", received_at, received_at))
            self._relink(connection, mac)
            summary = {"inserted": inserted, "duplicates": duplicates,
                       "conflicts": len(touched & self._conflicted(connection, mac)),
                       **self._associations(connection, mac)}
        return summary

    def merge_broadcast(self, mac, device_time, weight, counter, source, received_at) -> dict:
        """Store a small observation; counter is NOT an offline idx or native ID."""
        mac = _mac(mac)
        _metadata(source, received_at)
        key = (mac, _integer_key(device_time), _decimal_key(weight), _integer_key(counter))
        with self._transaction() as connection:
            cursor = connection.execute("""INSERT INTO broadcast
                (mac, device_time, weight, counter) VALUES (?, ?, ?, ?)
                ON CONFLICT(mac, device_time, weight, counter) DO NOTHING""", key)
            inserted = cursor.rowcount
            broadcast_id = connection.execute("""SELECT id FROM broadcast
                WHERE mac = ? AND device_time = ? AND weight = ? AND counter = ?""",
                key).fetchone()[0]
            connection.execute("""INSERT INTO broadcast_observations VALUES (?, ?, ?, ?)
                ON CONFLICT(broadcast_id, source) DO UPDATE SET
                first_received_at = MIN(first_received_at, excluded.first_received_at),
                last_received_at = MAX(last_received_at, excluded.last_received_at)""",
                (broadcast_id, source, received_at, received_at))
            self._relink(connection, mac)
            candidates = connection.execute(
                "SELECT candidate_count FROM broadcast WHERE id = ?", (broadcast_id,)).fetchone()[0]
            summary = {"inserted": inserted, "duplicates": 1 - inserted,
                       "conflicts": int(candidates > 1), **self._associations(connection, mac)}
        return summary

    def totals(self, mac) -> dict:
        """Counts only, safe to expose without body/identity/raw CSV data."""
        mac = _mac(mac)
        with closing(self._connect()) as connection, connection:
            # One read snapshot for counts and conflict/association calculation.
            connection.execute("BEGIN")
            return {
                "native": connection.execute(
                    "SELECT COUNT(*) FROM native WHERE mac = ?", (mac,)).fetchone()[0],
                "broadcast": connection.execute(
                    "SELECT COUNT(*) FROM broadcast WHERE mac = ?", (mac,)).fetchone()[0],
                "conflicts": len(self._conflicted(connection, mac)),
                **self._associations(connection, mac),
            }

    def query_associations(self, mac) -> list[dict]:
        """Return candidate metadata, never verified identity/same-measurement proof.

        association_status is candidate_exact for one exact time/weight match,
        ambiguous for multiple, unmatched for none. native_id is only a candidate
        reference (NULL unless unique); both original records remain stored.
        """
        with closing(self._connect()) as connection:
            return [dict(zip(("id", "native_id", "candidate_count", "association_status"), row))
                    for row in connection.execute("""SELECT id, native_id, candidate_count,
                        CASE WHEN candidate_count = 1 THEN 'candidate_exact'
                             WHEN candidate_count > 1 THEN 'ambiguous'
                             ELSE 'unmatched' END
                        FROM broadcast WHERE mac = ? ORDER BY id""", (_mac(mac),))]

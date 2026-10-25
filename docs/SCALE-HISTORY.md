# Scale history over ESPHome GATT

`xiaomi.scales.ms110` / S400 Pro only. An **explicit HA action** establishes a
short-lived GATT connection through an existing active ESPHome Bluetooth Proxy.
The ESP32 performs radio/GATT IO; the single HA gateway performs Standard Auth,
SPEC decoding, durable saving and merging. No custom board firmware or separate
Windows business program is needed.

## Run a pull

1. Keep the existing ESPHome integration connected with `bluetooth_proxy.active`
   enabled. Windows receivers remain passive and are not connection candidates.
2. Stop using the scale's mobile plugin during the short connection. Wake the
   scale near the ESP32; a sleeping device might not be connectable.
3. In HA Developer tools → Actions, invoke
   `xiaomi_ble_gateway.pull_scale_history` with the gateway entry and scale MAC.
   Replace both synthetic placeholders below:

   ```yaml
   action: xiaomi_ble_gateway.pull_scale_history
   data:
     config_entry_id: YOUR_GATEWAY_CONFIG_ENTRY_ID
     address: "02:00:00:00:00:01"
   response_variable: history_result
   ```

   `response_variable` is for a script action; the REST service endpoint supports
   `?return_response` too. Status/counts also appear in integration diagnostics.

This first version requires exactly one reachable connectable source, which
must be an ESPHome scanner. It rejects ambiguous routing rather than choosing a
local adapter or a passive Windows node. Concurrent pulls are rejected. There
is no background polling or automatic re-pairing.

The provider obtains only the selected scale's existing **12-byte BLE token**
from the loaded Xiaomi Miot account's device metadata. This is not its 16-byte
MiBeacon bindkey and not a cloud service token. It stays in HA RAM; do not place
it in public examples, diagnostics, receiver configuration or firmware.

## What the action sends

- Standard Auth login for an already bound device; device confirmation is
  checked before application confirmation and login success.
- Notification subscriptions and reliable transport READY/SUCCESS ACKs.
- One authenticated SPEC v2 **Action 9.1**, requesting offline data.

It never sends registration, Action 9.2 offline-record confirmation, Action
13.1 configuration synchronization, clock/user changes, or cloud writes.
Transport ACKs are not offline-record confirmation. Whether a particular scale
firmware accepts the request without the app's configuration sync is a live
compatibility question; failure does not authorize that sync as a fallback.
Not sending 9.2 is not a guarantee that the device preserves records forever.

`complete=true` requires a validated action response with record count, exactly
that many unique offline CSV records, durable saves, and an idle receive period.
An authenticated connection alone is not success. Partial results are retained,
but timeout, missing count or inconsistent transport cannot claim a full pull.
The current implementation supports the classic reliable fragmented channel,
not every possible newer MIoT channel variant. Missing fragments, including a
completely missing tail, are requested with a single-sequence transport
`ACK_LOST`; each missing sequence has at most three requests and the overall
collection remains bounded to 75 seconds. Valid out-of-order/repeated fragments
are reassembled before session-MIC verification and record delivery.

## Merge semantics

A separate SQLite history store keeps all 34 native fields, device time,
`miid/duid/userType`, offline indices, and observation sources. Native decimal
scales are decoded exactly; missing fields are not padded and no body metrics
are calculated. Device identity is bound to MAC **and DID**.

- Repeated offline pulls of the exact same full record do not add native rows.
- Online and offline versions with identical 34 common raw fields share a native
  record, preserving offline indices and source observations.
- Reused indices or differing complete records are retained as conflicts, never
  silently overwritten. An offline index is not a permanent unique record ID.
- After the first explicit pull, authenticated scale broadcasts are observed in
  a separate bounded local queue until integration reload. Different receiving
  nodes share the same store; drops/write failures are visible.
- An exact device-time **and exact decimal weight** match to one native record
  creates only a `candidate_exact` association. There is no proven shared ID
  across the two protocols, so it is **not proof of the same physical weighing**.
  Both records remain intact. A later second candidate revokes the unique link.
- Nearby times/weights, broadcast tag, impedance similarity or the nearest-user
  cloud-upload threshold are never sufficient to merge records.

History data never enters the cloud upload queue, changes replay high-water
marks, becomes a nearest-weight reference, or replaces the broadcast entity's
current weight. Existing fresh-broadcast upload permissions remain unchanged.

## Storage and privacy

File: `<HA config>/xiaomi_ble_gateway/scale_history.sqlite3` (directory 0700,
file 0600). Operations use executor IO and committed SQLite transactions.

**This health history has no automatic TTL.** The raw recorder's three-day
retention does not apply, and reload, uninstall or turning off raw recording does
not erase this history. It can include personal identifiers, weight and native
body-composition results. Manage its backups and deletion deliberately; the
integration does not encrypt this database or anonymize exported data.

No successful save implies a cloud upload or permission to clear the scale.

# GATT-first weighing uploads

Enabling scale uploads now enables one **GATT-first decision** for each fresh,
authenticated broadcast weighing. It does not immediately add a UserEditor
weight and later add another full record.

## One flow, two exclusive outcomes

1. Verify the live broadcast, supported device, measurement time, enable epoch
   and existing device-global consumption high-water mark.
2. Aggregate repeated counters/receivers by device MAC and device timestamp.
   The broadcast entity can update immediately, but cloud upload is deferred.
3. A separate preparation worker reads through the active ESPHome proxy and
   listens for 30 seconds after the request. CO2/TH's serial worker does not wait
   for this BLE operation. Complete authenticated native records are committed
   to the local health-history store before becoming candidates.
4. Freeze exactly one outcome:
   - **native:** use the complete native record, without recalculating body
     metrics, fabricating fields, changing its status, or changing its time;
   - **weight_only:** when there is no current native candidate, use the original
     fresh broadcast weight in the existing official UserEditor record shape;
   - **conflict:** ambiguous/incomplete current native data, a newer competing
     native time, or a simultaneous manual GATT operation causes no upload.
5. The same original serial writer consumes the measurement and persists that
   consumption **before** calling either cloud provider. It calls one provider
   with one frozen record. The measurement must still be recent at this point.
6. An unknown/failed native write cannot switch to UserEditor, retry the add, or
   authorize a later second record. Readback is read-only. Reload, profile/mode
   changes and late GATT data do not reset the device-global consumed boundary.

This is **at-most-once upload attempt**, not exactly-once cloud history or a
promise that every weighing will be delivered. A crash after consumption can
lose the upload. Other gateways and the official app are independent writers.

## Choosing native data conservatively

The native timestamp must be after enabling and fresh relative to the initial
live reception. Earlier native timestamps remain local history, not upload
candidates. A current candidate must have exactly the trigger's device timestamp;
this is a conservative gate, **not proof of a shared globally unique record ID**.
Newer competing times, conflicting complete variants or incomplete current data
stop the write instead of guessing a pairing.

The native weight is authoritative when native is selected. No 0.05/0.1kg fuzzy
match or rounding correction is used to claim association with the lower
precision broadcast. Native online/offline copies with identical 34 common raw
fields are deduplicated before selection.

Fixed/nearest profile selection retains its existing policy and threshold. For
native upload, `(miid, duid, userType)` must additionally agree with the selected
profile's `(uid, accountCode, type)`. A mismatch is a conflict, not permission to
attach another person's body data or fall back to a second record.

## Native status and record format

The official offline path retains all device fields for `status=1` (OnlyWeight);
it does not clear nonzero body fields or force `status=0`. This implementation
likewise preserves the source status and values. Sending them is **not** a claim
of medical validity or a promise that every app view displays them identically.
No BMI/body-fat computation or proprietary bcalc algorithm runs here.

Native `data` contains the 34 source fields plus `idx`, `reportFrom` and the
verified profile snapshot. Online source uses `Regular`, offline source uses
`Offline`. The official nine-field v3 outer record has `fromSource=2` and uses
the actual native time. Weight-only fallback uses the trigger's device time too,
not a later HA reception or retry time.

Precheck reads raw records for the exact DID/profile/time. **Any existing record
at that identity/time blocks add**, including an older UserEditor weight-only
record or an online/offline source variant. A mismatch is not a reason to append
another row. Full readback checks all sent native fields, not just weight.

Old stored records are never scheduled merely because they were fetched, restored
or merged. An explicit manual history pull remains a local-only action and does
not create cloud jobs or consume the upload state.

## Storage and operation

Native records returned during automatic preparation are stored in
`xiaomi_ble_gateway/scale_history.sqlite3` under the HA config directory. **This
health history has no automatic TTL**; the raw recorder's three days do not apply.
Enabling scale uploads therefore includes this local native-history collection.
The raw recorder remains independently configured.

Diagnostics expose the chosen record kind, reason, GATT status and queue counts,
not the native payload or BLE token. A GATT busy/conflict result has no immediate
weight-only write while another operation might still be obtaining full data.

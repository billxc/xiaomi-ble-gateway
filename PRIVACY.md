# Privacy and operational data

## What runs where

Receivers forward Windows/ESPHome-visible Bluetooth advertisements over an encrypted API connection. Xiaomi account sessions and bindkeys remain in Home Assistant. Cloud reporting applies only to configured supported devices and explicitly enabled fields.

## Raw recording is broader than cloud reporting

Raw recording currently defaults to on. When enabled, it records **all advertisements delivered to the integration by HA**, not only selected Xiaomi devices. This may include nearby, visitor or third-party devices.

Saved fields can include MAC addresses, names, RSSI, receiver source, timestamps, UUIDs, and service/manufacturer bytes. These can reveal device identities or routines even if an encrypted payload is not decoded. Arbitrary platform objects, account tokens and bindkeys are not intentionally recorded.

The default retention is three days. TTL cleanup runs while recording is active. Multiple enabled recording entries use the longest requested retention. Turning recording off does not erase the database; exports, filesystem snapshots and backups have their own lifetimes. There is no hard storage cap and the database is not encrypted by this integration.

Disable recording if this collection is not appropriate. Manage consent and retention according to your environment and applicable rules.

## Explicit scale-history pulls

The `pull_scale_history` action saves complete native scale records in a separate
`scale_history.sqlite3` file. This can include profile identifiers, weight and
native body-composition results. **There is no automatic TTL** for this health
history; raw-recorder retention does not apply. Reload/removal does not delete
it. After a pull, local authenticated broadcast observations are also recorded
until reload for candidate association. Neither historical records nor merge
updates authorize cloud writes or scale-history clearing. Enabling scale uploads
also enables GATT-first native collection for fresh broadcast-triggered weighings,
using this same private health-history store with no automatic TTL. Only a
separately validated current candidate can be uploaded through the single
consumption gate; saving history alone is not authorization. See
[Scale history](docs/SCALE-HISTORY.md) and [upload policy](docs/GATT-FIRST-UPLOAD.md).

## HA entities and diagnostics

HA's own Recorder may store entities and their attributes independently of this integration's raw database. Scale-match summaries can include profile names/IDs and reference weights. RAM-only integration metadata can therefore still appear in HA history or exported diagnostics.

Do not post unredacted diagnostics, screenshots, recordings or exports in public issues. Offline export/replay tools are not anonymizers.

## Files to keep private

- Receiver `config.json` (Noise PSK), ESPHome `secrets.yaml`, `.env` files.
- Xiaomi/HA login stores, tokens, bindkeys and account exports.
- SQLite databases, JSONL captures, replay output and cloud-history responses.
- Receiver `status.json`, logs, and PM2 dumps/backups (which may include environment secrets).

The repository's ignore rules are a safeguard, not a substitute for reviewing staged files and Git history before publication.

## Upload behavior

Unknown cloud-write outcomes are not retried automatically. Old captures, restored sensor values and discovery-cache replay are not an authorized source of new uploads. Do not reset the deduplication Store or rewrite old timestamps merely to make a failed sample upload.

Other gateways and mobile apps are independent writers. This integration cannot guarantee exactly-once history across them.

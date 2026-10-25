# Xiaomi BLE Gateway

A community Home Assistant integration with optional Windows BLE receivers. Multiple receivers feed **one Home Assistant processing layer** for MiBeacon decoding, persistent upload deduplication, entities, and optional Xiaomi Cloud reporting.

This is an unofficial project, not affiliated with Xiaomi or ESPHome. It is an ESPHome Native API **advertisement-compatible implementation**, not ESPHome firmware for Windows.

> **Privacy before installation:** raw recording currently defaults to **on**. It records all Bluetooth advertisements delivered by Home Assistant, including nearby devices not selected for cloud upload. Review the recording option and [privacy notes](PRIVACY.md) before adding the integration. All cloud-upload switches default to **off**.

## Architecture

```text
ESPHome Bluetooth Proxy ─┐
Windows WinRT receiver ──┼─ HA ESPHome / Bluetooth layer
Other HA BLE sources ───┘             │
                         one xiaomi_ble_gateway runtime
                          ├─ authenticated decoding and entities
                          ├─ persistent per-device replay gates
                          ├─ explicitly enabled cloud reporting
                          └─ shared raw recorder, independently configured
```

Windows receivers do not contain Xiaomi credentials or MiBeacon bindkeys, identify scale users, or upload measurements. They expose Noise-encrypted ESPHome Native API RAW advertisements, consumed by HA's native `ESPHomeScanner`.

## Supported devices

| Model | Local entities | Optional cloud functionality |
|---|---|---|
| `xiaomi.sensor_ht.new3pr`, PID 39609 | CO₂, temperature, humidity, battery | CO₂ and temperature/humidity have separate switches; battery is local only |
| `xiaomi.scales.ms110`, PID 19205 | Broadcast weight; explicit local GATT history pull through ESPHome | Weight-only history for a fixed profile or the unique nearest-weight profile |

Support is limited to these models. This is not a general Xiaomi Cloud uploader.

### Environmental reporting

Only fields in the current authenticated broadcast are eligible. Temperature/humidity require live HA reception after enabling; restored values and discovery-cache replay cannot initiate their upload. Temperature uses `7.1001`, humidity `7.1002`, and CO₂ `8.1029`.

Selected fields from one frame are reported in one request. Every expected property must have a matching successful acknowledgement; incomplete, duplicate or partially rejected acknowledgements are not presented as full success. Last-batch and per-field status are available in diagnostics.

### Scale reporting is experimental

Weight interpretation is community-derived and is not a complete official body-composition report. The weight-only cloud record follows the official `UserEditor` record shape; placeholder body-composition fields are **not measured zero body fat**. No local BMI/body-fat algorithm or native full-health cloud uploader is implemented here. Native GATT history can be saved locally through an active ESPHome proxy; see [Scale history](docs/SCALE-HISTORY.md).

Nearest matching compares all profiles associated with that scale against their available weight references. Only a unique closest match within the configured threshold (default **2.5 kg**, inclusive) is uploaded. An exact tie, no reference, or all candidates outside the threshold produces no write. Editable cloud recognition baselines are distinguished from historical references. This is a heuristic, **not identity authentication**; people with similar weights may be misassigned. Fixed-profile mode is also available.

The first live scale upload must satisfy the device-time/reception-time gates. Do not change old timestamps or replay recordings to force an upload.

## Install the HA integration

Tested with Home Assistant 2026.9.x. The cloud provider currently reuses the live `xiaomiio` login from **Xiaomi Miot Auto** (`xiaomi_miot`), so install and configure that integration first. `xiaomi_home` can supply matching account metadata, but its OAuth session alone is not a supported replacement for these legacy cloud APIs.

This integration does not copy account passwords or create another Xiaomi login. It does depend on third-party integration runtime internals and undocumented cloud endpoints; upstream changes can break compatibility.

1. Copy `custom_components/xiaomi_ble_gateway` into your HA configuration's `custom_components` directory.
2. Restart Home Assistant, then add **Xiaomi BLE Cloud Gateway** in Devices & services.
3. Select the source account and supported devices. Review the raw-recording option immediately.
4. Enable only the desired cloud features in the integration options. These are independent permissions; no upload is enabled merely by selecting a device.
5. Add receivers through HA's ESPHome integration. Do **not** create another gateway processing entry per receiver.

The included `hacs.json` supports using this repository as a custom integration repository. This does not mean it is listed in the HACS default store.

## Receivers

- ESP32: use a standard ESPHome Bluetooth Proxy. A generic, authenticated-OTA example is in [`examples/esphome`](examples/esphome/). It is not a migration recipe for arbitrary legacy bootloaders or partition layouts.
- Windows: see [`receivers/windows`](receivers/windows/README.md). Python 3.12+, WinRT, one dedicated environment, and PM2 are used. Only passive advertisements are supported; no GATT or OTA is advertised. Each node needs an independent Noise key. Restrict its API port to the HA host.

## Pull scale history through ESP32

The `xiaomi_ble_gateway.pull_scale_history` HA action uses the ESP32's existing
active GATT proxy, authenticates an already bound scale, and commits complete
native records to a separate local SQLite history store. Repeated pulls and
identical online/offline records are merged without overwriting conflicting
records. Exact broadcast matches are only **candidate associations**, not proof
of the same weighing. No historical cloud upload or offline-record confirmation
is performed. This history has **no automatic TTL**; raw-recording retention does
not apply. See [usage, limitations and privacy](docs/SCALE-HISTORY.md).

## Deduplication and failure semantics

All receivers share the same gateway device state, keyed by the measured device MAC rather than receiver source. A complete authenticated frame is consumed once. Scale measurements additionally use a device-global measurement-time high-water mark across profile/mode changes.

The attempt is persisted **before** a cloud write. Unknown outcomes are not automatically retried, including after restart. This is at-most-once attempt behavior, **not guaranteed delivery or exactly-once cloud history**. A crash after persistence can lose a sample. Deleting/restoring the Store or recreating the entry can invalidate replay protection.

The original Xiaomi gateway or mobile app may independently report the same physical sample. This project does not claim cross-system deduplication against those writers.

## Recording and offline tools

The independent recorder saves HA-delivered advertisement metadata and full service/manufacturer data to SQLite, without a device/model/UUID/decryption whitelist. Default retention is three days, configurable to a positive integer. There is no hard disk-size cap. Queue drops and write errors are visible.

This is **not RF/IQ capture**, not GATT recording, and cannot recover packets filtered upstream. Receivers discard unsent data on disconnect rather than replaying old advertisements as current events.

- `tools/export_recordings.py`: read-only export of all retained records to a new private JSONL file.
- `tools/replay_recordings.py`: offline per-device decryption with an explicitly provided key file; no cloud upload.

Exports and diagnostic attachments are not automatically anonymized. Turning recording off does not erase an existing database or exports. See [PRIVACY.md](PRIVACY.md).

## Tests

Tests use synthetic identities, keys and advertisements. The public test suite does not read private capture files or local device credentials.

```sh
python -m venv .venv
# Activate the environment for your platform.
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -q
python -m unittest discover -s receivers/windows/tests -q
```

Windows protocol tests run on Linux loopback as well; actual Windows hardware and HA deployment are separate acceptance steps. No real cloud writes are performed by the test suite.

## Licensing and third-party material

The project license is in `LICENSE`. The generated ESPHome protobuf definitions under `receivers/windows/protocol/` retain their separate upstream MIT notice and provenance. They are not original schema authored by this project.

No production configuration, firmware binary, recordings, cloud history, account export, decompiled vendor plugin or private investigation artifact is included in this repository. Protocol interoperability does not imply endorsement, server-side API stability, or official measurement equivalence.

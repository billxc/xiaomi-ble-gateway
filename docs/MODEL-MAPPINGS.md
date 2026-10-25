# Supported environmental model mappings

Only explicitly selected, same-account MAC/DID/model-verified bound devices are
eligible. All decoded measurements require MiBeacon V5 AES-CCM MIC verification;
unknown EIDs are ignored and known EIDs with incorrect lengths are rejected.
Counters remain the complete 32-bit MiBeacon counter, without reset/retry logic.

| Model | PID | Temperature cloud property | Humidity cloud property | CO₂ cloud property |
|---|---|---|---|---|
| `xiaomi.sensor_ht.new3pr` | 39609 | 7.1001 | 7.1002 (integer) | 8.1029 |
| `miaomiaoce.sensor_ht.t2` / LYWSD03MMC | 1371 / `0x055B` | 2.1 (float) | 2.2 (float) | None |

T2 official MIoT spec:
`urn:miot-spec-v2:device:temperature-humidity-sensor:0000A00A:miaomiaoce-t2:1`.
Temperature is −30…100 °C with step 0.1; humidity is 0…100 % with step 0.1.
Cloud values are rounded to three decimals only to remove floating-point tails,
not truncated to integer humidity. Battery 3.1 is local only, not a cloud field.

T2 decoding is restricted to PID 1371 and these standard legacy EIDs:

| EID | Exact value layout (little endian) | Local field |
|---|---|---|
| `0x1004` | int16 / 10 | Temperature °C |
| `0x1006` | uint16 / 10 | Humidity % |
| `0x100D` | int16 / 10, uint16 / 10 | Temperature °C, humidity % |
| `0x100A` | uint8 | Battery % |

The mapping follows the standard and cached official spec. Maintainer verification
confirmed float properties 2.1/2.2 in the cloud cache and PID 1371 with frame control
`0x5858` on a real device. Private evidence is not included; public tests use only
synthetic fixtures. This does not claim a live cloud-write acceptance test.
No other thermometer models or PIDs are inferred from it.

Temperature/humidity upload is globally opt-in (default off), requires a new
whole authenticated frame received live after runtime startup and the existing
enable epoch, and never fills missing fields from old state. The first frame
establishes the baseline. Consumption is persisted before the single batch write;
every property must be acknowledged, and unknown outcomes are never retried.

# Third-party material and protocol references

## Vendored generated ESPHome protobuf definitions

`receivers/windows/protocol/api_pb2.py` and `api_options_pb2.py` are unmodified generated files from the `aioesphomeapi` 46.22.0 distribution. Their upstream MIT copyright and permission notice is retained in `receivers/windows/protocol/LICENSE`; file hashes and provenance are recorded in `SOURCE.json`.

The schema is upstream material, not original schema authored by this project. The receiver's Python server is a limited interoperability implementation informed by the public ESPHome Native API framing, handshake and message behavior. It is not ESPHome firmware or a redistribution of the full client SDK.

Upstream: https://github.com/esphome/aioesphomeapi
Distribution: https://pypi.org/project/aioesphomeapi/46.22.0/

## Protocol and service mappings

MiBeacon layouts and model-specific interpretation use publicly documented interfaces and community protocol research. Environmental property mappings are available through the Xiaomi MIoT specification service. Model names, UUIDs, field numbers and wire layouts describe interoperability, not endorsement.

The scale's weight-only record builder reproduces the structure of a manual weight record, not a proprietary body-composition algorithm. No decompiled plugin, manufacturer firmware, authenticated download URL, account artifact or private capture is distributed here.

## Runtime dependencies

Dependencies listed in requirements files are installed separately and retain their own licenses. This project does not relicense those dependencies. In particular, keep the vendored protocol's notice when redistributing the receiver, regardless of the license selected for original project code.

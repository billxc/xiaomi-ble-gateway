"""Decode saved advertisements offline. Never imports upload providers or calls HA/cloud."""
import argparse
from dataclasses import asdict
import importlib.util
import json
import os
from pathlib import Path
import sys


def replay(input_file, output_file, address, key_file):
    module_path = Path(__file__).resolve().parents[1] / 'custom_components/xiaomi_ble_gateway/protocol.py'
    spec = importlib.util.spec_from_file_location('offline_xiaomi_ble_protocol', module_path)
    protocol = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = protocol
    spec.loader.exec_module(protocol)
    key = bytes.fromhex(Path(key_file).read_text().strip())
    address = address.replace(':', '').upper()
    fd = os.open(output_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with Path(input_file).open(encoding='utf-8') as source, os.fdopen(fd, 'w', encoding='utf-8') as output:
        for line in source:
            row = json.loads(line)
            if row['address'].replace(':', '').upper() != address:
                continue
            payload = row['service_data'].get('0000fe95-0000-1000-8000-00805f9b34fb')
            if payload is None:
                continue
            result = {k: row.get(k) for k in ('received_at', 'address', 'source', 'rssi')}
            try:
                frame = protocol.decode_advertisement(bytes.fromhex(payload), address, key)
                result['decoded'] = asdict(frame) if frame is not None else None
            except ValueError as error:
                result['decode_error'] = str(error)
            output.write(json.dumps(result, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--address', required=True, help='Device to decode; does not affect recording scope')
    parser.add_argument('--key-file', required=True, help='Private hex bindkey file; never written to output')
    args = parser.parse_args()
    replay(args.input, args.output, args.address, args.key_file)

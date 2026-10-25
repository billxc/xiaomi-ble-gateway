"""Export ALL retained Bluetooth callbacks as JSONL; no HA or cloud calls."""
import argparse
import os
from pathlib import Path
import sqlite3


def export(database, output):
    db = sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)
    db.execute('PRAGMA query_only=ON')
    try:
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as destination:
            for row in db.execute('SELECT payload_json FROM advertisements ORDER BY received_at,rowid'):
                destination.write(row[0] + '\n')
    finally:
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    export(args.database, args.output)

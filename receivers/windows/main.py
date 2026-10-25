"""Windows passive BLE receiver exposing an encrypted ESPHome Native API."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import sys
import threading
import time

from native_api import NativeApiServer
from winrt_capture import WindowsScanner

VERSION = "0.1.0"
_LOGGER = logging.getLogger("windows_ble_proxy")


def write_status(path, status):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


async def run(config, directory):
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    stats = {"bridge_version": VERSION, "bridge_dropped": 0}
    slots = threading.BoundedSemaphore(4096)
    server = None

    def forward(address, rssi, data, address_type):
        try:
            if server is not None:
                server.publish(address, rssi, data, address_type)
        finally:
            slots.release()

    def received(address, rssi, data, address_type):
        # Bound the thread -> event loop backlog as well as the API send queue.
        if not slots.acquire(blocking=False):
            stats["bridge_dropped"] += 1
            return
        loop.call_soon_threadsafe(forward, address, rssi, data, address_type)

    scanner = WindowsScanner(received, stats)
    started = time.monotonic()
    status = {"name": config["name"], "pid": os.getpid(),
              "started_at": datetime.now(timezone.utc).isoformat(),
              "port": config.get("port", 16053), "state": "starting"}
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
    try:
        scanner.start()
        mac = ":".join(f"{scanner.adapter_address:012X}"[i:i + 2] for i in range(0, 12, 2))
        status.update(adapter_address=mac, adapter_name=scanner.adapter_name)
        server = NativeApiServer(
            config["name"], mac, config["noise_psk"],
            friendly_name=config.get("friendly_name", config["name"]), stats=stats,
            host=config.get("host", "0.0.0.0"), port=config.get("port", 16053),
        )
        await server.start()
        status["state"] = "running"
        _LOGGER.info("Receiver started: %s, adapter %s, TCP %s, passive, Noise encrypted",
                     config["name"], mac, status["port"])
        while not stop.is_set():
            if not scanner.running:
                raise RuntimeError("BLE watcher stopped")
            status.update(updated_at=datetime.now(timezone.utc).isoformat(),
                          uptime_seconds=round(time.monotonic() - started), stats=dict(stats))
            write_status(directory / "status.json", status)
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                pass
    except Exception as err:
        status.update(state="failed", error_type=type(err).__name__)
        _LOGGER.exception("Receiver failed")
        raise
    finally:
        scanner.stop()
        if server is not None:
            await server.stop()
        if status["state"] != "failed":
            status["state"] = "stopped"
        status.update(updated_at=datetime.now(timezone.utc).isoformat(), stats=dict(stats))
        write_status(directory / "status.json", status)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    directory = args.config.resolve().parent
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    handler = RotatingFileHandler(directory / "receiver.log", maxBytes=1_000_000,
                                  backupCount=3, encoding="utf-8")
    handlers = [handler]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    try:
        asyncio.run(run(config, directory))
    except KeyboardInterrupt:
        return 0
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

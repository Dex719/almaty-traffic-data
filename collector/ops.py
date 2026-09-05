"""Health checks, systemd watchdog notification, process lock and verified backups."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import socket
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from collector.journal import Journal
from collector.store import utc_stamp


@contextmanager
def exclusive_collector(data_dir: Path):
    path = data_dir/".state/collector.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another collector owns this data directory") from exc
        yield


def notify_systemd(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0"+address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1)
            sock.sendto(message.encode(), address)
    except OSError:
        pass


def health_report(states: dict, now=None, data_dir=None) -> dict:
    now = now or datetime.now(timezone.utc)
    reports = {}
    for name, state in states.items():
        current = dict(state)
        last = state.get("last_success")
        age = (now-datetime.fromisoformat(last)).total_seconds() if last else None
        current["age_seconds"] = age
        current["healthy"] = (age is not None and 0 <= age <= state["interval"]*3
                              and state["status"] == "ok")
        reports[name] = current
    storage = {"healthy": True}
    if data_dir is not None:
        storage["free_bytes"] = shutil.disk_usage(data_dir).free
        storage["healthy"] = storage["free_bytes"] >= 100*1024*1024
        if os.environ.get("TRAFFIC_REQUIRE_BACKUP") == "1":
            try:
                backup = json.loads((Path(data_dir)/".state/backup.json").read_text())
                age = (now-datetime.fromisoformat(backup["backed_up_at"])).total_seconds()
                storage["backup_age_seconds"] = age
                storage["healthy"] = storage["healthy"] and 0 <= age <= 36*3600
            except (OSError, ValueError, KeyError):
                storage["healthy"] = False
                storage["backup_error"] = "missing_or_invalid_backup_status"
    return {"schema_version": 2, "checked_at": utc_stamp(now),
            "healthy": bool(reports) and storage["healthy"] and all(s["healthy"] for s in reports.values()),
            "sources": reports, "storage": storage}


def ping_heartbeat() -> bool:
    """Send no observation data. Never log the secret-bearing URL."""
    url = os.environ.get("TRAFFIC_HEARTBEAT_URL")
    if not url:
        return False
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    try:
        import httpx
        response = httpx.get(url, timeout=5, follow_redirects=False)
        return 200 <= response.status_code < 300
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "export", "backup"])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if args.command == "check":
        try:
            report = json.loads((args.data_dir/".state/health.json").read_text())
            age = (datetime.now(timezone.utc)-datetime.fromisoformat(report["checked_at"])).total_seconds()
            print(json.dumps(report, ensure_ascii=False))
            return 0 if report["healthy"] and 0 <= age <= 180 else 1
        except (OSError, ValueError, KeyError):
            return 1
    if args.command == "backup" and args.destination is None:
        parser.error("backup requires --destination (prefer an off-host mounted directory)")
    if not (args.data_dir/".state/journal.sqlite3").exists():
        parser.error("no existing journal in --data-dir")
    journal = Journal(args.data_dir)
    try:
        if args.command == "export":
            print(journal.export_pending())
        else:
            print(journal.backup(args.destination))
    finally:
        journal.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

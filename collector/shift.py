"""Independent source schedules, durable observations, graceful shutdown.

Git publication is opt-in (--git), intended only for the best-effort Actions
fallback. A persistent host uses --forever and a separate backup timer.
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import random
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from collector import sources, store
from collector.journal import Journal
from collector.ops import exclusive_collector, health_report, notify_systemd, ping_heartbeat

logger = logging.getLogger("collector.shift")
REPO_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_DIR/"data"
DGIS_EVERY_MIN, YANDEX_EVERY_MIN, EVENTS_EVERY_MIN = 1, 4, 5
JAMMAP_EVERY_MIN, COMMIT_EVERY_MIN = 5, 15
FAILS_TO_COOLDOWN, COOLDOWN_MIN = 3, 10
GIT_TIMEOUT_SEC, PUSH_STALL_MIN = 20, 45
INTERVALS = {"dgis": 60, "yandex": 240, "events_user": 300, "events_2gis": 300, "jammap": 300}


class SourceGuard:
    def __init__(self, name):
        self.name, self.fails, self.cooldown_until = name, 0, 0.0

    def ready(self, now):
        return now >= self.cooldown_until

    def ok(self):
        self.fails = 0

    def fail(self, now, error=None):
        self.fails += 1
        response = getattr(error, "response", None)
        wait = 0.0
        if response is not None:
            retry = response.headers.get("Retry-After")
            if retry:
                try:
                    wait = float(retry)
                except ValueError:
                    try:
                        wait = (parsedate_to_datetime(retry)-datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        pass
            if response.status_code in (401, 403):
                wait = max(wait, 3600)
        if not math.isfinite(wait):
            wait = 0
        if self.fails >= FAILS_TO_COOLDOWN:
            wait = max(wait, COOLDOWN_MIN*60)
            self.fails = 0
        self.cooldown_until = max(self.cooldown_until, now+max(wait, 0))


def _git(*args, timeout=None):
    try:
        return subprocess.run(["git", *args], cwd=REPO_DIR, capture_output=True,
                              text=True, timeout=timeout or GIT_TIMEOUT_SEC,
                              env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["git", *args], 124, "", "git timeout")


def commit_and_push() -> bool:
    """Never confuse an empty index with a synchronized remote."""
    deadline = time.monotonic()+120
    def run(*args):
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            return subprocess.CompletedProcess(["git", *args], 124, "", "budget exceeded")
        return _git(*args, timeout=min(GIT_TIMEOUT_SEC, remaining))

    if run("add", "--", "data").returncode:
        return False
    diff = run("diff", "--cached", "--quiet")
    if diff.returncode not in (0, 1):
        return False
    if diff.returncode == 1:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if run("commit", "-m", f"data: traffic observations {stamp}").returncode:
            return False
    if run("rev-parse", "--abbrev-ref", "@{u}").returncode:
        logger.error("Git publication requires an upstream tracking branch")
        return False
    for attempt in range(3):
        if run("pull", "--rebase").returncode:
            run("rebase", "--abort")
        elif run("push").returncode == 0:
            ahead = run("rev-list", "--count", "@{u}..HEAD")
            return ahead.returncode == 0 and ahead.stdout.strip() == "0"
        if time.monotonic() >= deadline:
            break
        time.sleep(min(2**attempt, max(0, deadline-time.monotonic())))
    logger.error("Data publication failed; local observations have not been discarded")
    return False


def advance_due(previous: float, now: float, interval: float) -> float:
    """Skip missed slots rather than issuing a catch-up request burst."""
    return previous+(max(0, math.floor((now-previous)/interval))+1)*interval


def _poll(function):
    result = function()
    return datetime.now(timezone.utc), result


def run_shift(minutes: int | None, data_dir: Path = DATA_DIR, *,
              git_enabled=False, stop_event=None, once=False) -> int:
    from collector import jammap
    stop = stop_event or threading.Event()
    journal = Journal(data_dir)
    guards = {name: SourceGuard(name) for name in INTERVALS}
    states = {name: {"interval": interval, "status": "starting"}
              for name, interval in INTERVALS.items()}
    map_enabled = (data_dir/"jam_map/ways.json").exists()
    if not map_enabled:
        states.pop("jammap")
    started = time.monotonic()
    deadline = float("inf") if minutes is None else started+minutes*60
    due = {name: started for name in states}
    functions = {"yandex": sources.fetch_yandex_score, "dgis": sources.fetch_dgis_score,
                 "events_user": lambda: sources.fetch_dgis_layer("user"),
                 "events_2gis": lambda: sources.fetch_dgis_layer("2gis")}
    pool, map_pool = ThreadPoolExecutor(max_workers=4), ThreadPoolExecutor(max_workers=1)
    map_future = None
    successes, fatal, publication_ok = 0, False, True
    next_export, next_health = started+COMMIT_EVERY_MIN*60, started
    last_push_ok = started

    def record(name, observed, payload=None, error=None):
        nonlocal successes
        source_ts = payload.get("ts") if isinstance(payload, dict) else None
        status = "error" if error is not None else "ok"
        invalid_rows = getattr(payload, "invalid_rows", 0)
        if invalid_rows and error is None:
            status = "partial"
        if name == "jammap" and error is None:
            status = payload["quality"]
        if name in ("yandex", "dgis") and error is None and source_ts is None:
            status = "missing_timestamp"
        if source_ts is not None:
            try:
                numeric_ts = float(source_ts)
                if not math.isfinite(numeric_ts):
                    raise ValueError("non-finite source timestamp")
                age = observed.timestamp()-numeric_ts
                if age > INTERVALS[name]*3 or age < -60:
                    status = "stale"
            except (ValueError, TypeError, OverflowError):
                status = "invalid_timestamp"
        document_payload = {"events": list(payload), "invalid_rows": invalid_rows} if name.startswith("events_") and payload is not None else payload
        journal.record(name, observed, document_payload, status=status, source_ts=source_ts,
                       error=type(error).__name__ if error else None)
        states[name].update(status=status, last_attempt=store.utc_stamp(observed), source_ts=source_ts)
        if error is not None:
            guards[name].fail(time.monotonic(), error)
            states[name]["error_code"] = type(error).__name__
        else:
            guards[name].ok()
            states[name].pop("error_code", None)
            states[name]["last_success"] = store.utc_stamp(observed)
            if status in ("ok", "partial"):
                successes += 1
        return status

    def save_map(future):
        try:
            frame = future.result()
        except Exception as exc:
            record("jammap", datetime.now(timezone.utc), error=exc)
            return
        record("jammap", datetime.now(timezone.utc), frame)
        jammap.append_frame(data_dir, frame)

    notify_systemd("READY=1")
    try:
        while time.monotonic() < deadline and not stop.is_set():
            now = time.monotonic()
            if map_future is not None and map_future.done():
                save_map(map_future)
                map_future = None
            ready = []
            for name in due:
                if now < due[name]:
                    continue
                due[name] = advance_due(due[name], now, INTERVALS[name])
                if not guards[name].ready(now):
                    states[name]["status"] = "cooldown"
                    continue
                if name == "jammap":
                    if map_future is None:
                        map_future = map_pool.submit(jammap.capture, data_dir, datetime.now(timezone.utc))
                else:
                    ready.append(name)
            futures = {name: pool.submit(_poll, functions[name]) for name in ready}
            results, observed_times = {}, []
            for name, future in futures.items():
                try:
                    observed, payload = future.result()
                except Exception as exc:
                    record(name, datetime.now(timezone.utc), error=exc)
                    continue
                record(name, observed, payload)
                results[name] = payload
                observed_times.append(observed)
            if results:
                observed = max(observed_times)
                event_names = [name for name in ("events_user", "events_2gis") if name in results]
                events = [e for name in event_names for e in results[name]]
                complete = len(event_names) == 2 and all(getattr(results[n], "invalid_rows", 0) == 0 for n in event_names)
                store.append_score_row(data_dir, observed, results.get("yandex"), results.get("dgis"),
                                       events if complete else None, monthly=True)
                if event_names:
                    store.update_event_registry(data_dir, observed, events, monthly=True)
                    store.append_snapshot(data_dir, observed, events, complete=complete,
                                          layers=[name.removeprefix("events_") for name in event_names])
            now = time.monotonic()
            if now >= next_export:
                journal.export_pending()
                if git_enabled:
                    publication_ok = commit_and_push()
                    if publication_ok:
                        last_push_ok = time.monotonic()
                    elif time.monotonic()-last_push_ok >= PUSH_STALL_MIN*60:
                        fatal = True
                        break
                next_export = advance_due(next_export, now, COMMIT_EVERY_MIN*60)
            if now >= next_health:
                report = health_report(states, data_dir=data_dir)
                store.atomic_json(data_dir/".state/health.json", report)
                if report["healthy"]:
                    ping_heartbeat()
                next_health = now+60
            notify_systemd("WATCHDOG=1")
            if once:
                break
            wait = max(0.05, min(1.0, min(due.values())-time.monotonic(), deadline-time.monotonic()))
            stop.wait(wait)
    except Exception:
        logger.exception("Collector stopped; durable journal will be retained")
        fatal = True
    finally:
        # No daemon writer can outlive this final export/publication.
        pool.shutdown(wait=True, cancel_futures=True)
        map_pool.shutdown(wait=True, cancel_futures=True)
        try:
            if map_future is not None and not map_future.cancelled():
                save_map(map_future)
            journal.export_pending()
            if git_enabled:
                publication_ok = commit_and_push()
            store.atomic_json(data_dir/".state/health.json", health_report(states, data_dir=data_dir))
        except Exception:
            logger.exception("Final persistence/publication failed")
            fatal = True
        finally:
            journal.close()
            sources.close_client()
            notify_systemd("STOPPING=1")
    return 1 if fatal or not publication_ok or successes == 0 else 0


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--minutes", type=int, default=340)
    mode.add_argument("--forever", action="store_true")
    mode.add_argument("--once", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--git", action="store_true", help="Explicitly enable legacy Git publication")
    args = parser.parse_args()
    if not args.forever and args.minutes <= 0:
        parser.error("--minutes must be positive")
    stop = threading.Event()
    def request_stop(signum, frame):
        stop.set()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    with exclusive_collector(args.data_dir):
        return run_shift(None if args.forever else args.minutes, args.data_dir,
                         git_enabled=args.git, stop_event=stop, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())

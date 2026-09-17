"""GitHub Releases publication for archive streams.

Git keeps only compact live views (scores, event registry, geometry registries).
The heavy archive streams (``observations``, ``snapshots``, ``jam_map/v2``)
travel every export cycle as immutable *segments* to the ``staging``
prerelease and, once a day is closed, are consolidated into
``traffic-YYYY-MM-DD.tar.xz`` inside the monthly ``data-YYYY-MM`` release.

Nothing here runs unless the collector was started with ``--git``.
Spec: ``.kiro/specs/release-archive-publishing``.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import logging
import os
import re
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from collector.store import atomic_json, utc_stamp

logger = logging.getLogger(__name__)

ARCHIVE_STREAMS = ("observations", "snapshots", "jam_map/v2")
ARCHIVE_PATHS = tuple(f"data/{stream}" for stream in ARCHIVE_STREAMS)
LIVE_PATHS = ("data/scores", "data/events", "data/jam_map/ways.json", "data/jam_map/registries")
STAGING_TAG = "staging"
CLOSE_MARGIN = timedelta(minutes=45)
SEGMENT_BYTES_CAP = 64 * 2**20
GH_TIMEOUT_SEC = 120
VERIFY_ATTEMPTS, VERIFY_PAUSE_SEC = 5, 2.0
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SEGMENT_RE = re.compile(r"^seg-(\d{8}T\d{6}Z)-([A-Za-z0-9_]+)-(\d{4})-((?:\d{4}-\d{2}-\d{2})(?:\+\d{4}-\d{2}-\d{2})*)\.tar\.xz$")
_SECRET_RE = re.compile(r"(gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|x-access-token:[^@\s]+@)")

STAGING_NOTES = ("Internal 15-minute segments of the archive streams. The collector consolidates "
                 "them into monthly data releases and deletes them; do not rely on these assets.")
MONTH_NOTES = """Daily archives of the Almaty traffic archive streams.

Each `traffic-YYYY-MM-DD.tar.xz` contains:

- `observations/<day>/<sha256>.jsonl` - canonical journal batches, uncompressed; the file name is the SHA-256 of the content (UTC date of the first row)
- `snapshots/<day>.jsonl` - event snapshots (Almaty date)
- `jam_map/v2/<day>.csv` - categorical jam classes per road geometry (Almaty date)
- `MANIFEST.json` - SHA-256 and size of every member, source segments, dropped duplicates, detected gaps

Verify a download against the `digest` field of the GitHub release-assets API.
Live views (scores, event registry, geometry registries) stay in the git repository.
"""


# --- helpers ---------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scrub(text: str) -> str:
    """Never let a token reach logs or exception messages."""
    return _SECRET_RE.sub("***", text or "")


def default_run_id() -> str:
    raw = os.environ.get("GITHUB_RUN_ID") or f"local-{socket.gethostname()}-{os.getpid()}"
    return re.sub(r"[^A-Za-z0-9_]", "_", raw)


def day_of(rel: str) -> str | None:
    """Calendar day encoded in an archive-stream path relative to ``data/``."""
    parts = rel.split("/")
    if parts[0] == "observations" and len(parts) == 3 and DAY_RE.match(parts[1]) \
            and parts[2].endswith(".jsonl.gz") and not parts[2].startswith("."):
        return parts[1]
    if parts[0] == "snapshots" and len(parts) == 3 and parts[2].endswith(".jsonl"):
        day = f"{parts[1]}-{parts[2][:-6]}"
        return day if DAY_RE.match(day) else None
    if parts[0] == "jam_map" and len(parts) == 3 and parts[1] == "v2" and parts[2].endswith(".csv"):
        day = parts[2][:-4]
        return day if DAY_RE.match(day) else None
    return None


def is_batch(rel: str) -> bool:
    return rel.startswith("observations/")


def archive_member(rel: str) -> str:
    """Member name inside the daily archive."""
    if is_batch(rel):
        return rel[:-len(".gz")]
    if rel.startswith("snapshots/"):
        return f"snapshots/{day_of(rel)}.jsonl"
    return rel


def day_closed(day: str, now_utc: datetime) -> bool:
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return now_utc >= start + timedelta(days=1) + CLOSE_MARGIN


def month_tag(day: str) -> str:
    return f"data-{day[:7]}"


def day_asset(day: str) -> str:
    return f"traffic-{day}.tar.xz"


def segment_name(now_utc: datetime, run_id: str, seq: int, days: list[str]) -> str:
    stamp = now_utc.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"seg-{stamp}-{run_id}-{seq:04d}-{'+'.join(days)}.tar.xz"


@dataclass(frozen=True)
class SegmentInfo:
    name: str
    stamp: str
    run_id: str
    seq: int
    days: tuple[str, ...]


def parse_segment_name(name: str) -> SegmentInfo | None:
    match = SEGMENT_RE.match(name)
    if not match:
        return None
    stamp, run_id, seq, days = match.groups()
    return SegmentInfo(name, stamp, run_id, int(seq), tuple(days.split("+")))


def scan_archive_files(data_dir: Path) -> dict[str, int]:
    """Recognised archive-stream files on disk: relative path -> size."""
    found: dict[str, int] = {}
    for stream in ARCHIVE_STREAMS:
        base = Path(data_dir) / stream
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.name.startswith(".tmp-"):
                continue
            rel = path.relative_to(data_dir).as_posix()
            if day_of(rel) is not None:
                found[rel] = path.stat().st_size
    return found


# --- release client ---------------------------------------------------------

@dataclass(frozen=True)
class AssetInfo:
    name: str
    size: int
    digest: str | None
    state: str
    id: int | None = None


class ReleaseClient(Protocol):
    def ensure_release(self, tag: str, title: str, notes: str, prerelease: bool = False) -> None: ...
    def list_assets(self, tag: str) -> dict[str, AssetInfo]: ...
    def upload(self, tag: str, path: Path) -> None: ...
    def download(self, tag: str, name: str, dest_dir: Path) -> Path: ...
    def delete_asset(self, tag: str, name: str) -> None: ...


class GhError(RuntimeError):
    pass


class GhReleases:
    """Thin wrapper over the ``gh`` CLI. Token comes only from GH_TOKEN."""

    def __init__(self, repo: str | None = None, timeout: float = GH_TIMEOUT_SEC):
        self.repo = repo or os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPOSITORY")
        self.timeout = timeout

    def _run(self, *args: str) -> str:
        env = dict(os.environ, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1")
        if self.repo:
            env["GH_REPO"] = self.repo
        try:
            # gh prints JSON/notes in UTF-8 regardless of the locale; never let the
            # platform codec (cp1252 on Windows) turn a release note into a crash.
            proc = subprocess.run(["gh", *args], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=self.timeout, env=env)
        except FileNotFoundError as exc:
            raise GhError("gh CLI not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise GhError(f"gh timeout: {' '.join(args[:2])}") from exc
        if proc.returncode:
            raise GhError(f"gh {' '.join(args[:2])} failed ({proc.returncode}): "
                          f"{scrub(proc.stderr).strip()[-300:]}")
        return proc.stdout

    def _release(self, tag: str) -> dict | None:
        try:
            return json.loads(self._run("api", f"repos/{{owner}}/{{repo}}/releases/tags/{tag}"))
        except GhError as exc:
            if "404" in str(exc) or "Not Found" in str(exc):
                return None
            raise

    def ensure_release(self, tag, title, notes, prerelease=False):
        if self._release(tag) is not None:
            return
        args = ["release", "create", tag, "--title", title, "--notes", notes]
        if prerelease:
            args.append("--prerelease")
        self._run(*args)
        logger.info("created release %s", tag)

    def list_assets(self, tag):
        release = self._release(tag)
        if release is None:
            return {}
        pages = json.loads(self._run("api", "--paginate", "--slurp",
                                     f"repos/{{owner}}/{{repo}}/releases/{release['id']}/assets?per_page=100"))
        assets = {}
        for page in pages:
            for row in page:
                digest = row.get("digest")
                if digest and digest.startswith("sha256:"):
                    digest = digest[len("sha256:"):]
                assets[row["name"]] = AssetInfo(row["name"], int(row["size"]), digest,
                                                row.get("state", "uploaded"), row.get("id"))
        return assets

    def upload(self, tag, path):
        self._run("release", "upload", tag, str(path))

    def download(self, tag, name, dest_dir):
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        self._run("release", "download", tag, "--pattern", name, "--dir", str(dest_dir), "--clobber")
        target = Path(dest_dir) / name
        if not target.exists():
            raise GhError(f"downloaded asset missing: {name}")
        return target

    def delete_asset(self, tag, name):
        self._run("release", "delete-asset", tag, name, "--yes")


def upload_verified(releases: ReleaseClient, tag: str, path: Path) -> bool:
    """Upload an immutable asset and confirm it with the API digest.

    An identical asset already present counts as success; a stale, partial or
    different one is deleted first. Returns False when the API cannot confirm
    the content, leaving the caller free to retry with the same data later.
    """
    name, digest, size = path.name, sha256_file(path), path.stat().st_size
    existing = releases.list_assets(tag).get(name)
    if existing is not None:
        if existing.state == "uploaded" and existing.size == size and existing.digest == digest:
            logger.info("asset %s already present in %s", name, tag)
            return True
        logger.info("replacing stale asset %s in %s", name, tag)
        releases.delete_asset(tag, name)
    releases.upload(tag, path)
    asset = None
    for attempt in range(VERIFY_ATTEMPTS):
        asset = releases.list_assets(tag).get(name)
        if asset is not None and asset.digest:
            if asset.digest == digest and asset.state == "uploaded":
                return True
            logger.error("asset %s digest mismatch after upload; deleting", name)
            releases.delete_asset(tag, name)
            return False
        if attempt < VERIFY_ATTEMPTS - 1:
            time.sleep(VERIFY_PAUSE_SEC)
    if asset is not None and asset.state == "uploaded" and asset.size == size:
        logger.warning("digest unavailable for %s; verified by size only", name)
        return True
    logger.error("upload of %s could not be verified", name)
    if asset is not None:
        try:
            releases.delete_asset(tag, name)
        except Exception:
            logger.warning("could not delete unverified asset %s", name)
    return False


# --- segments ---------------------------------------------------------------

@dataclass(frozen=True)
class FileRange:
    path: str
    offset: int
    length: int
    whole: bool


def build_segment(data_dir: Path, ranges: list[FileRange], out_path: Path, *,
                  run_id: str, seq: int, created_at: datetime) -> dict:
    chunks, files = [], []
    for item in ranges:
        with (Path(data_dir) / item.path).open("rb") as fh:
            fh.seek(item.offset)
            chunk = fh.read(item.length)
        if len(chunk) != item.length:
            raise RuntimeError(f"short read while packing {item.path}")
        chunks.append(chunk)
        files.append({"path": item.path, "offset": item.offset, "length": item.length,
                      "sha256": sha256_bytes(chunk), "whole": item.whole})
    manifest = {"schema_version": 1, "run_id": run_id, "seq": seq,
                "created_at": utc_stamp(created_at),
                "days": sorted({day_of(f["path"]) for f in files}), "files": files}
    mtime = int(created_at.timestamp())
    with tarfile.open(out_path, "w:xz", preset=3) as tar:
        _add_member(tar, "manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=1).encode(), mtime)
        for entry, chunk in zip(files, chunks):
            _add_member(tar, f"files/{entry['path']}", chunk, mtime)
    return manifest


def _add_member(tar: tarfile.TarFile, name: str, data: bytes, mtime: int) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mtime = len(data), mtime
    tar.addfile(info, io.BytesIO(data))


class Shipper:
    """Ships unshipped bytes of the archive streams as immutable segments."""

    def __init__(self, data_dir: Path, releases: ReleaseClient, run_id: str, *,
                 consolidated: set[str] | None = None, lock: threading.Lock | None = None):
        self.data_dir, self.releases, self.run_id = Path(data_dir), releases, run_id
        self.consolidated = consolidated if consolidated is not None else set()
        self.lock = lock or threading.Lock()
        self.state_path = self.data_dir / ".state" / "publish.json"
        self.shipped: dict[str, int] = {}
        self.seq = 0
        self._load_state()

    def _load_state(self) -> None:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if state.get("run_id") == self.run_id and isinstance(state.get("shipped"), dict):
            self.shipped = {key: int(value) for key, value in state["shipped"].items()}
            self.seq = int(state.get("seq", 0))

    def _save_state(self) -> None:
        atomic_json(self.state_path, {"schema_version": 1, "run_id": self.run_id,
                                      "seq": self.seq, "shipped": self.shipped})

    def pending(self, now_utc: datetime | None = None) -> list[FileRange]:
        """Unshipped ranges, open days first so fresh data never waits behind backfill."""
        now_utc = now_utc or datetime.now(timezone.utc)
        with self.lock:
            done = set(self.consolidated)
        ranges = []
        for rel, size in scan_archive_files(self.data_dir).items():
            day = day_of(rel)
            if day in done:
                continue
            offset = self.shipped.get(rel, 0)
            if size > offset:
                ranges.append(FileRange(rel, offset, size - offset, whole=is_batch(rel) and offset == 0))
        ranges.sort(key=lambda item: (day_closed(day_of(item.path), now_utc), day_of(item.path), item.path))
        return ranges

    def backlog_bytes(self) -> int:
        return sum(item.length for item in self.pending())

    def ship(self, now_utc: datetime | None = None) -> bool:
        now_utc = now_utc or datetime.now(timezone.utc)
        ranges = self.pending(now_utc)
        if not ranges:
            return True
        chosen, total = [], 0
        for item in ranges:
            if chosen and total + item.length > SEGMENT_BYTES_CAP:
                break
            chosen.append(item)
            total += item.length
        days = sorted({day_of(item.path) for item in chosen})
        name = segment_name(now_utc, self.run_id, self.seq, days)
        workdir = self.data_dir / ".state" / "segments"
        workdir.mkdir(parents=True, exist_ok=True)
        path = workdir / name
        try:
            build_segment(self.data_dir, chosen, path, run_id=self.run_id, seq=self.seq, created_at=now_utc)
            self.releases.ensure_release(STAGING_TAG, "Staging segments (internal)", STAGING_NOTES, prerelease=True)
            if not upload_verified(self.releases, STAGING_TAG, path):
                return False
        except Exception as exc:
            logger.error("segment shipping failed, will retry next cycle: %s", scrub(str(exc)))
            return False
        finally:
            path.unlink(missing_ok=True)
        for item in chosen:
            self.shipped[item.path] = item.offset + item.length
        self.seq += 1
        self._save_state()
        logger.info("shipped %s: %d files, %d bytes", name, len(chosen), total)
        return True


# --- consolidation ----------------------------------------------------------

@dataclass
class Contribution:
    run_id: str
    order: tuple
    path: str
    offset: int
    data: bytes
    source: str


@dataclass
class DayBuild:
    day: str
    members: dict[str, bytes] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    local_run: bool = False
    duplicates_dropped: int = 0
    partial_lines_dropped: int = 0
    gaps: list[dict] = field(default_factory=list)


def assemble_append_only(runs: dict[str, list[Contribution]], path: str, build: DayBuild) -> bytes:
    """Concatenate per-run ranges in offset order, then drop duplicate lines.

    Runs are ordered by their first segment; the local run always sorts last.
    An incomplete trailing line (writer died mid-row) is dropped at run
    boundaries so the result stays parseable; repeated CSV headers vanish with
    the line-level de-duplication.
    """
    run_order = sorted(runs, key=lambda run: min(item.order for item in runs[run]))
    blobs = []
    for run in run_order:
        buffer, position = bytearray(), 0
        for item in sorted(runs[run], key=lambda entry: (entry.offset, -len(entry.data))):
            end = item.offset + len(item.data)
            if end <= position:
                continue
            if item.offset > position:
                build.gaps.append({"run_id": run, "path": path, "expected_offset": position, "got_offset": item.offset})
            buffer += item.data[max(0, position - item.offset):]
            position = end
        if buffer and not buffer.endswith(b"\n"):
            del buffer[buffer.rfind(b"\n") + 1:]
            build.partial_lines_dropped += 1
        blobs.append(bytes(buffer))
    out, seen = bytearray(), set()
    for blob in blobs:
        for line in blob.splitlines(keepends=True):
            if line in seen:
                build.duplicates_dropped += 1
                continue
            seen.add(line)
            out += line
    return bytes(out)


class Consolidator:
    """Builds and publishes one immutable archive per closed day."""

    def __init__(self, data_dir: Path, releases: ReleaseClient, run_id: str, *,
                 consolidated: set[str] | None = None, lock: threading.Lock | None = None):
        self.data_dir, self.releases, self.run_id = Path(data_dir), releases, run_id
        self.consolidated = consolidated if consolidated is not None else set()
        self.lock = lock or threading.Lock()
        self._month_assets: dict[str, dict[str, AssetInfo]] = {}

    def staging_segments(self) -> list[SegmentInfo]:
        segments = [parse_segment_name(name) for name in self.releases.list_assets(STAGING_TAG)]
        return sorted((seg for seg in segments if seg is not None), key=lambda seg: seg.name)

    def _assets_of_month(self, day: str) -> dict[str, AssetInfo]:
        tag = month_tag(day)
        if tag not in self._month_assets:
            self._month_assets[tag] = self.releases.list_assets(tag)
        return self._month_assets[tag]

    def is_consolidated(self, day: str) -> bool:
        asset = self._assets_of_month(day).get(day_asset(day))
        return asset is not None and asset.state == "uploaded"

    def known_days(self, segments: list[SegmentInfo] | None = None) -> set[str]:
        days = {day_of(rel) for rel in scan_archive_files(self.data_dir)}
        for seg in (self.staging_segments() if segments is None else segments):
            days.update(seg.days)
        return days

    def candidate_days(self, now_utc: datetime) -> list[str]:
        self._month_assets.clear()
        closed = [day for day in self.known_days() if day_closed(day, now_utc)]
        done = {day for day in closed if self.is_consolidated(day)}
        with self.lock:
            self.consolidated.update(done)
        return sorted(day for day in closed if day not in done)

    def build_day(self, day: str, workdir: Path) -> tuple[Path, dict]:
        workdir = Path(workdir)
        build = DayBuild(day)
        contributions: list[Contribution] = []
        segment_dir = workdir / "segments"
        for seg in self.staging_segments():
            if day not in seg.days or seg.run_id == self.run_id:
                continue
            archive = self.releases.download(STAGING_TAG, seg.name, segment_dir)
            with tarfile.open(archive, "r:xz") as tar:
                manifest = json.load(tar.extractfile("manifest.json"))
                for entry in manifest["files"]:
                    if day_of(entry["path"]) != day:
                        continue
                    data = tar.extractfile(f"files/{entry['path']}").read()
                    if sha256_bytes(data) != entry["sha256"]:
                        raise RuntimeError(f"segment {seg.name} content mismatch for {entry['path']}")
                    contributions.append(Contribution(manifest["run_id"], (seg.stamp, seg.seq), entry["path"],
                                                      int(entry["offset"]), data, seg.name))
            build.sources.append(seg.name)
        for rel in scan_archive_files(self.data_dir):
            if day_of(rel) == day:
                build.local_run = True
                contributions.append(Contribution(self.run_id, ("~local", 0), rel, 0,
                                                  (self.data_dir / rel).read_bytes(), "local"))
        if not contributions:
            raise RuntimeError(f"no data found for {day}")
        append_only: dict[str, dict[str, list[Contribution]]] = {}
        for item in contributions:
            if is_batch(item.path):
                member = archive_member(item.path)
                if member in build.members:
                    continue
                raw = gzip.decompress(item.data)
                expected = Path(item.path).name[:-len(".jsonl.gz")]
                if sha256_bytes(raw) != expected:
                    raise RuntimeError(f"batch digest mismatch: {item.path}")
                build.members[member] = raw
            else:
                append_only.setdefault(item.path, {}).setdefault(item.run_id, []).append(item)
        for path, runs in append_only.items():
            build.members[archive_member(path)] = assemble_append_only(runs, path, build)
        manifest = self._manifest(build)
        archive = workdir / day_asset(day)
        mtime = int(datetime.now(timezone.utc).timestamp())
        with tarfile.open(archive, "w:xz", preset=6) as tar:
            _add_member(tar, "MANIFEST.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=1).encode(), mtime)
            for member in sorted(build.members):
                _add_member(tar, member, build.members[member], mtime)
        return archive, manifest

    def _manifest(self, build: DayBuild) -> dict:
        members, by_source, observations = [], {}, 0
        for member in sorted(build.members):
            data = build.members[member]
            lines = data.count(b"\n")
            members.append({"path": member, "size": len(data), "sha256": sha256_bytes(data), "lines": lines})
            if member.startswith("observations/"):
                for line in data.splitlines():
                    if not line.strip():
                        continue
                    try:
                        source = json.loads(line).get("source", "?")
                    except ValueError:
                        source = "invalid"
                    by_source[source] = by_source.get(source, 0) + 1
                    observations += 1
        return {"schema_version": 1, "day": build.day, "built_at": utc_stamp(datetime.now(timezone.utc)),
                "run_id": self.run_id, "members": members, "sources": build.sources,
                "local_run": build.local_run, "duplicates_dropped": build.duplicates_dropped,
                "partial_lines_dropped": build.partial_lines_dropped, "gaps": build.gaps,
                "observations": {"count": observations, "by_source": by_source}}

    def consolidate_day(self, day: str, workdir: Path) -> dict:
        archive, manifest = self.build_day(day, workdir)
        tag = month_tag(day)
        self.releases.ensure_release(tag, f"Traffic data {day[:7]}", MONTH_NOTES)
        if not upload_verified(self.releases, tag, archive):
            raise RuntimeError(f"upload of {archive.name} was not verified")
        self._month_assets.pop(tag, None)
        with self.lock:
            self.consolidated.add(day)
        if manifest["gaps"]:
            logger.warning("%s consolidated with %d gap(s)", day, len(manifest["gaps"]))
        logger.info("consolidated %s: %d members, %d observations, %d duplicate lines dropped",
                    day, len(manifest["members"]), manifest["observations"]["count"], manifest["duplicates_dropped"])
        return manifest

    def cleanup_staging(self) -> int:
        deleted = 0
        for seg in self.staging_segments():
            if all(self.is_consolidated(day) for day in seg.days):
                self.releases.delete_asset(STAGING_TAG, seg.name)
                deleted += 1
        return deleted

    def run(self, now_utc: datetime | None = None) -> dict:
        now_utc = now_utc or datetime.now(timezone.utc)
        summary: dict[str, Any] = {"consolidated": [], "failed": [], "deleted_segments": 0}
        state_dir = self.data_dir / ".state"
        state_dir.mkdir(parents=True, exist_ok=True)
        for day in self.candidate_days(now_utc):
            with tempfile.TemporaryDirectory(prefix=".tmp-consolidate-", dir=state_dir) as tmp:
                try:
                    self.consolidate_day(day, Path(tmp))
                    summary["consolidated"].append(day)
                except Exception as exc:
                    logger.error("consolidation of %s failed, will retry: %s", day, scrub(str(exc)))
                    summary["failed"].append(day)
        try:
            summary["deleted_segments"] = self.cleanup_staging()
        except Exception as exc:
            logger.error("staging cleanup failed: %s", scrub(str(exc)))
        return summary


# --- facade -----------------------------------------------------------------

class Publisher:
    """What ``collector.shift`` talks to when ``--git`` is enabled."""

    def __init__(self, data_dir: Path, releases: ReleaseClient | None = None, run_id: str | None = None):
        self.data_dir = Path(data_dir)
        self.releases = releases or GhReleases()
        self.run_id = run_id or default_run_id()
        self.lock = threading.Lock()
        self.consolidated: set[str] = set()
        self.shipper = Shipper(self.data_dir, self.releases, self.run_id, consolidated=self.consolidated, lock=self.lock)
        self.consolidator = Consolidator(self.data_dir, self.releases, self.run_id,
                                         consolidated=self.consolidated, lock=self.lock)
        self._thread: threading.Thread | None = None

    def ship(self, now_utc: datetime | None = None) -> bool:
        return self.shipper.ship(now_utc)

    def fully_shipped(self) -> bool:
        return self.shipper.backlog_bytes() == 0

    def consolidating(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def maybe_consolidate(self, now_utc: datetime | None = None) -> bool:
        """Start one background consolidation pass; idempotent and never blocking."""
        if self.consolidating():
            return False
        moment = now_utc or datetime.now(timezone.utc)

        def work():
            try:
                self.consolidator.run(moment)
            except Exception as exc:
                logger.error("consolidation pass failed: %s", scrub(str(exc)))

        self._thread = threading.Thread(target=work, name="consolidator", daemon=True)
        self._thread.start()
        return True

    def close(self) -> None:
        if self.consolidating():
            logger.warning("consolidation still running; it resumes on the next shift (idempotent)")


# --- CLI --------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "ship", "consolidate"])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    publisher = Publisher(args.data_dir, run_id=args.run_id)
    now_utc = datetime.now(timezone.utc)
    if args.command == "status":
        pending = publisher.shipper.pending(now_utc)
        report = {"run_id": publisher.run_id, "pending_files": len(pending),
                  "backlog_bytes": sum(item.length for item in pending),
                  "candidate_days": publisher.consolidator.candidate_days(now_utc)}
        print(json.dumps(report, ensure_ascii=False))
        return 0
    if args.command == "ship":
        ok = publisher.ship(now_utc)
        print(json.dumps({"shipped": ok, "backlog_bytes": publisher.shipper.backlog_bytes()}))
        return 0 if ok else 1
    summary = publisher.consolidator.run(now_utc)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

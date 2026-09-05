"""Durable canonical observations and a crash-replayable compressed outbox.

Legacy CSV/JSON are convenience views. SQLite is authoritative on a persistent
host; Actions must publish the compressed outbox before its runner disappears.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from collector.store import atomic_json, utc_stamp


def atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".tmp-", delete=False) as f:
            name = f.name
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


class Journal:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / ".state" / "journal.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS observations (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                observation_id TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                status TEXT NOT NULL,
                document TEXT NOT NULL,
                exported INTEGER NOT NULL DEFAULT 0 CHECK(exported IN (0,1))
            );
            CREATE INDEX IF NOT EXISTS pending_export ON observations(exported, seq);
            CREATE INDEX IF NOT EXISTS source_time ON observations(source, observed_at);
            CREATE TABLE IF NOT EXISTS export_batches (
                digest TEXT PRIMARY KEY, day TEXT NOT NULL, body TEXT NOT NULL,
                first_seq INTEGER NOT NULL, last_seq INTEGER NOT NULL, sent INTEGER NOT NULL DEFAULT 0
            );
        """)

    def record(self, source: str, observed_at: datetime, payload=None, *,
               status: str = "ok", source_ts=None, error: str | None = None,
               observation_id: str | None = None) -> str:
        observation_id = observation_id or str(uuid.uuid4())
        document = {"schema_version": 2, "observation_id": observation_id,
                    "source": source, "observed_at": utc_stamp(observed_at),
                    "source_ts": source_ts, "status": status,
                    "error_code": error, "payload": payload}
        encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, allow_nan=False)
        with self.db:
            old = self.db.execute("SELECT document FROM observations WHERE observation_id=?",
                                  (observation_id,)).fetchone()
            if old and old[0] != encoded:
                raise ValueError("observation id reused with different content")
            self.db.execute("""INSERT OR IGNORE INTO observations
                (observation_id, source, observed_at, status, document) VALUES (?,?,?,?,?)""",
                (observation_id, source, document["observed_at"], status, encoded))
        return observation_id

    def export_pending(self, batch_size: int = 1000) -> int:
        """Persist batch membership BEFORE writing; retry exactly that batch.

        The filename hashes uncompressed content, independent of gzip/Python
        version. A local export acknowledgment is not an off-host backup.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        total = 0
        while True:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                batch = self.db.execute("SELECT digest,day,body,first_seq,last_seq FROM export_batches "
                                        "WHERE sent=0 ORDER BY first_seq LIMIT 1").fetchone()
                if batch is None:
                    rows = self.db.execute("SELECT seq,observed_at,document FROM observations "
                                           "WHERE exported=0 ORDER BY seq LIMIT ?", (batch_size,)).fetchall()
                    if not rows:
                        self.db.commit()
                        return total
                    body = "\n".join(row[2] for row in rows)+"\n"
                    digest = hashlib.sha256(body.encode()).hexdigest()
                    batch = (digest, rows[0][1][:10], body, rows[0][0], rows[-1][0])
                    self.db.execute("INSERT INTO export_batches(digest,day,body,first_seq,last_seq) "
                                    "VALUES (?,?,?,?,?)", batch)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            digest, day, body, first_seq, last_seq = batch
            content = body.encode()
            path = self.data_dir/"observations"/day/f"{digest}.jsonl.gz"
            valid = False
            if path.exists():
                try:
                    valid = hashlib.sha256(gzip.decompress(path.read_bytes())).hexdigest() == digest
                except (OSError, EOFError):
                    pass
            if not valid:
                atomic_bytes(path, gzip.compress(content, mtime=0))
            with self.db:
                self.db.execute("UPDATE observations SET exported=1 WHERE seq BETWEEN ? AND ?",
                                (first_seq, last_seq))
                self.db.execute("UPDATE export_batches SET sent=1,body='' WHERE digest=?", (digest,))
            total += body.count("\n")

    def backup(self, destination: Path) -> Path:
        """Consistent, verified SQLite backup; destination should be off-host."""
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination) as td:
            copy_path = Path(td) / "journal.sqlite3"
            with sqlite3.connect(copy_path) as target:
                self.db.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("backup integrity check failed")
                count = target.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            content = gzip.compress(copy_path.read_bytes(), mtime=0)
        digest = hashlib.sha256(content).hexdigest()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = destination / f"journal-{stamp}-{digest[:12]}.sqlite3.gz"
        atomic_bytes(path, content)
        atomic_json(path.with_suffix(path.suffix + ".json"),
                    {"sha256": digest, "observations": count, "schema_version": 2,
                     "created_at": utc_stamp(datetime.now(timezone.utc))})
        for registry in (self.data_dir/"jam_map/registries").glob("*.json"):
            content = registry.read_bytes()
            if hashlib.sha256(content).hexdigest() != registry.stem:
                raise ValueError("registry checksum mismatch")
            target = destination/"registries"/registry.name
            if not target.exists():
                atomic_bytes(target, content)
        atomic_json(self.data_dir/".state/backup.json",
                    {"backed_up_at": utc_stamp(datetime.now(timezone.utc)), "sha256": digest})
        return path

    def close(self) -> None:
        self.db.close()

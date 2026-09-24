"""Offline tests for Releases publication: a fake client, no GitHub access, real local git."""
import gzip
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from collector import publish, shift, sources
from collector.publish import (STAGING_TAG, AssetInfo, Consolidator, FileRange, Publisher, Shipper,
                               day_asset, day_closed, day_of, month_tag, parse_segment_name, segment_name)

DAY = "2026-09-16"
NEXT = "2026-09-17"
CLOSED = datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc)      # DAY closed, NEXT open
OPEN = datetime(2026, 9, 16, 23, 0, tzinfo=timezone.utc)


class FakeReleases:
    """In-memory GitHub Releases with the digest and asset-state semantics of the real service.

    An interrupted upload stays in the REST asset listing as ``state="starter"`` without
    a digest, but ``gh release download`` and ``gh release delete-asset`` resolve names
    through the release object, which omits it. Deleting by id through REST works for
    any state.
    """

    def __init__(self):
        self.releases = {}
        self.uploads = 0
        self.deleted = []
        self.corrupt_digest = False
        self.fail_upload = False
        self.interrupt_at = None          # next upload dies midway, as with a gh timeout
        self.fail_delete = set()
        self.states, self.created, self.ids, self.next_id = {}, {}, {}, 0

    def ensure_release(self, tag, title, notes, prerelease=False):
        self.releases.setdefault(tag, {"prerelease": prerelease, "title": title, "assets": {}})

    def list_assets(self, tag):
        release = self.releases.get(tag)
        if release is None:
            return {}
        out = {}
        for name, data in release["assets"].items():
            state = self.states.get(name, "uploaded")
            digest = None if state != "uploaded" else "0"*64 if self.corrupt_digest else hashlib.sha256(data).hexdigest()
            extra = {"created_at": self.created[name]} if name in self.created else {}
            out[name] = AssetInfo(name, len(data), digest, state, self.ids[name], **extra)
        return out

    def upload(self, tag, path):
        if self.fail_upload:
            raise publish.GhError("simulated HTTP 502")
        path = Path(path)
        assets = self.releases[tag]["assets"]
        if path.name in assets:
            raise publish.GhError("asset already exists")
        assets[path.name] = path.read_bytes()
        self.next_id += 1
        self.ids[path.name] = self.next_id
        if self.interrupt_at is not None:
            self.states[path.name] = "starter"
            self.created[path.name] = self.interrupt_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            self.interrupt_at = None
            raise publish.GhError("gh timeout: release upload")
        self.uploads += 1

    def visible_to_gh(self, tag, name):
        return name in self.releases.get(tag, {"assets": {}})["assets"] and self.states.get(name, "uploaded") == "uploaded"

    def download(self, tag, name, dest_dir):
        if not self.visible_to_gh(tag, name):
            raise publish.GhError("gh release download failed (1): no assets match the file pattern")
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        target = dest/name
        target.write_bytes(self.releases[tag]["assets"][name])
        return target

    def delete_asset(self, tag, name, asset_id=None):
        if name in self.fail_delete:
            raise publish.GhError("simulated HTTP 500")
        if asset_id is None and not self.visible_to_gh(tag, name):
            raise publish.GhError(f"asset {name} not found in release {tag}")
        if asset_id is not None and self.ids.get(name) != asset_id:
            raise publish.GhError("HTTP 404: Not Found")
        del self.releases[tag]["assets"][name]
        self.states.pop(name, None)
        self.deleted.append(name)

    def names(self, tag):
        return sorted(self.releases.get(tag, {"assets": {}})["assets"])


def write_batch(data_dir, day, rows):
    body = "\n".join(json.dumps(row, sort_keys=True) for row in rows)+"\n"
    digest = hashlib.sha256(body.encode()).hexdigest()
    path = data_dir/"observations"/day/f"{digest}.jsonl.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(body.encode(), mtime=0))
    return f"observations/{day}/{digest}.jsonl.gz", body


def append(data_dir, rel, text):
    path = data_dir/rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as fh:
        fh.write(text)


def read_archive(data):
    with tarfile.open(fileobj=__import__("io").BytesIO(data), mode="r:xz") as tar:
        return {member.name: tar.extractfile(member).read() for member in tar.getmembers() if member.isfile()}


class NamingTests(unittest.TestCase):
    def test_day_of_recognises_only_archive_streams(self):
        self.assertEqual(day_of("observations/2026-09-16/abc.jsonl.gz"), "2026-09-16")
        self.assertEqual(day_of("snapshots/2026-09/16.jsonl"), "2026-09-16")
        self.assertEqual(day_of("jam_map/v2/2026-09-16.csv"), "2026-09-16")
        self.assertIsNone(day_of("jam_map/ways.json"))
        self.assertIsNone(day_of("jam_map/2026-09-02.csv"))
        self.assertIsNone(day_of("observations/2026-09-16/.tmp-x.jsonl.gz"))
        self.assertIsNone(day_of("scores/2026-09.csv"))

    def test_segment_name_roundtrip_and_ordering(self):
        name = segment_name(CLOSED, "35202959169", 7, [DAY, NEXT])
        info = parse_segment_name(name)
        self.assertEqual((info.run_id, info.seq, info.days), ("35202959169", 7, (DAY, NEXT)))
        later = segment_name(CLOSED+timedelta(minutes=15), "35202959169", 8, [NEXT])
        self.assertLess(name, later)
        self.assertIsNone(parse_segment_name("traffic-2026-09-16.tar.xz"))
        self.assertEqual(month_tag(DAY), "data-2026-09")
        self.assertEqual(day_asset(DAY), "traffic-2026-09-16.tar.xz")

    def test_day_closes_45_minutes_after_utc_midnight(self):
        self.assertFalse(day_closed(DAY, datetime(2026, 9, 17, 0, 44, tzinfo=timezone.utc)))
        self.assertTrue(day_closed(DAY, datetime(2026, 9, 17, 0, 45, tzinfo=timezone.utc)))


class ShipperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)/"data"
        self.data.mkdir()
        self.releases = FakeReleases()

    def shipper(self, run_id="run1", **kwargs):
        return Shipper(self.data, self.releases, run_id, **kwargs)

    def test_ship_builds_one_segment_and_advances_state(self):
        b1, _ = write_batch(self.data, NEXT, [{"source": "dgis", "n": 1}])
        b2, _ = write_batch(self.data, NEXT, [{"source": "dgis", "n": 2}])
        append(self.data, f"jam_map/v2/{NEXT}.csv", "ts,classes\n"+"x"*288+"\n")
        shipper = self.shipper()
        self.assertTrue(shipper.ship(CLOSED))
        names = self.releases.names(STAGING_TAG)
        self.assertEqual(len(names), 1)
        self.assertTrue(self.releases.releases[STAGING_TAG]["prerelease"])
        members = read_archive(self.releases.releases[STAGING_TAG]["assets"][names[0]])
        manifest = json.loads(members["manifest.json"])
        self.assertEqual({f["path"] for f in manifest["files"]}, {b1, b2, f"jam_map/v2/{NEXT}.csv"})
        self.assertTrue(all({"offset", "length", "sha256", "whole"} <= set(f) for f in manifest["files"]))
        self.assertEqual(manifest["days"], [NEXT])
        state = json.loads((self.data/".state/publish.json").read_text())
        self.assertEqual(state["shipped"][f"jam_map/v2/{NEXT}.csv"], 11+289)
        self.assertTrue(shipper.ship(CLOSED))
        self.assertEqual(self.releases.uploads, 1, "nothing new: no second upload")

    def test_bad_digest_keeps_state_and_retries_same_bytes(self):
        append(self.data, f"snapshots/2026-09/17.jsonl", '{"a":1}\n')
        shipper = self.shipper()
        self.releases.corrupt_digest = True
        self.assertFalse(shipper.ship(CLOSED))
        self.assertEqual(shipper.shipped, {})
        self.assertEqual(self.releases.names(STAGING_TAG), [], "unverified asset removed")
        self.releases.corrupt_digest = False
        self.assertTrue(shipper.ship(CLOSED+timedelta(minutes=15)))
        manifest = json.loads(read_archive(next(iter(self.releases.releases[STAGING_TAG]["assets"].values())))["manifest.json"])
        self.assertEqual(manifest["files"][0]["offset"], 0)

    def test_state_belongs_to_run(self):
        append(self.data, f"jam_map/v2/{NEXT}.csv", "h\n")
        self.assertTrue(self.shipper().ship(CLOSED))
        self.assertEqual(self.shipper().pending(CLOSED), [])
        self.assertEqual(len(self.shipper(run_id="run2").pending(CLOSED)), 1)

    def test_open_days_first_and_consolidated_days_skipped(self):
        append(self.data, f"jam_map/v2/{DAY}.csv", "old\n")
        append(self.data, f"jam_map/v2/{NEXT}.csv", "new\n")
        write_batch(self.data, "2026-09-10", [{"n": 0}])
        shipper = self.shipper(consolidated={"2026-09-10"})
        pending = shipper.pending(CLOSED)
        self.assertEqual([day_of(p.path) for p in pending], [NEXT, DAY])

    def test_segment_size_cap_splits_backlog(self):
        append(self.data, f"jam_map/v2/{DAY}.csv", "a"*100+"\n")
        append(self.data, f"jam_map/v2/{NEXT}.csv", "b"*100+"\n")
        with patch.object(publish, "SEGMENT_BYTES_CAP", 150):
            shipper = self.shipper()
            self.assertTrue(shipper.ship(CLOSED))
            self.assertEqual(self.releases.uploads, 1)
            self.assertEqual(shipper.backlog_bytes(), 101)
            self.assertTrue(shipper.ship(CLOSED+timedelta(seconds=1)))
        self.assertEqual(shipper.backlog_bytes(), 0)

    def test_gh_failure_is_reported_not_raised(self):
        append(self.data, f"jam_map/v2/{NEXT}.csv", "h\n")
        self.releases.fail_upload = True
        self.assertFalse(self.shipper().ship(CLOSED))
        self.assertFalse(list((self.data/".state/segments").glob("*")))


class ConsolidatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.releases = FakeReleases()

    def run_dir(self, name):
        path = self.root/name
        path.mkdir(exist_ok=True)
        return path

    def test_day_assembled_from_two_runs_and_local_files(self):
        run_a = self.run_dir("a")
        b1, body1 = write_batch(run_a, DAY, [{"source": "dgis", "n": 1}, {"source": "yandex", "n": 2}])
        append(run_a, f"jam_map/v2/{DAY}.csv", "ts,classes\n1,G\n")
        append(run_a, f"snapshots/2026-09/16.jsonl", '{"t":1}\n')
        shipper_a = Shipper(run_a, self.releases, "runA")
        self.assertTrue(shipper_a.ship(CLOSED-timedelta(hours=3)))
        append(run_a, f"jam_map/v2/{DAY}.csv", "2,Y\n")
        self.assertTrue(shipper_a.ship(CLOSED-timedelta(hours=2)))
        run_b = self.run_dir("b")
        b2, body2 = write_batch(run_b, DAY, [{"source": "dgis", "n": 3}])
        append(run_b, f"jam_map/v2/{DAY}.csv", "ts,classes\n3,R\n")       # fresh runner repeats the header
        append(run_b, f"snapshots/2026-09/16.jsonl", '{"t":2}\n')
        self.assertTrue(Shipper(run_b, self.releases, "runB").ship(CLOSED-timedelta(hours=1)))
        local = self.run_dir("c")
        append(local, f"jam_map/v2/{DAY}.csv", "ts,classes\n4,D\n")
        Shipper(local, self.releases, "runC").ship(CLOSED-timedelta(minutes=30))  # own segment must be ignored
        consolidator = Consolidator(local, self.releases, "runC")
        summary = consolidator.run(CLOSED)
        self.assertEqual(summary["consolidated"], [DAY])
        self.assertEqual(summary["failed"], [])
        archive = self.releases.releases[month_tag(DAY)]["assets"][day_asset(DAY)]
        members = read_archive(archive)
        self.assertEqual(members[f"jam_map/v2/{DAY}.csv"], b"ts,classes\n1,G\n2,Y\n3,R\n4,D\n")
        self.assertEqual(members[f"snapshots/{DAY}.jsonl"], b'{"t":1}\n{"t":2}\n')
        self.assertEqual(members[f"observations/{DAY}/{Path(b1).name[:-3]}"], body1.encode())
        self.assertEqual(members[f"observations/{DAY}/{Path(b2).name[:-3]}"], body2.encode())
        manifest = json.loads(members["MANIFEST.json"])
        self.assertEqual(manifest["duplicates_dropped"], 2, "two repeated CSV headers")
        self.assertEqual(manifest["gaps"], [])
        self.assertTrue(manifest["local_run"])
        self.assertEqual(len(manifest["sources"]), 3)
        self.assertEqual(manifest["observations"], {"count": 3, "by_source": {"dgis": 2, "yandex": 1}})
        for entry in manifest["members"]:
            self.assertEqual(hashlib.sha256(members[entry["path"]]).hexdigest(), entry["sha256"])
        self.assertEqual(self.releases.names(STAGING_TAG), [], "all segments of a consolidated day deleted")
        uploads = self.releases.uploads
        self.assertEqual(consolidator.run(CLOSED)["consolidated"], [])
        self.assertEqual(self.releases.uploads, uploads, "idempotent: no re-upload")

    def test_open_day_is_not_a_candidate(self):
        local = self.run_dir("c")
        append(local, f"jam_map/v2/{DAY}.csv", "h\n")
        self.assertEqual(Consolidator(local, self.releases, "run").candidate_days(OPEN), [])
        self.assertEqual(Consolidator(local, self.releases, "run").candidate_days(CLOSED), [DAY])

    def test_midnight_segment_survives_until_both_days_consolidated(self):
        run_a = self.run_dir("a")
        append(run_a, f"jam_map/v2/{DAY}.csv", "d\n")
        append(run_a, f"jam_map/v2/{NEXT}.csv", "n\n")
        self.assertTrue(Shipper(run_a, self.releases, "runA").ship(CLOSED-timedelta(hours=1)))
        run_b = self.run_dir("b")
        append(run_b, f"jam_map/v2/{DAY}.csv", "d2\n")
        self.assertTrue(Shipper(run_b, self.releases, "runB").ship(CLOSED-timedelta(minutes=50)))
        summary = Consolidator(self.run_dir("c"), self.releases, "runC").run(CLOSED)
        self.assertEqual(summary["consolidated"], [DAY])
        remaining = self.releases.names(STAGING_TAG)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(parse_segment_name(remaining[0]).days, (DAY, NEXT))
        self.assertEqual(summary["deleted_segments"], 1)

    def test_upload_failure_is_contained_and_retried(self):
        local = self.run_dir("c")
        append(local, f"jam_map/v2/{DAY}.csv", "h\n")
        consolidator = Consolidator(local, self.releases, "runC")
        self.releases.fail_upload = True
        self.assertEqual(consolidator.run(CLOSED)["failed"], [DAY])
        self.releases.fail_upload = False
        self.assertEqual(consolidator.run(CLOSED)["consolidated"], [DAY])

    def test_gap_and_partial_line_are_recorded_not_fatal(self):
        run_a = self.run_dir("a")
        rel = f"jam_map/v2/{DAY}.csv"
        append(run_a, rel, "".join(f"line{i}\n" for i in range(10)))       # 60 bytes
        out = self.root/"seg"
        out.mkdir()
        for seq, (offset, length) in enumerate(((0, 18), (36, 24))):
            name = segment_name(CLOSED-timedelta(hours=2, minutes=seq), "runA", seq, [DAY])
            publish.build_segment(run_a, [FileRange(rel, offset, length, False)], out/name,
                                  run_id="runA", seq=seq, created_at=CLOSED)
            self.releases.ensure_release(STAGING_TAG, "s", "s", prerelease=True)
            self.releases.upload(STAGING_TAG, out/name)
        run_b = self.run_dir("b")
        append(run_b, rel, "line9\nline10 partial")
        self.assertTrue(Shipper(run_b, self.releases, "runB").ship(CLOSED-timedelta(hours=1)))
        Consolidator(self.run_dir("c"), self.releases, "runC").run(CLOSED)
        members = read_archive(self.releases.releases[month_tag(DAY)]["assets"][day_asset(DAY)])
        self.assertEqual(members[rel], b"line0\nline1\nline2\nline6\nline7\nline8\nline9\n")
        manifest = json.loads(members["MANIFEST.json"])
        self.assertEqual(len(manifest["gaps"]), 1)
        self.assertEqual(manifest["partial_lines_dropped"], 1)
        self.assertEqual(manifest["duplicates_dropped"], 1)

    def test_interrupted_segment_upload_does_not_block_consolidation(self):
        run_a = self.run_dir("a")
        append(run_a, f"snapshots/2026-09/16.jsonl", '{"t":1}\n')
        shipper = Shipper(run_a, self.releases, "runA")
        self.releases.interrupt_at = CLOSED-timedelta(hours=5)
        self.assertFalse(shipper.ship(CLOSED-timedelta(hours=5)))
        append(run_a, f"snapshots/2026-09/16.jsonl", '{"t":2}\n')
        self.assertTrue(shipper.ship(CLOSED-timedelta(hours=4, minutes=45)), "retry: new name, same bytes")
        summary = Consolidator(self.run_dir("c"), self.releases, "runC").run(CLOSED)
        self.assertEqual(summary["consolidated"], [DAY])
        members = read_archive(self.releases.releases[month_tag(DAY)]["assets"][day_asset(DAY)])
        self.assertEqual(members[f"snapshots/{DAY}.jsonl"], b'{"t":1}\n{"t":2}\n')
        self.assertEqual(self.releases.names(STAGING_TAG), [], "retried segment consolidated, orphan deleted")

    def test_orphaned_upload_deleted_only_after_grace_period(self):
        run_a = self.run_dir("a")
        append(run_a, f"jam_map/v2/{NEXT}.csv", "n\n")
        self.releases.interrupt_at = CLOSED-timedelta(minutes=10)
        self.assertFalse(Shipper(run_a, self.releases, "runA").ship(CLOSED-timedelta(minutes=10)))
        consolidator = Consolidator(self.run_dir("c"), self.releases, "runC")
        self.assertEqual(consolidator.cleanup_staging(CLOSED), 0, "young: the upload may still be running")
        self.assertEqual(len(self.releases.names(STAGING_TAG)), 1)
        self.assertEqual(consolidator.cleanup_staging(CLOSED+timedelta(hours=1)), 1)
        self.assertEqual(self.releases.names(STAGING_TAG), [])

    def test_cleanup_continues_after_failed_deletion(self):
        run_a = self.run_dir("a")
        shipper = Shipper(run_a, self.releases, "runA")
        append(run_a, f"jam_map/v2/{DAY}.csv", "a\n")
        self.assertTrue(shipper.ship(CLOSED-timedelta(hours=2)))
        append(run_a, f"jam_map/v2/{DAY}.csv", "b\n")
        self.assertTrue(shipper.ship(CLOSED-timedelta(hours=1)))
        first, second = self.releases.names(STAGING_TAG)
        self.releases.fail_delete.add(first)
        summary = Consolidator(self.run_dir("c"), self.releases, "runC").run(CLOSED)
        self.assertEqual(summary["consolidated"], [DAY])
        self.assertEqual(summary["deleted_segments"], 1)
        self.assertEqual(self.releases.names(STAGING_TAG), [first])

    def test_stale_daily_archive_in_starter_state_is_replaced(self):
        local = self.run_dir("c")
        append(local, f"jam_map/v2/{DAY}.csv", "h\n")
        consolidator = Consolidator(local, self.releases, "runC")
        self.releases.interrupt_at = CLOSED
        self.assertEqual(consolidator.run(CLOSED)["failed"], [DAY])
        self.assertEqual(self.releases.list_assets(month_tag(DAY))[day_asset(DAY)].state, "starter")
        self.assertEqual(consolidator.run(CLOSED)["consolidated"], [DAY])
        self.assertEqual(self.releases.list_assets(month_tag(DAY))[day_asset(DAY)].state, "uploaded")

    def test_stuck_day_is_annotated_once(self):
        local = self.run_dir("c")
        append(local, f"jam_map/v2/{DAY}.csv", "h\n")
        consolidator = Consolidator(local, self.releases, "runC")
        self.releases.fail_upload = True
        out = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), redirect_stdout(out):
            consolidator.run(CLOSED)                          # closed 15 minutes ago: not stuck yet
            consolidator.run(CLOSED+timedelta(hours=25))
            consolidator.run(CLOSED+timedelta(hours=26))
        annotations = [line for line in out.getvalue().splitlines() if line.startswith("::error")]
        self.assertEqual(len(annotations), 1)
        self.assertIn(DAY, annotations[0])


class PublisherTests(unittest.TestCase):
    def test_background_consolidation_runs_once_at_a_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)/"data"
            data.mkdir()
            append(data, f"jam_map/v2/{DAY}.csv", "h\n")
            releases = FakeReleases()
            publisher = Publisher(data, releases, run_id="run")
            self.assertTrue(publisher.maybe_consolidate(CLOSED))
            publisher._thread.join(timeout=30)
            self.assertFalse(publisher.consolidating())
            self.assertIn(day_asset(DAY), releases.names(month_tag(DAY)))
            self.assertTrue(publisher.fully_shipped(), "consolidated day no longer counts as backlog")


class GhClientTests(unittest.TestCase):
    def test_commands_env_and_secret_scrubbing(self):
        seen = []
        def fake_run(cmd, **kwargs):
            seen.append((cmd, kwargs["env"].get("GH_REPO")))
            if cmd[1] == "api" and "tags/" in cmd[2]:
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"id": 5}), "")
            if cmd[1] == "api":
                return subprocess.CompletedProcess(cmd, 0, json.dumps([[{"name": "a", "size": 3, "digest": "sha256:ff", "state": "uploaded", "id": 1}]]), "")
            return subprocess.CompletedProcess(cmd, 1, "", "denied for ghp_abcdefghijklmnop123 at x-access-token:ghs_zzzzzzzzzz@github.com")
        with patch.object(subprocess, "run", side_effect=fake_run):
            client = publish.GhReleases(repo="Dex719/almaty-traffic-data")
            assets = client.list_assets("staging")
            self.assertEqual(assets["a"].digest, "ff")
            with self.assertRaises(publish.GhError) as ctx:
                client.upload("staging", Path("x.tar.xz"))
        self.assertNotIn("ghp_", str(ctx.exception))
        self.assertNotIn("ghs_", str(ctx.exception))
        self.assertTrue(all(repo == "Dex719/almaty-traffic-data" for _, repo in seen))
        self.assertEqual(seen[-1][0][:4], ["gh", "release", "upload", "staging"])

    def test_delete_asset_goes_by_id_through_rest(self):
        seen = []
        def fake_run(cmd, **kwargs):
            seen.append(cmd)
            if "tags/" in cmd[-1]:
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"id": 5}), "")
            if cmd[-1].endswith("assets?per_page=100"):
                return subprocess.CompletedProcess(cmd, 0, json.dumps([[{
                    "name": "a", "size": 3, "digest": None, "state": "starter", "id": 9,
                    "created_at": "2026-09-17T20:07:33Z"}]]), "")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with patch.object(subprocess, "run", side_effect=fake_run):
            client = publish.GhReleases(repo="Dex719/almaty-traffic-data")
            self.assertEqual(client.list_assets("staging")["a"].created_at, "2026-09-17T20:07:33Z")
            client.delete_asset("staging", "a", 7)
            self.assertEqual(seen[-1], ["gh", "api", "-X", "DELETE", "repos/{owner}/{repo}/releases/assets/7"])
            client.delete_asset("staging", "a")        # looked up in the listing, which includes starter assets
            self.assertEqual(seen[-1], ["gh", "api", "-X", "DELETE", "repos/{owner}/{repo}/releases/assets/9"])


class CliTests(unittest.TestCase):
    def test_ship_refuses_days_already_archived(self):
        now = datetime.now(timezone.utc)
        archived, today = (now-timedelta(days=3)).date().isoformat(), now.date().isoformat()
        releases = FakeReleases()
        with tempfile.TemporaryDirectory() as tmp:
            first, recovery = Path(tmp)/"first", Path(tmp)/"recovery"
            append(first, f"jam_map/v2/{archived}.csv", "early\n")
            self.assertEqual(Consolidator(first, releases, "runA").run(now)["consolidated"], [archived])
            append(recovery, f"jam_map/v2/{archived}.csv", "late\n")
            append(recovery, f"jam_map/v2/{today}.csv", "fresh\n")
            out = io.StringIO()
            with patch.object(publish, "GhReleases", return_value=releases), redirect_stdout(out):
                code = publish.main(["ship", "--data-dir", str(recovery), "--run-id", "recovery_1"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["refused_days"], [archived])
        shipped = [parse_segment_name(name).days for name in releases.names(STAGING_TAG)]
        self.assertEqual(shipped, [(today,)], "only the open day was shipped")

    def test_rejects_run_id_that_breaks_segment_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(publish, "GhReleases", return_value=FakeReleases()), redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit) as ctx:
                publish.main(["status", "--data-dir", tmp, "--run-id", "recovery-1"])
            self.assertEqual(ctx.exception.code, 2)
            with self.assertRaises(ValueError):
                Shipper(Path(tmp), FakeReleases(), "recovery-1")


class GitPublicationTests(unittest.TestCase):
    """Real local git: live views only, archive drop, hourly registry, autostash."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.remote, self.work = base/"remote.git", base/"work"
        self.git(base, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(base, "clone", str(self.remote), str(self.work))
        self.git(self.work, "config", "user.name", "Offline test")
        self.git(self.work, "config", "user.email", "test@example.invalid")
        for rel, text in {"data/scores/2026-09.csv": "h\n", "data/events/2026-09.json": "{}",
                          "data/jam_map/ways.json": "{}", "data/observations/2026-09-16/a.jsonl.gz": "old",
                          "data/snapshots/2026-09/16.jsonl": "s\n", "data/jam_map/v2/2026-09-16.csv": "j\n"}.items():
            path = self.work/rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.git(self.work, "add", "data")
        self.git(self.work, "commit", "-m", "initial")
        self.git(self.work, "push", "--set-upstream", "origin", "main")

    @staticmethod
    def git(cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

    def committed_paths(self):
        return set(self.git(self.work, "show", "--name-only", "--format=", "HEAD").splitlines())

    def test_live_views_only_and_registry_throttled(self):
        (self.work/"data/scores/2026-09.csv").write_text("h\n1\n")
        (self.work/"data/events/2026-09.json").write_text('{"e":1}')
        (self.work/"data/observations/2026-09-16/b.jsonl.gz").write_text("new")
        (self.work/"data/jam_map/v2/2026-09-16.csv").write_text("j\nk\n")
        with patch.object(shift, "REPO_DIR", self.work):
            self.assertTrue(shift.commit_and_push(include_events=False))
        self.assertEqual(self.committed_paths(), {"data/scores/2026-09.csv"})
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), self.git(self.remote, "rev-parse", "main"))
        with patch.object(shift, "REPO_DIR", self.work):
            self.assertTrue(shift.commit_and_push(include_events=True, drop_archive=True))
        committed = self.committed_paths()
        self.assertIn("data/events/2026-09.json", committed)
        self.assertIn("data/observations/2026-09-16/a.jsonl.gz", committed, "removed from index")
        self.assertEqual(self.git(self.work, "ls-files", "data/observations", "data/snapshots", "data/jam_map/v2"), "")
        self.assertTrue((self.work/"data/observations/2026-09-16/a.jsonl.gz").exists(), "kept on disk")
        self.assertTrue((self.work/"data/observations/2026-09-16/b.jsonl.gz").exists())

    def test_missing_live_path_and_dirty_registry_do_not_break_publication(self):
        self.git(self.work, "rm", "-r", "-q", "data/events")
        self.git(self.work, "commit", "-m", "no registry")
        self.git(self.work, "push")
        (self.work/"data/scores/2026-09.csv").write_text("h\n2\n")
        (self.work/"data/jam_map/ways.json").write_text('{"dirty": true}')   # tracked, will stay unstaged
        with patch.object(shift, "REPO_DIR", self.work), patch.object(publish, "LIVE_PATHS", ("data/scores", "data/events")), \
             patch.object(shift, "LIVE_PATHS", ("data/scores", "data/events")):
            self.assertTrue(shift.commit_and_push())
        self.assertEqual(self.committed_paths(), {"data/scores/2026-09.csv"})
        self.assertIn("ways.json", self.git(self.work, "status", "--porcelain"))


class ShiftIntegrationTests(unittest.TestCase):
    def test_shift_ships_before_pushing_and_skips_publisher_without_git(self):
        calls = []

        class FakePublisher:
            def __init__(self, data_dir, releases=None, run_id=None):
                calls.append("init")
            def ship(self, now_utc=None):
                calls.append("ship")
                return True
            def fully_shipped(self):
                return False
            def maybe_consolidate(self, now_utc=None):
                calls.append("consolidate")
            def close(self):
                calls.append("close")

        def fake_push(*, include_events, drop_archive):
            calls.append(("push", include_events, drop_archive))
            return True

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)/"data"
            data.mkdir()
            with patch.object(sources, "fetch_yandex_score", return_value={"score": 1, "ts": int(datetime.now().timestamp())}), \
                 patch.object(sources, "fetch_dgis_score", return_value={"score": 1, "ts": int(datetime.now().timestamp())}), \
                 patch.object(sources, "fetch_dgis_layer", return_value=sources.EventList([])), \
                 patch.object(publish, "Publisher", FakePublisher), patch.object(shift, "commit_and_push", fake_push):
                self.assertEqual(shift.run_shift(1, data, once=True, git_enabled=True), 0)
                self.assertEqual(calls, ["init", "ship", ("push", True, False), "close"])
                calls.clear()
                self.assertEqual(shift.run_shift(1, data, once=True), 0)
                self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()

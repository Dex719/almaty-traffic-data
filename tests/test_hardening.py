"""Offline regression tests: no live provider calls or production Git writes."""
import csv
import gzip
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from PIL import Image
from collector import jammap, journal, ops, shift, sources, store

NOW = datetime(2026, 9, 5, 9, tzinfo=timezone.utc)
EVENT = {"id": "same", "key": "user:same", "layer": "user", "type": "crash",
         "lat": 43.25, "lon": 76.9}


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)/"data"
        self.data.mkdir()

    def test_zero_is_not_missing(self):
        row = store.append_score_row(self.data, NOW, {"score": 0, "jam_length_m": 0}, None, None)
        self.assertEqual(row["yandex_jam_km"], 0.0)

    def test_timestamps_preserve_subminute_observations(self):
        a = store.append_score_row(self.data, NOW, None, None, None)
        b = store.append_score_row(self.data, NOW+timedelta(seconds=45), None, None, None)
        self.assertNotEqual(a["ts_utc"], b["ts_utc"])
        self.assertEqual(datetime.fromisoformat(a["ts_utc"]), NOW)

    def test_legacy_migration_preserves_first_seen(self):
        store.update_event_registry(self.data, NOW-timedelta(days=1), [{k:v for k,v in EVENT.items() if k!='key'}])
        store.update_event_registry(self.data, NOW, [EVENT], monthly=True)
        old = json.loads((self.data/"events.json").read_text())["same"]
        new = json.loads((self.data/"events/2026-09.json").read_text())["user:same"]
        self.assertEqual(old["first_seen"], new["first_seen"])
        self.assertNotEqual(old["last_seen"], new["last_seen"])

    def test_layer_namespaces_do_not_collide(self):
        other = dict(EVENT, key="2gis:same", layer="2gis")
        store.update_event_registry(self.data, NOW, [EVENT, other], monthly=True)
        registry = json.loads((self.data/"events/2026-09.json").read_text())
        self.assertEqual(set(registry), {"user:same", "2gis:same"})

    def test_mutable_registry_is_not_retained_in_cache(self):
        store.update_event_registry(self.data, NOW-timedelta(days=1), [{k: v for k, v in EVENT.items() if k != "key"}])
        store._read_cached.cache_clear()
        events = []
        for i in range(4):   # every write changes the monthly file: a cached copy could never be hit again
            events.append(dict(EVENT, id=f"e{i}", key=f"user:e{i}"))
            store.update_event_registry(self.data, NOW+timedelta(minutes=5*i), events, monthly=True)
        self.assertLessEqual(store._read_cached.cache_info().currsize, 1, "only the frozen legacy registry")

    def test_frozen_registries_are_not_mutated_through_the_cache(self):
        store.update_event_registry(self.data, NOW-timedelta(days=1), [{k: v for k, v in EVENT.items() if k != "key"}])
        before = json.loads((self.data/"events.json").read_text())
        store.update_event_registry(self.data, NOW, [dict(EVENT, comment="new", segment=[[76.9, 43.25]])], monthly=True)
        store.update_event_registry(self.data, NOW+timedelta(minutes=5), [dict(EVENT, comment="newer")], monthly=True)
        self.assertEqual(store.read_frozen_json(self.data/"events.json"), before)
        self.assertEqual(json.loads((self.data/"events/2026-09.json").read_text())["user:same"]["comment"], "newer")

    def test_snapshot_deduplicates_and_marks_partial(self):
        path = store.append_snapshot(self.data, NOW, [EVENT, EVENT], complete=False, layers=["user"])
        row = json.loads(path.read_text())
        self.assertEqual(row["event_ids"], ["user:same"])
        self.assertFalse(row["complete"])

    def test_atomic_registry_retains_previous_file(self):
        path = self.data/"registry.json"
        store.atomic_json(path, {"old": True})
        with patch.object(store.os, "replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                store.atomic_json(path, {"new": True})
        self.assertEqual(json.loads(path.read_text()), {"old": True})
        self.assertFalse(list(self.data.glob(".tmp-*")))

    def test_bad_event_does_not_drop_valid_event(self):
        raw = {"id": "valid", "type": "crash", "location": {"type": "Point", "coordinates": [76.9,43.25]}}
        malformed = {"id": "bad", "type": "restriction", "location": {"type": "MultiPoint", "coordinates": [[]]}}
        response = SimpleNamespace(json=lambda **kwargs: [malformed, raw])
        with patch.object(sources, "_get", return_value=response):
            result = sources.fetch_dgis_layer("user")
        self.assertEqual(len(result), 1)
        self.assertEqual(result.invalid_rows, 1)
        self.assertEqual(result[0]["key"], "user:valid")

    def test_invalid_container_is_quarantined(self):
        raw = {"id":"bad", "type":"crash", "location": [1,2]}
        with patch.object(sources, "_get", return_value=SimpleNamespace(json=lambda **kwargs: [raw])):
            with self.assertRaises(ValueError):
                sources.fetch_dgis_layer("user")

    def test_segment_crossing_aoi_is_recognized(self):
        self.assertTrue(sources.intersects_aoi([[76.5,43.25],[77.5,43.25]]))
        self.assertFalse(sources.intersects_aoi([[76.5,44.0],[77.5,44.0]]))

    def test_out_of_area_event_is_tagged_not_deleted(self):
        raw = {"id":"far", "type":"crash", "location":{"type":"Point","coordinates":[82.0,46.0]}}
        event = sources._normalize_event(raw, "user")
        self.assertFalse(event["in_aoi"])

    def test_retry_after_is_honored(self):
        error = RuntimeError("rate limited")
        error.response = SimpleNamespace(headers={"Retry-After":"900"}, status_code=429)
        guard = shift.SourceGuard("source")
        guard.fail(100, error)
        self.assertFalse(guard.ready(999))
        self.assertTrue(guard.ready(1000))

    def test_schedule_skips_missed_slots(self):
        self.assertEqual(shift.advance_due(0, 125, 60), 180)
        self.assertEqual(shift.advance_due(60, 60, 60), 120)

    def test_neighbor_tile_pixel_is_detected(self):
        left = Image.new("RGBA", (256,256), (0,0,0,0))
        right = left.copy()
        right.putpixel((0,128), (80,200,90,255))
        self.assertEqual(jammap.sample_pixel({(10,20):left,(11,20):right}, 10*256+255,20*256+128), "G")

    def test_corrupt_png_isolated_from_good_tile(self):
        buf = io.BytesIO()
        Image.new("RGBA", (256,256), (80,200,90,255)).save(buf, format="PNG")
        class Client:
            def get(self, url, **kwargs):
                content = b"\x89PNG\r\n\x1a\ninvalid" if "x=0&" in url else buf.getvalue()
                return SimpleNamespace(status_code=200, content=content)
        tiles = jammap.fetch_tiles(Client(), grid=[(0,0),(1,0)], min_interval=0, workers=1)
        self.assertEqual(set(tiles), {(1,0)})

    def test_registry_cache_and_version(self):
        path = self.data/"jam_map/ways.json"
        store.atomic_json(path, {"ids":[1],"highway":["primary"],"polylines":[[[43.25,76.9],[43.25,76.91]]]})
        jammap._prepare.cache_clear()
        tile = Image.new("RGBA", (256,256), (80,200,90,255))
        with patch.object(jammap, "fetch_tiles", side_effect=lambda **kwargs:{xy:tile for xy in kwargs["grid"]}):
            first = jammap.capture(self.data, NOW)
            second = jammap.capture(self.data, NOW+timedelta(minutes=5))
        self.assertEqual(first["registry_version"], second["registry_version"])
        self.assertEqual(jammap._prepare.cache_info().hits, 1)
        self.assertEqual(first["classes"], "G")
        self.assertEqual(first["matched_fraction"], [1.0])
        saved = self.data/"jam_map/registries"/f'{first["registry_version"]}.json'
        import hashlib
        self.assertEqual(hashlib.sha256(saved.read_bytes()).hexdigest(), first["registry_version"])

    def test_journal_idempotence_and_reopen(self):
        log = journal.Journal(self.data)
        log.record("dgis", NOW, {"score":1}, observation_id="stable")
        log.record("dgis", NOW, {"score":1}, observation_id="stable")
        with self.assertRaises(ValueError):
            log.record("dgis", NOW, {"score":2}, observation_id="stable")
        log.close()
        log = journal.Journal(self.data)
        self.addCleanup(log.close)
        self.assertEqual(log.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 1)

    def test_outbox_crash_replays_stable_batch_with_new_observations(self):
        log = journal.Journal(self.data)
        self.addCleanup(log.close)
        log.record("dgis", NOW, {"score":1}, observation_id="one")
        real_write = journal.atomic_bytes
        def crash_after_write(path, content):
            real_write(path, content)
            raise OSError("crash before acknowledgment")
        with patch.object(journal, "atomic_bytes", side_effect=crash_after_write):
            with self.assertRaises(OSError):
                log.export_pending()
        log.record("dgis", NOW+timedelta(minutes=1), {"score":2}, observation_id="two")
        self.assertEqual(log.export_pending(), 2)
        self.assertEqual(log.export_pending(), 0)
        ids=[]
        for file in (self.data/"observations").rglob("*.gz"):
            ids.extend(json.loads(line)["observation_id"] for line in gzip.decompress(file.read_bytes()).splitlines())
        self.assertCountEqual(ids, ["one","two"])

    def test_verified_backup_roundtrip(self):
        log = journal.Journal(self.data)
        self.addCleanup(log.close)
        log.record("dgis", NOW, {"score":1})
        path = log.backup(Path(self.temp.name)/"backup")
        restored = Path(self.temp.name)/"restore.sqlite3"
        restored.write_bytes(gzip.decompress(path.read_bytes()))
        with sqlite3.connect(restored) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 1)

    def test_health_rejects_stale_or_partial_sources(self):
        state={"dgis":{"interval":60,"status":"ok","last_success":store.utc_stamp(NOW)}}
        self.assertTrue(ops.health_report(state, NOW)["healthy"])
        self.assertFalse(ops.health_report(state, NOW+timedelta(minutes=4))["healthy"])
        state["dgis"]["status"]="partial"
        self.assertFalse(ops.health_report(state, NOW)["healthy"])

    def test_second_process_cannot_claim_data_directory(self):
        with ops.exclusive_collector(self.data):
            with self.assertRaises(RuntimeError):
                with ops.exclusive_collector(self.data):
                    pass

    def test_all_sources_failed_is_not_success(self):
        with patch.object(sources,"fetch_yandex_score",side_effect=RuntimeError), \
             patch.object(sources,"fetch_dgis_score",side_effect=RuntimeError), \
             patch.object(sources,"fetch_dgis_layer",side_effect=RuntimeError):
            code=shift.run_shift(1,self.data,once=True)
        self.assertEqual(code,1)
        self.assertTrue(list((self.data/"observations").rglob("*.gz")))

    def test_partial_layer_survives_in_monthly_registry(self):
        def layer(name):
            if name=="2gis":
                raise RuntimeError("official layer down")
            return sources.EventList([EVENT])
        with patch.object(sources,"fetch_yandex_score",return_value={"score":1,"ts":int(time.time())}), \
             patch.object(sources,"fetch_dgis_score",return_value={"score":1,"ts":int(time.time())}), \
             patch.object(sources,"fetch_dgis_layer",side_effect=layer):
            code=shift.run_shift(1,self.data,once=True)
        self.assertEqual(code,0)
        registry=next((self.data/"events").glob("*.json"))
        self.assertIn("user:same",json.loads(registry.read_text()))
        snapshot=next((self.data/"snapshots").rglob("*.jsonl"))
        self.assertFalse(json.loads(snapshot.read_text())["complete"])
        self.assertFalse((self.data/"events.json").exists())

    def test_blank_map_is_not_healthy(self):
        path = self.data/"jam_map/ways.json"
        store.atomic_json(path, {"ids":[1],"highway":["primary"],"polylines":[[[43.25,76.9],[43.25,76.91]]]})
        tile = Image.new("RGBA", (256,256), (0,0,0,0))
        with patch.object(jammap, "fetch_tiles", side_effect=lambda **kwargs:{xy:tile for xy in kwargs["grid"]}):
            frame = jammap.capture(self.data, NOW)
        self.assertEqual(frame["quality"], "low_coverage")
        self.assertEqual(frame["covered"], 0)

    def test_missing_required_backup_is_unhealthy(self):
        states={"dgis":{"interval":60,"status":"ok","last_success":store.utc_stamp(NOW)}}
        with patch.dict(ops.os.environ,{"TRAFFIC_REQUIRE_BACKUP":"1"}):
            report=ops.health_report(states,NOW,data_dir=self.data)
        self.assertFalse(report["healthy"])
        self.assertIn("backup_error",report["storage"])

    def test_git_add_failure_is_not_success(self):
        result=subprocess.CompletedProcess([],1,"","failed")
        (self.data/"scores").mkdir()  # a live view must exist for "git add" to be attempted
        with patch.object(shift,"_git",return_value=result) as mock,              patch.object(shift,"REPO_DIR",self.data.parent):
            self.assertFalse(shift.commit_and_push())
            self.assertEqual(mock.call_count,1)

    def test_git_clean_index_still_pushes_pending_commit(self):
        base=Path(self.temp.name)
        remote,work=base/"remote.git",base/"work"
        def git(cwd,*args):
            return subprocess.run(["git",*args],cwd=cwd,capture_output=True,text=True,check=True).stdout.strip()
        git(base,"init","--bare","--initial-branch=main",str(remote))
        git(base,"clone",str(remote),str(work))
        git(work,"config","user.name","Offline test")
        git(work,"config","user.email","test@example.invalid")
        (work/"data").mkdir()
        file=work/"data/value.txt"
        file.write_text("initial\n")
        git(work,"add","data"); git(work,"commit","-m","initial")
        git(work,"push","--set-upstream","origin","main")
        file.write_text("initial\nunsynced\n")
        git(work,"add","data"); git(work,"commit","-m","pending")
        with patch.object(shift,"REPO_DIR",work):
            self.assertTrue(shift.commit_and_push())
        self.assertEqual(git(work,"rev-parse","HEAD"),git(base,"--git-dir="+str(remote),"rev-parse","main"))

    def test_network_git_gets_longer_timeout_than_local_git(self):
        (self.data/"scores").mkdir()
        timeouts={}
        def fake_git(*args,timeout=None):
            timeouts[args[0]]=timeout
            return subprocess.CompletedProcess(args,1 if args[:2]==("diff","--cached") else 0,"0\n","")
        with patch.object(shift,"_git",side_effect=fake_git),patch.object(shift,"REPO_DIR",self.data.parent):
            self.assertTrue(shift.commit_and_push())
        self.assertGreater(timeouts["pull"],shift.GIT_TIMEOUT_SEC)   # a fresh shallow runner clone needs >20 s at first
        self.assertGreater(timeouts["push"],shift.GIT_TIMEOUT_SEC)
        self.assertEqual(timeouts["commit"],shift.GIT_TIMEOUT_SEC)

    def test_failed_network_git_is_logged_with_reason_and_scrubbed(self):
        (self.data/"scores").mkdir()
        def fake_git(*args,timeout=None):
            if args[0]=="push":
                return subprocess.CompletedProcess(args,128,"","fatal: unable to access 'https://x-access-token:ghs_secret0123456789@github.com/r.git/'")
            return subprocess.CompletedProcess(args,1 if args[:2]==("diff","--cached") else 0,"","")
        with patch.object(shift,"_git",side_effect=fake_git),patch.object(shift,"REPO_DIR",self.data.parent), \
             patch.object(shift.time,"sleep"),self.assertLogs("collector.shift","WARNING") as logs:
            self.assertFalse(shift.commit_and_push())
        text="\n".join(logs.output)
        self.assertIn("git push failed (128)",text)
        self.assertIn("unable to access",text)
        self.assertNotIn("ghs_secret",text)

    def test_missing_tile_is_retried_once(self):
        buf = io.BytesIO()
        Image.new("RGBA", (256,256), (80,200,90,255)).save(buf, format="PNG")
        calls = {}
        class Client:
            def get(self, url, **kwargs):
                x = url.split("&x=")[1].split("&")[0]
                calls[x] = calls.get(x, 0)+1
                if x == "0" and calls[x] == 1:
                    return SimpleNamespace(status_code=500, content=b"")
                return SimpleNamespace(status_code=200, content=buf.getvalue())
        tiles = jammap.fetch_tiles(Client(), grid=[(0,0),(1,0)], min_interval=0, workers=1)
        self.assertEqual(set(tiles), {(0,0),(1,0)})
        self.assertEqual(calls["0"], 2)

    def test_partial_map_with_high_coverage_is_healthy(self):
        state = {"jammap": {"interval": 300, "status": "partial", "coverage_ratio": 0.99,
                            "last_success": store.utc_stamp(NOW)}}
        self.assertTrue(ops.health_report(state, NOW)["healthy"])
        state["jammap"]["coverage_ratio"] = 0.8
        self.assertFalse(ops.health_report(state, NOW)["healthy"])
        events = {"events_user": {"interval": 300, "status": "partial", "last_success": store.utc_stamp(NOW)}}
        self.assertFalse(ops.health_report(events, NOW)["healthy"])

    def test_atomic_write_skips_directory_fsync_only_on_windows(self):
        real_open = os.open
        def guard(path, *args, **kwargs):
            if os.path.isdir(path):
                raise PermissionError("directories cannot be opened on Windows")
            return real_open(path, *args, **kwargs)
        with patch.object(store.os, "name", "nt"), patch.object(store.os, "open", guard):
            store.atomic_json(self.data/"x.json", {"ok": True})
            journal.atomic_bytes(self.data/"y.bin", b"ok")
        self.assertEqual(json.loads((self.data/"x.json").read_text()), {"ok": True})
        self.assertEqual((self.data/"y.bin").read_bytes(), b"ok")
        with patch.object(store.os, "name", "posix"), patch.object(store.os, "open", guard):
            with self.assertRaises(PermissionError):
                store.atomic_json(self.data/"z.json", {})


if __name__ == "__main__":
    unittest.main()


class SilentFailureTests(unittest.TestCase):
    """collector-silent-failures-bugfix: a dead source must be visible, poison input must stay local."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)/"data"
        self.data.mkdir()
        self.now = int(time.time())

    def test_source_failure_is_logged_once_and_dead_source_annotated(self):
        error = httpx.HTTPStatusError("forbidden", request=httpx.Request("GET", "https://example.test/"),
                                      response=httpx.Response(403, headers={"Retry-After": "120"}))
        out = io.StringIO()
        with patch.object(sources, "fetch_yandex_score", side_effect=error), \
             patch.object(sources, "fetch_dgis_score", return_value={"score": 1, "ts": self.now}), \
             patch.object(sources, "fetch_dgis_layer", return_value=sources.EventList([EVENT])), \
             patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), redirect_stdout(out), \
             self.assertLogs("collector.shift", level="WARNING") as logs:
            code = shift.run_shift(1, self.data, once=True)
        self.assertEqual(code, 0)  # the other sources worked; the run page carries the warning
        failures = [line for line in logs.output if "source yandex failed" in line]
        self.assertEqual(len(failures), 1)
        self.assertIn("HTTP 403", failures[0])
        self.assertIn("Retry-After 120", failures[0])
        self.assertIn("cooldown", failures[0])
        annotations = [line for line in out.getvalue().splitlines() if line.startswith("::warning::")]
        self.assertEqual(len(annotations), 1)
        self.assertIn("yandex", annotations[0])
        self.assertNotIn("dgis", annotations[0])

    def test_healthy_shift_has_no_warnings(self):
        out = io.StringIO()
        with patch.object(sources, "fetch_yandex_score", return_value={"score": 2, "ts": self.now}), \
             patch.object(sources, "fetch_dgis_score", return_value={"score": 1, "ts": self.now}), \
             patch.object(sources, "fetch_dgis_layer", return_value=sources.EventList([EVENT])), \
             patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), redirect_stdout(out):
            with self.assertLogs("collector.shift", level="INFO") as logs:
                self.assertEqual(shift.run_shift(1, self.data, once=True), 0)
        warnings = [line for line in logs.output if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 1)
        self.assertIn("jam map disabled", warnings[0])   # no ways.json in this test
        self.assertEqual(out.getvalue(), "")

    def test_poison_payload_is_only_that_sources_failure(self):
        with patch.object(sources, "fetch_yandex_score", return_value={"score": 3, "ts": self.now}), \
             patch.object(sources, "fetch_dgis_score", return_value={"score": float("nan"), "ts": self.now}), \
             patch.object(sources, "fetch_dgis_layer", return_value=sources.EventList([EVENT])):
            code = shift.run_shift(1, self.data, once=True)
        self.assertEqual(code, 0)
        rows = list(csv.DictReader(io.StringIO(next((self.data/"scores").glob("*.csv")).read_text(encoding="utf-8"))))
        self.assertEqual((rows[0]["yandex_score"], rows[0]["dgis_score"]), ("3", ""))
        db = sqlite3.connect(self.data/".state/journal.sqlite3")
        try:
            documents = [json.loads(row[0]) for row in db.execute("SELECT document FROM observations WHERE source='dgis'")]
        finally:
            db.close()
        self.assertEqual([(d["status"], d["error_code"]) for d in documents], [("error", "ValueError")])
        exported = [json.loads(line) for path in (self.data/"observations").rglob("*.gz")
                    for line in gzip.decompress(path.read_bytes()).decode().splitlines()]
        self.assertTrue(any(d["source"] == "yandex" and d["status"] == "ok" for d in exported))

    def test_empty_layer_response_is_an_error(self):
        with patch.object(sources, "_get", return_value=SimpleNamespace(json=lambda **kwargs: [])):
            with self.assertRaises(ValueError):
                sources.fetch_dgis_layer("user")

    def test_non_finite_json_constants_are_rejected_inside_the_source(self):
        events = ('[{"id":"a","type":"crash","location":{"type":"Point","coordinates":[76.9,43.25]},'
                  '"feedbacks":{"likes":NaN}}]')
        meta = '[{"id":67,"score":Infinity,"time":1}]'
        for text, call in ((events, lambda: sources.fetch_dgis_layer("user")), (meta, sources.fetch_dgis_score)):
            fake = SimpleNamespace(json=lambda t=text, **kwargs: json.loads(t, **kwargs))
            with patch.object(sources, "_get", return_value=fake):
                with self.assertRaises(ValueError):
                    call()

    def test_error_description_never_carries_a_response_body(self):
        error = httpx.HTTPStatusError("boom", request=httpx.Request("GET", "https://example.test/"),
                                      response=httpx.Response(500, text="<html>driver comment here</html>"))
        self.assertEqual(shift.describe_error(error), "HTTPStatusError, HTTP 500")
        leaked = shift.describe_error(ValueError("token ghp_abcdefghijklmnop123 leaked"))
        self.assertIn("ValueError", leaked)
        self.assertNotIn("ghp_abcdef", leaked)


class ChainAndTilesTests(unittest.TestCase):
    """collector-chain-and-tiles-bugfix: a blocked tile endpoint must reach the guard with its status."""

    def test_blocked_tiles_stop_acquisition_and_reach_the_guard(self):
        calls = []
        class Client:
            def get(self, url, **kwargs):
                calls.append(url)
                return httpx.Response(403, request=httpx.Request("GET", url))
        with self.assertRaises(httpx.HTTPStatusError) as ctx:
            jammap.fetch_tiles(Client(), grid=[(0, 0), (1, 0), (2, 0)], min_interval=0, workers=1)
        self.assertEqual(ctx.exception.response.status_code, 403)
        self.assertLessEqual(len(calls), 3)  # no second pass over a blocked endpoint
        guard = shift.SourceGuard("jammap")
        guard.fail(100, ctx.exception)
        self.assertFalse(guard.ready(100+3599))
        self.assertTrue(guard.ready(100+3600))

    def test_no_retry_pass_when_nothing_arrived(self):
        calls = []
        class Client:
            def get(self, url, **kwargs):
                calls.append(url)
                return SimpleNamespace(status_code=200, content=b"<html>captcha</html>")
        tiles = jammap.fetch_tiles(Client(), grid=[(0, 0), (1, 0)], min_interval=0, workers=1)
        self.assertEqual(tiles, {})
        self.assertEqual(len(calls), 2)


class SecondTierTests(unittest.TestCase):
    """collector-audit-second-tier: stale values, final steps, backups, registry commits."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)/"data"
        self.data.mkdir()
        self.now = int(time.time())

    def test_stale_score_stays_out_of_the_live_csv(self):
        with patch.object(sources, "fetch_yandex_score", return_value={"score": 4, "ts": self.now}), \
             patch.object(sources, "fetch_dgis_score", return_value={"score": 9, "ts": self.now-3600}), \
             patch.object(sources, "fetch_dgis_layer", return_value=sources.EventList([EVENT])):
            self.assertEqual(shift.run_shift(1, self.data, once=True), 0)
        rows = list(csv.DictReader(io.StringIO(next((self.data/"scores").glob("*.csv")).read_text(encoding="utf-8"))))
        self.assertEqual((rows[0]["yandex_score"], rows[0]["dgis_score"]), ("4", ""))
        db = sqlite3.connect(self.data/".state/journal.sqlite3")
        try:
            statuses = [row[0] for row in db.execute("SELECT status FROM observations WHERE source='dgis'")]
        finally:
            db.close()
        self.assertEqual(statuses, ["stale"])

    def test_failing_map_write_does_not_skip_export_or_health(self):
        store.atomic_json(self.data/"jam_map/ways.json",
                          {"ids": [1], "highway": ["primary"], "polylines": [[[43.25, 76.9], [43.25, 76.91]]]})
        jammap._prepare.cache_clear()
        tile = Image.new("RGBA", (256, 256), (80, 200, 90, 255))
        with patch.object(jammap, "fetch_tiles", side_effect=lambda **kwargs: {xy: tile for xy in kwargs["grid"]}), \
             patch.object(jammap, "append_frame", side_effect=OSError("disk full")), \
             patch.object(sources, "fetch_yandex_score", return_value={"score": 4, "ts": self.now}), \
             patch.object(sources, "fetch_dgis_score", return_value={"score": 1, "ts": self.now}), \
             patch.object(sources, "fetch_dgis_layer", return_value=sources.EventList([EVENT])):
            code = shift.run_shift(1, self.data, once=True)
        self.assertEqual(code, 1)   # the map write failed: fatal, but not before saving everything else
        self.assertTrue(list((self.data/"observations").rglob("*.gz")), "final export still ran")
        self.assertTrue((self.data/".state/health.json").exists(), "health report still written")
        db = sqlite3.connect(self.data/".state/journal.sqlite3")
        try:
            frames = db.execute("SELECT COUNT(*) FROM observations WHERE source='jammap'").fetchone()[0]
        finally:
            db.close()
        self.assertEqual(frames, 1, "the frame is journaled once, not replayed in the finally")

    def test_backup_is_streamed_and_pruned(self):
        log = journal.Journal(self.data)
        self.addCleanup(log.close)
        log.record("dgis", NOW, {"score": 1})
        dest = Path(self.temp.name)/"backup"
        dest.mkdir()
        for stamp in ("20260101T000000Z", "20260102T000000Z"):
            (dest/f"journal-{stamp}-old.sqlite3.gz").write_bytes(b"old")
            (dest/f"journal-{stamp}-old.sqlite3.gz.json").write_text("{}")
        with patch.object(journal.gzip, "compress", side_effect=AssertionError("must stream, not buffer")):
            path = log.backup(dest, keep=2)
        manifest = json.loads(path.with_suffix(path.suffix + ".json").read_text())
        self.assertEqual(manifest["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        restored = Path(self.temp.name)/"restore.sqlite3"
        restored.write_bytes(gzip.decompress(path.read_bytes()))
        with sqlite3.connect(restored) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 1)
        names = sorted(p.name for p in dest.glob("journal-*"))
        self.assertNotIn("journal-20260101T000000Z-old.sqlite3.gz", names)
        self.assertNotIn("journal-20260101T000000Z-old.sqlite3.gz.json", names)
        self.assertIn("journal-20260102T000000Z-old.sqlite3.gz", names)
        self.assertEqual(len([n for n in names if n.endswith(".gz")]), 2)
        self.assertEqual(journal.prune_backups(dest, 0), [])

"""Оффлайн-тесты: разбор ответов источников и запись в хранилище."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from collector import sources, store

YANDEX_XML = """<?xml version="1.0" encoding="utf-8"?>
<info lang="ru">
  <traffic region="162">
    <region id="162">
      <length>146883</length>
      <level>7</level>
      <timestamp>1788180660</timestamp>
      <tend>0</tend>
    </region>
  </traffic>
</info>
"""


class FakeResponse:
    def __init__(self, text: str = "", payload=None):
        self.text = text
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def test_yandex_score_parses_xml(monkeypatch):
    monkeypatch.setattr(sources, "_get", lambda url, params=None: FakeResponse(YANDEX_XML))
    out = sources.fetch_yandex_score()
    assert out == {"score": 7, "trend": 0, "jam_length_m": 146883, "ts": 1788180660}


def test_dgis_score_picks_region(monkeypatch):
    payload = [{"time": 1788180360, "score": 8, "id": 67}]
    monkeypatch.setattr(sources, "_get", lambda url, params=None: FakeResponse(payload=payload))
    assert sources.fetch_dgis_score() == {"score": 8, "ts": 1788180360}


def test_events_normalized_and_cameras_dropped(monkeypatch):
    payload = [
        {
            "id": "aaa",
            "type": "crash",
            "timestamp": 100,
            "location": {"type": "Point", "coordinates": [76.87, 43.22]},
            "feedbacks": {"likes": 2},
        },
        {
            "id": "bbb",
            "type": "restriction",
            "timestamp": 0,
            "location": {
                "type": "MultiPoint",
                "coordinates": [[76.78, 43.34], [76.79, 43.35]],
            },
            "data": {"start_time": 1, "finish_time": 2},
        },
        {
            "id": "cam",
            "type": "camera",
            "location": {"type": "Point", "coordinates": [76.8, 43.2]},
        },
    ]
    monkeypatch.setattr(sources, "_get", lambda url, params=None: FakeResponse(payload=payload))
    events = sources.fetch_dgis_events()
    # два слоя (user + 2gis) с одинаковым фейком: 2 события × 2 слоя
    assert len(events) == 4
    crash = events[0]
    assert (crash["lat"], crash["lon"]) == (43.22, 76.87)
    assert crash["likes"] == 2
    restriction = events[1]
    assert restriction["lat"] == 43.34 and restriction["segment"] is not None
    assert restriction["start_ts"] == 1 and restriction["finish_ts"] == 2
    assert all(e["type"] != "camera" for e in events)


NOW = datetime(2026, 8, 31, 13, 0, tzinfo=timezone.utc)
EVENT = {
    "id": "aaa",
    "layer": "user",
    "type": "crash",
    "lat": 43.22,
    "lon": 76.87,
    "created_ts": 100,
    "comment": None,
    "likes": 0,
    "start_ts": None,
    "finish_ts": None,
    "segment": None,
}


def test_score_row_written(tmp_path: Path):
    yandex = {"score": 7, "trend": 1, "jam_length_m": 146883, "ts": 1}
    row = store.append_score_row(tmp_path, NOW, yandex, {"score": 8}, [EVENT])
    assert row["ts_almaty"] == "2026-08-31T18:00"
    assert row["yandex_jam_km"] == 146.9
    assert row["ev_crash"] == 1
    with (tmp_path / "scores.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["dgis_score"] == "8"


def test_score_row_survives_dead_sources(tmp_path: Path):
    row = store.append_score_row(tmp_path, NOW, None, None, None)
    assert row["yandex_score"] == "" and row["ev_crash"] == ""


def test_registry_tracks_lifetime(tmp_path: Path):
    store.update_event_registry(tmp_path, NOW, [EVENT])
    later = NOW + timedelta(minutes=30)
    stats = store.update_event_registry(tmp_path, later, [EVENT])
    assert stats == {"new": 0, "active": 1, "total": 1}
    # событие пропало с карты — карточка остаётся с прежним last_seen
    stats = store.update_event_registry(tmp_path, later + timedelta(minutes=30), [])
    assert stats["total"] == 1
    registry = json.loads((tmp_path / "events.json").read_text())
    assert registry["aaa"]["first_seen"] == "2026-08-31T13:00"
    assert registry["aaa"]["last_seen"] == "2026-08-31T13:30"


def test_snapshot_appends(tmp_path: Path):
    path = store.append_snapshot(tmp_path, NOW, [EVENT])
    line = json.loads(path.read_text().splitlines()[0])
    assert line["event_ids"] == ["aaa"]
    assert path.name == "31.jsonl"  # 18:00 алматинского — ещё 31 августа


if __name__ == "__main__":
    pytest.main([__file__, "-q"])

"""Источники данных о дорожной обстановке Алматы.

Все каналы — публичные endpoints без токенов:

* Яндекс: ``export.yandex.ru/bar/reginfo.xml?region=162`` — балл пробок,
  тренд (растут/спадают) и суммарная длина затруднений в метрах.
* 2ГИС балл: ``jam.api.2gis.com/meta?reg=67`` (67 = Алматы).
* 2ГИС события: ``tugc.2gis.com/1.0/layers/{user,2gis}`` — ДТП, ремонты,
  перекрытия, комментарии водителей (user) и официальные ограничения (2gis).

Камеры из официального слоя не логируем: это статичный справочник
(3 000+ точек), а не события.
"""

from __future__ import annotations

import logging
import math
import threading
import xml.etree.ElementTree as ET
from typing import Any

import httpx

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "ru,en;q=0.9"}
TIMEOUT = 20.0
_CLIENT = None
_CLIENT_LOCK = threading.Lock()
AOI = (43.165, 43.375, 76.72, 77.06)

YANDEX_REGION = 162  # Алматы в справочнике Яндекса
DGIS_REGION = 67  # Алматы в справочнике 2ГИС
DGIS_PROJECT = "almaty"

YANDEX_BAR_URL = "https://export.yandex.ru/bar/reginfo.xml"
DGIS_META_URL = "https://jam.api.2gis.com/meta"
DGIS_TUGC_URL = "https://tugc.2gis.com/1.0/layers/{layer}"

# Типы событий, которые просим у tugc. camera намеренно исключена из
# пользовательского слоя тоже: жалобы на камеры — не дорожные события.
EVENT_TYPES = '["crash","roadwork","restriction","comment","other"]'


def _get(url: str, params: dict | None = None) -> httpx.Response:
    resp = get_client().get(url, params=params)
    resp.raise_for_status()
    return resp


def fetch_yandex_score() -> dict[str, Any]:
    """Балл Яндекс.Пробок по Алматы.

    Возвращает ``{"score": int, "trend": int, "jam_length_m": int | None,
    "ts": int}``. trend: -1 спадают, 0 стабильно, 1 растут (поле <tend>).
    """
    resp = _get(YANDEX_BAR_URL, {"region": YANDEX_REGION})
    root = ET.fromstring(resp.text)
    region = root.find(".//traffic/region")
    if region is None:
        raise ValueError("reginfo.xml: нет блока traffic/region")

    def _int(tag: str) -> int | None:
        node = region.find(tag)
        if node is None or node.text is None:
            return None
        try:
            return int(node.text)
        except ValueError:
            return None

    score = _int("level")
    if score is None:
        raise ValueError("reginfo.xml: нет уровня пробок <level>")
    return {
        "score": score,
        "trend": _int("tend"),
        "jam_length_m": _int("length"),
        "ts": _int("timestamp"),
    }


def fetch_dgis_score() -> dict[str, Any]:
    """Балл пробок 2ГИС по Алматы: ``{"score": int, "ts": int}``."""
    resp = _get(DGIS_META_URL, {"reg": DGIS_REGION, "time": "", "score": ""})
    payload = resp.json()
    entry = next((row for row in payload if row.get("id") == DGIS_REGION), None)
    if entry is None or "score" not in entry:
        raise ValueError(f"jam meta: нет региона {DGIS_REGION} в ответе {payload!r}")
    return {"score": int(entry["score"]), "ts": entry.get("time")}


def _normalize_event(raw: dict, layer: str) -> dict[str, Any] | None:
    """Приводит событие tugc к плоской строке лога. None — пропустить."""
    if not isinstance(raw, dict):
        raise ValueError("event is not an object")
    if raw.get("id") is None:
        raise ValueError("missing event id")
    etype = raw.get("type")
    if not isinstance(etype, str):
        raise ValueError("invalid event type")
    if etype == "camera":
        return None
    loc = raw.get("location") or {}
    coords = loc.get("coordinates")
    if not coords:
        return None
    # У restriction бывает MultiPoint (участок) — берём первую точку,
    # полную геометрию сохраняем отдельно.
    if loc.get("type") == "MultiPoint":
        lon, lat = coords[0][0], coords[0][1]
        segment = coords
    else:
        lon, lat = coords[0], coords[1]
        segment = None
    data = raw.get("data") or {}
    event = {
        "id": str(raw["id"]),
        "key": f"{layer}:{raw['id']}",
        "layer": layer,  # user — сообщения водителей, 2gis — официальный слой
        "type": etype,
        "lat": round(float(lat), 6),
        "lon": round(float(lon), 6),
        "created_ts": raw.get("timestamp") or None,
        "comment": (raw.get("comment") or "").strip() or None,
        "likes": (raw.get("feedbacks") or {}).get("likes"),
        "start_ts": data.get("start_time"),
        "finish_ts": data.get("finish_time"),
        "segment": segment,
    }
    if not event["id"] or not etype:
        raise ValueError("missing event id/type")
    if not all(math.isfinite(v) for v in (event["lat"], event["lon"])):
        raise ValueError("non-finite event coordinates")
    if not (-90 <= event["lat"] <= 90 and -180 <= event["lon"] <= 180):
        raise ValueError("invalid event coordinates")
    event["in_aoi"] = intersects_aoi(segment or [[event["lon"], event["lat"]]])
    return event


class EventList(list):
    """List-compatible result with explicit parser-loss metadata."""
    def __init__(self, events, invalid_rows=0):
        super().__init__(events)
        self.invalid_rows = invalid_rows


def fetch_dgis_layer(layer: str) -> list[dict[str, Any]]:
    """One independently scheduled/failing layer. Invalid rows are quarantined."""
    if layer not in ("user", "2gis"):
        raise ValueError("unknown layer")
    resp = _get(DGIS_TUGC_URL.format(layer=layer),
                {"project": DGIS_PROJECT, "layers": EVENT_TYPES})
    payload = resp.json()
    if not isinstance(payload, list):
        raise ValueError("event response is not a list")
    events = {}
    invalid_rows = 0
    for raw in payload:
        try:
            event = _normalize_event(raw, layer)
        except (KeyError, TypeError, ValueError, IndexError, OverflowError, AttributeError):
            # Do not log driver comments or the entire response.
            invalid_rows += 1
            logger.warning("invalid event skipped in layer %s", layer)
            continue
        if event is not None:
            events[event["key"]] = event
        elif raw.get("type") != "camera":
            invalid_rows += 1
    if payload and not events and invalid_rows:
        raise ValueError("no valid non-camera events in non-empty response")
    return EventList(events.values(), invalid_rows)


def fetch_dgis_events() -> list[dict[str, Any]]:
    """Compatibility API. Strict completeness; the scheduler uses separate layers."""
    return fetch_dgis_layer("user") + fetch_dgis_layer("2gis")


def get_client():
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = httpx.Client(headers=HEADERS, timeout=TIMEOUT,
                                   limits=httpx.Limits(max_connections=8, max_keepalive_connections=8))
        return _CLIENT


def close_client() -> None:
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            _CLIENT.close()
            _CLIENT = None


def intersects_aoi(points: list) -> bool:
    """Rectangle/segment intersection, not just the first restriction point.

    Tag observations rather than silently deleting out-of-area history.
    Coordinates follow GeoJSON order: longitude, latitude.
    """
    for point in points:
        if len(point) < 2 or not all(math.isfinite(float(v)) for v in point[:2]):
            raise ValueError("invalid segment geometry")
        if not (-180 <= float(point[0]) <= 180 and -90 <= float(point[1]) <= 90):
            raise ValueError("invalid segment coordinates")
    lat0, lat1, lon0, lon1 = AOI
    if any(lon0 <= float(x) <= lon1 and lat0 <= float(y) <= lat1 for x, y, *_ in points):
        return True
    for first, second in zip(points, points[1:]):
        x, y = map(float, first[:2])
        dx, dy = float(second[0])-x, float(second[1])-y
        lo, hi = 0.0, 1.0
        for p, q in ((-dx, x-lon0), (dx, lon1-x), (-dy, y-lat0), (dy, lat1-y)):
            if p == 0:
                if q < 0:
                    break
            elif p < 0:
                lo = max(lo, q/p)
            else:
                hi = min(hi, q/p)
        else:
            if lo <= hi:
                return True
    return False

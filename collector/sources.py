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
import xml.etree.ElementTree as ET
from typing import Any

import httpx

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "ru,en;q=0.9"}
TIMEOUT = 30.0

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
    resp = httpx.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
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
        "trend": _int("tend") or 0,
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
    etype = raw.get("type")
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
        "id": raw["id"],
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
    return event


def fetch_dgis_events() -> list[dict[str, Any]]:
    """Активные дорожные события 2ГИС: пользовательские + официальные."""
    events: list[dict[str, Any]] = []
    for layer in ("user", "2gis"):
        resp = _get(
            DGIS_TUGC_URL.format(layer=layer),
            {"project": DGIS_PROJECT, "layers": EVENT_TYPES},
        )
        for raw in resp.json():
            try:
                event = _normalize_event(raw, layer)
            except (KeyError, TypeError, ValueError):
                logger.warning("событие не разобралось: %r", raw, exc_info=True)
                continue
            if event is not None:
                events.append(event)
    return events

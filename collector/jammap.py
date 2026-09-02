"""Посегментные пробки по всей карте: тайлы Яндекса → классы улиц.

Яндекс отдаёт растровые тайлы слоя пробок без подписи и токенов:
``core-jams-rdr-cache.maps.yandex.net/1.1/tiles?trf&l=trf&x=..&y=..&z=..``
(y — в эллиптическом Меркаторе Яндекса, не в сферическом OSM).

Съёмка: сетка z14 на весь город (~230 плиток) → для каждой улицы нашего
OSM-графа сэмплируем точки каждые ~60 м, читаем цвет пикселя и голосованием
назначаем класс: G свободно · Y плотно · R пробка · D стоит · «-» нет цвета.

Хранение (git-дружелюбное): реестр геометрий ``data/jam_map/ways.json``
пишется один раз; каждый срез — одна строка в ``data/jam_map/YYYY-MM-DD.csv``:
``ts_utc,ts_almaty,covered,classes`` где classes — строка из len(ways)
символов в порядке реестра. Срез ~6 КБ, git хранит дельтами.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from collector.sources import HEADERS as BASE_HEADERS
from collector.store import ALMATY_TZ

logger = logging.getLogger(__name__)

Z = 14
BBOX = (43.165, 43.375, 76.72, 77.06)  # lat0, lat1, lon0, lon1
TILE_URL = (
    "https://core-jams-rdr-cache.maps.yandex.net/1.1/tiles"
    "?trf&l=trf&x={x}&y={y}&z={z}&scale=1&tm={tm}"
)
HEADERS = dict(BASE_HEADERS, Referer="https://yandex.kz/maps/")
_E = 0.0818191908426  # эксцентриситет WGS84
SAMPLE_STEP_M = 60
CLASS_ORDER = {"D": 3, "R": 2, "Y": 1, "G": 0}
FIELDS = ["ts_utc", "ts_almaty", "covered", "classes"]


def tile_x(lon: float) -> float:
    return (lon + 180) / 360 * 2**Z


def tile_y(lat: float) -> float:
    """Эллиптический Меркатор Яндекса (сферический даёт сдвиг ~6 плиток)."""
    la = math.radians(lat)
    esin = _E * math.sin(la)
    ts = math.tan(math.pi / 4 + la / 2) * ((1 - esin) / (1 + esin)) ** (_E / 2)
    return (1 - math.log(ts) / math.pi) / 2 * 2**Z


def tile_grid() -> list[tuple[int, int]]:
    x0, x1 = int(tile_x(BBOX[2])), int(tile_x(BBOX[3]))
    y0, y1 = int(tile_y(BBOX[1])), int(tile_y(BBOX[0]))
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def classify(rgba: tuple[int, int, int, int]) -> str | None:
    """Цвет пикселя → класс. Серый кант и прозрачность — None."""
    r, g, b, a = rgba
    if a < 200:
        return None
    if max(r, g, b) - min(r, g, b) < 30:
        return None
    if r > 180 and g > 140 and b < 110:
        return "Y"
    if g > 140 and r < 140:
        return "G"
    if r > 170 and g < 110:
        return "R"
    if 90 < r <= 170 and g < 70:
        return "D"
    return None


def fetch_tiles(client: httpx.Client | None = None) -> dict[tuple[int, int], Any]:
    """Скачивает сетку тайлов. Вернёт то, что удалось (частичная сетка — ок)."""
    from PIL import Image  # ленивый импорт: без pillow срез просто пропускается

    own = client is None
    client = client or httpx.Client(headers=HEADERS, timeout=15)
    tm = int(time.time() // 60 * 60)
    tiles: dict[tuple[int, int], Any] = {}
    try:
        for x, y in tile_grid():
            try:
                r = client.get(TILE_URL.format(x=x, y=y, z=Z, tm=tm))
                if r.status_code == 200 and r.content[:4] == b"\x89PNG":
                    tiles[(x, y)] = Image.open(io.BytesIO(r.content)).convert("RGBA")
            except httpx.HTTPError:
                pass
            time.sleep(0.04)
    finally:
        if own:
            client.close()
    return tiles


def sample_class(tiles: dict, lat: float, lon: float) -> str | None:
    """Класс в точке: спираль до 3 px вокруг — дорога рисуется линией 3–5 px."""
    fx, fy = tile_x(lon), tile_y(lat)
    xi, yi = int(fx), int(fy)
    tile = tiles.get((xi, yi))
    if tile is None:
        return None
    px, py = int((fx - xi) * 256), int((fy - yi) * 256)
    for rad in range(4):
        for dx in range(-rad, rad + 1):
            for dy in range(-rad, rad + 1):
                if max(abs(dx), abs(dy)) != rad:
                    continue
                qx, qy = px + dx, py + dy
                if 0 <= qx < 256 and 0 <= qy < 256:
                    cls = classify(tile.getpixel((qx, qy)))
                    if cls:
                        return cls
    return None


def way_points(polyline: list[list[float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for (a_lat, a_lon), (b_lat, b_lon) in zip(polyline, polyline[1:]):
        seg = math.hypot((b_lat - a_lat) * 111000, (b_lon - a_lon) * 81000)
        n = max(1, int(seg / SAMPLE_STEP_M))
        for i in range(n):
            f = i / n
            out.append((a_lat + (b_lat - a_lat) * f, a_lon + (b_lon - a_lon) * f))
    return out


def way_class(tiles: dict, polyline: list[list[float]]) -> str:
    votes: dict[str, int] = {}
    for lat, lon in way_points(polyline):
        cls = sample_class(tiles, lat, lon)
        if cls:
            votes[cls] = votes.get(cls, 0) + 1
    if not votes:
        return "-"
    total = sum(votes.values())
    # доминирующий класс; при споре >=30% голосов берём худший — пробку
    # на половине улицы важнее показать, чем свободную половину
    cand = [c for c, n in votes.items() if n / total >= 0.3]
    return max(cand, key=lambda c: CLASS_ORDER[c]) if cand else max(votes, key=votes.get)


def load_registry(data_dir: Path) -> dict[str, Any]:
    return json.loads((data_dir / "jam_map" / "ways.json").read_text())


def harvest(data_dir: Path, now_utc: datetime) -> dict[str, int]:
    """Один срез всей карты → строка в data/jam_map/YYYY-MM-DD.csv."""
    registry = load_registry(data_dir)
    tiles = fetch_tiles()
    if len(tiles) < len(tile_grid()) * 0.5:
        raise RuntimeError(f"тайлов слишком мало: {len(tiles)}/{len(tile_grid())}")
    classes = "".join(way_class(tiles, pl) for pl in registry["polylines"])
    covered = sum(1 for c in classes if c != "-")

    alm = now_utc.astimezone(ALMATY_TZ)
    path = data_dir / "jam_map" / f"{alm.strftime('%Y-%m-%d')}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    with path.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if fresh:
            writer.writeheader()
        writer.writerow(
            {
                "ts_utc": now_utc.strftime("%Y-%m-%dT%H:%M"),
                "ts_almaty": alm.strftime("%Y-%m-%dT%H:%M"),
                "covered": covered,
                "classes": classes,
            }
        )
    return {"tiles": len(tiles), "ways": len(classes), "covered": covered}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from datetime import timezone

    stats = harvest(Path(__file__).resolve().parents[1] / "data", datetime.now(timezone.utc))
    logger.info("jam_map: %s", stats)

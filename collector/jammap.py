"""Bounded raster acquisition and versioned, confidence-aware road classes.

Classes are a categorical proxy, not directional speeds. Geometry is sampled
only inside the declared AOI; old registry versions are retained immutably.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import os
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from collector.sources import HEADERS as BASE_HEADERS, AOI
from collector.store import ALMATY_TZ, atomic_json, utc_stamp

logger = logging.getLogger(__name__)
Z = 14
BBOX = AOI
TILE_URL = ("https://core-jams-rdr-cache.maps.yandex.net/1.1/tiles"
            "?trf&l=trf&x={x}&y={y}&z={z}&scale=1&tm={tm}")
HEADERS = dict(BASE_HEADERS, Referer="https://yandex.kz/maps/")
_E = 0.0818191908426
SAMPLE_STEP_M = 60
CLASS_ORDER = {"D": 3, "R": 2, "Y": 1, "G": 0}
CLASSIFIER_VERSION = "2-weighted-aoi-neighbor-tiles"
FIELDS = ["ts_utc", "ts_almaty", "covered", "classes"]
V2_FIELDS = FIELDS + ["registry_version", "classifier_version", "tiles_received",
                      "tiles_requested", "quality", "matched_fraction"]
OFFSETS = tuple((dx, dy) for radius in range(4)
                for dx in range(-radius, radius+1) for dy in range(-radius, radius+1)
                if max(abs(dx), abs(dy)) == radius)


def tile_x(lon: float) -> float:
    return (lon + 180) / 360 * 2**Z


def tile_y(lat: float) -> float:
    la = math.radians(lat)
    esin = _E * math.sin(la)
    ts = math.tan(math.pi/4 + la/2) * ((1-esin)/(1+esin)) ** (_E/2)
    return (1-math.log(ts)/math.pi)/2 * 2**Z


def tile_grid() -> list[tuple[int, int]]:
    return [(x, y) for x in range(int(tile_x(BBOX[2])), int(tile_x(BBOX[3]))+1)
            for y in range(int(tile_y(BBOX[1])), int(tile_y(BBOX[0]))+1)]


def classify(rgba) -> str | None:
    r, g, b, a = rgba
    if a < 200 or max(r, g, b)-min(r, g, b) < 30:
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


def fetch_tiles(client=None, *, grid=None, workers=4, budget=120.0,
                min_interval=0.2) -> dict[tuple[int, int], Any]:
    """Shared rate limit and acquisition budget; drain in-flight requests on exit.

    HTTPX timeouts apply per network phase. A process watchdog is the final
    protection against pathological slow streams, not this soft batch budget.
    """
    from PIL import Image, UnidentifiedImageError
    grid = list(tile_grid() if grid is None else grid)
    own = client is None
    client = client or httpx.Client(headers=HEADERS, timeout=15,
                                    limits=httpx.Limits(max_connections=workers))
    deadline = time.monotonic() + budget
    tm = int(time.time() // 60 * 60)  # cache parameter, NOT a provider timestamp
    stop, lock = threading.Event(), threading.Lock()
    next_request = [0.0]
    rate_error = []

    def fetch(xy):
        with lock:
            delay = max(0.0, next_request[0] - time.monotonic())
            if stop.wait(min(delay, max(0.0, deadline-time.monotonic()))):
                return xy, None
            if time.monotonic() >= deadline:
                return xy, None
            next_request[0] = time.monotonic() + min_interval
        if stop.is_set():
            return xy, None
        try:
            x, y = xy
            response = client.get(TILE_URL.format(x=x, y=y, z=Z, tm=tm),
                                  timeout=max(0.1, min(15.0, deadline-time.monotonic())))
            if response.status_code in (429, 503):
                response.raise_for_status()
            if response.status_code != 200 or len(response.content) > 2_000_000:
                return xy, None
            if not response.content.startswith(b"\x89PNG\r\n\x1a\n"):
                return xy, None
            with Image.open(io.BytesIO(response.content)) as image:
                if image.size != (256, 256):
                    return xy, None
                return xy, image.convert("RGBA")
        except httpx.HTTPStatusError as exc:
            with lock:
                rate_error.append(exc)
            stop.set()
            return xy, None
        except (httpx.HTTPError, UnidentifiedImageError, OSError, ValueError):
            return xy, None

    tiles = {}
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = [pool.submit(fetch, xy) for xy in grid]
    try:
        for future in as_completed(futures, timeout=max(0.01, budget)):
            xy, image = future.result()
            if image is not None:
                tiles[xy] = image
    except TimeoutError:
        logger.warning("tile acquisition budget exceeded")
    finally:
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
        if own:
            client.close()
    if rate_error:
        raise rate_error[0]
    return tiles


def sample_pixel(tiles: dict, gx: int, gy: int) -> str | None:
    for dx, dy in OFFSETS:
        x, y = gx+dx, gy+dy
        tile = tiles.get((x//256, y//256))
        if tile is not None:
            found = classify(tile.getpixel((x % 256, y % 256)))
            if found:
                return found
    return None


def sample_class(tiles: dict, lat: float, lon: float) -> str | None:
    return sample_pixel(tiles, int(tile_x(lon)*256), int(tile_y(lat)*256))


def way_samples(polyline):
    for (a_lat, a_lon), (b_lat, b_lon) in zip(polyline, polyline[1:]):
        length = math.hypot((b_lat-a_lat)*111000, (b_lon-a_lon)*81000)
        if not length:
            continue
        n = max(1, math.ceil(length/SAMPLE_STEP_M))
        for i in range(n):
            f = (i+0.5)/n
            yield a_lat+(b_lat-a_lat)*f, a_lon+(b_lon-a_lon)*f, length/n


def way_points(polyline):
    return [(lat, lon) for lat, lon, _ in way_samples(polyline)]


def choose_class(votes):
    total = sum(votes.values())
    if not total:
        return "-"
    candidates = [c for c, weight in votes.items() if weight/total >= 0.3]
    return max(candidates, key=CLASS_ORDER.get) if candidates else max(votes, key=votes.get)


def way_class(tiles, polyline):
    votes = Counter()
    for lat, lon, weight in way_samples(polyline):
        cls = sample_class(tiles, lat, lon)
        if cls:
            votes[cls] += weight
    return choose_class(votes)


def load_registry(data_dir):
    return json.loads((Path(data_dir)/"jam_map/ways.json").read_text())


@lru_cache(maxsize=2)
def _prepare(path: str, mtime: int, size: int):
    raw = Path(path).read_bytes()
    registry = json.loads(raw)
    canonical = json.dumps(registry, ensure_ascii=False, sort_keys=True, indent=0).encode()
    version = hashlib.sha256(canonical).hexdigest()
    if not (len(registry["ids"]) == len(registry["polylines"]) == len(registry["highway"])):
        raise ValueError("registry arrays have different lengths")
    if len(set(registry["ids"])) != len(registry["ids"]):
        raise ValueError("duplicate road ids")
    grid, prepared, needed = set(tile_grid()), [], set()
    for polyline in registry["polylines"]:
        points = []
        for lat, lon, weight in way_samples(polyline):
            if not (BBOX[0] <= lat <= BBOX[1] and BBOX[2] <= lon <= BBOX[3]):
                continue
            gx, gy = int(tile_x(lon)*256), int(tile_y(lat)*256)
            points.append((gx, gy, weight))
            for dx, dy in ((-3,-3), (-3,3), (3,-3), (3,3), (0,0)):
                needed.add(((gx+dx)//256, (gy+dy)//256))
        prepared.append(tuple(points))
    return registry, version, tuple(prepared), tuple(sorted(needed & grid))


def capture(data_dir: Path, now_utc: datetime) -> dict:
    path = data_dir/"jam_map/ways.json"
    stat = path.stat()
    registry, version, ways, grid = _prepare(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    if not grid:
        raise ValueError("registry has no samples in the AOI")
    version_path = data_dir/"jam_map/registries"/f"{version}.json"
    if not version_path.exists():
        atomic_json(version_path, registry)
    tiles = fetch_tiles(grid=grid)
    if len(tiles) < len(grid)*0.5:
        raise RuntimeError(f"insufficient tiles: {len(tiles)}/{len(grid)}")
    classes, fractions = [], []
    for points in ways:
        votes = Counter()
        for gx, gy, weight in points:
            cls = sample_pixel(tiles, gx, gy)
            if cls:
                votes[cls] += weight
        total = sum(p[2] for p in points)
        fractions.append(round(sum(votes.values())/total, 3) if total else 0.0)
        classes.append(choose_class(votes))
    covered = sum(c != "-" for c in classes)
    eligible = sum(bool(points) for points in ways)
    ratio = covered/eligible if eligible else 0.0
    quality = "low_coverage" if ratio < 0.5 else ("ok" if len(tiles) == len(grid) else "partial")
    return {"ts_utc": utc_stamp(now_utc), "ts_almaty": now_utc.astimezone(ALMATY_TZ).isoformat(),
            "covered": covered, "coverage_ratio": ratio, "eligible_ways": eligible, "classes": "".join(classes),
            "registry_version": version, "classifier_version": CLASSIFIER_VERSION,
            "tiles_received": len(tiles), "tiles_requested": len(grid),
            "quality": quality,
            "matched_fraction": fractions}


def append_frame(data_dir: Path, frame: dict, *, legacy=False) -> Path:
    day = frame["ts_almaty"][:10]
    folder = data_dir/"jam_map" if legacy else data_dir/"jam_map/v2"
    path = folder/f"{day}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = FIELDS if legacy else V2_FIELDS
    row = {key: frame[key] for key in fields}
    if not legacy:
        row["matched_fraction"] = json.dumps(row["matched_fraction"], separators=(",", ":"))
    fresh = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if fresh:
            writer.writeheader()
        writer.writerow(row)
        f.flush()
        os.fsync(f.fileno())
    return path


def harvest(data_dir: Path, now_utc: datetime) -> dict:
    frame = capture(data_dir, now_utc)
    append_frame(data_dir, frame, legacy=True)
    return {"tiles": frame["tiles_received"], "ways": len(frame["classes"]), "covered": frame["covered"]}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    data = Path(__file__).resolve().parents[1]/"data"
    logger.info("jam_map: %s", harvest(data, datetime.now(timezone.utc)))

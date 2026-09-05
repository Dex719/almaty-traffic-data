"""Хранилище: плоские файлы в data/, дружелюбные к git-диффам.

* ``data/scores.csv`` — по строке на замер: баллы обоих сервисов, тренд
  Яндекса, длина затруднений, счётчики активных событий по типам.
* ``data/events.json`` — реестр событий: id → карточка с ``first_seen`` /
  ``last_seen``. Событие исчезло с карты — карточка остаётся, а по паре
  first/last видно, сколько оно жило (время рассасывания ДТП!).
* ``data/snapshots/YYYY-MM/DD.jsonl`` — сырые снапшоты событий на каждый
  замер: строка = {ts, event_ids}. По ним восстанавливается «что висело на
  карте в 08:30 такого-то числа» без раскопок git-истории.
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ALMATY_TZ = timezone(timedelta(hours=5))

SCORE_FIELDS = [
    "ts_utc",
    "ts_almaty",
    "yandex_score",
    "yandex_trend",
    "yandex_jam_km",
    "dgis_score",
    "ev_crash",
    "ev_roadwork",
    "ev_restriction",
    "ev_comment",
    "ev_other",
]


def append_score_row(
    data_dir: Path,
    now_utc: datetime,
    yandex: dict[str, Any] | None,
    dgis: dict[str, Any] | None,
    events: list[dict[str, Any]] | None,
    monthly: bool = False,
) -> dict[str, Any]:
    """Дописывает строку замера в CSV. Возвращает записанную строку.

    ``monthly=True`` (вахтовый режим) пишет в ``data/scores/YYYY-MM.csv``
    по алматинскому времени — файлы остаются небольшими на горизонте
    месяцев. Иначе — легаси ``data/scores.csv``.
    """
    counts: dict[str, int] = {}
    for event in events or []:
        counts[event["type"]] = counts.get(event["type"], 0) + 1
    jam_m = (yandex or {}).get("jam_length_m")
    row = {
        "ts_utc": utc_stamp(now_utc),
        "ts_almaty": now_utc.astimezone(ALMATY_TZ).isoformat(timespec="microseconds"),
        "yandex_score": (yandex or {}).get("score", ""),
        "yandex_trend": (yandex or {}).get("trend", ""),
        "yandex_jam_km": round(jam_m / 1000, 1) if jam_m is not None else "",
        "dgis_score": (dgis or {}).get("score", ""),
        "ev_crash": counts.get("crash", 0) if events is not None else "",
        "ev_roadwork": counts.get("roadwork", 0) if events is not None else "",
        "ev_restriction": counts.get("restriction", 0) if events is not None else "",
        "ev_comment": counts.get("comment", 0) if events is not None else "",
        "ev_other": counts.get("other", 0) if events is not None else "",
    }
    if monthly:
        month = now_utc.astimezone(ALMATY_TZ).strftime("%Y-%m")
        path = data_dir / "scores" / f"{month}.csv"
    else:
        path = data_dir / "scores.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SCORE_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)
        fh.flush()
        os.fsync(fh.fileno())
    return row


def _prev_month(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    if mon == 1:
        return f"{year - 1}-12"
    return f"{year}-{mon - 1:02d}"


def update_event_registry(
    data_dir: Path,
    now_utc: datetime,
    events: list[dict[str, Any]],
    monthly: bool = False,
) -> dict[str, int]:
    """Сливает активные события в реестр.

    ``monthly=True`` — реестр месяца ``data/events/YYYY-MM.json``; событие,
    пережившее границу месяца, переезжает в новый файл с сохранением
    ``first_seen`` (ищем карточку и в прошлом месяце). Иначе — легаси
    ``data/events.json``. Возвращает статистику: новых / активных / всего.
    """
    if monthly:
        month = now_utc.astimezone(ALMATY_TZ).strftime("%Y-%m")
        path = data_dir / "events" / f"{month}.json"
        prev_path = data_dir / "events" / f"{_prev_month(month)}.json"
    else:
        path = data_dir / "events.json"
        prev_path = None
    registry: dict[str, Any] = {}
    if path.exists():
        registry = read_json(path)
    prev_registry: dict[str, Any] = {}
    if prev_path is not None and prev_path.exists():
        prev_registry = read_json(prev_path)
    legacy_path = data_dir / "events.json"
    legacy = read_json(legacy_path) if monthly and legacy_path.exists() else {}
    stamp = utc_stamp(now_utc)
    fresh = 0
    for event in events:
        key = event_key(event)
        card = registry.get(key) or registry.get(event["id"])
        if card is None and prev_registry:
            old = prev_registry.get(key) or prev_registry.get(event["id"])
            if old is not None:
                card = dict(old)  # переезд через границу месяца
        if card is None and event["id"] in legacy:
            card = dict(legacy[event["id"]])
        if card is None:
            card = dict(event)
            card["first_seen"] = stamp
            fresh += 1
        else:
            # Комментарий/лайки могли обновиться; координаты стабильны.
            card.update({k: v for k, v in event.items() if v is not None})
        card["last_seen"] = stamp
        registry[key] = card
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, registry)
    return {"new": fresh, "active": len(events), "total": len(registry)}


def append_snapshot(
    data_dir: Path, now_utc: datetime, events: list[dict[str, Any]],
    *, complete: bool = True, layers: list[str] | None = None,
) -> Path:
    """Дописывает снапшот «какие события активны сейчас» в JSONL месяца."""
    local = now_utc.astimezone(ALMATY_TZ)
    path = data_dir / "snapshots" / local.strftime("%Y-%m") / f"{local.strftime('%d')}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "ts_utc": utc_stamp(now_utc),
        "event_ids": sorted({event_key(event) for event in events}),
        "complete": complete,
        "layers": layers,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return path


def utc_stamp(value: datetime) -> str:
    """Unambiguous observation time; never truncate jittered polls to minutes."""
    if value.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def event_key(event: dict) -> str:
    return event.get("key") or event["id"]


@lru_cache(maxsize=8)
def _read_cached(path: str, mtime_ns: int, size: int) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_json(path: Path) -> dict:
    stat = path.stat()
    # Callers mutate cards: never expose the cached object itself.
    import copy
    return copy.deepcopy(_read_cached(str(path.resolve()), stat.st_mtime_ns, stat.st_size))


def atomic_json(path: Path, value: Any) -> None:
    """Replace complete JSON atomically; retain the previous file on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".tmp-", delete=False) as handle:
            name = handle.name
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=0)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)

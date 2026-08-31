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
) -> dict[str, Any]:
    """Дописывает строку замера в scores.csv. Возвращает записанную строку."""
    counts: dict[str, int] = {}
    for event in events or []:
        counts[event["type"]] = counts.get(event["type"], 0) + 1
    jam_m = (yandex or {}).get("jam_length_m")
    row = {
        "ts_utc": now_utc.strftime("%Y-%m-%dT%H:%M"),
        "ts_almaty": now_utc.astimezone(ALMATY_TZ).strftime("%Y-%m-%dT%H:%M"),
        "yandex_score": (yandex or {}).get("score", ""),
        "yandex_trend": (yandex or {}).get("trend", ""),
        "yandex_jam_km": round(jam_m / 1000, 1) if jam_m else "",
        "dgis_score": (dgis or {}).get("score", ""),
        "ev_crash": counts.get("crash", 0) if events is not None else "",
        "ev_roadwork": counts.get("roadwork", 0) if events is not None else "",
        "ev_restriction": counts.get("restriction", 0) if events is not None else "",
        "ev_comment": counts.get("comment", 0) if events is not None else "",
        "ev_other": counts.get("other", 0) if events is not None else "",
    }
    path = data_dir / "scores.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SCORE_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)
    return row


def update_event_registry(
    data_dir: Path, now_utc: datetime, events: list[dict[str, Any]]
) -> dict[str, int]:
    """Сливает активные события в реестр events.json.

    Возвращает статистику: сколько новых, сколько продлено, всего в реестре.
    """
    path = data_dir / "events.json"
    registry: dict[str, Any] = {}
    if path.exists():
        registry = json.loads(path.read_text(encoding="utf-8"))
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M")
    fresh = 0
    for event in events:
        card = registry.get(event["id"])
        if card is None:
            card = dict(event)
            card["first_seen"] = stamp
            fresh += 1
        else:
            # Комментарий/лайки могли обновиться; координаты стабильны.
            card.update({k: v for k, v in event.items() if v is not None})
        card["last_seen"] = stamp
        registry[event["id"]] = card
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(registry, ensure_ascii=False, indent=0, sort_keys=True),
        encoding="utf-8",
    )
    return {"new": fresh, "active": len(events), "total": len(registry)}


def append_snapshot(data_dir: Path, now_utc: datetime, events: list[dict[str, Any]]) -> Path:
    """Дописывает снапшот «какие события активны сейчас» в JSONL месяца."""
    local = now_utc.astimezone(ALMATY_TZ)
    path = data_dir / "snapshots" / local.strftime("%Y-%m") / f"{local.strftime('%d')}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "ts_utc": now_utc.strftime("%Y-%m-%dT%H:%M"),
        "event_ids": sorted(event["id"] for event in events),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return path

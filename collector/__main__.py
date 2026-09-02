"""Один замер: баллы Яндекс/2ГИС + события 2ГИС → data/.

Запуск: ``python -m collector``. Каждый источник fail-soft: упавший канал
пишет пустые поля и не валит остальные. Ненулевой exit-код только когда
не ответил ни один источник — тогда коммитить нечего.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from collector import sources, store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("collector")

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def collect_once(data_dir: Path = DATA_DIR) -> int:
    now_utc = datetime.now(timezone.utc)
    yandex = dgis = None
    events = None
    try:
        yandex = sources.fetch_yandex_score()
    except Exception:
        logger.exception("Яндекс-балл не получен")
    try:
        dgis = sources.fetch_dgis_score()
    except Exception:
        logger.exception("2ГИС-балл не получен")
    try:
        events = sources.fetch_dgis_events()
    except Exception:
        logger.exception("события 2ГИС не получены")

    if yandex is None and dgis is None and events is None:
        logger.error("все источники упали — замер пропущен")
        return 1

    row = store.append_score_row(data_dir, now_utc, yandex, dgis, events)
    logger.info("scores.csv += %s", row)
    try:
        from collector import jammap

        if (data_dir / "jam_map" / "ways.json").exists():
            logger.info("jam_map: %s", jammap.harvest(data_dir, now_utc))
    except ImportError:
        logger.info("jam_map пропущен: нет pillow")
    except Exception:
        logger.exception("jam_map не снялся")
    if events is not None:
        stats = store.update_event_registry(data_dir, now_utc, events)
        store.append_snapshot(data_dir, now_utc, events)
        logger.info(
            "события: активно %(active)d, новых %(new)d, в реестре %(total)d", stats
        )
    return 0


if __name__ == "__main__":
    sys.exit(collect_once())

"""Вахта: непрерывный цикл замеров внутри одного GitHub Actions джоба.

Запуск: ``python -m collector.shift --minutes 340``.

Каденции (потолки полезности источников):
* 2ГИС балл — каждую минуту (сервис пересчитывает ~раз в 1–2 мин);
* Яндекс балл — каждые 4 минуты (пересчёт ~раз в 4 мин);
* события 2ГИС (ДТП/ремонты/перекрытия) — каждые 5 минут.

Строка в CSV пишется на каждый минутный тик; поля источников, чей черёд
не настал, остаются пустыми (пустое = «не замеряли», а не «ноль»).

Устойчивость на месяцы:
* джиттер ±15 сек на каждом тике — не долбим сервисы по ровным секундам;
* fail-soft: упавший источник получает кулдаун (после 3 подряд ошибок —
  пауза 10 минут именно для него), остальные работают;
* git-коммит пачкой каждые COMMIT_EVERY минут с pull --rebase и ретраями;
* по завершении вахты финальный коммит делает вызывающий workflow,
  он же диспатчит следующую вахту.
"""

from __future__ import annotations

import argparse
import logging
import random
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from collector import sources, store

logger = logging.getLogger("collector.shift")

REPO_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_DIR / "data"

DGIS_EVERY_MIN = 1
YANDEX_EVERY_MIN = 4
EVENTS_EVERY_MIN = 5
JAMMAP_EVERY_MIN = 5
COMMIT_EVERY_MIN = 15

FAILS_TO_COOLDOWN = 3
COOLDOWN_MIN = 10

GIT_TIMEOUT_SEC = 180          # ни одна git-команда не должна висеть дольше
PUSH_STALL_MIN = 45            # нет удачного пуша столько минут — вахта сдаётся,
                               # чтобы workflow поднял свежий раннер


class SourceGuard:
    """Считает подряд идущие ошибки источника и выдаёт кулдаун."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.fails = 0
        self.cooldown_until = 0.0

    def ready(self, now: float) -> bool:
        return now >= self.cooldown_until

    def ok(self) -> None:
        self.fails = 0

    def fail(self, now: float) -> None:
        self.fails += 1
        if self.fails >= FAILS_TO_COOLDOWN:
            self.cooldown_until = now + COOLDOWN_MIN * 60
            self.fails = 0
            logger.warning("%s: %d ошибок подряд — кулдаун %d мин",
                           self.name, FAILS_TO_COOLDOWN, COOLDOWN_MIN)


def _git(*args: str) -> subprocess.CompletedProcess:
    """git с жёстким таймаутом: зависший push не должен вешать всю вахту."""
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO_DIR, capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        logger.error("git %s: таймаут %d сек", " ".join(args), GIT_TIMEOUT_SEC)
        return subprocess.CompletedProcess(
            ["git", *args], 124, "", f"timeout {GIT_TIMEOUT_SEC}s"
        )


def commit_and_push() -> bool:
    """Коммитит data/ и пушит с ретраями. Ошибки не валят вахту.

    Возвращает True, если пушить было нечего или пуш прошёл.
    """
    _git("add", "data")
    diff = _git("diff", "--cached", "--quiet")
    if diff.returncode == 0:
        return True
    stamp = datetime.now(store.ALMATY_TZ).strftime("%Y-%m-%d %H:%M")
    _git("commit", "-m", f"data: вахта, срез {stamp} (алматинское)")
    for attempt in range(3):
        pull = _git("pull", "--rebase")
        if pull.returncode != 0:
            # недоделанный rebase ломает все следующие команды — сбрасываем
            _git("rebase", "--abort")
            logger.warning("pull --rebase не прошёл: %s", pull.stderr.strip()[-200:])
        push = _git("push")
        if push.returncode == 0:
            return True
        logger.warning("push не прошёл (попытка %d): %s",
                       attempt + 1, push.stderr.strip()[-200:])
        time.sleep(5 + attempt * 10)
    logger.error("push не удался трижды — данные останутся до следующего среза")
    return False


def _jammap_worker(data_dir: Path, guard: SourceGuard) -> None:
    """Съёмка всей карты в фоне: ~1 мин работы, минутные тики не ждут."""
    from collector import jammap

    now_utc = datetime.now(timezone.utc)
    try:
        stats = jammap.harvest(data_dir, now_utc)
        guard.ok()
        logger.info("jam_map: %s", stats)
    except Exception as exc:  # noqa: BLE001 - фон не должен ронять вахту
        guard.fail(time.monotonic())
        logger.warning("jam_map: %s", exc)


def run_shift(minutes: int, data_dir: Path = DATA_DIR) -> int:
    guards = {
        "yandex": SourceGuard("Яндекс"),
        "dgis": SourceGuard("2ГИС балл"),
        "events": SourceGuard("2ГИС события"),
        "jammap": SourceGuard("карта пробок"),
    }
    jammap_thread: threading.Thread | None = None
    jammap_available = (data_dir / "jam_map" / "ways.json").exists()
    try:
        import PIL  # noqa: F401
    except ImportError:
        jammap_available = False
        logger.warning("pillow не установлен — съёмка карты пропускается")
    if not jammap_available:
        logger.warning("jam_map выключен (нет реестра или pillow)")
    deadline = time.monotonic() + minutes * 60
    tick = 0
    rows = 0
    last_push_ok = time.monotonic()
    while time.monotonic() < deadline:
        tick_started = time.monotonic()
        now = time.monotonic()
        now_utc = datetime.now(timezone.utc)

        yandex = dgis = None
        events = None
        want_yandex = tick % YANDEX_EVERY_MIN == 0 and guards["yandex"].ready(now)
        want_events = tick % EVENTS_EVERY_MIN == 0 and guards["events"].ready(now)
        want_dgis = tick % DGIS_EVERY_MIN == 0 and guards["dgis"].ready(now)

        if want_yandex:
            try:
                yandex = sources.fetch_yandex_score()
                guards["yandex"].ok()
            except Exception as exc:
                guards["yandex"].fail(now)
                logger.warning("Яндекс-балл: %s", exc)
        if want_dgis:
            try:
                dgis = sources.fetch_dgis_score()
                guards["dgis"].ok()
            except Exception as exc:
                guards["dgis"].fail(now)
                logger.warning("2ГИС-балл: %s", exc)
        if want_events:
            try:
                events = sources.fetch_dgis_events()
                guards["events"].ok()
            except Exception as exc:
                guards["events"].fail(now)
                logger.warning("события 2ГИС: %s", exc)

        if yandex is not None or dgis is not None or events is not None:
            store.append_score_row(
                data_dir, now_utc, yandex, dgis, events, monthly=True
            )
            rows += 1
        if events is not None:
            store.update_event_registry(data_dir, now_utc, events)
            store.append_snapshot(data_dir, now_utc, events)

        if (
            jammap_available
            and tick % JAMMAP_EVERY_MIN == 0
            and guards["jammap"].ready(now)
            and (jammap_thread is None or not jammap_thread.is_alive())
        ):
            jammap_thread = threading.Thread(
                target=_jammap_worker, args=(data_dir, guards["jammap"]), daemon=True
            )
            jammap_thread.start()

        if tick > 0 and tick % COMMIT_EVERY_MIN == 0:
            if commit_and_push():
                last_push_ok = time.monotonic()
            elif time.monotonic() - last_push_ok > PUSH_STALL_MIN * 60:
                logger.error(
                    "нет удачного пуша %d мин — сдаём вахту, пусть workflow "
                    "поднимет свежий раннер", PUSH_STALL_MIN,
                )
                break

        tick += 1
        # до следующей минуты с джиттером ±15 сек, но не короче 20 сек
        elapsed = time.monotonic() - tick_started
        time.sleep(max(20.0, 60.0 - elapsed + random.uniform(-15, 15)))

    if jammap_thread is not None and jammap_thread.is_alive():
        jammap_thread.join(timeout=120)
    commit_and_push()
    logger.info("вахта закончена: тиков %d, строк %d", tick, rows)
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description="вахта сборщика")
    parser.add_argument("--minutes", type=int, default=340)
    args = parser.parse_args()
    return run_shift(args.minutes)


if __name__ == "__main__":
    raise SystemExit(main())

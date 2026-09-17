# Bugfix Spec: collector-hardening-bugfix

**Spec Type:** Bugfix Spec · **Created:** 2026-09-17 · **Status:** BUG-1..4 merged (PR #6); BUG-5 in progress
**Источник:** ревью кода и Actions от 2026-09-17 (последняя вахта 35178520994, лог 14 504 строк, 0 WARNING/ERROR).

## BUG-1 Преемник не ставится в очередь при таймауте джоба

- **Current behavior:** шаг «Queue successor» в `.github/workflows/collect.yml` имеет условие `always() && !cancelled()`. Джоб, превысивший `timeout-minutes: 355`, GitHub помечает как *cancelled*, поэтому преемник не запускается; цепочка вахт рвётся до ближайшего cron-запуска. Cron троттлится: за последние 100 запусков 59 cron-запусков отменены, реально приходит ≈6 в сутки вместо 48 — простой может длиться часы.
- **Expected behavior:** превышение бюджета времени процессом сборщика завершается *внутри* шага (SIGTERM → штатная остановка → финальный экспорт/публикация → при необходимости SIGKILL), шаг получает статус *failure*, а не *cancelled*; recovery artifact и преемник запускаются. Ручная отмена по-прежнему останавливает цепочку.
- **Unchanged behavior:** `timeout-minutes: 355` остаётся последней страховкой; `--minutes 330` сохраняется; условие `failure() || cancelled()` для recovery artifact сохраняется.
- **Root cause:** семантика `cancelled()` в GitHub Actions включает таймаут джоба; условие шага не различает таймаут и ручную отмену.
- **Regressions to check:** YAML валиден; штатная вахта завершается кодом 0; при SIGTERM от `timeout` сборщик выполняет финальный export/push (существующий обработчик SIGTERM).

## BUG-2 Карта «partial» из-за 1–7 недополученных тайлов держит health в unhealthy

- **Current behavior:** за 2026-09-17 70 из 108 снимков карты получили 201–207 тайлов из 208 при покрытии геометрий ≥ 0.993. `jammap.capture` ставит `quality="partial"` при любом недоборе тайлов; `shift.record` копирует quality в статус источника; `ops.health_report` считает здоровым только `status == "ok"`. Итог: heartbeat на постоянном сервере отправлялся бы лишь в ≈35 % циклов, внешний dead-man-switch ложно срабатывает. В Actions влияния нет.
- **Expected behavior:** (a) недополученные тайлы повторно запрашиваются один раз в пределах бюджета, поэтому транзиентные сбои не порождают partial; (b) health считает `jammap` здоровым при `partial` с `coverage_ratio ≥ 0.95`; `partial` у событий (карантин строк) остаётся unhealthy; `low_coverage` остаётся unhealthy.
- **Unchanged behavior:** значения `quality` в CSV и журнале не меняются по смыслу (`ok` = все тайлы, `partial` = часть, `low_coverage` < 50 %); `coverage_ratio` уже пишется в журнал; порог 50 % не меняется.
- **Root cause:** отсутствие повторного запроса тайлов и бинарная трактовка `partial` в health без учёта доли покрытия.
- **Regressions to check:** `test_corrupt_png_isolated_from_good_tile`, `test_blank_map_is_not_healthy`, `test_health_rejects_stale_or_partial_sources` проходят; новые тесты: повтор возвращает недостающий тайл; health с `partial` + 0.99 здоров, с `partial` + 0.8 нет.

## BUG-3 Логи вахты забиты INFO-сообщениями httpx

- **Current behavior:** `logging.basicConfig(level=INFO)` в `shift.main` включает логгер `httpx`, который пишет строку на каждый запрос: 14 273 строки и 3.4 МБ за вахту при нуле полезных сообщений.
- **Expected behavior:** логгеры `httpx` и `httpcore` на уровне WARNING; собственные INFO сборщика сохраняются.
- **Unchanged behavior:** уровень корневого логгера INFO; формат сообщений.
- **Root cause:** глобальная конфигурация без ограничения шумных библиотечных логгеров.
- **Regressions to check:** предупреждения httpx по-прежнему видны.

## BUG-4 Устаревший докстринг модуля `store.py`

- **Current behavior:** докстринг описывает `data/scores.csv` и `data/events.json` как основные файлы; фактически используются месячные `data/scores/YYYY-MM.csv` и `data/events/YYYY-MM.json`, а legacy-файлы заморожены.
- **Expected behavior:** докстринг описывает актуальные пути и статус legacy-файлов.
- **Unchanged behavior:** код.
- **Root cause:** докстринг не обновлён при переходе на вахтовый режим.

## BUG-5 Атомарные записи падают на Windows из-за fsync каталога (Micro)

- **Current behavior:** `store.atomic_json` и `journal.atomic_bytes` после `os.replace` открывают каталог через `os.open(dir, O_RDONLY)` для `fsync`; на Windows это `PermissionError`. Операторский CLI `python -m collector.publish` и `Journal.export_pending()` на Windows завершаются трейсбеком уже после того, как файл записан (обнаружено при восстановлении из артефакта 2026-09-17).
- **Expected behavior:** на `os.name == "nt"` fsync каталога пропускается (NTFS не имеет такой семантики); на POSIX поведение прежнее.
- **Unchanged behavior:** порядок temp → fsync файла → replace; гарантии на Linux.
- **Root cause:** Linux-специфичный приём без ветки для Windows в модулях, которые по стандартам проекта должны импортироваться и работать на любой ОС.
- **Regressions to check:** новый тест `test_atomic_write_skips_directory_fsync_only_on_windows`; `test_atomic_registry_retains_previous_file`, `test_outbox_crash_replays_stable_batch_with_new_observations`.

## Не в объёме

- Слияние PR Dependabot #2–#5 — решение оператора.
- Частота cron (`13,43 * * * *`) — не баг, страховочный триггер.

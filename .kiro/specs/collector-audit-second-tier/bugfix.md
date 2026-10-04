# Bugfix Spec: collector-audit-second-tier

**Spec Type:** Bugfix Spec (bugfix + tasks одним проходом) · **Created:** 2026-10-04 · **Status:** In progress
**Источник:** аудит 2026-10-04 (локальный отчёт, «второй эшелон»). Прод здоров; все пункты латентные или документационные.
**Связь:** [collector-silent-failures-bugfix](../collector-silent-failures-bugfix/bugfix.md), [collector-chain-and-tiles-bugfix](../collector-chain-and-tiles-bugfix/bugfix.md).

## BUG-1 Репозиторий растёт на ~30 МБ в неделю

- **Current behavior:** реестр событий (`data/events/YYYY-MM.json`, до ~20 МБ к концу месяца) коммитится ежечасно, `scores` — каждые 15 минут: 671 коммит за 7 дней, 140 МБ на GitHub, ~1,5 ГБ в год. Рекомендованный GitHub 1 ГБ будет пройден за полгода. Каждая версия реестра — новая дельта независимо от интервала, потому что `last_seen` всех активных карточек меняется при каждом коммите.
- **Expected behavior:** реестр коммитится один раз за вахту, в финальной публикации (4–5 раз в сутки). `scores` — как раньше.
- **Unchanged behavior:** `commit_and_push(include_events=...)`; `pull --rebase --autostash` переносит незакоммиченный реестр между вахтами; `--depth 1` клоны.
- **Root cause:** интервал в час выбран до измерения дельт.
- **Regressions to check:** `test_live_views_only_and_registry_throttled`, `test_shift_ships_before_pushing_and_skips_publisher_without_git` (финальная публикация включает реестр).

## BUG-2 Один сбой в finally отменяет остальные финальные шаги и дублирует кадр карты

- **Current behavior:** в цикле `map_future` обнуляется только после возврата `save_map`; если `append_frame` падает (ENOSPC/EIO), внешний `except` ставит `fatal`, `map_future` остаётся, и `finally` вызывает `save_map` снова: вторая запись того же кадра в журнал (новый uuid), снова исключение, и, так как финальный блок — один `try`, пропускаются `export_pending`, `publish(final=True)` и запись `health.json`.
- **Expected behavior:** `map_future` обнуляется до `save_map`; каждый финальный шаг (кадр, экспорт, публикация, health) в своём `try`, сбой логируется и ставит `fatal`, остальные выполняются.
- **Unchanged behavior:** порядок шагов; закрытие ресурсов в `finally`; код выхода 1 при сбое.
- **Root cause:** один `try` на несколько независимых обязанностей.
- **Regressions to check:** новый `test_failing_map_write_does_not_skip_export_or_health`.

## BUG-3 Stale-значения провайдера попадают в живой CSV как свежие

- **Current behavior:** `record()` вычисляет `stale`/`missing_timestamp`/`invalid_timestamp`, но цикл игнорирует возвращаемый статус и кладёт payload в `results`; `append_score_row` пишет значение, а у CSV нет колонки статуса. Измерено: 2026-10-03 — 0 stale, поэтому гейтинг безопасен.
- **Expected behavior:** в CSV попадают только значения со статусом `ok`/`partial`; остальное — пустое поле (пустое ≠ ноль по контракту) и запись в журнале со статусом.
- **Unchanged behavior:** журнал, статусы, health; кадры карты (у `jam_map/v2` есть колонка `quality`).
- **Regressions to check:** `test_zero_is_not_missing`, `test_score_row_survives_dead_sources`; новый `test_stale_score_stays_out_of_the_live_csv`.

## BUG-4 Серверный backup читает всю базу в память и не ротируется

- **Current behavior:** `Journal.backup()` делает `gzip.compress(copy_path.read_bytes())` — вся база в RAM (~50 МБ/сутки роста, ~18 ГБ/год), копии не удаляются (квадратичный рост тома), порог health 100 МиБ ≈ двое суток запаса.
- **Expected behavior:** потоковое сжатие (`shutil.copyfileobj` в `GzipFile`), SHA-256 потоково, `os.replace` внутри каталога назначения; retention `keep` (по умолчанию 14, `--keep 0` — хранить все); `MIN_FREE_BYTES = 1 ГиБ`; README описывает рост.
- **Unchanged behavior:** SQLite backup API, `integrity_check`, manifest JSON, копирование реестров, `.state/backup.json`.
- **Regressions to check:** `test_verified_backup_roundtrip`, `test_missing_required_backup_is_unhealthy`; новый `test_backup_is_streamed_and_pruned`.

## BUG-5 README расходится с кодом

- `README:16` «scores every minute» — Яндекс 240 с, в 75 % строк пусто; пример на Python печатает, скорее всего, пустое поле.
- `README:40`, `README:310` обещают «сырые ответы в журнале» — журнал хранит нормализованные поля.
- `ev_*` считают события всего проекта «almaty», ≈ 22 % ДТП вне AOI; нигде не сказано.
- Замороженные `data/events.json`, `data/scores.csv`, `data/jam_map/2026-09-0*.csv` (≈ 15 МБ) не упомянуты.
- `MANIFEST.gaps` — только разрывы смещений внутри run; README называет «разрывы».
- Проверка загрузки падает на «только по размеру» после 10 с без digest; публичные доки обещают только sha256.
- **Expected behavior:** формулировки совпадают с кодом (правки в README, `context.md`, `git-workflow.md`, `project-standards.md`).

## Попутно

- Отсутствие `jam_map/ways.json` в data-dir теперь даёт WARNING при старте (раньше карта выключалась молча при зелёном heartbeat).
- `fetch_tiles` считает причины пропуска тайлов (`HTTP 404`, `not PNG`, `size …`, класс исключения) и пишет их в строку «retrying N missing tiles», плюс список тайлов, не пришедших и после повтора: это данные, чтобы решить судьбу 76 % кадров `partial`.

## Раскатка

Код безопасен для перекрывающей вахты: формат CSV/журнала не меняется, старый код продолжит ежечасные коммиты реестра до конца своей вахты. `MIN_FREE_BYTES` влияет только на health (сервера нет). Откат — revert.

## Не в объёме

- Удаление legacy-lookup `events.json` в `store.update_event_registry`, lru_cache прошлых месяцев, `.gitattributes`, тесты на YAML.

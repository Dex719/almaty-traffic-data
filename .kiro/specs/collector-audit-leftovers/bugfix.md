# Bugfix Spec: collector-audit-leftovers

**Spec Type:** Bugfix Spec (bugfix + tasks одним проходом) · **Created:** 2026-10-04 · **Status:** In progress
**Источник:** аудит 2026-10-04 (локальный отчёт, раздел «Мелочи»: подтверждённые low-находки). Прод здоров.
**Связь:** [collector-silent-failures-bugfix](../collector-silent-failures-bugfix/bugfix.md), [collector-chain-and-tiles-bugfix](../collector-chain-and-tiles-bugfix/bugfix.md), [collector-audit-second-tier](../collector-audit-second-tier/bugfix.md).

## Сборщик (`shift.py`, `store.py`, `journal.py`, `jammap.py`, `sources.py`, `ops.py`)

| # | Было | Стало |
|---|---|---|
| L1 | `Retry-After` без верхней границы: один заголовок `2592000` паркует источник на месяц | `RETRY_AFTER_CAP_SEC = 3600`; 401/403 → час как раньше |
| L2 | `last_success` обновлялся и для `stale`/`missing_timestamp`/`invalid_timestamp`; монитор по возрасту пропускал устаревшие данные | `last_success` только при `ok`/`partial` |
| L3 | Правило 45 минут без публикации завершало вахту без строки в логе | ERROR «No successful publication for 45 min…» |
| L4 | После SIGKILL `.tmp-*` (в т. ч. ~18 МБ рядом с реестром) и рабочие файлы сегментов оставались навсегда | `store.sweep_temporary()` при старте вахты под process lock, INFO с числом удалённых |
| L5 | `append_score_row` решал про заголовок по `exists()`: пустой файл после kill оставался без заголовка | проверка `st_size == 0`, как в `append_frame` |
| L6 | `zlib.error` при чтении битого батча не ловился: экспорт застревал навсегда | `except (OSError, EOFError, zlib.error)` → батч переписывается |
| L7 | `DecompressionBombError` не в except-кортеже загрузчика тайлов: один вредоносный PNG ронял кадр | добавлен в кортеж, тайл считается пропуском |
| L8 | `quality=ok` при покрытии 0,5–0,95 считался здоровым, `partial` при 0,9 — нет | для карты health требует `coverage_ratio ≥ 0.95` при любом `quality` (`ops.MIN_HEALTHY_COVERAGE`) |
| L9 | `jammap.harvest`/`python -m collector.jammap` писали замороженную legacy-схему в `data/jam_map/<day>.csv` (трекается git) | пишут `jam_map/v2`, `append_frame` без `legacy` |
| L10 | `fetch_dgis_events` терял `invalid_rows` | возвращает `EventList` с суммой потерь |
| L11 | Мёртвые константы `*_EVERY_MIN`, `collect_once` в `__main__.py` | удалены; расписание только в `INTERVALS` |

## Публикация (`publish.py`)

| # | Было | Стало |
|---|---|---|
| P1 | Файл с календарно невозможным днём (`2026-02-30`) ронял `day_closed()` → `pending()`/`candidate_days()` падали каждые 15 минут | `valid_day()` в `day_of` и `parse_segment_name`: такие файлы и сегменты игнорируются |
| P2 | `_release()` считал отсутствием релиза любой текст с «404» или «Not Found» (rate limit с id, содержащим 404; транзитные 5xx) | только `(HTTP 404)` |
| P3 | `_save_state()` после успешной загрузки вне `try`: ENOSPC на `publish.json` → fatal вахты | OSError логируется; повтор отгрузки дедуплицируется при сборке |
| P4 | Застрявший день перекачивал все свои сегменты каждые 15 минут без backoff | экспоненциальная пауза 15 мин → 6 ч (`RETRY_BASE`, `RETRY_CAP`), `summary["deferred"]`, эскалация 24 ч учитывает отложенные дни |
| P5 | Поздние байты уже собранного дня отбрасывались молча в режиме вахты | WARNING один раз на файл; порог — максимум из отгруженного и включённого в архив локально (`archived`, общий словарь Shipper/Consolidator) |

## Репозиторий и тесты

- `.gitattributes`: `* text=auto eol=lf` + бинарные типы. Имена реестров — sha256 байтов; при `core.autocrlf=true` Windows-клон раньше получал CRLF и несовпадающие хэши.
- Новые тесты на контракты без покрытия: валидация heartbeat URL, правило 45 минут, финальный экспорт по stop-event, CLI `ops check`, cap `Retry-After`, sweep при старте, пустой CSV, битый батч, decompression bomb, health карты, backoff консолидации, поздние байты, 404 vs прочие ошибки `gh`, невозможные даты.

## Не менялось (осознанно)

- Один 429/503 на тайле по-прежнему прерывает кадр: это сигнал лимита, который должен дойти до guard с `Retry-After`.
- Recovery-артефакты доступны любому залогиненному на публичном репозитории: вопрос политики, не кода; комментарии и так публикуются в `data/events/*.json`.
- `test.yml` не триггерится на `collect.yml`/`deploy/` при push в `main`: на PR фильтра нет, а офлайн-тестов для YAML и unit-файлов не существует.
- Cooldown-слоты не журналируются: журнал хранит наблюдения, пауза видна по WARNING при входе и по `health.json`.

## Раскатка

Безопасно для перекрывающей вахты: форматы файлов не меняются; `.gitattributes` нормализует только рабочие копии (индекс уже LF). Откат — revert.

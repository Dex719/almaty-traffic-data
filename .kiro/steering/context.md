# Контекст проекта: almaty-traffic-data

Сборщик дорожных данных Алматы для krisha-fair-price / baǵam. Один процесс,
независимые расписания источников, долговечный журнал, публикация в GitHub.
Владелец: Dex719. Язык документации и спек — русский; код и комментарии — английский.

## Стек

- Python 3.12/3.13, зависимости закреплены в `requirements.txt` (`httpx`, `pillow`) и `requirements-dev.txt` (`pytest`). Остальное — stdlib: `sqlite3`, `lzma`, `tarfile`, `csv`, `json`, `threading`.
- Linux-only части: `fcntl` (process lock в `ops.py`), `sd_notify` (systemd watchdog). Тесты и запуск — только Linux/WSL.
- GitHub Actions (`ubuntu-latest`) + `gh` CLI; GitHub Releases как хранилище архивов.
- Деплой на постоянный сервер: `deploy/*.service|timer` (systemd, пользователь `traffic`, `/var/lib/almaty-traffic`).

## Структура

| Путь | Назначение |
|---|---|
| `collector/sources.py` | HTTP-адаптеры источников: балл Яндекса (XML), балл 2ГИС, слои событий 2ГИС `user`/`2gis`; нормализация, AOI |
| `collector/store.py` | Совместимые представления: `scores/YYYY-MM.csv`, `events/YYYY-MM.json`, `snapshots/`; атомарные записи |
| `collector/journal.py` | Канонический журнал SQLite/WAL, экспорт батчей `observations/<UTC-день>/<sha256>.jsonl.gz`, backup |
| `collector/jammap.py` | Растровая карта пробок Яндекса → классы `G/Y/R/D` по геометриям OSM (`jam_map/ways.json`), `jam_map/v2/<день>.csv` |
| `collector/ops.py` | Process lock, health report, heartbeat, CLI `check|export|backup` |
| `collector/shift.py` | Главный цикл: расписания, guards/cooldown, экспорт, публикация, graceful shutdown; CLI |
| `collector/publish.py` | Публикация в Releases: сегменты (`staging`) и дневные архивы (`data-YYYY-MM`); CLI `status|ship|consolidate` |
| `tests/` | Только офлайн-тесты: фейки ответов, `FakeReleases`, локальный bare-remote для git |
| `data/` | Данные; `data/.state/` (журнал, health, publish.json) в git не попадает |
| `.kiro/specs/*` | Спеки фич и багфиксов (канон Kiro); `.kiro/steering/` — этот контекст |

## Режимы работы

- **Сервер** (`--forever`, рекомендуемый): всё на диске, backup timer, heartbeat. Git и Releases не используются.
- **GitHub Actions** (`--git`, best-effort): вахта 330 мин, экспорт и публикация каждые 15 мин, self-dispatch преемника, cron-страховка (сильно троттлится GitHub). Обёртка `timeout -k 5m 340m` превращает зависание в failure шага, а не отмену джоба.

## Контракты данных (не ломать)

- **Live-представления в git (`main`)**: `data/scores/YYYY-MM.csv` (каждые 15 мин), `data/events/YYYY-MM.json` (не чаще раза в час), `data/jam_map/ways.json`, `data/jam_map/registries/*.json`.
- **Архивные потоки в Releases**: `observations/`, `snapshots/`, `jam_map/v2/`. Каждые 15 мин — неизменяемый сегмент `seg-<UTC>-<run_id>-<seq>-<days>.tar.xz` в prerelease `staging`; после закрытия дня (`D+1 00:45 UTC`) — `traffic-YYYY-MM-DD.tar.xz` в релизе `data-YYYY-MM` с `MANIFEST.json`. Загрузки подтверждаются sha256-`digest` из API. Сегменты удаляются после консолидации всех их дней.
- **Журнал**: `observation_id` уникален; повтор ID с другим содержимым отклоняется; имя батча = SHA-256 **распакованного** JSONL; состав батча фиксируется в SQLite до записи файла.
- **CSV**: пустое поле ≠ ноль; `0.0` сохраняется; счётчики событий только при полном результате обоих слоёв; времена — полный ISO 8601 с микросекундами.
- **События**: ключ `layer:id`; вне AOI не удаляются, а помечаются `in_aoi=false`; `first_seen` переносится через границу месяца; неполный снимок (`complete=false`) не доказывает исчезновение события.
- **Карта**: `quality` = `ok` (все тайлы) / `partial` (часть) / `low_coverage` (< 50 % геометрий); health терпит `partial` при `coverage_ratio ≥ 0.95`. Классы — категориальная оценка растра, не скорости.
- **Время**: метки в файлах — UTC; месяц/день для `scores`, `snapshots`, `jam_map` — алматинская дата (+05); день каталога `observations` — UTC-дата первой строки батча.
- **RPO** ≤ 15 мин в Actions (экспорт + сегмент за цикл); при жёстком уничтожении runner теряется не более одного интервала.

## Глоссарий

- **Балл** — интегральный уровень пробок 0–10 (Яндекс `level`, 2ГИС `score`).
- **Слой событий** — `user` (сообщения водителей) и `2gis` (официальные ограничения); типы `crash|roadwork|restriction|comment|other`; `camera` исключён.
- **AOI** — прямоугольник `(43.165, 43.375, 76.72, 77.06)`, lat/lon min/max.
- **Вахта (shift)** — один запуск `collector.shift`; в Actions = один run с `run_id = GITHUB_RUN_ID`.
- **Экспорт** — перенос неэкспортированных записей журнала в батч-файл.
- **Сегмент / дневной архив / консолидация** — см. контракты выше; **закрытие дня** — момент, после которого архив дня можно собирать.
- **Guard / cooldown** — защита источника: `Retry-After`, 401/403 → час, 3 ошибки подряд → 10 мин.
- **Recovery artifact** — `traffic-recovery-<run_id>` с полным `data/` (включая `.state/`), загружается при failure/cancel.

## Ограничения и риски

- Публичные endpoints без токенов; условия использования и квоты провайдеров не гарантированы; комментарии водителей могут содержать персональные данные — архивы и артефакты содержат их.
- GitHub `schedule` ненадёжен; единственная гарантия непрерывности — self-dispatch и внешний мониторинг.
- Репозиторий уже содержит ~128 МБ истории данных до перехода на Releases; история намеренно не переписывалась.

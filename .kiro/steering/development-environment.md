---
inclusion: fileMatch
fileMatchPattern: 'requirements*.txt|tests/*.py|.github/workflows/*.yml|deploy/*'
---

# Окружение разработки

## Локально

- Linux или WSL Ubuntu: `python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt`.
- На этой Windows-машине WSL Ubuntu без `venv`/`ensurepip`: копировать `collector/`, `tests/`, `requirements*.txt` в каталог WSL и запускать `python3 -m pip install --target deps -r requirements-dev.txt`, затем `PYTHONPATH=deps python3 -m pytest tests/ -q -p no:cacheprovider`. Не ставить пакеты в пользовательский site-packages WSL.
- Под Windows напрямую работают модули `store`, `journal`, `publish`, `sources` (операторский CLI `python -m collector.publish`), но не `shift`/`ops` (`fcntl`).
- Один цикл без публикации: `python -m collector`; короткая вахта: `python -m collector.shift --minutes 5`. Карта требует `data/jam_map/ways.json` и Pillow.

## Переменные окружения

| Переменная | Где | Назначение |
|---|---|---|
| `GH_TOKEN`, `GH_REPO` | Actions (шаг сбора), операторский CLI | Доступ `gh` к Releases; локально `gh` авторизован через keyring, достаточно `GH_REPO` |
| `GITHUB_RUN_ID` | Actions | `run_id` сегментов; локально — `local_<host>_<pid>` или `--run-id` |
| `TRAFFIC_HEARTBEAT_URL` | сервер (`/etc/almaty-traffic.env`) | внешний dead-man-switch, только HTTPS |
| `TRAFFIC_REQUIRE_BACKUP` | сервер | `1` — без свежего backup heartbeat не отправляется |

## GitHub Actions

- Запуск вахты вручную: `gh workflow run collect.yml --ref main`. Статус: `gh run list --workflow collect.yml`.
- Отмена: `gh run cancel <run_id>` — шаг «Preserve recovery data» загрузит `data/` (с `.state/`), преемник **не** запустится.
- Артефакт: `gh run download <run_id> -n traffic-recovery-<run_id> -D <dir>`; хвост журнала: `Journal(<dir>).export_pending()` (или `python -m collector.ops export` на Linux); отгрузка: `python -m collector.publish ship --data-dir <dir-с-файлами-дня> --run-id recovery_<run_id>`.
- Логи вахты: `gh run view <run_id> --log`; собственные сообщения сборщика — `collector.*`, `httpx` приглушён.

## Releases

- `gh release view staging --json assets` — очередь сегментов; `gh release view data-YYYY-MM --json assets` — дневные архивы.
- Проверка/досборка вручную: `GH_REPO=Dex719/almaty-traffic-data python -m collector.publish status|consolidate --data-dir data`.

## Сервер

- См. README «Развёртывание на постоянном сервере»: `deploy/almaty-traffic.service` (Type=notify, WatchdogSec=180), backup service/timer с `ConditionPathIsMountPoint=/mnt/traffic-backups`.
- Health: `python -m collector.ops check --data-dir /var/lib/almaty-traffic` (код 0 только при свежем `healthy`).

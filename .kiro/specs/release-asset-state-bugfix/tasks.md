# Tasks: release-asset-state-bugfix

**Spec:** [bugfix.md](bugfix.md) · **Created:** 2026-09-23

- [x] **TSK-B01**: Тесты-репродукции (red)
  - Bug: BUG-1..5
  - Deliverables: `tests/test_publish.py`: `FakeReleases` моделирует состояния активов как `gh` и REST (незавершённый актив виден в листинге, но не виден `download` и удалению по имени; удаление по id работает для любого состояния; `interrupt_at` имитирует убитый таймаутом `gh release upload`); новые тесты из bugfix.md
  - Acceptance: новые тесты падают на текущем коде по причинам из bugfix.md; существующие 62 теста зелёные.
  - Факт: на старом коде `8 failed, 62 passed`; упали ровно 8 новых тестов (WSL Ubuntu, Python 3.14.4).

- [x] **TSK-B02**: Консолидация только из `uploaded`, очистка сирот по id
  - Bug: BUG-1
  - Deliverables: `collector/publish.py`: `AssetInfo.created_at`, `GhReleases.list_assets`, `GhReleases.delete_asset(tag, name, asset_id=None)` через `gh api -X DELETE`, `Consolidator.staging_segments` (фильтр `uploaded`), `Consolidator.cleanup_staging(now_utc)` (сироты старше `ORPHAN_AGE`, изоляция ошибок)
  - Acceptance: регрессии BUG-1 зелёные.
  - Факт: `test_interrupted_segment_upload_does_not_block_consolidation`, `test_orphaned_upload_deleted_only_after_grace_period`, `test_cleanup_continues_after_failed_deletion`, `test_delete_asset_goes_by_id_through_rest` зелёные.

- [x] **TSK-B03**: Замена незавершённого или неверного актива по id
  - Bug: BUG-2
  - Deliverables: `collector/publish.py::upload_verified`
  - Acceptance: регрессии BUG-2 зелёные.
  - Факт: `test_stale_daily_archive_in_starter_state_is_replaced`, `test_upload_failure_is_contained_and_retried`, `test_bad_digest_keeps_state_and_retries_same_bytes` зелёные.

- [x] **TSK-B04**: `ship` отказывается от уже консолидированных дней
  - Bug: BUG-3
  - Deliverables: `collector/publish.py::main` (`candidate_days` перед отгрузкой, `refused_days`, код 1)
  - Acceptance: регрессии BUG-3 зелёные.
  - Факт: `test_ship_refuses_days_already_archived` зелёный; все `ShipperTests` зелёные.

- [x] **TSK-B05**: Эскалация застрявшего дня
  - Bug: BUG-4
  - Deliverables: `collector/publish.py`: `closing_time`, `STUCK_AFTER`, `annotate` (`::error::` под `GITHUB_ACTIONS=true`), один раз на день за процесс
  - Acceptance: регрессии BUG-4 зелёные.
  - Факт: `test_stuck_day_is_annotated_once` зелёный (одна аннотация за три прохода); `test_day_closes_45_minutes_after_utc_midnight` зелёный.

- [x] **TSK-B06**: Проверка `run_id`
  - Bug: BUG-5
  - Deliverables: `collector/publish.py`: `RUN_ID_RE`, проверка в `main` и `Shipper.__init__`
  - Acceptance: регрессии BUG-5 зелёные.
  - Факт: `test_rejects_run_id_that_breaks_segment_names` зелёный; на Windows `python -m collector.publish ship --run-id bad-id` сразу отвечает ошибкой argparse, до обращения к сети.

- [x] **TSK-B07**: Документация и каскад спеки
  - Bug: BUG-1..5
  - Deliverables: `README.md` (эксплуатация публикации: незавершённые загрузки, отказ `ship` для собранных дней), `.kiro/steering/development-environment.md` (раздел Releases), `.kiro/steering/context.md` (контракты), `.kiro/specs/release-archive-publishing/requirements.md` и `design.md` (FR-2, FR-3, FR-7, edge case, интерфейс, алгоритм, обработка ошибок)
  - Acceptance: описания совпадают с кодом; ссылки ведут на эту спеку.
  - Факт: правки внесены; формулировки сверены с `publish.py` (константы `ORPHAN_AGE`, `STUCK_AFTER`, `RUN_ID_RE`).

- [x] **TSK-B08**: Разовое восстановление 2026-09-17/18 (оператор)
  - Bug: BUG-1
  - Deliverables: запуск исправленного CLI из ветки фикса на пустом каталоге: `GH_REPO=Dex719/almaty-traffic-data python -m collector.publish consolidate --data-dir <пустой каталог> --run-id hotfix_20260923` (раздел «Разовое восстановление» bugfix.md)
  - Acceptance: `traffic-2026-09-17.tar.xz` и `traffic-2026-09-18.tar.xz` в `data-2026-09` в состоянии `uploaded` с digest; актив 571001306 удалён; в `staging` нет сегментов 17–18 сентября.
  - Статус: первый запуск из сессии агента 2026-09-23 заблокирован политикой; повторный 2026-09-24 06:34 UTC по явному «продолжай до конца» оператора выполнен.
  - Факт: `{"consolidated": ["2026-09-17"], "failed": ["2026-09-18"], "deleted_segments": 36}`. День 17: 99 членов, 2 672 наблюдения, 369 повторных строк отброшено, `traffic-2026-09-17.tar.xz` `uploaded` в 06:37:46 UTC. День 18 упал на транзитном `HTTP 500` GitHub при скачивании сегмента (asset 571220983); сработала эскалация BUG-4. Актив 571001306 удалён по id (API отвечает 404). Урок: очистка сирот не ждёт успеха всех дней, поэтому оставшийся день подхватывает вахта на старом коде на `GITHUB_TOKEN`. Это корректно, но тратит её лимит; при повторе плана оставшийся день лучше добрать тем же CLI сразу.
  - Факт: день 18 собрала вахта 35945587754 на старом коде, `traffic-2026-09-18.tar.xz` `uploaded` в 07:05:02 UTC; в `staging` 153 актива, все `uploaded`, незавершённых нет.

- [x] **TSK-B09**: Проверка на проде после merge
  - Bug: BUG-1..4
  - Deliverables: лог первой завершённой вахты на новом коде
  - Acceptance: нет строк `consolidation of ... failed`; `staging` содержит только незакрытые дни и сегменты последних часов.
  - Факт (2026-09-25): четыре вахты на новом коде (35970259955, 36003115848, 36042143878, 36075821379) завершились success, каждая отгрузила 22 сегмента. Строк `consolidation of ... failed`, эскалаций и ошибок лимита API нет. Вахта 36075821379 собрала `traffic-2026-09-24.tar.xz` в 00:52:10 UTC (98 членов, 2 663 наблюдения, 5 повторных строк отброшено). В `staging` 46 активов, все `uploaded`. Попутно найдена не связанная с этим фиксом задержка первого git-пуша вахты: [git-publication-timeout-bugfix](../git-publication-timeout-bugfix/bugfix.md).

## Progress

| Task | Status |
|---|---|
| TSK-B01 | Complete |
| TSK-B02 | Complete |
| TSK-B03 | Complete |
| TSK-B04 | Complete |
| TSK-B05 | Complete |
| TSK-B06 | Complete |
| TSK-B07 | Complete |
| TSK-B08 | Complete |
| TSK-B09 | Complete |

## Evidence (2026-09-23)

- Базовая линия до изменений: 62 passed (WSL Ubuntu, Python 3.14.4, pytest 8.4.2).
- Red: на старом коде `8 failed, 62 passed`, упали ровно новые тесты.
- Green: `70 passed`; `python -m compileall -q collector` без ошибок; `import collector.publish` на Windows (Python 3.12.10) работает.
- Прод до фикса: `staging` 245 активов, из них 1 `starter` (id 571001306); в `data-2026-09` нет дней 17 и 18; лог run 35843486197 — ошибка `no assets match the file pattern` для обоих дней каждые 15 минут.

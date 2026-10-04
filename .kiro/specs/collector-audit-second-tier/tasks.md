# Tasks: collector-audit-second-tier

**Spec:** [bugfix.md](bugfix.md) · **Created:** 2026-10-04

- [x] **TSK-T01**: Тесты-репродукции (red)
  - Bug: BUG-2..4
  - Deliverables: `tests/test_hardening.py::SecondTierTests` (3 теста); `test_healthy_shift_has_no_warnings` ожидает ровно одно WARNING «jam map disabled» (в тесте нет `ways.json`).
  - Факт: старый `collector/` + новые тесты: `4 failed, 82 passed` — упали три новых и скорректированный `test_healthy_shift_has_no_warnings`.

- [x] **TSK-T02**: Реестр один раз за вахту
  - Bug: BUG-1
  - Deliverables: `collector/shift.py`: `include_events=final`, удалены `EVENTS_COMMIT_EVERY_MIN` и `last_events_commit`.
  - Acceptance: `test_shift_ships_before_pushing_and_skips_publisher_without_git` (финальный push с `include_events=True`), `test_live_views_only_and_registry_throttled` зелёные.

- [x] **TSK-T03**: Финальные шаги независимы, кадр не дублируется
  - Bug: BUG-2
  - Deliverables: `collector/shift.py`: `map_future` обнуляется до `save_map`; `final_step()` для кадра, экспорта, публикации, health.
  - Acceptance: `test_failing_map_write_does_not_skip_export_or_health` зелёный (код 1, экспорт и health выполнены, ровно одна запись кадра).

- [x] **TSK-T04**: Stale-значения не попадают в CSV
  - Bug: BUG-3
  - Deliverables: `collector/shift.py`: в `results` только статусы `ok`/`partial`.
  - Acceptance: `test_stale_score_stays_out_of_the_live_csv`, `test_zero_is_not_missing`, `test_score_row_survives_dead_sources` зелёные.

- [x] **TSK-T05**: Потоковый backup с retention, порог свободного места
  - Bug: BUG-4
  - Deliverables: `collector/journal.py`: `backup(destination, keep=14)`, `prune_backups()`; `collector/ops.py`: `--keep`, `MIN_FREE_BYTES = 1 ГиБ`.
  - Acceptance: `test_backup_is_streamed_and_pruned` (gzip.compress подменён на AssertionError — сжатие потоковое; sha256 манифеста совпадает с файлом; старейшая копия и её манифест удалены при `keep=2`; `keep=0` ничего не удаляет), `test_verified_backup_roundtrip`, `test_missing_required_backup_is_unhealthy` зелёные.

- [x] **TSK-T06**: README и steering совпадают с кодом
  - Bug: BUG-5
  - Deliverables: `README.md` (English summary, принципы, таблица файлов с замороженными, `MANIFEST.gaps`, пример на Python, раздел «Баллы», сервер: 1 ГиБ и рост, backup: потоково и retention, Releases: верификация по размеру, «Участие»), `.kiro/steering/context.md`, `git-workflow.md`, `project-standards.md`.

- [x] **TSK-T07**: Попутно
  - Deliverables: WARNING при отсутствии `jam_map/ways.json` (`shift.py`); причины пропуска тайлов и список непришедших после повтора в INFO (`jammap.py`).
  - Acceptance: `test_missing_tile_is_retried_once`, `test_corrupt_png_isolated_from_good_tile`, `ChainAndTilesTests` зелёные.

- [ ] **TSK-T08**: Проверка на проде после merge
  - Deliverables: лог первой вахты на новом коде: строки «retrying N missing tiles ({...})» с причинами и «tiles still missing after retry» — по ним решить судьбу кадров `partial` (отдельная спека); коммитов `data/events/` не больше одного на вахту.

## Progress

| Task | Status |
|---|---|
| TSK-T01 | Complete |
| TSK-T02 | Complete |
| TSK-T03 | Complete |
| TSK-T04 | Complete |
| TSK-T05 | Complete |
| TSK-T06 | Complete |
| TSK-T07 | Complete |
| TSK-T08 | Pending (после merge) |

## Evidence (2026-10-04)

- Red: `4 failed, 82 passed` (WSL Ubuntu, Python 3.14.6).
- Green: `86 passed`; `python -m compileall -q collector` (WSL и Windows 3.12); `import collector.publish, store, journal, sources` на Windows работает.
- Прод до фикса: 671 коммит за 7 дней, 140 МБ на GitHub, 160 МиБ pack локально за месяц; 2026-10-03 — 0 stale по yandex/dgis (гейтинг безопасен); журнал ≈ 50 МБ/сутки несжатого текста.

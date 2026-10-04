# Tasks: collector-chain-and-tiles-bugfix

**Spec:** [bugfix.md](bugfix.md) · **Created:** 2026-10-04

- [x] **TSK-C01**: Тесты-репродукции (red)
  - Bug: BUG-2
  - Deliverables: `tests/test_hardening.py::ChainAndTilesTests` (2 теста).
  - Acceptance: падают на старом `jammap.py`; существующие 81 зелёные.
  - Факт: старый `collector/` + новые тесты: `2 failed, 81 passed` (WSL Ubuntu, Python 3.14.6).

- [x] **TSK-C02**: Предохранитель цепочки и лёгкий артефакт
  - Bug: BUG-1
  - Deliverables: `.github/workflows/collect.yml`: `SHIFT_STARTED` в `$GITHUB_ENV`, `id: collect`, артефакт без `data/events.json`, `data/scores.csv`, `data/jam_map/*.csv`; «Queue successor»: пауза 600 с и `::error::` при `outcome != success` и < 900 с, dispatch до 3 раз (паузы 20/40/60 с), код 1 и `::error::` после трёх неудач.
  - Acceptance: dry-run shell-блока с подменённым `gh`: штатная вахта — один вызов и выход 0; падение через 90 с — `::error::`, пауза, вызов; падение через 50 мин — без паузы; два неудачных dispatch — третий успешен, выход 0; все неудачны — `::error::`, выход 1.
  - Факт: все пять сценариев прошли (bash, `sleep`/`gh` подменены функциями); YAML разбирается PyYAML, шаги и `if:` на месте.

- [x] **TSK-C03**: Блокировка тайлов доходит до guard
  - Bug: BUG-2
  - Deliverables: `collector/jammap.py::fetch_tiles`: `raise_for_status()` для 401/403/429/503; повторный проход только если первый принёс хотя бы один тайл.
  - Acceptance: регрессии BUG-2 зелёные; `test_missing_tile_is_retried_once`, `test_corrupt_png_isolated_from_good_tile` зелёные.
  - Факт: `83 passed`; `compileall` без ошибок.

- [x] **TSK-C04**: Документация
  - Deliverables: `README.md` (режим Actions, загрузка тайлов), `.kiro/steering/context.md` (режимы, глоссарий «Guard / cooldown», «Recovery artifact»), `project-standards.md` («Бюджеты»); статус спеки `collector-silent-failures-bugfix` → Merged (PR #13).

- [ ] **TSK-C05**: Проверка на проде после merge
  - Bug: BUG-1
  - Deliverables: первый run на новом workflow: шаг «Queue successor» штатной вахты делает один вызов без паузы; при следующем реальном падении — аннотация и пауза.
  - Acceptance: лог шага без `::error::` у успешной вахты; преемник в очереди сразу после завершения.

## Progress

| Task | Status |
|---|---|
| TSK-C01 | Complete |
| TSK-C02 | Complete |
| TSK-C03 | Complete |
| TSK-C04 | Complete |
| TSK-C05 | Pending (после merge) |

## Evidence (2026-10-04)

- Red: `2 failed, 81 passed` — упали `test_blocked_tiles_stop_acquisition_and_reach_the_guard`, `test_no_retry_pass_when_nothing_arrived`.
- Green: `83 passed`; `python -m compileall -q collector`.
- Workflow: PyYAML разбирает файл; dry-run «Queue successor» в пяти сценариях (см. TSK-C02).
- Прод до фикса: 2026-10-03 — 220 из 288 кадров карты `partial`, «retrying N missing tiles» каждые 5 мин (повторный проход при частичных пропусках сохранён); recovery-артефакт содержал бы ~44 МБ, из них ~15 МБ замороженных файлов.

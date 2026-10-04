# Tasks: collector-audit-leftovers

**Spec:** [bugfix.md](bugfix.md) · **Created:** 2026-10-04

- [x] **TSK-L01**: Тесты-репродукции (red)
  - Deliverables: `tests/test_hardening.py::LeftoverTests` (13 тестов), `tests/test_publish.py` (`test_invalid_calendar_days_are_ignored`, `test_failed_day_backs_off_between_passes`, `test_release_lookup_distinguishes_404_from_other_errors`, `test_late_bytes_for_an_archived_day_are_reported_once`), `test_jammap_harvest_writes_csv` → v2.
  - Факт: старый `collector/` + новые тесты: `14 failed, 88 passed` (WSL Ubuntu, Python 3.14.6). Три теста-покрытия (heartbeat, stop-event, `ops check`) зелёные и на старом коде — они фиксируют контракт, а не баг.

- [x] **TSK-L02**: Сборщик L1–L11
  - Deliverables: `collector/shift.py`, `store.py` (`sweep_temporary`), `journal.py`, `jammap.py`, `sources.py`, `ops.py` (`MIN_HEALTHY_COVERAGE`), `__main__.py`.
  - Acceptance: регрессии L1–L11 зелёные.

- [x] **TSK-L03**: Публикация P1–P5
  - Deliverables: `collector/publish.py` (`valid_day`, `_release`, `_save_state` под `try`, backoff консолидации, `archived` + WARNING о поздних байтах).
  - Acceptance: регрессии P1–P5 зелёные; `test_upload_failure_is_contained_and_retried` и `test_stale_daily_archive_in_starter_state_is_replaced` повторяют попытку через 15 минут (новое поведение backoff).

- [x] **TSK-L04**: `.gitattributes` и документация
  - Deliverables: `.gitattributes`; `README.md` (health карты, cap `Retry-After`, backoff и поздние байты); `.kiro/steering/context.md`, `project-standards.md`.

- [ ] **TSK-L05**: Проверка на проде после merge
  - Deliverables: первая вахта на новом коде: нет строк `removed N temporary files` (чистый runner) и `consolidation of … failed (attempt`; `jam_map/v2` пишется как раньше.

## Progress

| Task | Status |
|---|---|
| TSK-L01 | Complete |
| TSK-L02 | Complete |
| TSK-L03 | Complete |
| TSK-L04 | Complete |
| TSK-L05 | Pending (после merge) |

## Evidence (2026-10-04)

- Red: `14 failed, 88 passed`.
- Green: `102 passed`; `python -m compileall -q collector` (WSL и Windows 3.12); импорт `publish/store/journal/sources` на Windows.
- После `.gitattributes` git перестал предупреждать «LF will be replaced by CRLF» в этой Windows-копии.

# Tasks: registry-cache-bugfix

**Spec:** [bugfix.md](bugfix.md) · **Created:** 2026-09-23

- [x] **TSK-B01**: Тесты (red)
  - Bug: BUG-1
  - Deliverables: `tests/test_hardening.py`: `test_mutable_registry_is_not_retained_in_cache`, `test_frozen_registries_are_not_mutated_through_the_cache`
  - Acceptance: новые тесты падают на текущем коде; остальные зелёные.
  - Факт: на старом коде `2 failed, 62 passed`; в кэше 4 записи вместо 1 (legacy + 3 мёртвые версии месячного реестра).

- [x] **TSK-B02**: Раздельное чтение изменяемых и неизменяемых реестров
  - Bug: BUG-1
  - Deliverables: `collector/store.py`: `read_json` без кэша, `read_frozen_json` (кэш без `deepcopy`), `update_event_registry`
  - Acceptance: весь набор тестов зелёный.
  - Факт: `64 passed`, `compileall` без ошибок (WSL Ubuntu, Python 3.14.4).

- [x] **TSK-B03**: Замер на реальном реестре
  - Bug: BUG-1
  - Deliverables: удерживаемая память и время цикла до и после
  - Acceptance: удерживаемая память кратно ниже; время цикла не хуже.
  - Факт: два прогона на копии `data/events/2026-09.json`, 337 активных событий, 10 циклов. Удерживается 237 → 13 МиБ, пик 359 → 135 МиБ, записей кэша 8 → 1. Медиана цикла 0,9–2,3 → 1,4–1,8 с: в пределах шума файловой системы Windows.

## Progress

| Task | Status |
|---|---|
| TSK-B01 | Complete |
| TSK-B02 | Complete |
| TSK-B03 | Complete |

## Evidence (2026-09-23)

- Red: `2 failed, 62 passed`; green: `64 passed`.
- Замер (Windows, Python 3.12.10, `tracemalloc` только для памяти): удерживается 237 → 13 МиБ, пик 359 → 135 МиБ.

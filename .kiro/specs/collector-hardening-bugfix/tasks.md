# Tasks: collector-hardening-bugfix

**Spec:** [bugfix.md](bugfix.md) · **Created:** 2026-09-17

- [x] **TSK-B01**: Таймаут-обёртка шага сбора
  - Bug: BUG-1
  - Deliverables: `.github/workflows/collect.yml` — `timeout -k 5m 340m python -m collector.shift --minutes 330 --git`
  - Acceptance: YAML валиден; шаг завершается failure (код 124) вместо cancelled при зависании; `Queue successor` и recovery artifact срабатывают; ручная отмена не запускает преемника.

- [x] **TSK-B02**: Повтор недополученных тайлов и допуск partial в health
  - Bug: BUG-2
  - Deliverables: `collector/jammap.py::fetch_tiles` (один повторный проход по недостающим тайлам в пределах бюджета), `collector/shift.py::record` (в state `coverage_ratio` для jammap), `collector/ops.py::health_report` (partial + coverage_ratio ≥ 0.95 → healthy), тесты в `tests/test_hardening.py`, README (условие heartbeat)
  - Acceptance: тесты из bugfix.md проходят; существующие тесты карты и health зелёные.

- [x] **TSK-B03**: Тишина httpx в логах
  - Bug: BUG-3
  - Deliverables: `collector/shift.py::main` — `httpx`/`httpcore` на WARNING
  - Acceptance: лог следующей вахты не содержит строк `HTTP Request:`.

- [x] **TSK-B04**: Докстринг `store.py`
  - Bug: BUG-4
  - Deliverables: `collector/store.py` — модульный докстринг
  - Acceptance: описаны месячные файлы и статус legacy.

## Progress

| Task | Status |
|---|---|
| TSK-B01 | Complete |
| TSK-B02 | Complete |
| TSK-B03 | Complete |
| TSK-B04 | Complete |

## Evidence (2026-09-17)

- BUG-1: `collect.yml` — `timeout -k 5m 340m ...`, env `GH_TOKEN`/`GH_REPO`; YAML валиден.
- BUG-2: `jammap.fetch_tiles` повторный проход; `ops.health_report` допуск `partial` при `coverage_ratio ≥ 0.95`; `shift.record` пишет `coverage_ratio` в state; тесты `test_missing_tile_is_retried_once`, `test_partial_map_with_high_coverage_is_healthy`; README обновлён.
- BUG-3: `shift.main` — `httpx`/`httpcore` на WARNING (проверка по логу следующей вахты).
- BUG-4: докстринг `store.py` обновлён.
- Прогон: 61 passed (WSL, Python 3.14).

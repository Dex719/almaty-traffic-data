# Tasks: collector-silent-failures-bugfix

**Spec:** [bugfix.md](bugfix.md) · **Created:** 2026-10-04

- [x] **TSK-S01**: Тесты-репродукции (red)
  - Bug: BUG-1..3
  - Deliverables: `tests/test_hardening.py::SilentFailureTests` (6 тестов), `tests/test_publish.py::NamingTests::test_run_id_carries_the_rerun_attempt`; фейки ответов принимают `**kwargs` (`FakeResponse.json`, `SimpleNamespace(json=...)`), потому что адаптеры теперь зовут `resp.json(parse_constant=...)`.
  - Acceptance: новые тесты падают на старом коде по причинам из bugfix.md; существующие 74 зелёные.
  - Факт: на старом коде `6 failed, 75 passed`, упали ровно 6 новых (WSL Ubuntu, Python 3.14.6).

- [x] **TSK-S02**: Видимость ошибок источника
  - Bug: BUG-1
  - Deliverables: `collector/shift.py`: `describe_error()` (класс, HTTP-код, `Retry-After`; прочее через `scrub()`, 120 символов), `record()` логирует WARNING на переход статуса и INFO на восстановление, счётчик `ok_counts`, итог вахты «no successful observation from …» как WARNING + `annotate("warning", …)`; `.github/workflows/collect.yml`: `TRAFFIC_HEARTBEAT_URL: ${{ secrets.TRAFFIC_HEARTBEAT_URL }}`.
  - Acceptance: регрессии BUG-1 зелёные; здоровая вахта не пишет ни одной строки WARNING и ничего в stdout.
  - Факт: `test_source_failure_is_logged_once_and_dead_source_annotated`, `test_healthy_shift_has_no_warnings`, `test_error_description_never_carries_a_response_body` зелёные.

- [x] **TSK-S03**: `run_id` с номером попытки
  - Bug: BUG-2
  - Deliverables: `collector/publish.py::default_run_id` (`<GITHUB_RUN_ID>_<GITHUB_RUN_ATTEMPT>` при попытке ≠ 1).
  - Acceptance: первая попытка без изменений; `SEGMENT_RE` разбирает новое имя.
  - Факт: `test_run_id_carries_the_rerun_attempt` зелёный; `NamingTests`, `ConsolidatorTests` зелёные.

- [x] **TSK-S04**: Проверка ввода провайдера и изоляция `record()`
  - Bug: BUG-3
  - Deliverables: `collector/sources.py`: `_json()` с `parse_constant=_reject_constant`, `ValueError("empty layer response")`, сообщение `fetch_dgis_score` без `payload!r`; `collector/shift.py`: `record(name, observed, payload)` в `try/except (ValueError, TypeError, OverflowError)` с откатом на error-запись.
  - Acceptance: регрессии BUG-3 зелёные; `sqlite3.Error` по-прежнему fatal.
  - Факт: `test_empty_layer_response_is_an_error`, `test_non_finite_json_constants_are_rejected_inside_the_source`, `test_poison_payload_is_only_that_sources_failure` зелёные.

- [x] **TSK-S05**: Документация
  - Bug: BUG-1..3
  - Deliverables: `README.md` (режим Actions: secret heartbeat, `::warning::`, re-run; «Как собираются данные»: лог переходов, пустой слой, NaN), `.kiro/steering/context.md` (режимы, глоссарий «Вахта»), `project-standards.md` («Обработка ошибок»), `development-environment.md` (таблица переменных).
  - Факт: правки внесены вместе с кодом.

- [ ] **TSK-S06**: Secret и проверка на проде после merge
  - Bug: BUG-1
  - Deliverables: secret репозитория `TRAFFIC_HEARTBEAT_URL` (HTTPS, grace ≥ 20 мин); лог первой завершённой вахты на новом коде.
  - Acceptance: в логе здоровой вахты нет строк `source … failed`/`degraded` и аннотаций; при имитации (временно неверный URL источника недопустим на проде — достаточно дождаться первого реального сбоя) появляется одна строка на переход и `::warning::` в конце вахты; dead-man switch получает пинги раз в минуту.

## Progress

| Task | Status |
|---|---|
| TSK-S01 | Complete |
| TSK-S02 | Complete |
| TSK-S03 | Complete |
| TSK-S04 | Complete |
| TSK-S05 | Complete |
| TSK-S06 | Pending (после merge) |

## Evidence (2026-10-04)

- Базовая линия до изменений: `74 passed` (WSL Ubuntu, Python 3.14.6, deps из `requirements-dev.txt` в `--target deps`).
- Red: старый `collector/` + новые тесты: `6 failed, 75 passed`; упали `test_empty_layer_response_is_an_error`, `test_error_description_never_carries_a_response_body`, `test_non_finite_json_constants_are_rejected_inside_the_source`, `test_poison_payload_is_only_that_sources_failure`, `test_source_failure_is_logged_once_and_dead_source_annotated`, `test_run_id_carries_the_rerun_attempt`.
- Green: `81 passed`; `python -m compileall -q collector` без ошибок (WSL и Windows 3.12); `import collector.publish, store, journal, sources` на Windows работает.
- Прод до фикса: лог run 37193420258 (success, 5 ч 30 мин) — ни одной строки WARNING/ERROR; `collect.yml` без `TRAFFIC_HEARTBEAT_URL`; `gh run list` — 0 failure за 100 run, артефактов 0.

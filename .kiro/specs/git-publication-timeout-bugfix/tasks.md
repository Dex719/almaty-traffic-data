# Tasks: git-publication-timeout-bugfix

**Spec:** [bugfix.md](bugfix.md) · **Created:** 2026-09-25

- [x] **TSK-B01**: Тесты (red)
  - Bug: BUG-1
  - Deliverables: `tests/test_hardening.py`: `test_network_git_gets_longer_timeout_than_local_git`, `test_failed_network_git_is_logged_with_reason_and_scrubbed`
  - Acceptance: новые тесты падают на текущем коде; остальные зелёные.
  - Факт: на старом коде 2 failed: `20 not greater than 20` и в логе только `Data publication failed` без причины.

- [x] **TSK-B02**: Таймаут и диагностика сетевых команд git
  - Bug: BUG-1
  - Deliverables: `collector/shift.py`: `GIT_NETWORK_TIMEOUT_SEC = 60`, выбор таймаута и WARNING в `commit_and_push.run`
  - Acceptance: весь набор тестов зелёный.
  - Факт: `74 passed`, `compileall` без ошибок (WSL Ubuntu, Python 3.14.4).

- [ ] **TSK-B03**: Проверка на проде
  - Bug: BUG-1
  - Deliverables: логи вахт после merge
  - Acceptance: первый цикл публикуется без `Data publication failed`; если сбой всё же будет, WARNING называет команду и причину.

## Progress

| Task | Status |
|---|---|
| TSK-B01 | Complete |
| TSK-B02 | Complete |
| TSK-B03 | Pending (после merge) |

## Evidence

- До фикса: `Data publication failed` в 6 из 16 вахт (35641876973, 35692883420, 35843486197, 35878602605, 36003115848, 36042143878), всегда на первом цикле; `commit_and_push` 47–68 с.
- Red: `2 failed`; green: `74 passed`.

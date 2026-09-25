# Bugfix Spec: git-publication-timeout-bugfix

**Spec Type:** Bugfix Spec (Micro) · **Created:** 2026-09-25 · **Status:** Fixed (74 passed); проверка на проде после merge (TSK-B03)
**Источник:** проверка TSK-B09 спеки [release-asset-state-bugfix](../release-asset-state-bugfix/tasks.md) 2026-09-25, логи 16 последних вахт (21–25 сентября).

## BUG-1 Первая сетевая git-операция вахты упирается в таймаут 20 с

- **Current behavior:** в 6 из 16 вахт первый цикл публикации (сегмент `seq 0000`) заканчивается `ERROR Data publication failed`. `commit_and_push` длится 47–68 с, то есть три попытки, в каждой `pull` или `push` обрезается `GIT_TIMEOUT_SEC = 20`. Эти коммиты публикует следующий цикл через 15 минут: данные не теряются, но live-представления в git отстают на 15 минут. Причину из лога не установить, потому что stderr git не записывается.
- **Expected behavior:** сетевые команды git (`pull`, `push`) получают таймаут 60 с в пределах прежнего бюджета 120 с на цикл; локальные остаются на 20 с. Каждая неудачная сетевая команда пишет WARNING с кодом возврата и хвостом stderr (до 300 символов, через `publish.scrub`).
- **Unchanged behavior:** бюджет 120 с на цикл, 3 попытки, паузы между ними, `PUSH_STALL_MIN`, `GIT_TERMINAL_PROMPT=0`, значение, которое возвращает `commit_and_push`.
- **Root cause:** таймаут 20 с меньше длительности первой сетевой операции свежей shallow-копии раннера (замер: три попытки по ~20 с). Какая именно команда медленная и почему, не установлено, потому что stderr не сохранялся. WARNING из этого фикса даст факт при следующем случае.
- **Regressions to check:** `test_git_add_failure_is_not_success`, `test_git_clean_index_still_pushes_pending_commit`, `GitPublicationTests`; новые `test_network_git_gets_longer_timeout_than_local_git`, `test_failed_network_git_is_logged_with_reason_and_scrubbed`.

## Не в объёме

- Смена `fetch-depth` или отказ от shallow-клона в `collect.yml`.
- Перенос реестра событий из первого цикла публикации.

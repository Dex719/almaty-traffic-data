# Bugfix Spec: collector-chain-and-tiles-bugfix

**Spec Type:** Bugfix Spec (bugfix + tasks одним проходом) · **Created:** 2026-10-04 · **Status:** In progress
**Источник:** аудит 2026-10-04 (локальный отчёт, пункты 3 и 4 топ-5). Прод здоров (0 failure за 100 run), оба бага латентные.
**Связь:** [collector-silent-failures-bugfix](../collector-silent-failures-bugfix/bugfix.md) (тот же аудит), контракт «Guard / cooldown» в `.kiro/steering/context.md`.

## BUG-1 Цепочка Actions без предохранителя

- **Current behavior:** шаг «Queue successor» (`if: always() && !cancelled()`) делает один вызов dispatch без задержки и без повтора. Сборщик, падающий на старте (битый merge, недоступный pin зависимости, повторяющийся payload провайдера), порождает горячий цикл ~1–2 мин на run; каждый run загружает recovery-артефакт `data/` целиком (~44 МБ, из них ~15 МБ — замороженные `data/events.json`, `data/scores.csv`, `data/jam_map/2026-09-0*.csv`, которые и так лежат в git). Один сбой API dispatch разрывает цепочку, и её перезапускает только cron, который GitHub троттлит.
- **Expected behavior:** если шаг сбора завершился не `success` быстрее чем за 15 минут, преемник ставится в очередь через 10 минут, на странице run — `::error::`. Вызов dispatch повторяется до трёх раз с паузами 20/40/60 с; после трёх неудач шаг завершается кодом 1 с `::error::`, цепочку подхватывает cron. Артефакт не включает замороженные legacy-файлы.
- **Unchanged behavior:** штатная вахта (330 мин, `success`) ставит преемника сразу; отменённый run преемника не ставит; `include-hidden-files` (журнал в `.state/`) и retention 14 дней.
- **Root cause:** цепочка проектировалась для выживания, а не для ограничения ущерба при детерминированном падении.
- **Regressions to check:** офлайн-тестов для YAML нет; проверка — локальный dry-run shell-блока с подменённым `gh` (четыре сценария: success, короткий failure, долгий failure, недоступный dispatch) и первая вахта на новом workflow.

## BUG-2 Блокировка тайлов карты не доходит до SourceGuard

- **Current behavior:** в `jammap.fetch_tiles` исключение бросается только на 429/503. 401/403, редирект на капчу или HTML со статусом 200 возвращают `None`; повторный проход запрашивает все пропавшие тайлы снова; `capture` поднимает `RuntimeError("insufficient tiles")` без `.response`, и `SourceGuard` применяет правило «3 ошибки → 10 мин» вместо контрактного «401/403 → час». Итог: около 3 × 416 запросов каждые 20 минут к endpoint, который нас блокирует, вдевятеро больше контракта.
- **Expected behavior:** 401/403 обрабатываются как 429/503: первый такой ответ останавливает захват, исключение несёт код ответа, guard даёт час. Повторного прохода нет, если первый не принёс ни одного тайла.
- **Unchanged behavior:** бюджет 120 с, 4 потока, интервал 0.2 с, один повторный проход при частичных пропусках, классификация `ok/partial/low_coverage`.
- **Root cause:** `fetch()` отбрасывал код ответа для всего, кроме 429/503.
- **Regressions to check:** `test_missing_tile_is_retried_once`, `test_corrupt_png_isolated_from_good_tile`, `test_retry_after_is_honored`; новые `test_blocked_tiles_stop_acquisition_and_reach_the_guard`, `test_no_retry_pass_when_nothing_arrived`.

## Раскатка

Изменения `collect.yml` применяются к следующему dispatched run; текущая вахта дорабатывает на старом определении. Код карты безопасен для перекрывающей вахты (меняется только поведение при блокировке). Откат — revert.

## Не в объёме

- Второй эшелон аудита: частота коммитов реестра, finally-блок `shift.py`, stale-значения в CSV, расхождения README, серверный бэкап.

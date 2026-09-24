# Bugfix Spec: registry-cache-bugfix

**Spec Type:** Bugfix Spec (Micro) · **Created:** 2026-09-23 · **Status:** Fixed locally (64 passed), ждёт merge
**Источник:** ревью 2026-09-23; замер на реальном `data/events/2026-09.json` (13,6 МБ, 30 092 карточки, 337 активных), Windows, Python 3.12.10.

## BUG-1 Кэш чтения JSON удерживает мёртвые версии месячного реестра событий

- **Current behavior:** `store.read_json` читает все файлы через `lru_cache(maxsize=8)` с ключом `(path, mtime_ns, size)` и отдаёт `deepcopy`. Месячный реестр `data/events/YYYY-MM.json` переписывается при каждом обновлении (раз в 5 минут), поэтому ключ всегда новый. Кэш по нему не попадает ни разу, зато хранит до 8 разобранных устаревших версий. Замер после 10 циклов: удерживается ~237 МиБ, пик 359 МиБ. Кроме того, на каждом цикле выполняется `deepcopy` неизменяемых файлов: legacy `data/events.json` и реестра прошлого месяца.
- **Expected behavior:** изменяемый месячный реестр читается без кэша, каждый раз свежей копией. Неизменяемые файлы (legacy и прошлый месяц) берутся из кэша без `deepcopy`, а вызывающий код копирует карточку (`dict(old)`) перед изменением, как делает и сейчас. Удерживается только неизменяемое.
- **Unchanged behavior:** формат и содержимое реестров; перенос `first_seen` через границу месяца и из legacy; атомарная запись.
- **Root cause:** один кэш для изменяемых и неизменяемых файлов; `deepcopy` всего файла как защита от мутаций там, где хватает копии изменяемой карточки.
- **Regressions to check:** `test_legacy_migration_preserves_first_seen`, `test_layer_namespaces_do_not_collide`, перенос между месяцами в `tests/test_collector.py`; новые `test_mutable_registry_is_not_retained_in_cache`, `test_frozen_registries_are_not_mutated_through_the_cache`.

## Не в объёме

- Держать реестр в памяти вахты между циклами или писать его реже: меняет гарантии долговечности live-представления.

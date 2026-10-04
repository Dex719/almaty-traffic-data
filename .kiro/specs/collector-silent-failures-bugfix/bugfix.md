# Bugfix Spec: collector-silent-failures-bugfix

**Spec Type:** Bugfix Spec (bugfix + tasks одним проходом) · **Created:** 2026-10-04 · **Status:** Fixed in branch `fix/collector-silent-failures-bugfix` (81 passed), ждёт PR; TSK-S06 (secret + проверка на проде) после merge
**Источник:** аудит 2026-10-04 (локальный отчёт, пункты 1, 2 и 5 топ-5): чтение кода и живых данных, 5 офлайн-аудиторов, перекрёстная проверка. Прод на момент аудита здоров (0 failure за 100 run, все источники ok), баги латентные.
**Связь:** уточняет стандарты «Обработка ошибок» и «Логи» (`.kiro/steering/project-standards.md`), контракт `run_id` спеки [release-archive-publishing](../release-archive-publishing/requirements.md).

## BUG-1 Мёртвый источник невидим: ни лога, ни аннотации, ни внешнего сигнала

- **Current behavior:** `shift.record()` и цикл опроса не вызывают `logger` ни на одном пути ошибки источника; в журнал попадает только имя класса исключения (`HTTPStatusError` без кода). Код выхода вахты 0, если за вахту удался хоть один опрос (`successes == 0` — единственный критерий), а 2ГИС опрашивается каждую минуту. `health.json` в Actions никто не читает, `TRAFFIC_HEARTBEAT_URL` в `collect.yml` не передаётся, то есть внешнего мониторинга в режиме Actions нет, вопреки `context.md`. Лог успешной вахты 37193420258 не содержит ни одной строки WARNING/ERROR. Яндекс, отдающий 403 (cooldown час), даёт один error-ряд в час, пустые колонки и зелёные run'ы неделями.
- **Expected behavior:** смена статуса источника логируется одной строкой WARNING на переход (`source yandex failed: HTTPStatusError, HTTP 403, Retry-After 120; cooldown 3600 s`), восстановление — INFO. В конце вахты источник без единого успешного наблюдения даёт WARNING и `::warning::` на странице run. `collect.yml` передаёт необязательный secret `TRAFFIC_HEARTBEAT_URL`: пока он задан, dead-man switch пингуется раз в минуту только при здоровье всех источников.
- **Unchanged behavior:** код выхода (мёртвый провайдер не должен порождать recovery-артефакт каждую вахту); запись статусов в журнал и `health.json`; правила cooldown; запрет на логирование тел ответов и комментариев (`describe_error` берёт только класс, HTTP-код и `Retry-After`, прочие сообщения — через `scrub()` и обрезку).
- **Root cause:** статус ошибки считался внутренним состоянием (журнал + health), а не событием для оператора; режим Actions унаследовал серверную модель «heartbeat настроен в `/etc/almaty-traffic.env`», которой у runner нет.
- **Regressions to check:** `test_shift_guard_cooldown`, `test_all_sources_failed_is_not_success`, `test_partial_layer_survives_in_monthly_registry`, `test_failed_network_git_is_logged_with_reason_and_scrubbed`; новые `test_source_failure_is_logged_once_and_dead_source_annotated`, `test_healthy_shift_has_no_warnings`, `test_error_description_never_carries_a_response_body`.

## BUG-2 «Re-run failed jobs» молча теряет или портит дневной архив

- **Current behavior:** `publish.default_run_id()` берёт только `GITHUB_RUN_ID`; `GITHUB_RUN_ATTEMPT` не используется. Повторная попытка имеет тот же `run_id`, но пустой диск. Если день консолидирует она сама, `build_day` пропускает сегменты первой попытки как «свои» (`seg.run_id == self.run_id`, покрытые локальными файлами, которых нет), а `cleanup_staging` затем удаляет их: тихая потеря. Если день консолидирует другая вахта, сегменты обеих попыток группируются под одним `run_id`, и `assemble_append_only` склеивает байты попытки 2 поверх байтов попытки 1 (оба писали `snapshots/D.jsonl` и `jam_map/v2/D.csv` с offset 0) посреди строки: испорченный неизменяемый архив. Батчи `observations` не страдают (имя = digest содержимого).
- **Expected behavior:** при `GITHUB_RUN_ATTEMPT` ≠ 1 `run_id = <GITHUB_RUN_ID>_<attempt>`. Сегменты первой попытки становятся чужими: скачиваются и сливаются как отдельный run. Имена сегментов первых попыток не меняются.
- **Unchanged behavior:** `RUN_ID_RE` (`_` допустим), `SEGMENT_RE`, формат манифестов, локальный `local_<host>_<pid>`, `--run-id`.
- **Root cause:** допущение «один run = одна попытка».
- **Regressions to check:** `NamingTests`, `test_day_assembled_from_two_runs_and_local_files`; новый `test_run_id_carries_the_rerun_attempt`.

## BUG-3 Непроверенный ввод провайдера: ложный полный снимок и crash loop

- **Current behavior:** (а) пустой `[]` от `tugc` принимается как полный слой: snapshot `complete=true` без событий (по контракту это доказательство исчезновения всех событий) и `ev_* = 0` в CSV, хотя в октябре активных событий никогда не было меньше 179. (б) `resp.json()` принимает нестандартные `NaN`/`Infinity`; `likes`, `created_ts`, `start_ts`, `finish_ts`, третья координата, `time` у 2ГИС не валидируются; `record(name, observed, payload)` стоит вне `try`, а `Journal.record` использует `allow_nan=False` → `ValueError` в главном цикле → `fatal` → exit 1 → преемник получает тот же payload → горячий цикл dispatch с recovery-артефактом на каждой итерации. Попутно нарушается изоляция источников: результаты остальных futures того же тика не записываются. (в) `fetch_dgis_score` вкладывает `payload!r` в текст исключения, который после BUG-1 попал бы в лог.
- **Expected behavior:** пустой список слоя — `ValueError("empty layer response")`, слой записывается как ошибка, snapshot — `complete=false`. JSON разбирается с `parse_constant`, отвергающим `NaN`/`Infinity` внутри адаптера. Payload, который журнал не может закодировать, записывается как ошибка этого источника (`error_code=ValueError`), остальные источники тика пишутся как обычно, вахта продолжается. Текст исключения без ответа провайдера.
- **Unchanged behavior:** `invalid_rows`/`partial` для битых строк; `sqlite3.Error` по-прежнему fatal (инфраструктурный сбой); схема CSV/snapshot.
- **Root cause:** доверие к структуре ответа за пределами проверяемых полей; граница «ошибка источника / ошибка сборщика» проведена до `record()`, а сериализация происходит внутри него.
- **Regressions to check:** `test_invalid_container_is_quarantined`, `test_bad_event_does_not_drop_valid_event`, `test_dgis_score_picks_region`, `test_events_normalized_and_cameras_dropped`; новые `test_empty_layer_response_is_an_error`, `test_non_finite_json_constants_are_rejected_inside_the_source`, `test_poison_payload_is_only_that_sources_failure`.

## Раскатка

Изменения безопасны для одной перекрывающей вахты: старый код ничего не читает из новых полей, имена сегментов первых попыток и формат журнала не меняются. Secret `TRAFFIC_HEARTBEAT_URL` можно добавить в любой момент после merge (без него строка env пуста, `ping_heartbeat` молчит как раньше). Откат — revert коммита.

## Не в объёме

- Backoff и retry self-dispatch, облегчение recovery-артефакта (пункт 3 аудита).
- 401/403 на тайлах карты (пункт 4), stale-значения в CSV, рост репозитория, расхождения README (второй эшелон аудита).

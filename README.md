# almaty-traffic-data

**Открытый набор данных о дорожной обстановке Алматы.** Каждую минуту, круглые
сутки, в собственной схеме и с проверяемой историей: баллы пробок, ДТП,
ремонты, перекрытия, комментарии водителей и категориальная карта пробок на
геометриях OpenStreetMap.

Провайдеры показывают «сейчас». Этот репозиторий хранит «всегда»: что было на
дорогах в 08:30 такого-то числа, сколько жило конкретное ДТП, как менялся балл
в течение года. Данные публикуются открыто, без регистрации и API-ключей, и не
зависят от условий, форматов и доступности источников.

## English summary

An open, provider-independent dataset of road traffic in Almaty, Kazakhstan:
Yandex and 2GIS congestion scores every minute, user-reported and official road
events (crashes, roadworks, closures, driver comments) every five minutes, and a
categorical jam map on OpenStreetMap road geometries. Providers only show "now";
this repository keeps a verifiable "always" in its own schema, so the history
does not depend on any provider's API, terms or availability.

- **Live views** (git, `data/`): monthly score CSVs, monthly event registries,
  road geometries. `git clone --depth 1 https://github.com/Dex719/almaty-traffic-data.git`
- **Full history** (GitHub Releases `data-YYYY-MM`): one `traffic-YYYY-MM-DD.tar.xz`
  per day with the raw observation journal, event snapshots and jam-map classes,
  plus `MANIFEST.json` with the SHA-256 of every member. Verify downloads against
  the `digest` field of the GitHub release-assets API. A day is final at
  `D+1 00:45 UTC`.
- **Licence**: data CC BY 4.0 (`LICENSE-DATA.md`), code MIT (`LICENSE`).
  Attribution: *Almaty traffic data, https://github.com/Dex719/almaty-traffic-data, CC BY 4.0.*
- **Run your own collector**: Python 3.12+, Linux; see «Свой сборщик» below.

The rest of this document is in Russian.

## Принципы

- **Независимость от провайдеров.** Значения берутся из публичных endpoints
  Яндекса и 2ГИС, но схема данных своя: события с ключами `layer:id`, баллы в CSV,
  классы пробок на геометриях OSM. Источник можно заменить или добавить, история
  при этом остаётся целой. Сырые ответы источников сохраняются в журнале, поэтому
  смена формата у провайдера не уничтожает прошлое.
- **Проверяемость.** Каждый пакет наблюдений именуется SHA-256 своего содержимого,
  каждый дневной архив имеет `MANIFEST.json`, каждая загрузка на GitHub
  подтверждается digest из API. Что вы скачали, то и было собрано.
- **Честность измерений.** Время получения и время провайдера — разные поля.
  Пустое значение не равно нулю. Неполный снимок помечен как неполный. Балл —
  не скорость, класс пробки — не время в пути. Ограничения описаны ниже, а не спрятаны.
- **Воспроизводимость.** Сборщик — открытый код в этом же репозитории; любой
  может запустить свой экземпляр и получить те же файлы.
- **Открытая лицензия.** Данные — CC BY 4.0 (`LICENSE-DATA.md`), код — MIT
  (`LICENSE`). Используйте, анализируйте и перепубликуйте со ссылкой на источник.

## Как получить данные

Аккаунты у провайдеров не нужны. Достаточно git и, для архивов, GitHub CLI
или прямых ссылок на релизы.

### Живые сводки (git)

```bash
git clone --depth 1 https://github.com/Dex719/almaty-traffic-data.git
```

| Файл | Что внутри | Обновление |
|---|---|---|
| `data/scores/YYYY-MM.csv` | балл Яндекса и 2ГИС, тренд, длина затруднений, счётчики активных событий по типам | каждые 15 минут |
| `data/events/YYYY-MM.json` | реестр событий месяца: тип, координаты, комментарий, `first_seen` / `last_seen` | не чаще раза в час |
| `data/jam_map/ways.json`, `data/jam_map/registries/*.json` | геометрии дорог OSM, по которым считаются классы пробок; неизменяемые версии | при изменении |

### Полная история (GitHub Releases)

Тяжёлые потоки лежат в релизах `data-YYYY-MM`, по одному архиву на день:

```bash
gh release download data-2026-09 --repo Dex719/almaty-traffic-data --pattern 'traffic-2026-09-16.tar.xz'
tar -xJf traffic-2026-09-16.tar.xz
```

Внутри `traffic-YYYY-MM-DD.tar.xz`:

- `observations/<day>/<sha256>.jsonl` — канонический журнал: каждое наблюдение
  каждого источника с полезной нагрузкой, статусом и временем; день — UTC-дата
  первой строки пакета;
- `snapshots/<day>.jsonl` — какие события были активны в каждый момент опроса
  (алматинская дата);
- `jam_map/v2/<day>.csv` — классы пробок `G/Y/R/D` для каждой геометрии каждые
  5 минут (алматинская дата);
- `MANIFEST.json` — SHA-256 и размер каждого файла, источники, отброшенные
  дубликаты, разрывы.

Проверка скачанного:

```bash
gh api repos/Dex719/almaty-traffic-data/releases/tags/data-2026-09 --jq '.assets[] | select(.name=="traffic-2026-09-16.tar.xz") | .digest'
sha256sum traffic-2026-09-16.tar.xz
```

День D закрывается в `D+1 00:45 UTC`; до этого его данные лежат только во
внутреннем prerelease `staging` (сегменты по 15 минут). На `staging` не
опирайтесь: сборщик удаляет сегменты после сборки дневного архива.

### Пример на Python

```python
import csv, gzip, json, tarfile
from pathlib import Path

# баллы за месяц
rows = list(csv.DictReader(open("data/scores/2026-09.csv", encoding="utf-8")))
print(rows[-1]["ts_almaty"], rows[-1]["yandex_score"], rows[-1]["dgis_score"])

# журнал за день из архива
with tarfile.open("traffic-2026-09-16.tar.xz", "r:xz") as tar:
    for member in tar.getmembers():
        if member.name.startswith("observations/"):
            for line in tar.extractfile(member):
                obs = json.loads(line)
                if obs["source"] == "events_user" and obs["status"] == "ok":
                    crashes = [e for e in obs["payload"]["events"] if e["type"] == "crash" and e["in_aoi"]]
                    print(obs["observed_at"], len(crashes), "ДТП в городе")
```

## Что внутри данных

### Баллы (`scores`)

Столбцы: `ts_utc`, `ts_almaty`, `yandex_score`, `yandex_trend` (−1 спадают, 0
стабильно, 1 растут), `yandex_jam_km`, `dgis_score`, `ev_crash`, `ev_roadwork`,
`ev_restriction`, `ev_comment`, `ev_other`. Строка отражает завершившуюся группу
опросов, не гарантированную минутную сетку. Пустое поле не равно нулю; нулевая
длина пробок сохраняется как `0.0`. Счётчики событий заполняются только при
полном результате обоих слоёв событий. Времена — полный ISO 8601 с
микросекундами; ранние строки сентября 2026 записаны с минутной точностью.

### События (`events`, `snapshots`)

Два слоя 2ГИС: `user` — сообщения водителей, `2gis` — официальные ограничения.
Типы: `crash`, `roadwork`, `restriction`, `comment`, `other`; камеры исключены.
Ключ события `layer:id`. Карточка в реестре живёт после исчезновения события:
`last_seen − first_seen` — окно наблюдения на карте, не точная длительность.
Событие, пережившее границу месяца, переезжает в новый файл с прежним `first_seen`.
События вне AOI не удаляются, а помечаются `in_aoi=false`; для ограничений
проверяется пересечение сегмента с прямоугольником AOI
`(43.165, 43.375, 76.72, 77.06)` (lat min/max, lon min/max).
Снимок с `complete=false` нельзя использовать для вывода, что событие исчезло.
Комментарии водителей могут содержать персональные данные — см. «Приватность».

### Карта пробок (`jam_map`)

Растровый слой пробок Яндекса читается по точкам внутри AOI вдоль геометрий OSM
(`ways.json`), классы взвешиваются по длине: `G` свободно, `Y` затруднено, `R`
пробка, `D` глухая пробка, `-` нет данных. `quality`: `ok` — получены все тайлы,
`partial` — часть, `low_coverage` — распознано менее 50 % геометрий. Столбец
`matched_fraction` — доля распознанной длины для каждой геометрии. Порядок
классов определяется версией реестра (`registry_version`). Это категориальная
оценка растра, **не скорость направленного ребра**; смена цветовой схемы у
провайдера требует проверки человеком.

### Журнал (`observations`)

Одна строка — одно наблюдение: `observation_id`, `source`, `observed_at`
(время получения, UTC с микросекундами), `source_ts` (время провайдера, если
есть), `status` (`ok`, `partial`, `stale`, `error`, …), `error_code`, `payload`.
Повтор `observation_id` с другим содержимым отклоняется ещё при сборе. При
объединении копий дедуплицируйте по `observation_id`, не по минуте и баллу.
`tm` в URL тайла и время получения не доказывают, что провайдер обновил данные.

## Как собираются данные

Один процесс, независимые расписания: балл 2ГИС — 60 с, балл Яндекса — 240 с,
каждый слой событий и карта — 300 с. Пропущенные слоты не догоняются шквалом
запросов. Ошибка одного источника не блокирует остальные. `Retry-After`
соблюдается; после серии ошибок включается cooldown. Загрузка тайлов ограничена
четырьмя потоками, интервалом 0.2 с и бюджетом 120 с с одним повторным проходом.

Канонический журнал — SQLite/WAL с одной транзакцией на наблюдение. Каждые
15 минут неэкспортированные записи упаковываются в пакет
`observations/YYYY-MM-DD/<sha256>.jsonl.gz`; состав пакета фиксируется в базе до
записи файла, после сбоя повторяется тот же пакет. Живые сводки — производные
представления; при аварии они могут отставать от журнала.

Публичный endpoint не означает неограниченную лицензию: условия и квоты
провайдеров остаются их условиями. Добавляя источник, проверяйте допустимость
перепубликации.

## Свой сборщик

Python 3.12 или 3.13, Linux для process lock и systemd. Pillow нужен для карты.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest tests/ -q
python -m collector                                      # один цикл без публикации
python -m collector.shift --minutes 5                    # короткий запуск без публикации
python -m collector.shift --forever --data-dir /var/lib/almaty-traffic
```

Два режима:

- **Постоянный сервер — рекомендуемый.** `deploy/almaty-traffic.service`
  (systemd, watchdog, перезапуск), данные на постоянном диске, проверяемые
  резервные копии, внешний heartbeat. Git и GitHub не нужны.
- **GitHub Actions — best-effort.** Вахта 330 минут, очередь через
  `concurrency`, следующий запуск через `workflow_dispatch`, cron-страховка.
  Только этот режим использует `--git`: живые сводки коммитятся в `main`,
  архивные потоки уходят сегментами в Releases. Процесс ограничен `timeout`
  на 340 минут, чтобы зависание завершалось как failure шага и преемник
  запускался. GitHub может задерживать и пропускать schedule; гарантии 24/7 нет.

Не запускайте два экземпляра на один каталог (файловая блокировка) и не
совмещайте серверный экземпляр с Actions для одной публичной ленты.

### Развёртывание на сервере

Предполагаются `/opt/almaty-traffic-data` и пользователь `traffic`. Файлы из
`deploy/` ничего не устанавливают сами.

1. Клонируйте проверенную версию, создайте `.venv`, установите `requirements.txt`
   (закреплены зависимости верхнего уровня; обновления приходят от Dependabot).
2. Создайте пользователя `traffic` и каталог `/var/lib/almaty-traffic/jam_map`,
   скопируйте туда `data/jam_map/ways.json`.
3. Скопируйте `.env.example` в `/etc/almaty-traffic.env` (права `0600`). При
   необходимости задайте `TRAFFIC_HEARTBEAT_URL` внешнего dead-man-switch:
   сигнал уходит только после сохранённых здоровых результатов всех источников
   (карта `partial` считается здоровой при покрытии ≥ 95 %). Порог отсутствия
   сигнала — 5 минут.
4. Установите `deploy/almaty-traffic.service`:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now almaty-traffic.service
sudo journalctl -u almaty-traffic.service -f
.venv/bin/python -m collector.ops check --data-dir /var/lib/almaty-traffic
```

SIGTERM прекращает новые опросы, дожидается активных задач и выполняет
финальный экспорт. Нулевой сбор или ошибка финального сохранения дают ненулевой
код. Оповещение о выключенном сервере должно быть внешним. Health check требует
минимум 100 МиБ свободного места; с `TRAFFIC_REQUIRE_BACKUP=1` отсутствие
проверенной копии или её возраст более 36 часов останавливает heartbeat.

### Резервные копии и восстановление

```bash
.venv/bin/python -m collector.ops export --data-dir /var/lib/almaty-traffic
.venv/bin/python -m collector.ops backup --data-dir /var/lib/almaty-traffic --destination /mnt/traffic-backups
```

Backup использует SQLite backup API и `integrity_check`, пишет gzip и SHA-256
manifest, копирует версии реестров геометрий. Смонтируйте внешнюю файловую
систему в `/mnt/traffic-backups` и установите backup service/timer из `deploy/`;
без mount point задание не запускается. Daily timer допускает потерю до суток —
для меньшего RPO увеличьте частоту. Восстановление: остановить сервис, проверить
SHA-256 по manifest, распаковать snapshot в новый `.state/journal.sqlite3`,
выполнить `PRAGMA integrity_check`, восстановить реестры и права, запустить
экспорт. Не копируйте живой `.sqlite3` без WAL.

### Эксплуатация публикации в Releases

Внутри сборщика: каждые 15 минут неотгруженные байты архивных потоков уходят
неизменяемым сегментом в `staging`; закрытые дни собираются в дневные архивы в
фоновом потоке; сегменты удаляются после консолидации всех их дней. Повторная
отгрузка одних байт безопасна: при сборке дня дубликаты строк отбрасываются.

```bash
GH_REPO=Dex719/almaty-traffic-data python -m collector.publish status --data-dir data
GH_REPO=Dex719/almaty-traffic-data python -m collector.publish consolidate --data-dir data
```

Меняя код публикации, помните: после merge текущая вахта дорабатывает на старом
коде и подтягивает `main` каждые 15 минут. Если изменение небезопасно для одной
перекрывающей вахты — отмените run, дождитесь recovery artifact, запустите новую
вахту вручную и отгрузите хвост из артефакта сегментом. История git не
переписывается; данные до перехода на Releases остались в прошлых коммитах.

## Участие

- **Новый источник** — адаптер в `collector/sources.py`, возвращающий словарь с
  полезной нагрузкой и `ts` провайдера, плюс запись в расписание `shift.py` и
  офлайн-тест с фейковым ответом. Сырой ответ должен целиком сохраняться в
  журнале, производные сводки — отдельно.
- **Изменение схемы** — только новым файлом или каталогом (как `jam_map/v2`);
  старые файлы замораживаются, не переписываются.
- Работа ведётся через спеки в `.kiro/specs/` (Kiro-формат), контекст проекта —
  в `.kiro/steering/`. Тесты только офлайн: без запросов к провайдерам и GitHub.
- PR по шаблону: изменения, проверка, эксплуатационные ограничения. CI — Python
  3.12 и 3.13.

## Приватность и безопасность

Тексты комментариев водителей и recovery-артефакты могут содержать персональные
данные: определите политику публикации и удаления, прежде чем расширять
аудиторию, и ограничивайте доступ к артефактам. Токен GitHub берётся только из
`GH_TOKEN` и вырезается из сообщений об ошибках; PAT никогда не попадает в
исходники, remote URL, логи или PR. Recovery artifacts не заменяют независимую
резервную копию.

## Лицензия

- **Данные** (`data/` в git и все активы релизов `data-YYYY-MM`) —
  [CC BY 4.0](LICENSE-DATA.md): свободное использование, изменение и
  перепубликация, в том числе коммерческая, с указанием источника.
  Рекомендуемая атрибуция:

  > Almaty traffic data, https://github.com/Dex719/almaty-traffic-data, CC BY 4.0.

- **Код** сборщика — [MIT](LICENSE).

Лицензия покрывает компиляцию и производные файлы проекта, а не сервисы
провайдеров: условия Яндекса и 2ГИС продолжают действовать при прямом
обращении к их сервисам. Комментарии водителей могут содержать персональные
данные — соблюдение законодательства при повторном использовании лежит на
пользователе данных. Данные предоставляются «как есть», без гарантий полноты и
точности; пробелы и частичные наблюдения записаны явно, а не скрыты.

# almaty-traffic-data

Сборщик дорожных данных Алматы для исследований krisha-fair-price / baǵam.
Каждые 30 минут снимает и коммитит в этот репозиторий:

| Что | Источник | Куда пишется |
| --- | --- | --- |
| Балл пробок, тренд, км затруднений | Яндекс `export.yandex.ru/bar/reginfo.xml?region=162` | `data/scores.csv` |
| Балл пробок | 2ГИС `jam.api.2gis.com/meta?reg=67` | `data/scores.csv` |
| ДТП, ремонты, перекрытия, комментарии водителей | 2ГИС `tugc.2gis.com/1.0/layers/user` | `data/events.json` + счётчики в `scores.csv` |
| Официальные ограничения (с сроками) | 2ГИС `tugc.2gis.com/1.0/layers/2gis` | `data/events.json` |

Все каналы публичные, без токенов. Камеры не логируются — это статичный
справочник, а не события.

## Данные

- **`data/scores.csv`** — по строке на замер: `ts_utc, ts_almaty,
  yandex_score, yandex_trend, yandex_jam_km, dgis_score, ev_crash,
  ev_roadwork, ev_restriction, ev_comment, ev_other`.
- **`data/events.json`** — реестр событий: `id → карточка` с координатами,
  типом, текстом комментария и парой `first_seen`/`last_seen`. Исчезнувшее
  с карты событие остаётся в реестре — по паре видно время жизни
  (сколько рассасывалось ДТП, сколько длился ремонт).
- **`data/snapshots/YYYY-MM/DD.jsonl`** — на каждый замер список id активных
  событий: восстанавливает картину «что висело на карте в такой-то момент».

## Запуск

```bash
pip install -r requirements.txt
python -m collector          # один замер → data/
python -m pytest tests/ -q   # оффлайн-тесты
```

## Расписание

GitHub Actions, каждые 30 минут: **`ops/github-workflows/collect.yml`**.

⚠️ После первого пуша файл нужно один раз перенести руками (у бота нет
права писать в `.github/workflows/`):

```bash
git mv ops/github-workflows/collect.yml .github/workflows/collect.yml
git commit -m "ci: включить сбор по расписанию" && git push
```

Дальше всё само: workflow снимает замер и коммитит diff. Ручной запуск —
кнопка Run workflow (workflow_dispatch). Расход Actions ≈ 1 мин × 48
запусков/день ≈ 1 500 мин/мес — впритык к бесплатным 2 000 мин приватных
репо; если станет тесно, репо можно сделать публичным (данные и так
публичные) — тогда лимита нет.

## Зачем

Ряд `scores.csv` + реестр событий = обучающие данные для транспортной
модели baǵam (равновесие Уордропа, калибровка по баллу) и будущей фичи
транспортной доступности в krisha-fair-price: профиль недели, эффект
ремонтов/ДТП, 1 сентября, снегопады.

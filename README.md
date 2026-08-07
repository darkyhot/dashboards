# УЗП — генератор дэшбордов

Инструмент строит дэшборды по данным УЗП. Работает в **двух контурах** одной
кодовой базой:

- **Открытый** (этот) — контейнер Postgres + DeepSeek. Здесь код пишется и
  отлаживается на **синтетике**, сгенерированной по профилям из `profiles/`.
- **Закрытый** (пром) — та же кодовая база, но подключение к пром-БД через
  SQLAlchemy и LLM через корпоративный REST (`glm-5.1` / `Qwen3.5-397b`).

**Принцип:** код дэша один и тот же в обоих контурах. Между контурами меняются
только (а) строка подключения к БД и (б) backend LLM (см. `.env`). Локальная БД
повторяет пром **точь-в-точь** — та же схема `s_grnplm_ld_salesntwrk_pcap_sn_uzp`
и имена таблиц, поэтому SQL переносится без правок.

## Структура

В корне — только тетрадка запуска и зависимости; всё остальное по папкам.

```
run.ipynb                 тетрадка запуска (1 ячейка): CONN + контур + выбор дэша
requirements.txt          зависимости
docker-compose.yml        Postgres для открытого контура (порт 55433)
.env                      ключи и URL БД (не в git)

uzp_dash/                 ЯДРО (контур-агностично)
  config.py                 SCHEMA, контур, пути
  db.py                     get_engine / read_sql (единая точка доступа к БД)
  llm.py                    complete() — DeepSeek (open) | glm/Qwen (closed)
  registry.py               реестр дэшей; generate_dashboard(), list_dashboards()
  render/                   apple-design: theme, components, сборка HTML
  dashboards/<name>/        по папке на дэш (queries/analyze/prompts/view)

synth/                    генерация синтетики (только открытый контур)
  schema.sql                DDL: схема + 5 пром-таблиц + dim_company + uzp_dim_extended_metrics
  reference.py              загрузка CSV-справочников как есть
  generate.py               синтетика с бизнес-инвариантами
  __main__.py               `python3 -m synth` — собрать БД

data/                     исходные данные прод (только открытый контур)
  *.csv                     2 справочника (uzp_dim_gosb, uzp_dim_metric)
  profiles/                 профили прод-таблиц (reference для синтетики)

output/                   сгенерированные .html
```

## Запуск

**Закрытый контур (пром)** — основной сценарий: открыть `run.ipynb`, указать `CONN`
к пром-БД и `CONTOUR="closed"`, задать в `.env` креды LLM (`JPY_API_TOKEN`,
`GIGACHAT_API_URL`, `UZP_LLM_MODEL`), выполнить единственную ячейку.

**Открытый контур (отладка на синтетике):**
```bash
pip install -r requirements.txt        # при необходимости --break-system-packages
docker compose up -d                   # Postgres на 55433
python3 -m synth                        # собрать синтетическую БД (из data/*.csv)
```
Затем в `run.ipynb` раскомментировать блок открытого контура (или вызвать
`generate_dashboard("tb_health", conn=..., contour="open",
params={"report_month": "2026-07"})`). Отчёт всегда строится по всему банку: уровень
СБ плюс вкладка на каждый ТБ.

## Перенос в закрытый контур

Скопировать репозиторий (папки `synth/`, `data/`, `output/` и `.env` — open-only,
не нужны на проме). В `.env` задать креды LLM, в `run.ipynb` — `CONN` к пром-БД и
`CONTOUR="closed"`. Всё остальное работает как есть.

## Добавить новый дэш

Создать `uzp_dash/dashboards/<name>/` с `view.py`:
```python
from ...registry import Context, dashboard
from ...render import page, components as C

@dashboard("<name>")
def build(ctx: Context) -> str:
    df = ...  # ctx.engine, C, ctx.llm(...)
    return page(title="…", subtitle="…", body="…")
```
Реестр подхватит его автоматически. Управление — тем же `run.ipynb`.

## Данные

Две основные бизнес-метрики: `1000164` (Общий ФОТ, млн ₽) и
`12400196` (уникальные получатели до ИНН). Сегмент — `extended_dim_1`
(`1` = все сегменты), справочник `uzp_dim_extended_metrics`. Организация →
сегмент — через `dim_company(inn, extended_dim_id)`.

## Дэш `tb_health`

Для управляющего ТБ: вердикт по плану, тепловая карта ГОСБ×сегмент, карточки
проблемных ГОСБ с конкретикой «что сделать», интерактивный список организаций к
работе (поиск/фильтр/пагинация), симуляция закрытия плана и нарратив LLM с
анализом свободного текста активностей. Подробнее — `uzp_dash/dashboards/tb_health/README.md`.

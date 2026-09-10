"""Шаг 1: выгрузка. Тяжёлое считается в БД, в pandas едет только результат.

Правила платформы, из-за которых модуль выглядит именно так:

* **больше миллиона строк в память не тянем** — и не обрезаем молча: тихо
  усечённая выборка даёт неверные итоги, которые выглядят правдоподобно;
* **всё, что можно свернуть в SQL, сворачивается в SQL.** Разбор идёт по парам
  (человек, ИНН), их на проме миллионы; в тетрадку приезжают агрегаты.

Главное решение модуля — РАБОЧИЙ НАБОР. Двенадцать запросов разбора смотрят на
одни и те же две выборки ведомостей. Гонять по тяжёлой партиционированной витрине
двенадцать одинаковых сканов незачем, поэтому выборки материализуются один раз —
временными таблицами. Там, где временные таблицы запрещены, ТЕ ЖЕ определения
подставляются запросам как CTE: определение одно, путей исполнения два.
"""
from __future__ import annotations

import pandas as pd

from uzp_dash import db, progress

from . import queries as LQ

MAX_ROWS = 1_000_000


class RowLimitError(RuntimeError):
    pass


def guard_rows(df: pd.DataFrame, name: str, limit: int | None = None) -> pd.DataFrame:
    """Не дать выборке съесть память ядра.

    Лимит разрешается ВНУТРИ функции, а не в значении по умолчанию: иначе он
    зафиксируется в момент импорта, и правка MAX_ROWS перед прогоном молча ни на
    что не повлияет — защита окажется выключенной ровно тогда, когда её решили
    ужесточить.
    """
    limit = MAX_ROWS if limit is None else limit
    if len(df) > limit:
        raise RowLimitError(
            f"{name}: {len(df):,} строк — больше лимита {limit:,}. "
            f"Сверните выборку в SQL или сузьте период.")
    return df


class Workspace:
    """Рабочий набор разбора: шесть выборок и способ их подставить в запрос.

    `use_temp=True`  — выборки материализованы временными таблицами; запрос идёт
                       к ним по имени и читает уже посчитанное.
    `use_temp=False` — те же определения подставляются запросу как CTE. Медленнее
                       (каждый запрос считает их заново), но работает везде.
    """

    def __init__(self, engine, conn, code_col: str, params: dict,
                 use_temp: bool = True) -> None:
        self.engine = engine
        self.conn = conn
        self.code_col = code_col
        self.params = params
        self.use_temp = use_temp
        self.built: list[str] = []
        self._plain_ddl = False

    # -- подготовка ---------------------------------------------------------- #
    def _body(self, body: str) -> str:
        """Тело выборки с подставленным именем колонки кода.

        `{schema}` НЕ трогаем — его подставит слой БД. Поэтому format здесь
        применить нельзя, и подстановка идёт заменой по имени.
        """
        return body.replace("{code_col}", self.code_col)

    def build(self) -> None:
        """Материализовать рабочий набор. В режиме CTE — ничего не делает."""
        if not self.use_temp:
            progress.done("рабочий набор: режим CTE, таблицы не создаются")
            return
        progress.step("Рабочий набор: материализация выборок")
        for name, body, dist in LQ.WORKSET:
            if self._plain_ddl:
                db.execute(self.engine,
                           LQ.CREATE_TMP_PLAIN.format(name=name, body=self._body(body)),
                           self.params, conn=self.conn)
                self.built.append(name)
                db.execute(self.engine, LQ.ANALYZE_TMP.format(name=name), conn=self.conn)
                continue
            sql = LQ.CREATE_TMP.format(name=name, body=self._body(body), dist=dist)
            try:
                db.execute(self.engine, sql, self.params, conn=self.conn)
            except Exception as ex:
                # DISTRIBUTED BY — расширение Greenplum. В открытом контуре стоит
                # обычный PostgreSQL, и оператор с ним не разбирается вовсе.
                # Повторяем без него: смысл выборки от этого не меняется, меняется
                # только раскладка по сегментам, которой здесь и нет.
                if "DISTRIBUTED" not in str(ex).upper():
                    raise
                # Откат обязателен ПЕРЕД повтором. Упавший оператор оставляет
                # транзакцию в аварийном состоянии, и следующий получает
                # «current transaction is aborted» — ошибку, за которой настоящей
                # причины уже не видно.
                self.conn.rollback()
                if not self._plain_ddl:
                    progress.done("DISTRIBUTED BY не поддерживается — выборки "
                                  "создаются без указания распределения")
                    self._plain_ddl = True
                db.execute(self.engine,
                           LQ.CREATE_TMP_PLAIN.format(name=name,
                                                      body=self._body(body)),
                           self.params, conn=self.conn)
            self.built.append(name)
            db.execute(self.engine, LQ.ANALYZE_TMP.format(name=name),
                       conn=self.conn)
        progress.done(f"рабочий набор готов: {', '.join(self.built)}")

    def drop(self) -> None:
        """Убрать за собой. Соединение из пула переживает прогон, и оставленная
        временная таблица досталась бы следующему — с чужими датами внутри."""
        for name in reversed(self.built):
            try:
                db.execute(self.engine, LQ.DROP_TMP.format(name=name), conn=self.conn)
            except Exception:       # noqa: BLE001 — уборка не должна валить прогон
                pass
        self.built = []

    # -- исполнение ---------------------------------------------------------- #
    def _needed(self, sql: str) -> list[tuple[str, str, str]]:
        """Какие выборки рабочего набора нужны ЭТОМУ запросу — вместе с их зависимостями.

        Подставлять все шесть подряд нельзя. На ядре 9.4 CTE — барьер оптимизации:
        объявленная выборка считается ВСЕГДА, даже если запрос к ней не обращается.
        Полный набор в запросе про справочник ГОСБ означал бы два лишних скана
        партиционированной витрины — на проме это минуты на пустом месте.

        Обход идёт с конца: если выборка нужна, её зависимости упоминаются в её же
        теле, и к моменту проверки они уже будут отмечены.
        """
        need: set[str] = set()
        for name, body, _ in reversed(LQ.WORKSET):
            used_by_query = name in sql
            used_by_needed = any(name in b for n, b, _ in LQ.WORKSET
                                 if n in need and n != name)
            if used_by_query or used_by_needed:
                need.add(name)
        return [item for item in LQ.WORKSET if item[0] in need]

    def _prelude(self, sql: str) -> str:
        """Подставить нужные определения рабочего набора запросу в виде CTE.

        Порядок из `LQ.WORKSET` соблюдается: CTE видит только то, что объявлено
        выше него, а `t_seen_epk` считается по `t_seen`.

        Если запрос уже начинается со своего `WITH`, его первое слово снимается —
        два `WITH` подряд в одном операторе не разбираются.
        """
        need = self._needed(sql)
        body = sql.strip()
        if not need:
            return body
        parts = [f"{name} AS (\n{body_}\n)" for name, body_, _ in need]
        head = "WITH " + ",\n".join(parts)
        if body.upper().startswith("WITH "):
            return head + ",\n" + body[len("WITH "):]
        return head + "\n" + body

    def sql(self, name: str, sql: str, params: dict | None = None,
            limit: int | None = None) -> pd.DataFrame:
        """Выполнить запрос разбора поверх рабочего набора.

        `_body` применяется к ГОТОВОМУ тексту, а не только к телам выборок:
        имя колонки кода стоит и в самих запросах (`p.{code_col}`), и запрос,
        собранный из подставленных выборок и неподставленного тела, падал бы с
        `KeyError: 'code_col'` уже в слое БД. Ловушка срабатывала только на
        запасном пути — то есть только там, где отладить её нельзя.
        """
        text = self._body(self._prelude(sql) if not self.use_temp else sql)
        df = db.read_sql(self.engine, text, {**self.params, **(params or {})},
                         conn=self.conn)
        return guard_rows(df, name, limit)


def _opt(ws: Workspace, name: str, sql: str, params: dict | None = None) -> pd.DataFrame:
    """Необязательная выборка: не читается — раздел отключается, прогон идёт."""
    try:
        return ws.sql(name, sql, params)
    except (Exception, RowLimitError) as ex:
        progress.warn(f"{name} не читается ({type(ex).__name__}: {str(ex)[:200]}) — "
                      f"раздел, который его использует, будет пропущен")
        return pd.DataFrame()


# --------------------------------------------------------------------------- #
def month_totals(ws: Workspace) -> pd.DataFrame:
    """Итоги двух опорных месяцев: пары, люди, организации, сумма."""
    df = ws.sql("month_totals", LQ.MONTH_TOTALS)
    if len(df) < 2:
        raise RuntimeError(
            "в рабочем наборе меньше двух месяцев — сравнивать год к году не с чем. "
            "Проверьте, что оба месяца есть в витрине и что сегмент размечен.")
    for r in df.itertuples():
        progress.done(f"{pd.Timestamp(r.report_dt):%m.%Y}: пар {int(r.n_pairs):,}, "
                      f"людей {int(r.n_epk):,}, организаций {int(r.n_inn):,}")
    return df


def lost(ws: Workspace) -> pd.DataFrame:
    """Потерянные пары по причинам — ядро разбора."""
    df = ws.sql("lost_totals", LQ.LOST_TOTALS)
    progress.done(f"потеряно пар: {int(df['n_pairs'].sum()):,} "
                  f"по {len(df)} причинам")
    return df


def gained(ws: Workspace) -> pd.DataFrame:
    """Пришедшие пары по причинам."""
    df = ws.sql("gained_totals", LQ.GAINED_TOTALS)
    progress.done(f"пришло пар: {int(df['n_pairs'].sum()):,}")
    return df


def lost_by_inn(ws: Workspace) -> pd.DataFrame:
    """Потери в разрезе организации, с составом причин."""
    df = ws.sql("lost_by_inn", LQ.LOST_BY_INN)
    progress.done(f"потери по организациям: {len(df):,} строк, "
                  f"{df['inn'].nunique() if len(df) else 0:,} организаций")
    return df


def gained_by_inn(ws: Workspace) -> pd.DataFrame:
    return _opt(ws, "gained_by_inn", LQ.GAINED_BY_INN)


def seg_attrs(ws: Workspace) -> pd.DataFrame:
    """Атрибуты бюджетных организаций: имя, холдинг, отрасль, флаги, ликвидация."""
    df = ws.sql("seg_attrs", LQ.SEG_ATTRS)
    progress.done(f"справочник сегмента: {len(df):,} организаций")
    return df


def monthly(ws: Workspace, d_from: str, d_to: str) -> pd.DataFrame:
    """Помесячный ряд бюджетной сферы — ответ на «когда»."""
    progress.step(f"Помесячный ряд: {d_from} … {d_to}")
    df = _opt(ws, "monthly", LQ.MONTHLY, {"d_from": d_from, "d_to": d_to})
    if not df.empty:
        progress.done(f"ряд: {len(df)} мес., пар от {int(df['n_pairs'].min()):,} "
                      f"до {int(df['n_pairs'].max()):,}")
    return df


def monthly_all(ws: Workspace, d_from: str, d_to: str) -> pd.DataFrame:
    """Полнота загрузки по всему банку — проверяется ДО объяснений.

    Единственный запрос отчёта БЕЗ фильтра сегмента: недогруженную партицию видно
    только на фоне всего банка. Считается лишь число строк — этого достаточно,
    а уникальные значения по всей витрине стоили бы несоизмеримо дороже.
    """
    return _opt(ws, "monthly_all", LQ.MONTHLY_ALL, {"d_from": d_from, "d_to": d_to})


def survival(ws: Workspace) -> pd.DataFrame:
    """Кривая дожития когорты базового месяца."""
    progress.step("Дожитие когорты базового месяца")
    df = _opt(ws, "survival", LQ.SURVIVAL)
    if not df.empty:
        progress.done(f"дожитие: {len(df)} точек, "
                      f"осталось {int(df['n_alive'].iloc[-1]):,} из "
                      f"{int(df['n_alive'].iloc[0]):,}")
    return df


def threshold_sens(ws: Workspace) -> pd.DataFrame:
    """Чувствительность к порогу получателя."""
    return _opt(ws, "threshold_sens", LQ.THRESHOLD_SENS)


def code_mix(ws: Workspace) -> pd.DataFrame:
    """Смесь кодов зачисления в двух опорных месяцах, включая коды ВНЕ списка.

    Даты не передаются: опорные месяцы уже лежат в параметрах рабочего набора.
    """
    return _opt(ws, "code_mix", LQ.CODE_MIX)


def inn_migration(ws: Workspace, min_movers: int) -> pd.DataFrame:
    """Куда переехали люди, потерявшие свой ИНН."""
    df = _opt(ws, "inn_migration", LQ.INN_MIGRATION, {"min_movers": min_movers})
    if not df.empty:
        progress.done(f"миграция ИНН: {len(df):,} пар «откуда→куда» "
                      f"с порогом {min_movers} человек")
    return df


def gosb_dim(ws: Workspace) -> pd.DataFrame:
    """Справочник территории: ГОСБ → ТБ → регион."""
    df = _opt(ws, "gosb_dim", LQ.GOSB_DIM)
    if not df.empty:
        n_reg = int(df["region_name"].notna().sum()) if "region_name" in df else 0
        progress.done(f"справочник территории: {len(df)} ГОСБ, регион известен у {n_reg}")
    return df


def gosb_map(ws: Workspace) -> pd.DataFrame:
    """Соответствие старых ГОСБ новым — тем же запросом, что в дэше."""
    return _opt(ws, "gosb_map", LQ.GOSB_MAP)

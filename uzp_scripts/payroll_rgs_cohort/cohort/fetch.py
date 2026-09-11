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
                 use_temp: bool = True,
                 amt_scope: str = LQ.AMT_SCOPE_INN) -> None:
        self.engine = engine
        self.conn = conn
        self.code_col = code_col
        self.params = params
        self.use_temp = use_temp
        self.amt_scope = amt_scope
        self.built: list[str] = []
        self._plain_ddl = False
        # Показанные читателю запросы: имя -> (текст, параметры). Заполняется
        # ФАКТИЧЕСКИМ исполнением, см. `sql()`.
        self.shown: dict[str, tuple[str, dict]] = {}

    # -- подготовка ---------------------------------------------------------- #
    def _body(self, body: str) -> str:
        """Тело выборки с подставленными именем колонки кода и условием порога.

        `{schema}` НЕ трогаем — его подставит слой БД. Поэтому format здесь
        применить нельзя, и подстановка идёт заменой по имени.
        """
        return (body.replace("{code_col}", self.code_col)
                    .replace("{amt_cond}", LQ.AMT_COND[self.amt_scope]))

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
        args = {**self.params, **(params or {})}
        text = self._body(self._prelude(sql) if not self.use_temp else sql)
        # Читателю показывается СЕБЯ-ДОСТАТОЧНАЯ форма — с подставленными CTE,
        # даже когда исполнялась форма по временным таблицам. Текст `FROM t_pairs`
        # выполнить негде: таблица жила внутри чужой сессии, и значок «как
        # проверить цифру» обещал бы проверку, которой нет. Определение выборок
        # одно и то же, поэтому показанный запрос считает ровно то же число.
        self.shown[name] = (db.render(self._body(self._prelude(sql))), args)
        df = db.read_sql(self.engine, text, args, conn=self.conn)
        return guard_rows(df, name, limit)


def _opt(ws: Workspace, name: str, sql: str, params: dict | None = None) -> pd.DataFrame:
    """Необязательная выборка: не читается — раздел отключается, прогон идёт.

    ОТКАТ ОБЯЗАТЕЛЕН. Все выборки идут в одной сессии (в ней живут временные
    таблицы), а упавший оператор — таймаут, нехватка памяти, что угодно — оставляет
    транзакцию в состоянии «aborted». Без отката КАЖДЫЙ следующий запрос этой
    сессии падает с «current transaction is aborted», и ошибка одной необязательной
    выборки молча уносит все разделы после неё. Ровно так на проме таймаут
    помесячного ряда унёс разбор по видам зачислений и территорию: `tb_dim` просто
    стоял в очереди позже. Временные таблицы откат переживают — они закоммичены
    при создании.
    """
    try:
        return ws.sql(name, sql, params)
    except (Exception, RowLimitError) as ex:
        progress.warn(f"{name} не читается ({type(ex).__name__}: {str(ex)[:200]}) — "
                      f"раздел, который его использует, будет пропущен")
        if ws.conn is not None:
            try:
                ws.conn.rollback()
            except Exception as rb:                              # noqa: BLE001
                progress.warn(f"откат после {name} не удался "
                              f"({type(rb).__name__}: {str(rb)[:120]}) — "
                              f"следующие выборки сессии тоже могут не прочитаться")
        return pd.DataFrame()


# --------------------------------------------------------------------------- #
# Выгрузки разбора.
#
# Имя, под которым выборка кладётся в `ws.shown`, — это же имя показывается
# читателю рядом с блоком отчёта. Оно должно называть ВОПРОС, а не таблицу:
# «lost_totals» читатель соотнесёт с разделом, «q7» — нет.
# --------------------------------------------------------------------------- #
def month_totals(ws: Workspace) -> pd.DataFrame:
    """Итоги опорных месяцев: получатели, люди, организации, сумма."""
    df = ws.sql("month_totals", LQ.MONTH_TOTALS)
    if len(df) < 2:
        raise RuntimeError(
            "в рабочем наборе меньше двух месяцев — сравнивать не с чем. "
            "Проверьте, что опорные месяцы есть в витрине и сегмент размечен.")
    for r in df.itertuples():
        progress.done(f"{pd.Timestamp(r.report_dt):%m.%Y}: получателей "
                      f"{int(r.n_triples):,}, людей {int(r.n_epk):,}, "
                      f"организаций {int(r.n_inn):,}")
    return df


def lost(ws: Workspace, base: str, tag: str = "") -> pd.DataFrame:
    """Потерянные получатели по причинам — ядро разбора."""
    df = ws.sql(f"lost_totals{tag}", LQ.LOST_TOTALS, {"d_base": base})
    progress.done(f"потеряно получателей: {int(df['n_triples'].sum()):,} "
                  f"по {len(df)} причинам")
    return df


def gained(ws: Workspace, base: str, tag: str = "") -> pd.DataFrame:
    """Пришедшие получатели по причинам."""
    df = ws.sql(f"gained_totals{tag}", LQ.GAINED_TOTALS, {"d_base": base})
    progress.done(f"пришло получателей: {int(df['n_triples'].sum()):,}")
    return df


def lost_epk(ws: Workspace, base: str, tag: str = "") -> pd.DataFrame:
    """Потерянные ЛЮДИ по причинам — вторая, аддитивная по людям лестница."""
    df = ws.sql(f"lost_epk{tag}", LQ.LOST_EPK_TOTALS, {"d_base": base})
    progress.done(f"потеряно людей: {int(df['n_epk'].sum()):,} "
                  f"по {len(df)} причинам")
    return df


def gained_epk(ws: Workspace, base: str, tag: str = "") -> pd.DataFrame:
    """Пришедшие ЛЮДИ по причинам."""
    df = ws.sql(f"gained_epk{tag}", LQ.GAINED_EPK_TOTALS, {"d_base": base})
    progress.done(f"пришло людей: {int(df['n_epk'].sum()):,}")
    return df


def lost_by_inn(ws: Workspace, base: str) -> pd.DataFrame:
    """Потери в разрезе организации, с составом причин."""
    df = ws.sql("lost_by_inn", LQ.LOST_BY_INN, {"d_base": base})
    progress.done(f"потери по организациям: {len(df):,} строк, "
                  f"{df['inn'].nunique() if len(df) else 0:,} организаций")
    return df


def gained_by_inn(ws: Workspace, base: str) -> pd.DataFrame:
    """Приход в разрезе организации — без него таблица крупнейших потерь врёт."""
    return _opt(ws, "gained_by_inn", LQ.GAINED_BY_INN, {"d_base": base})


def tenure(ws: Workspace, base: str, d_from: str) -> pd.DataFrame:
    """Стаж ушедших: сколько месяцев человек был в сегменте до ухода."""
    progress.step("Стаж ушедших")
    df = _opt(ws, "tenure", LQ.TENURE, {"d_base": base, "d_from": d_from})
    if not df.empty:
        progress.done(f"стаж: {len(df)} корзин, "
                      f"{int(df['n_epk'].sum()):,} человек разобрано")
    return df


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
        progress.done(f"ряд: {len(df)} мес., получателей от "
                      f"{int(df['n_triples'].min()):,} до "
                      f"{int(df['n_triples'].max()):,}")
    return df


def monthly_all(ws: Workspace, d_from: str, d_to: str) -> pd.DataFrame:
    """Полнота загрузки по всему банку — проверяется ДО объяснений.

    Единственный запрос отчёта БЕЗ фильтра сегмента: недогруженную партицию видно
    только на фоне всего банка. Считается лишь число строк — этого достаточно,
    а уникальные значения по всей витрине стоили бы несоизмеримо дороже.
    """
    return _opt(ws, "monthly_all", LQ.MONTHLY_ALL, {"d_from": d_from, "d_to": d_to})


def survival(ws: Workspace, base: str) -> pd.DataFrame:
    """Кривая дожития когорты базового месяца."""
    progress.step("Дожитие когорты базового месяца")
    df = _opt(ws, "survival", LQ.SURVIVAL, {"d_base": base})
    if not df.empty:
        progress.done(f"дожитие: {len(df)} точек, "
                      f"осталось {int(df['n_alive'].iloc[-1]):,} из "
                      f"{int(df['n_alive'].iloc[0]):,}")
    return df


def threshold_sens(ws: Workspace) -> pd.DataFrame:
    """Чувствительность к порогу получателя."""
    return _opt(ws, "threshold_sens", LQ.THRESHOLD_SENS)


def seasonal(ws: Workspace) -> pd.DataFrame:
    """Сезонность, подтверждённая повтором год к году."""
    return _opt(ws, "seasonal", LQ.SEASONAL)


def code_months(ws: Workspace) -> pd.DataFrame:
    """Зарплатные коды по опорным месяцам — какой вид выплаты просел."""
    df = _opt(ws, "code_months", LQ.CODE_MONTHS)
    if not df.empty:
        progress.done(f"зарплатные коды: {df['code'].nunique()} видов выплат "
                      f"по {df['report_dt'].nunique()} месяцам")
    return df


def left_segment(ws: Workspace, base: str) -> pd.DataFrame:
    """В какой сегмент ушли те, кто ушёл из бюджетного."""
    df = _opt(ws, "left_segment", LQ.LEFT_SEGMENT, {"d_base": base})
    if not df.empty:
        progress.done(f"ушли в другие сегменты: {int(df['n_epk'].sum()):,} человек "
                      f"по {len(df)} сегментам")
    return df


def left_codes(ws: Workspace, base: str) -> pd.DataFrame:
    """Чем заменились зарплатные зачисления у тех, кто их лишился."""
    df = _opt(ws, "left_codes", LQ.LEFT_CODES, {"d_base": base})
    if not df.empty:
        progress.done(f"перешли на другие коды: {len(df)} видов зачисления")
    return df


def left_codes_inn(ws: Workspace, base: str, per_code: int) -> pd.DataFrame:
    """Кто именно перешёл на каждый вид зачисления — топ организаций по коду."""
    df = _opt(ws, "left_codes_inn", LQ.LEFT_CODES_INN,
              {"d_base": base, "per_code": per_code})
    if not df.empty:
        progress.done(f"организации по видам зачисления: {len(df):,} строк, "
                      f"по {per_code} на код")
    return df


def inn_migration(ws: Workspace, base: str, min_movers: int) -> pd.DataFrame:
    """Куда переехали люди, потерявшие свою организацию."""
    df = _opt(ws, "inn_migration", LQ.INN_MIGRATION,
              {"d_base": base, "min_movers": min_movers})
    if not df.empty:
        progress.done(f"миграция организаций: {len(df):,} пар «откуда→куда» "
                      f"с порогом {min_movers} человек")
    return df


def tb_dim(ws: Workspace) -> pd.DataFrame:
    """ТБ по номеру из ведомостей. Территория держится на нём, а не на ГОСБ."""
    return _opt(ws, "tb_dim", LQ.TB_DIM)


def gosb_dim(ws: Workspace) -> pd.DataFrame:
    """Справочник ГОСБ — обоими ключами сразу, выбор делает разведка."""
    df = _opt(ws, "gosb_dim", LQ.GOSB_DIM)
    if not df.empty:
        n_reg = int(df["region_name"].notna().sum()) if "region_name" in df else 0
        progress.done(f"справочник территории: {len(df)} строк, "
                      f"регион известен у {n_reg}")
    return df


def tb_match(ws: Workspace) -> pd.DataFrame:
    """Опознаётся ли номер ТБ ведомостей справочником.

    Вопрос, которого раньше не задавали. ТБ считался колонкой, заполненной
    всегда, и когда на проме он не сошёлся, отчёт показал разрез из одной
    заглушки «ТБ неизвестен» — без предупреждения и без числа, по которому это
    можно было бы заметить.
    """
    return _opt(ws, "tb_match", LQ.TB_MATCH)


def gosb_match(ws: Workspace) -> pd.DataFrame:
    """Каким ключом справочника опознаётся ГОСБ ведомостей.

    На проме первый прогон показал, что не опознаётся никаким, и весь
    территориальный разрез схлопнулся в одну строку-заглушку. Угадывать ключ
    нельзя: ошибка не видна по результату, она видна только по тому, что разрез
    состоит из одной строки — а это легко принять за свойство данных.
    """
    return _opt(ws, "gosb_match", LQ.GOSB_MATCH)

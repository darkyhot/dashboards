"""Выгрузка. Тяжёлое считается в БД, в pandas едут только свёртки.

Главное решение модуля — ведомости читаются РОВНО ОДИН РАЗ: узкая копия витрины
`t_raw` за все нужные месяцы (ряд, набор, месяц после отчётного), одна временная
таблица в одной сессии, заполняется оператором на партицию — каждый укладывается
в statement_timeout. Всё остальное (полнота, ряд, коды, тройки, присутствие ФЛ)
считается из неё; к витрине больше никто не обращается.

Запасного пути через CTE здесь НЕТ, в отличие от разбора одного сегмента: на
объёме всего банка каждый запрос пересчитывал бы тройки за все месяцы, а это часы.
Если временные таблицы запрещены, разведка останавливает прогон с объяснением.
"""
from __future__ import annotations

import pandas as pd

from . import db, progress
from . import months as M
from . import queries as Q

MAX_ROWS = 1_000_000


class RowLimitError(RuntimeError):
    pass


def guard_rows(df: pd.DataFrame, name: str, limit: int | None = None) -> pd.DataFrame:
    """Больше миллиона строк в память не тянем — и не обрезаем молча: тихо
    усечённая выборка даёт правдоподобные неверные итоги."""
    limit = MAX_ROWS if limit is None else limit
    if len(df) > limit:
        raise RowLimitError(f"{name}: {len(df):,} строк — больше лимита {limit:,}. "
                            f"Сверните выборку в SQL или сузьте период.")
    return df


class Workspace:
    def __init__(self, conn, months: list, params: dict) -> None:
        self.conn = conn
        self.months = [M.iso(m) for m in sorted({M.parse(x) for x in months})]
        self.params = dict(params)               # codes, amt_min
        self.built: list[str] = []
        self.plain_ddl = False
        # Показанные читателю запросы: имя → (самодостаточный текст, параметры).
        self.shown: dict[str, tuple[str, dict]] = {}
        self.timing: dict[str, float] = {}
        self.series_rows: list[pd.DataFrame] = []
        self.load = pd.DataFrame()
        self.codes = pd.DataFrame()

    # -- построение -------------------------------------------------------- #
    def _create(self, name: str, body: str, params: dict | None = None) -> int:
        args = {**self.params, **(params or {})}
        if not self.plain_ddl:
            sql = Q.CREATE_TMP.format(name=name, body=body, dist=Q.DIST[name])
            try:
                n = db.execute(self.conn, sql, args)
                self.built.append(name)
                return n
            except Exception as ex:
                # DISTRIBUTED BY — расширение Greenplum; на обычном PostgreSQL
                # (открытый контур) не разбирается. Откат ПЕРЕД повтором: упавший
                # оператор оставил транзакцию в аварийном состоянии.
                if "DISTRIBUTED" not in str(ex).upper():
                    raise
                db.rollback(self.conn)
                self.plain_ddl = True
                progress.done("DISTRIBUTED BY не поддерживается — таблицы без распределения")
        n = db.execute(self.conn, Q.CREATE_TMP_PLAIN.format(name=name, body=body), args)
        self.built.append(name)
        return n

    def _insert(self, name: str, body: str, params: dict) -> int:
        return db.execute(self.conn, Q.INSERT_TMP.format(name=name, body=body),
                          {**self.params, **params})

    def _analyze(self, name: str) -> None:
        db.execute(self.conn, Q.ANALYZE_TMP.format(name=name))

    def build_raw(self, months: list) -> dict[str, int]:
        """ЕДИНСТВЕННОЕ чтение ведомостей: узкая копия витрины `t_raw` за все нужные
        месяцы, оператор на партицию. Возвращает число строк по месяцам: месяц с
        нулём — месяца нет в витрине."""
        progress.step(f"Копия ведомостей t_raw: {len(months)} мес. "
                      f"({M.label(months[0])}…{M.label(months[-1])})")
        n = self._create("t_org", Q.T_ORG)
        self._analyze("t_org")
        progress.done(f"t_org: {n:,} организаций справочника")
        rows: dict[str, int] = {}
        for i, m in enumerate(M.iso(x) for x in months):
            t0 = pd.Timestamp.now()
            n = (self._create("t_raw", Q.T_RAW_MONTH, {"m": m}) if i == 0
                 else self._insert("t_raw", Q.T_RAW_MONTH, {"m": m}))
            rows[m] = n
            sec = (pd.Timestamp.now() - t0).total_seconds()
            progress.done(f"t_raw · {M.label(m)}: {n:,} строк за {sec:.0f} с")
        self._analyze("t_raw")
        progress.done(f"t_raw: всего {sum(rows.values()):,} строк")
        return rows

    def build(self, hist: list, code_months: list) -> None:
        """Всё остальное — из t_raw: ряд по каждому месяцу ряда, тройки месяцев
        набора, ФЛ-месяц, присутствие ФЛ, зарплатные коды, полнота."""
        progress.step(f"Рабочий набор из t_raw: {len(self.months)} мес. "
                      f"({', '.join(M.label(m) for m in self.months)}), ряд {len(hist)} мес.")
        # Каждый шаг — ОДИН проход по копии за все месяцы, без циклов по месяцам:
        # у временной таблицы нет партиций, и фильтр по месяцу читал бы её целиком.
        all_m = sorted({M.iso(x) for x in hist} | set(self.months))
        t0 = pd.Timestamp.now()
        n = self._create("t_stage", Q.T_STAGE)
        progress.done(f"t_stage (зарплатные тройки, все месяцы): {n:,} строк за "
                      f"{(pd.Timestamp.now() - t0).total_seconds():.0f} с")
        t0 = pd.Timestamp.now()
        ser = self.sql("series", Q.SERIES_ALL, {"months": all_m})
        ser["report_dt"] = ser["report_dt"].astype(str)
        self.series_rows = [ser[ser["report_dt"].isin(set(M.iso(x) for x in hist))]]
        progress.done(f"ряд: {ser['report_dt'].nunique()} мес. за "
                      f"{(pd.Timestamp.now() - t0).total_seconds():.0f} с")
        t0 = pd.Timestamp.now()
        n = self._create("t_pairs", Q.PAIRS_FROM_STAGE, {"months": self.months})
        self._drop_one("t_stage")
        self._analyze("t_pairs")
        progress.done(f"t_pairs (получатели набора): {n:,} строк за "
                      f"{(pd.Timestamp.now() - t0).total_seconds():.0f} с")
        for name, body in (("t_epk", Q.T_EPK), ("t_epk_seg", Q.T_EPK_SEG),
                           ("t_keys", Q.T_KEYS)):
            n = self._create(name, body)
            self._analyze(name)
            progress.done(f"{name}: {n:,} строк")
        t0 = pd.Timestamp.now()
        n = self._create("t_person", Q.T_PERSON, {"months": self.months})
        self._analyze("t_person")
        progress.done(f"t_person: {n:,} строк за {(pd.Timestamp.now() - t0).total_seconds():.0f} с")
        self.load = self.sql("load", Q.LOAD_FROM_RAW)
        self.codes = self.opt("code_months", Q.CODE_MONTH,
                              {"code_months": [M.iso(x) for x in code_months]})
        progress.done("рабочий набор готов")

    def _drop_one(self, name: str) -> None:
        db.execute(self.conn, Q.DROP_TMP.format(name=name))
        if name in self.built:
            self.built.remove(name)

    def drop(self) -> None:
        for name in reversed(self.built):
            try:
                db.execute(self.conn, Q.DROP_TMP.format(name=name))
            except Exception:                                  # noqa: BLE001
                db.rollback(self.conn)
        self.built = []

    # -- исполнение -------------------------------------------------------- #
    def show_form(self, sql: str) -> str:
        """Самодостаточная форма запроса: нужные таблицы набора — как CTE.

        Обход с конца: если таблица нужна, её зависимости упомянуты в её теле.
        """
        need: set[str] = set()
        for name in reversed(Q.SHOW_ORDER):
            if name in sql or any(name in Q.SHOW_DEFS[n] for n in need if n != name):
                need.add(name)
        body = sql.strip()
        if not need:
            return db.render(body)
        parts = [f"{n} AS (\n{Q.SHOW_DEFS[n].strip()}\n)" for n in Q.SHOW_ORDER if n in need]
        head = "WITH " + ",\n".join(parts)
        if body.upper().startswith("WITH "):
            return db.render(head + ",\n" + body[5:])
        return db.render(head + "\n" + body)

    def sql(self, name: str, sql: str, params: dict | None = None,
            limit: int | None = None) -> pd.DataFrame:
        args = {**self.params, "months": self.months, **(params or {})}
        self.shown[name] = (self.show_form(sql), args)
        t0 = pd.Timestamp.now()
        df = db.read_sql(self.conn, sql, args)
        self.timing[name] = self.timing.get(name, 0) + (pd.Timestamp.now() - t0).total_seconds()
        return guard_rows(df, name, limit)

    def opt(self, name: str, sql: str, params: dict | None = None) -> pd.DataFrame:
        """Необязательная выборка: не читается — раздел отключается, прогон идёт.
        Откат обязателен, иначе ошибка уносит все следующие запросы сессии."""
        try:
            return self.sql(name, sql, params)
        except (Exception, RowLimitError) as ex:
            progress.warn(f"{name} не читается ({type(ex).__name__}: {str(ex)[:200]}) — "
                          f"раздел будет пропущен")
            db.rollback(self.conn)
            return pd.DataFrame()


# --------------------------------------------------------------------------- #
# Переходы между двумя месяцами набора
# --------------------------------------------------------------------------- #
DUMMY = "1900-01-31"


def flow_params(ws: Workspace, b, c) -> dict:
    """Параметры перехода b → c с метками доступности соседних месяцев."""
    have = set(ws.months)
    bp1, bp2, cn1 = M.iso(M.shift(b, -1)), M.iso(M.shift(b, -2)), M.iso(M.shift(c, 1))
    return {"b": M.iso(b), "c": M.iso(c),
            "bp1": bp1 if bp1 in have else DUMMY, "has_bp1": bp1 in have,
            "bp2": bp2 if bp2 in have else DUMMY, "has_bp2": bp2 in have,
            "cn1": cn1 if cn1 in have else DUMMY, "has_cn1": cn1 in have}


def fl_flow(ws: Workspace, b, c, tag: str) -> pd.DataFrame:
    return ws.sql(f"fl_flow_{tag}", Q.FL_FLOW, flow_params(ws, b, c))


def _step_table(ws: Workspace, name: str, body: str, p: dict) -> None:
    """Временная таблица одного шага: пересоздаётся под каждый переход."""
    db.execute(ws.conn, Q.DROP_TMP.format(name=name))
    if name in ws.built:
        ws.built.remove(name)
    ws._create(name, body, p)
    ws._analyze(name)


def triple_flow(ws: Workspace, b, c, tag: str) -> pd.DataFrame:
    """Два шага: набор исчезнувших/появившихся троек → их ситуации (см. T_TFLOW)."""
    p = flow_params(ws, b, c)
    _step_table(ws, "t_tflow", Q.T_TFLOW, p)
    return ws.sql(f"triple_flow_{tag}", Q.TRIPLE_FLOW, p)


def cohort(ws: Workspace, k, kp, tag: str) -> pd.DataFrame:
    return ws.opt(f"cohort_{tag}", Q.COHORT, {"k": M.iso(k), "kp": M.iso(kp)})


def seasonal(ws: Workspace, m) -> pd.DataFrame:
    p = {"b_prev": M.iso(M.shift(m, -13)), "b": M.iso(M.shift(m, -12)),
         "b_next": M.iso(M.shift(m, -11)), "c_prev": M.iso(M.shift(m, -1)),
         "c": M.iso(m), "c_next": M.iso(M.shift(m, 1))}
    return ws.opt(f"seasonal_{M.iso(m)}", Q.SEASONAL, p)


def org_params(b, c, o: dict) -> dict:
    return {"b": M.iso(b), "c": M.iso(c),
            "reorg_min_movers": int(o["reorg_min_movers"]),
            "reorg_min_share": float(o["reorg_min_share"]),
            "min_base": int(o["min_base"]), "min_real": int(o["min_real"]),
            "min_share": float(o["min_share"]), "max_rows": int(o["max_rows"])}


def orgs(ws: Workspace, b, c, o: dict, tag: str) -> dict[str, pd.DataFrame]:
    fp = flow_params(ws, b, c)
    p = {**org_params(b, c, o), "cn1": fp["cn1"], "has_cn1": fp["has_cn1"]}
    try:
        _step_table(ws, "t_oflow", Q.T_OFLOW, p)
    except Exception as ex:                                    # noqa: BLE001
        progress.warn(f"набор ушедших/пришедших по организациям не построен "
                      f"({type(ex).__name__}: {str(ex)[:160]}) — список пропущен")
        db.rollback(ws.conn)
        return {"list": pd.DataFrame(), "summary": pd.DataFrame(), "reorg": pd.DataFrame()}
    return {"list": ws.opt(f"org_list_{tag}", Q.ORG_LIST, p),
            "summary": ws.opt(f"org_summary_{tag}", Q.ORG_SUMMARY, p),
            "reorg": ws.opt(f"org_reorg_{tag}", Q.ORG_REORG, p)}


def august(ws: Workspace, m, hole_min_base: int, max_rows: int) -> dict:
    """Перенос или потеря в месяце m: организации с провалом и подпись переноса,
    для этого года и года назад."""
    out = {}
    for tag, mm in (("cur", M.parse(m)), ("prev", M.shift(m, -12))):
        p = {"m_prev": M.iso(M.shift(mm, -1)), "m": M.iso(mm), "m_next": M.iso(M.shift(mm, 1)),
             "hole_min_base": int(hole_min_base), "max_rows": int(max_rows)}
        out[f"hole_{tag}"] = ws.opt(f"org_hole_{M.iso(mm)}", Q.ORG_HOLE, p)
        out[f"pay_{tag}"] = ws.opt(f"return_pay_{M.iso(mm)}", Q.RETURN_PAY, p)
    return out

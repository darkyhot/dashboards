"""Выгрузка. Тяжёлое — во временных таблицах одной сессии, в pandas — только свёртки.

Каждая большая витрина читается ОДИН раз:
* ведомости — узкая копия `t_pay`, оператор на месяц;
* клики СБОЛ и операции ВСП — одним проходом, сразу сжатые до «ФЛ × месяц» и
  только по ФЛ из НФЛ;
* воронка продаж — одним проходом, одна строка на сделку.
Всё остальное считается из временных таблиц.
"""
from __future__ import annotations

import pandas as pd

from . import db, names, progress
from . import months as M
from . import queries as Q

MAX_ROWS = 1_000_000


class RowLimitError(RuntimeError):
    pass


def guard_rows(df: pd.DataFrame, name: str) -> pd.DataFrame:
    """Больше миллиона строк в память не тянем и не обрезаем молча."""
    if len(df) > MAX_ROWS:
        raise RowLimitError(f"{name}: {len(df):,} строк — больше лимита {MAX_ROWS:,}. "
                            f"Сверните выборку в SQL.")
    return df


class Workspace:
    def __init__(self, conn, params: dict) -> None:
        self.conn = conn
        self.params = dict(params)
        self.built: list[str] = []
        self.plain_ddl = False
        self.shown: dict[str, tuple[str, dict]] = {}
        self.timing: dict[str, float] = {}
        self.rows: dict[str, int] = {}

    # -- построение -------------------------------------------------------- #
    def _create(self, name: str, body: str, params: dict | None = None) -> int:
        args = {**self.params, **(params or {})}
        db.execute(self.conn, Q.DROP_TMP.format(name=name))
        if not self.plain_ddl:
            try:
                n = db.execute(self.conn, Q.CREATE_TMP.format(name=name, body=body,
                                                              dist=Q.WORKSET_DIST[name]), args)
                self.built.append(name)
                return n
            except Exception as ex:
                # DISTRIBUTED BY — расширение Greenplum; обычный PostgreSQL (открытый
                # контур) его не разбирает. Откат ПЕРЕД повтором.
                if "DISTRIBUTED" not in str(ex).upper():
                    raise
                db.rollback(self.conn)
                self.plain_ddl = True
                progress.done("DISTRIBUTED BY не поддерживается — таблицы без распределения")
        n = db.execute(self.conn, Q.CREATE_TMP_PLAIN.format(name=name, body=body), args)
        self.built.append(name)
        return n

    def step(self, name: str, body: str, params: dict | None = None, label: str = "") -> int:
        t0 = pd.Timestamp.now()
        n = self._create(name, body, params)
        db.execute(self.conn, Q.ANALYZE_TMP.format(name=name))
        sec = (pd.Timestamp.now() - t0).total_seconds()
        self.timing[name] = sec
        self.rows[name] = n
        progress.done(f"{name}{' (' + label + ')' if label else ''}: {n:,} строк за {sec:.0f} с")
        return n

    def build(self, cell_months: list) -> None:
        progress.step("Справочники")
        self.step("t_org", Q.T_ORG, label="организации")
        self.step("t_exc", Q.T_EXC, label="организации без порога")

        progress.step(f"Копия ведомостей t_pay: {', '.join(M.label(m) for m in cell_months)}")
        for i, m in enumerate(cell_months):
            t0 = pd.Timestamp.now()
            p = {"m": M.iso(m)}
            if i == 0:
                n = self._create("t_pay", Q.T_PAY_MONTH, p)
            else:
                n = db.execute(self.conn, Q.INSERT_TMP.format(name="t_pay", body=Q.T_PAY_MONTH),
                               {**self.params, **p})
            progress.done(f"t_pay · {M.label(m)}: {n:,} строк за "
                          f"{(pd.Timestamp.now() - t0).total_seconds():.0f} с")
        db.execute(self.conn, Q.ANALYZE_TMP.format(name="t_pay"))
        self.step("t_cell", Q.T_CELL, label="ячейки ГОСБ × организация")
        self.step("t_cls", Q.T_CLS, label="классы ячеек, две пары лет")

        progress.step("НФЛ и действия каналов")
        self.step("t_nfl", Q.T_NFL, label="НФЛ")
        self.step("t_nkey", Q.T_NKEY, label="ФЛ из НФЛ")
        self.step("t_vsp", Q.T_VSP, label="консультации ВСП, ФЛ × месяц")
        self.step("t_dig", Q.T_DIG, label="консультации СБОЛ, ФЛ × месяц")
        self.step("t_deal", Q.T_DEAL, label="сделки МЗП")
        self.step("t_act", Q.T_ACT, label="действия, действующие на первую ЗП")
        self.step("t_nflc", Q.T_NFLC, label="НФЛ с каналом")

    def drop(self) -> None:
        for name in reversed(self.built):
            try:
                db.execute(self.conn, Q.DROP_TMP.format(name=name))
            except Exception:                                  # noqa: BLE001
                db.rollback(self.conn)
        self.built = []

    # -- исполнение -------------------------------------------------------- #
    def show_form(self, sql: str) -> str:
        """Самодостаточная форма запроса: нужные временные таблицы — как CTE."""
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

    def sql(self, name: str, sql: str, params: dict | None = None) -> pd.DataFrame:
        args = {**self.params, **(params or {})}
        self.shown[name] = (self.show_form(sql), args)
        t0 = pd.Timestamp.now()
        df = db.read_sql(self.conn, sql, args)
        self.timing[name] = self.timing.get(name, 0) + (pd.Timestamp.now() - t0).total_seconds()
        # Названия с ФИО отсекаются здесь — у КАЖДОЙ выборки, до любых расчётов.
        return names.mask_frame(guard_rows(df, name))

    def opt(self, name: str, sql: str, params: dict | None = None) -> pd.DataFrame:
        """Необязательная выборка: не читается — раздел отключается, прогон идёт."""
        try:
            return self.sql(name, sql, params)
        except (Exception, RowLimitError) as ex:
            progress.warn(f"{name} не читается ({type(ex).__name__}: {str(ex)[:200]}) — "
                          f"раздел будет пропущен")
            db.rollback(self.conn)
            return pd.DataFrame()

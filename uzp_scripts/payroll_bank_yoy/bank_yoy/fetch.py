"""Выгрузка. Тяжёлое считается в БД, в pandas едут только свёртки.

Главное решение модуля — РАБОЧИЙ НАБОР временных таблиц в ОДНОЙ сессии. Двадцать
запросов разбора смотрят на одни и те же месяцы ведомостей; сканировать витрину
всего банка двадцать раз незачем. Набор строится ПОМЕСЯЧНО — оператор на партицию,
— чтобы каждый укладывался в statement_timeout.

Запасного пути через CTE здесь НЕТ, в отличие от разбора одного сегмента: на
объёме всего банка каждый запрос пересчитывал бы тройки за все месяцы, а это часы.
Если временные таблицы запрещены, разведка останавливает прогон с объяснением.
"""
from __future__ import annotations

import hashlib
import json

import pandas as pd

from . import config, db, progress
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

    def _monthly(self, name: str, body: str) -> None:
        """CREATE по первому месяцу, INSERT по остальным — оператор на партицию."""
        total = 0
        for i, m in enumerate(self.months):
            t0 = pd.Timestamp.now()
            n = (self._create(name, body, {"m": m}) if i == 0
                 else self._insert(name, body, {"m": m}))
            total += n
            sec = (pd.Timestamp.now() - t0).total_seconds()
            progress.done(f"{name} · {M.label(m)}: {n:,} строк за {sec:.0f} с")
        self._analyze(name)
        progress.done(f"{name}: всего {total:,} строк")

    def build(self) -> None:
        progress.step(f"Рабочий набор: {len(self.months)} мес. "
                      f"({', '.join(M.label(m) for m in self.months)})")
        n = self._create("t_org", Q.T_ORG)
        self._analyze("t_org")
        progress.done(f"t_org: {n:,} организаций справочника")
        self._monthly("t_pairs", Q.T_PAIRS_MONTH)
        for name, body in (("t_epk", Q.T_EPK), ("t_epk_seg", Q.T_EPK_SEG),
                           ("t_keys", Q.T_KEYS)):
            n = self._create(name, body)
            self._analyze(name)
            progress.done(f"{name}: {n:,} строк")
        self._monthly("t_person", Q.T_PERSON_MONTH)
        progress.done("рабочий набор готов")

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
# Ряд по месяцам — прямо по витрине, с кэшем на диске
# --------------------------------------------------------------------------- #
def _cache_key(kind: str, m: str, fp: dict) -> str:
    raw = json.dumps({"kind": kind, "m": m, **fp}, sort_keys=True, default=str)
    return f"{kind}_{m}_{hashlib.md5(raw.encode()).hexdigest()[:10]}.csv"


def month_cached(ws: Workspace, kind: str, sql: str, m: str, fp: dict,
                 use_cache: bool = True) -> pd.DataFrame:
    """Один месяц ряда. Кэш ключуется всем, от чего зависит число: схема, месяц,
    число строк партиции, порог, коды, имя колонки кода, срез справочника ЕПК.
    Закрытый месяц не меняется, и повторный прогон его не пересчитывает; перезагрузка
    партиции меняет число строк — и ключ."""
    config.ensure_dirs()
    key = {k: v for k, v in fp.items() if k != "rows"}
    key["n_rows"] = (fp.get("rows") or {}).get(m)
    path = config.CACHE_DIR / _cache_key(kind, m, key)
    if use_cache and path.exists():
        return pd.read_csv(path)
    t0 = pd.Timestamp.now()
    df = ws.sql(f"{kind}_{m}", sql, {"m": m})
    df.to_csv(path, index=False)
    progress.done(f"{kind} · {M.label(m)}: {(pd.Timestamp.now() - t0).total_seconds():.0f} с")
    return df


def series(ws: Workspace, hist: list, fp: dict, use_cache: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Численность по сегментам и полнота загрузки за каждый месяц ряда."""
    progress.step(f"Ряд за {len(hist)} мес. (кэш: {config.CACHE_DIR.name}/)")
    rows, loads = [], []
    for m in hist:
        m = M.iso(m)
        try:
            s = month_cached(ws, "series", Q.MONTH_SERIES, m, fp, use_cache)
            s["report_dt"] = m
            rows.append(s)
            ld = month_cached(ws, "load", Q.MONTH_LOAD, m, fp, use_cache)
            ld["report_dt"] = m
            loads.append(ld)
        except Exception as ex:                                # noqa: BLE001
            progress.warn(f"ряд · {M.label(m)} не читается ({type(ex).__name__}: "
                          f"{str(ex)[:160]}) — месяц пропущен")
            db.rollback(ws.conn)
    cat = (lambda xs: pd.concat(xs, ignore_index=True) if xs else pd.DataFrame())
    progress.done(f"ряд: {len(rows)} из {len(hist)} мес.")
    return cat(rows), cat(loads)


def code_months(ws: Workspace, ms: list, fp: dict, use_cache: bool = True) -> pd.DataFrame:
    """Зарплатные коды по месяцам — тоже скан партиции, тоже в кэш."""
    out = []
    for m in ms:
        m = M.iso(m)
        try:
            df = month_cached(ws, "code_month", Q.CODE_MONTH, m, fp, use_cache)
        except Exception as ex:                                # noqa: BLE001
            progress.warn(f"коды · {M.label(m)} не читаются ({type(ex).__name__}: {str(ex)[:160]})")
            db.rollback(ws.conn)
            continue
        df["report_dt"] = m
        out.append(df)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


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
    p = org_params(b, c, o)
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

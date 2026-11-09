"""Самопроверки без БД — до первого запроса. И сверка с синтетикой после прогона.

Проверяется то, из-за чего результату нельзя было бы верить: автономность папки,
однократное чтение больших витрин, диалект 9.4, приведение номера организации
только под маской, маска названий с ФИО, отсутствие слова-блокатора, тождества
расчётов на игрушечных данных.
"""
from __future__ import annotations

import ast
import json
import re
import sys

import pandas as pd

from . import PKG_DIR, analyze as A, config, names as N, queries as Q, view as V

ALLOWED_TOP = {"pandas", "numpy", "sqlalchemy"}
BIG_TEMP = ("t_pay", "t_cell", "t_nfl", "t_act", "t_vsp", "t_dig", "t_nkey")


def _fail(name: str, msg: str) -> None:
    raise AssertionError(f"{name}: {msg}")


def check_self_contained() -> None:
    """Пакет импортирует только stdlib, pandas, numpy, sqlalchemy и себя."""
    std = set(getattr(sys, "stdlib_module_names", ()))
    for path in PKG_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                mods = [node.module or ""]
            for m in mods:
                top = m.split(".")[0]
                if top not in ALLOWED_TOP | {"attraction", "__future__"} and top not in std:
                    _fail("автономность", f"{path.name} импортирует «{m}»")


def check_no_blocked_word() -> None:
    """Слова-блокатора нет нигде в папке — ни в коде, ни в документах (даже внутри слов)."""
    w = V.BLOCKED.lower()
    for path in PKG_DIR.parent.rglob("*"):
        if (path.is_file() and "output" not in path.parts and "__pycache__" not in path.parts
                and path.suffix in (".py", ".md", ".ipynb", ".txt", ".json")):
            t = path.read_text(encoding="utf-8", errors="ignore").lower()
            if w in t:
                _fail("слово-блокатор", f"{path.name}: позиция {t.index(w)}")


def check_big_once() -> None:
    """Каждая большая витрина читается одним запросом; ведомости — помесячно."""
    owners = {"mis_data_payroll_m": "T_PAY_MONTH", "asm_data_operation": "T_VSP",
              "ml_ksa_clickstream_events_oaa": "T_DIG", "uzp_dwh_sale_funnel_task": "T_DEAL"}
    for tbl, owner in owners.items():
        readers = [n for n, sql in Q.all_sql().items() if tbl in sql]
        if readers != [owner]:
            _fail("одно чтение витрины", f"{tbl}: читают {readers}")
    if "p.report_dt = CAST(:m AS date)" not in Q.T_PAY_MONTH:
        _fail("одно чтение витрины", "копия ведомостей не ограничена месяцем")


def check_dialect() -> None:
    """Нет конструкций, которых не знает ядро 9.4, и своих функций заказчика."""
    bad = {r"=>": "make_interval(months => n)", r"ON CONFLICT": "9.5+", r"GROUPING SETS": "не 9.4",
           r"\blast_day\s*\(": "last_day() — функция не из ядра", r"DISTINCT ON": "DISTINCT ON",
           r"\bINNER\b": "INNER JOIN — экранирование показа испортит ключевое слово"}
    for name, sql in Q.all_sql().items():
        for pat, why in bad.items():
            if re.search(pat, sql, re.IGNORECASE):
                _fail("диалект", f"{name}: {why}")


def check_no_distinct() -> None:
    """Нет count(DISTINCT) рядом с большими временными таблицами."""
    for name, sql in Q.all_sql().items():
        if "count(DISTINCT" in sql and any(re.search(rf"\b{t}\b", sql) for t in BIG_TEMP + ("t_nflc",)):
            _fail("count(DISTINCT)", f"{name}: считайте двухступенчато")


def check_mask_cast() -> None:
    """Номер организации приводится к числу только под маской цифр."""
    for name, sql in Q.all_sql().items():
        for m in re.finditer(r"CAST\(([\w.()]+?) AS bigint\)", sql):
            src = m.group(1)
            if f"{src} ~ " + Q.MASK not in sql and f"{src} ~ '" not in sql:
                _fail("маска номера", f"{name}: CAST({src} AS bigint) без маски")


def check_placeholders() -> None:
    """После подстановки схем фигурных плейсхолдеров не остаётся."""
    from . import db
    for name, sql in Q.all_sql().items():
        if name.startswith(("CREATE_", "INSERT_", "ANALYZE_", "DROP_")):
            continue
        left = re.findall(r"\{(?!1,12\})[a-z_]+\}", db.render(sql))
        if left:
            _fail("плейсхолдеры", f"{name}: {left}")


def check_names_masked() -> None:
    """Названия с ФИО скрываются: маска на примерах, и названия едут только колонками под маской."""
    hide = ["ИП Иванов Иван Иванович", "ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ СИДОРОВА АННА ПЕТРОВНА",
            "Глава КФХ Петров П.П.", "ГК Сидоров Пётр Ильич"]
    keep = ["ООО «Ромашка»", "МИНОБОРОНЫ", "МБОУ СОШ № 5", "АО «Типография»"]
    for n in hide:
        if N.safe(n) is not None:
            _fail("маска ФИО", f"не скрыто: {n}")
    for n in keep:
        if N.safe(n) is None:
            _fail("маска ФИО", f"скрыто лишнее: {n}")
    for name, sql in Q.all_sql().items():
        for col in ("company_name", "holding_name"):
            for m in re.finditer(rf"\b\w+\.{col}\b[^,\n]*", sql):
                frag = m.group(0)
                if "=" in frag or "IS NOT NULL" in frag or "btrim" in frag:
                    continue
                alias = re.search(r"\bAS\s+(\w+)", frag)
                out = alias.group(1) if alias else col
                if out not in N.NAME_COLS:
                    _fail("маска ФИО", f"{name}: {col} выводится колонкой «{out}»")


def check_shown_sql() -> None:
    """Показанный SQL — без латинского имени колонки номера организации."""
    for name, sql in Q.all_sql().items():
        if V._LAT in V.hide_col(sql).lower():
            _fail("показ SQL", f"{name}: осталось имя колонки")
    s = V.sanitize(f"номер {V.BLOCKED} 1; дл{V.BLOCKED.lower()}ый")
    if V._WORD.search(s) or s.count(V.ORG_ID) != 1:
        _fail("слово-блокатор", s)


def check_toy_identities() -> None:
    """Тождества на игрушечных данных: каналы складываются в НФЛ, классы ячеек — в портфель."""
    nm = pd.DataFrame([["2026-01-31", "cur", "mzp", "cur", 3], ["2026-01-31", "cur", "other", "cur", 2],
                       ["2025-01-31", "prev", "vsp", "arch", 4]], columns=["report_dt", "yr", "ch", "src", "n"])
    t = A.nfl_tables(nm, ["2026-01-31"], ["2025-01-31"])
    y = t["yoy"].set_index("ch")
    if y.loc["all", "cur"] != 5 or y.loc["all", "prev"] != 4 or y.loc[A.CH, "delta"].sum() != 1:
        _fail("каналы", "каналы не складываются в НФЛ")
    cs = pd.DataFrame([["cur", "grow", "2-5", 2, 10, 14], ["cur", "closed", "6-20", 1, 7, 0],
                       ["cur", "flat", "0", 1, 5, 5]], columns=["pair", "cls", "step", "n_cells", "n_b", "n_c"])
    ct = pd.DataFrame([["2025-08-31", 4, 22], ["2026-08-31", 3, 19]], columns=["report_dt", "n_cells", "n_fl"])
    c = A.cell_tables(cs, ct, ["2024-08-31", "2025-08-31", "2026-08-31"])
    if c["grp"].loc["cur", "d"].sum() != -3 or c["grp"].loc[("cur", "decl"), "n_cells"] != 1:
        _fail("ячейки", "классы не складываются")
    if "interval '3 month' - interval '1 day'" not in Q.valid_to("d"):
        _fail("окно", "конец действия — не последний день месяца +2")


CHECKS = [check_self_contained, check_no_blocked_word, check_big_once, check_dialect, check_no_distinct,
          check_mask_cast, check_placeholders, check_names_masked, check_shown_sql, check_toy_identities]


def run_all() -> None:
    for f in CHECKS:
        f()
        print(f"  ✓ {f.__doc__.strip().splitlines()[0]}", flush=True)
    print(f"Самопроверки: {len(CHECKS)} из {len(CHECKS)} прошли", flush=True)


# --------------------------------------------------------------------------- #
def check_against_synth(res: dict) -> None:
    """Совпадает ли отчёт с независимым расчётом по тем же синтетическим строкам."""
    exp = json.loads((config.OUTPUT_DIR / "synth_expect.json").read_text(encoding="utf-8"))
    ok, errs = [], []

    def need(cond, what):
        (ok if cond else errs).append(what)

    tot = res["nfl"]["tot"]
    for y, tag in (("2025", "prev"), ("2026", "cur")):
        for c in A.CH:
            got, want = float(tot.loc[tag, c]), float(exp["nfl"][y].get(c, 0))
            need(got == want, f"НФЛ {y} {A.CH_T[c]}: {got:.0f} против {want:.0f}")
    t = res["cell_t"]["tot"]
    for d, v in exp["portfolio"].items():
        need(float(t.loc[d, "n_fl"]) == v if d in t.index else False, f"портфель {d}: {v}")
    cls = res["cell_t"]["cls"]
    for pair, d in exp["cells"].items():
        for k, v in d.items():
            got = float(cls.loc[(pair, k), "n_cells"]) if (pair, k) in cls.index else 0.0
            need(got == v, f"ячейки {pair} {k}: {got:.0f} против {v}")
    mz = res["mzp"]["year"]
    need(mz.loc["prev", "staff"] == exp["staff"]["2025-08-31"] and mz.loc["cur", "staff"] == exp["staff"]["2026-08-31"],
         "штат МЗП (повторные строки сотрудника не задваивают)")
    need(mz.loc["prev", "deals"] == exp["deals_mzp"]["2025"] and mz.loc["cur", "deals"] == exp["deals_mzp"]["2026"],
         "сделки МЗП: одна строка на сделку, только роль МЗП")
    ne = res["raw"]["nfl_no_epk"]
    need(float(ne["n"].sum()) == exp["nfl_no_epk"], "НФЛ без ЕПК посчитаны отдельно")
    html = open(res["path"], encoding="utf-8").read()
    leaked = [n for n in exp["fio_names"] if n in html]
    need(not leaked, f"названия с ФИО не показаны (просочились: {leaked[:3]})")
    need(N.HIDDEN_HOLDING in html, "холдинг с ФИО показан как «название скрыто»")
    need(V.BLOCKED.lower() not in html.lower() and V._LAT not in html.lower(),
         "в HTML нет ни кириллического, ни латинского имени колонки номера организации")
    need(all(c["ok"] for c in res["checks"]), f"проверки сходимости: {sum(c['ok'] for c in res['checks'])} "
                                               f"из {len(res['checks'])}")
    for w in ok:
        print(f"  ✓ {w}", flush=True)
    for w in errs:
        print(f"  ✗ {w}", flush=True)
    if errs:
        raise AssertionError(f"не совпало с синтетикой: {len(errs)} из {len(ok) + len(errs)}")
    print(f"Сверка с синтетикой: {len(ok)} из {len(ok)}", flush=True)

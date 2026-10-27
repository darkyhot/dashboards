"""Самопроверки.

`run_all()` — без БД, секунды: автономность папки, фильтр партиции в каждом
обращении к ведомостям, диалект 9.4, нет запретного слова, столбцы сводки.

`check_against_synth()` — только открытый контур: сводка из БД сверяется с
независимым эталоном (`reference.py`) по каждой ячейке и с заложенными событиями.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from . import REPORT_DIR, db
from . import sql as Q

ALLOWED_IMPORTS = {"__future__", "argparse", "calendar", "collections", "contextlib", "datetime",
                   "functools", "inspect", "io", "json", "os", "pathlib", "re", "statistics", "sys", "time",
                   "typing", "numpy", "pandas", "sqlalchemy"}


def _sql_texts() -> list[tuple[str, str]]:
    out = [(n, v) for n, v in vars(Q).items() if isinstance(v, str) and n.isupper()]
    out.append(("final_select()", Q.final_select()))
    out.append(("row_checks()", Q.row_checks()))
    return out


def check_self_contained() -> list[str]:
    bad = []
    for f in (REPORT_DIR / "ptf").glob("*.py"):
        for ln in f.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*(?:from|import)\s+([\w.]+)", ln)
            if not m or m.group(1).startswith("."):
                continue
            root = m.group(1).split(".")[0]
            if root not in ALLOWED_IMPORTS:
                bad.append(f"{f.name}: импорт «{m.group(1)}» — пакет обязан быть автономным")
    return bad


def check_partition_filter() -> list[str]:
    return [f"{n}: обращение к ведомостям без фильтра одного месяца"
            for n, s in _sql_texts()
            if "{payroll}" in s and "report_dt = CAST(:m AS date)" not in s]


def check_dialect() -> list[str]:
    bad = []
    for n, s in _sql_texts():
        u = s.upper()
        for w in ("GROUPING SETS", "ON CONFLICT", "MAKE_INTERVAL", "COUNT(DISTINCT", "FILTER (WHERE"):
            if w in u:
                bad.append(f"{n}: «{w}» — нет в Greenplum 9.4 или запрещено правилами")
    return bad


def check_forbidden_word() -> list[str]:
    """Трёхбуквенное кириллическое имя номера организации запрещено везде (блокатор
    закрытого контура срабатывает и на код). Слово собирается из кодов символов,
    чтобы его не было в самом этом файле."""
    word = "".join(chr(c) for c in (0x418, 0x41D, 0x41D))
    bad = []
    for f in REPORT_DIR.rglob("*"):
        if f.is_file() and f.suffix in (".py", ".md", ".ipynb", ".txt", ".sql") and "output" not in f.parts:
            # Отдельное слово, а не подстрока: «причинный» его содержит, но им не является.
            pat = re.compile(rf"(?<![а-яё]){word.lower()}(?![а-яё])")
            if pat.search(f.read_text(encoding="utf-8", errors="ignore").lower()):
                bad.append(f"{f.relative_to(REPORT_DIR)}: запрещённое слово")
    return bad


def check_columns() -> list[str]:
    cols = Q.final_columns()
    bad = []
    if len(cols) != len(set(cols)):
        bad.append("в сводке повторяются имена столбцов")
    sel = Q.final_select()
    for c in cols:
        if not re.search(rf"\b{c}\b", sel):
            bad.append(f"столбец {c} не собирается в final_select")
    return bad


def run_all() -> None:
    problems = []
    for fn in (check_self_contained, check_partition_filter, check_dialect, check_forbidden_word,
               check_columns):
        p = fn()
        print(f"{'✓' if not p else '✗'} {fn.__name__}" + ("" if not p else f": {len(p)}"), flush=True)
        problems += p
    if problems:
        for p in problems:
            print("   ", p, flush=True)
        raise AssertionError(f"Самопроверки не прошли: {len(problems)}")
    print(f"Все самопроверки прошли; столбцов в сводке: {len(Q.final_columns())}", flush=True)


# --------------------------------------------------------------------------- #
# Синтетика
# --------------------------------------------------------------------------- #
def read_result(conn_url: str) -> dict:
    """Сводка, потоки, журнал и поколение последнего прогона."""
    eng = db.get_engine(conn_url)
    with db.session(eng) as c:
        out = {"summary": db.read_sql(c, "SELECT * FROM {schema_t}.yva_ptf_month_org"),
               "pairs": db.read_sql(c, "SELECT * FROM {schema_t}.yva_ptf_flow_pairs"),
               "journal": db.read_sql(c, "SELECT * FROM {schema_t}.yva_ptf_journal WHERE run_id = "
                                         "(SELECT max(run_id) FROM {schema_t}.yva_ptf_journal)"),
               "vintage": db.read_sql(c, "SELECT * FROM {schema_t}.yva_ptf_vintage WHERE run_id = "
                                         "(SELECT max(run_id) FROM {schema_t}.yva_ptf_vintage)")}
    for df in out.values():
        if "report_dt" in df:
            df["report_dt"] = pd.to_datetime(df["report_dt"]).dt.date.astype(str)
    return out


def compare(got: pd.DataFrame, exp: pd.DataFrame, key: list[str],
            text_cols: tuple = ("seg_rep", "seg_cur", "seg_src", "slice_type", "slice_value")) -> list[str]:
    g = got.set_index(key).sort_index()
    e = exp.set_index(key).sort_index()
    bad = []
    only_g = g.index.difference(e.index)
    only_e = e.index.difference(g.index)
    if len(only_g) or len(only_e):
        bad.append(f"ключи: только в SQL {len(only_g)}, только в эталоне {len(only_e)}; "
                   f"пример {list(only_g[:2]) or list(only_e[:2])}")
    idx = g.index.intersection(e.index)
    for c in e.columns:
        a, b = g.loc[idx, c], e.loc[idx, c]
        na_a, na_b = a.isna(), b.isna()
        diff = na_a != na_b
        both = ~na_a & ~na_b
        if c not in text_cols and not c.endswith("base_seg") and c != "prelim":
            av = pd.to_numeric(a[both], errors="coerce").astype(float)
            bv = pd.to_numeric(b[both], errors="coerce").astype(float)
            diff[both] = ~np.isclose(av, bv, atol=1e-6, rtol=1e-9)
        else:
            diff[both] = a[both].astype(str) != b[both].astype(str)
        if diff.any():
            i = diff[diff].index[0]
            bad.append(f"{c}: расходится в {int(diff.sum())} строках; пример {i}: SQL={a[i]!r} эталон={b[i]!r}")
    return bad


def scenarios(got: dict, expect: dict) -> list[tuple[str, bool, str]]:
    """Заложенные события — проверяются по смыслу, независимо от эталона."""
    from . import months as M
    df, pairs, journal = got["summary"], got["pairs"], got["journal"]
    roles = expect["roles"]
    out = []

    def add(name, ok, detail=""):
        out.append((name, bool(ok), detail))

    def org(role):
        return df[df["org_id"] == roles[role]]

    start = M.parse(expect["start"])
    stop_m = M.iso(M.shift(start, expect["gi_stop"]))
    prev_m = M.iso(M.shift(start, expect["gi_stop"] - 1))
    st = org("stop")
    pf_before = st.loc[st["report_dt"] == prev_m, "pf_recip"].sum()
    at = st[st["report_dt"] == stop_m]
    outs = at[["m_out_left_bank", "m_out_to_other_org", "m_out_to_nonzp", "m_out_zp_out"]].sum().sum()
    add("организация перестала платить: ушли все", pf_before > 0 and outs == pf_before
        and at["m_lb_org_stopped"].sum() > 0, f"было {pf_before}, ушло {outs}, "
        f"из них «перестала платить» {at['m_lb_org_stopped'].sum()}")
    add("после остановки организации портфеля нет",
        st.loc[st["report_dt"] >= stop_m, "pf_recip"].sum() == 0)

    seas = df.groupby("report_dt")["m_lb_seasonal"].sum(min_count=1)
    add("сезонники (провал 1 мес.) пойманы в августе 2025 и 2026",
        seas.get("2025-08-31", 0) > 0 and seas.get("2026-08-31", 0) > 0,
        f"авг-25 {seas.get('2025-08-31', 0)}, авг-26 {seas.get('2026-08-31', 0)}")
    add("в августе 2024 причин ухода нет (нет 12 мес. памяти) — пусто",
        df.loc[df["report_dt"] == "2024-08-31", "m_lb_seasonal"].isna().all())
    s3 = org("seas3")
    s4 = org("seas4")
    add("сезон длиной 3 мес. пойман", s3.loc[s3["report_dt"] == "2026-08-31", "m_lb_seasonal"].sum() > 0)
    add("провал 4 мес. сезоном не считается",
        s4.loc[s4["report_dt"] == "2026-08-31", "m_lb_seasonal"].sum() == 0
        and s4.loc[s4["report_dt"] == "2026-08-31", "m_out_left_bank"].sum() > 0)
    sv = org("seas").loc[lambda d: d["report_dt"] == "2025-09-30", "m_in_returned"].sum()
    add("сезонники вернулись в сентябре как «вернулся»", sv > 0, f"{sv}")

    rc = org("reclass")
    rc6 = rc[rc["report_dt"] == "2025-06-30"]
    add("смена исторического сегмента видна год к году",
        len(rc6) > 0 and (rc6["seg_rep"] == "КСБ").all() and (rc6["y_base_seg"] == "ММБ").all(),
        f"{rc6[['seg_rep', 'y_base_seg']].drop_duplicates().values.tolist()}")
    old = rc[rc["report_dt"] == "2024-06-30"]
    add("до смены: сегмент отчётного месяца ММБ, текущий КСБ",
        len(old) > 0 and (old["seg_rep"] == "ММБ").all() and (old["seg_cur"] == "КСБ").all(),
        f"{old[['seg_rep', 'seg_cur']].drop_duplicates().values.tolist()}")
    add("текущая сегментация — по последнему полному месяцу (сен-26, окт-26 загружен наполовину)",
        (journal["seg_cur_dt"].astype(str).str[:10] == "2026-09-30").all())
    nh = org("nohist")
    add("нет в истории → сегмент из ЕПК", len(nh) > 0 and (nh["seg_src"] == "ЕПК").all())
    ne = org("noepk")
    add("нет нигде → «Не найден»", len(ne) > 0 and (ne["seg_rep"] == "Не найден").all()
        and (ne["seg_cur"] == "Не найден").all())
    add("ТИК/ЦИК исключена целиком", (df["org_id"] == roles["tik"]).sum() == 0)
    add("ТИК с признаком false осталась", (df["org_id"] == roles["tik_false"]).sum() > 0)
    add("Сбер и непригодные номера не попали", (df["org_id"] == 7707083893).sum() == 0)
    for r in ("edu", "mo"):
        add(f"организация без порога ({r}) в портфеле при суммах 1500", org(r)["pf_recip"].sum() > 0)
    add("«мигающие» зп ↔ не зп пойманы", org("flap")["flapping_recip"].sum() > 0)
    add("разовые пойманы", df["one_time_pf"].sum() > 0)
    add("пропуск одного месяца пойман", df["m_out_temp"].sum() > 0)
    add("люди с одной выплатой посчитаны", 0 < df["single_pay_recip"].sum() < df["pf_recip"].sum())
    tot = df.groupby("report_dt")[["multi_recip", "y_in_oth_same_hold", "y_out_oth_same_org"]].sum().sum()
    add("совместители и переходы внутри холдинга / организации есть",
        (tot > 0).all(), tot.to_dict().__repr__())
    reorg_m = M.iso(M.shift(start, expect["gi_reorg"]))
    pr = pairs[(pairs["from_org_id"] == roles["reorg_a"]) & (pairs["to_org_id"] == roles["reorg_b"])]
    add("реорганизация A → B — одна пара на 60 человек",
        len(pr) == 1 and pr["report_dt"].iloc[0] == reorg_m and int(pr["n_recip"].iloc[0]) == 60,
        pr[["report_dt", "n_recip"]].values.tolist().__repr__())
    add("месяцы до окна не читаются", df["report_dt"].min() == "2023-11-30")
    add("до 2024-01 сравнений нет: база января 2024 пуста",
        df.loc[df["report_dt"] == "2024-01-31", "m_base_pf"].isna().all()
        and df.loc[df["report_dt"] == "2024-02-29", "m_base_pf"].notna().all())
    add("год к году — только с базой от 2024-01",
        df.loc[df["report_dt"] == "2024-12-31", "y_base_pf"].isna().all()
        and df.loc[df["report_dt"] == "2025-01-31", "y_base_pf"].notna().all())
    return out


def check_against_synth(conn_url: str, tables: dict, pair_min: int = 10) -> bool:
    from . import reference
    got = read_result(conn_url)
    exp = reference.compute(tables, tables["expect"]["report_month"], pair_min=pair_min)
    ok = True

    def report(name, bad):
        nonlocal ok
        ok &= not bad
        print((f"✓ {name}: совпадает с эталоном" if not bad else f"✗ {name}: расхождений {len(bad)}"),
              flush=True)
        for b in bad[:20]:
            print("   ", b, flush=True)

    print(f"Сводка SQL: {len(got['summary']):,} строк; эталон: {len(exp['summary']):,}", flush=True)
    report("сводка (каждая ячейка)", compare(got["summary"], exp["summary"],
                                             ["report_dt", "org_id", "sys_gosb_id"]))
    pk = ["report_dt", "from_org_id", "from_gosb_id", "to_org_id", "to_gosb_id"]
    report(f"потоки «орг → орг» ({len(exp['pairs'])} пар)", compare(got["pairs"], exp["pairs"], pk))
    j = got["journal"][["report_dt", "slice_type", "slice_value", "persons"]]
    report("точное число людей по срезам", compare(j, exp["persons"],
                                                    ["report_dt", "slice_type", "slice_value"]))
    # Журнал и поколение сходятся со сводкой.
    s = got["summary"]
    jb = got["journal"].query("slice_type == 'bank'").set_index("report_dt")["pf_recip"]
    report("журнал: портфель банка = сводка",
           [] if (jb.sort_index() == s.groupby("report_dt")["pf_recip"].sum().sort_index()).all()
           else ["итоги по месяцам расходятся"])
    va = got["vintage"].query("lvl == 'atom'").groupby("report_dt")["pf_recip"].sum()
    report("поколение: атомы = сводка",
           [] if (va.sort_index() == s.groupby("report_dt")["pf_recip"].sum().sort_index()).all()
           else ["итоги по месяцам расходятся"])
    for name, good, detail in scenarios(got, tables["expect"]):
        ok &= good
        print(f"{'✓' if good else '✗'} {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def check_window_shift(conn_url: str, report_month: str, n_months: int = 24, **kw) -> bool:
    """Главная проверка независимости от окна: прогоны на M и M+1 (окно сдвигается на
    месяц) обязаны совпасть по общим месяцам во всех непустых ячейках. Исключаются
    только последние 2 месяца первого прогона (там разовые и пропуск ещё
    предварительные) и текущий сегмент (он и должен обновляться). Открытый контур."""
    from . import build
    from . import months as M
    runs = []
    for rm in (report_month, M.iso(M.shift(report_month, 1))[:7]):
        build.run(conn=conn_url, report_month=rm, n_months=n_months, verbose=False, **kw)
        runs.append(read_result(conn_url)["summary"])
    a, b = runs
    common = sorted(set(a["report_dt"]) & set(b["report_dt"]))[:-2]
    key = ["report_dt", "org_id", "sys_gosb_id"]
    a = a[a["report_dt"].isin(common)].set_index(key).sort_index()
    b = b[b["report_dt"].isin(common)].set_index(key).sort_index()
    idx = a.index.intersection(b.index)
    bad = []
    # Строки «был только в базе» (людей в месяце нет) появляются, только если база в
    # окне, — у начала окна их может не быть. Сравниваются ключи строк с людьми.
    live_a = set(a.index[a["any_recip"] > 0])
    live_b = set(b.index[b["any_recip"] > 0])
    if live_a != live_b:
        bad.append(f"ключи строк с людьми: различаются {len(live_a ^ live_b)}")
    for c in a.columns:
        if c in ("seg_cur", "prelim"):
            continue
        x, y = a.loc[idx, c], b.loc[idx, c]
        both = x.notna() & y.notna()
        if c in ("seg_rep", "seg_src") or c.endswith("base_seg"):
            d = (x[both].astype(str) != y[both].astype(str)).sum()
        else:
            d = (~np.isclose(pd.to_numeric(x[both]).astype(float),
                             pd.to_numeric(y[both]).astype(float), atol=1e-6)).sum()
        if d:
            bad.append(f"{c}: {int(d)} ячеек различаются")
    print(f"Сдвиг окна {report_month} → +1 мес., {len(common)} общих месяцев: "
          + ("✓ все непустые ячейки совпадают" if not bad else f"✗ {len(bad)} столбцов различаются"),
          flush=True)
    for x in bad[:20]:
        print("   ", x, flush=True)
    return not bad

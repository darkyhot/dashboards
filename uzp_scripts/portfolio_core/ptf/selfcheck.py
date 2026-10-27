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
                   "functools", "inspect", "io", "json", "os", "pathlib", "re", "sys", "time",
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
            if word.lower() in f.read_text(encoding="utf-8", errors="ignore").lower():
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
def read_result(conn_url: str) -> pd.DataFrame:
    eng = db.get_engine(conn_url)
    with db.session(eng) as c:
        df = db.read_sql(c, "SELECT * FROM {schema_t}.yva_ptf_month_org")
    df["report_dt"] = pd.to_datetime(df["report_dt"]).dt.date.astype(str)
    return df


def compare(got: pd.DataFrame, exp: pd.DataFrame) -> list[str]:
    key = ["report_dt", "org_id", "sys_gosb_id"]
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
        if pd.api.types.is_numeric_dtype(b.dropna().infer_objects()) and c not in ("seg", "seg_src") \
                and not c.endswith("base_seg"):
            av = pd.to_numeric(a[both], errors="coerce").astype(float)
            bv = pd.to_numeric(b[both], errors="coerce").astype(float)
            diff[both] = ~np.isclose(av, bv, atol=1e-6, rtol=1e-9)
        else:
            diff[both] = a[both].astype(str) != b[both].astype(str)
        if diff.any():
            i = diff[diff].index[0]
            bad.append(f"{c}: расходится в {int(diff.sum())} строках; пример {i}: SQL={a[i]!r} эталон={b[i]!r}")
    return bad


def scenarios(df: pd.DataFrame, expect: dict) -> list[tuple[str, bool, str]]:
    """Заложенные события — проверяются по смыслу, независимо от эталона."""
    from . import months as M
    roles = expect["roles"]
    out = []

    def add(name, ok, detail=""):
        out.append((name, bool(ok), detail))

    start = M.parse(expect["start"])
    stop_m = M.iso(M.shift(start, expect["gi_stop"]))
    prev_m = M.iso(M.shift(start, expect["gi_stop"] - 1))
    st = df[df["org_id"] == roles["stop"]]
    pf_before = st.loc[st["report_dt"] == prev_m, "pf_recip"].sum()
    at = st[st["report_dt"] == stop_m]
    outs = at[["m_out_left_bank", "m_out_to_other_org", "m_out_to_nonzp", "m_out_zp_out"]].sum().sum()
    add("организация перестала платить: ушли все", pf_before > 0 and outs == pf_before
        and at["m_lb_org_stopped"].sum() > 0, f"было {pf_before}, ушло {outs}, "
        f"из них «перестала платить» {at['m_lb_org_stopped'].sum()}")
    add("после остановки организации портфеля нет",
        st.loc[st["report_dt"] >= stop_m, "pf_recip"].sum() == 0)

    seas = df.groupby("report_dt")["m_lb_seasonal"].sum()
    add("сезонники пойманы в августе 2025 и 2026",
        seas.get("2025-08-31", 0) > 0 and seas.get("2026-08-31", 0) > 0,
        f"авг-25 {seas.get('2025-08-31', 0)}, авг-26 {seas.get('2026-08-31', 0)}")
    add("в августе 2024 сезонности не видно (нет M−13 в окне)", seas.get("2024-08-31", 0) == 0)
    sv = df[(df["org_id"] == roles["seas"]) & (df["report_dt"] == "2025-09-30")]["m_in_returned"].sum()
    add("сезонники вернулись в сентябре как «вернулся»", sv > 0, f"{sv}")

    rc = df[(df["org_id"] == roles["reclass"]) & (df["report_dt"] == "2025-06-30")]
    add("смена исторического сегмента видна год к году",
        len(rc) > 0 and (rc["seg"] == "КСБ").all() and (rc["y_base_seg"] == "ММБ").all(),
        f"{rc[['seg', 'y_base_seg']].drop_duplicates().values.tolist()}")
    nh = df[df["org_id"] == roles["nohist"]]
    add("нет в истории → сегмент из ЕПК", len(nh) > 0 and (nh["seg_src"] == "ЕПК").all())
    ne = df[df["org_id"] == roles["noepk"]]
    add("нет нигде → «Не найден»", len(ne) > 0 and (ne["seg"] == "Не найден").all())
    add("ТИК/ЦИК исключена целиком", (df["org_id"] == roles["tik"]).sum() == 0)
    add("ТИК с признаком false осталась", (df["org_id"] == roles["tik_false"]).sum() > 0)
    add("Сбер и непригодные номера не попали", (df["org_id"] == 7707083893).sum() == 0)
    for r in ("edu", "mo"):
        add(f"организация без порога ({r}) в портфеле при суммах 1500",
            df.loc[df["org_id"] == roles[r], "pf_recip"].sum() > 0)
    fl = df[df["org_id"] == roles["flap"]]
    add("«мигающие» зп ↔ не зп пойманы", fl["flapping_recip"].sum() > 0 and fl["flip_to_nonzp"].sum() > 0)
    add("разовые пойманы", df["one_time_recip"].sum() > 0)
    add("пропуск одного месяца пойман", df["m_out_temp"].sum() > 0)
    tot = df.groupby("report_dt")[["multi_recip", "y_in_oth_same_hold", "y_out_oth_same_org"]].sum().sum()
    add("совместители и переходы внутри холдинга / организации есть",
        (tot > 0).all(), tot.to_dict().__repr__())
    add("месяцы до окна не читаются", df["report_dt"].min() == "2023-11-30")
    return out


def check_against_synth(conn_url: str, tables: dict) -> bool:
    from . import reference
    got = read_result(conn_url)
    exp = reference.compute(tables, tables["expect"]["report_month"])
    print(f"Сводка SQL: {len(got):,} строк; эталон: {len(exp):,} строк", flush=True)
    bad = compare(got, exp)
    print(("✓ SQL совпадает с эталоном по всем ячейкам" if not bad
           else f"✗ расхождений: {len(bad)}"), flush=True)
    for b in bad[:30]:
        print("   ", b, flush=True)
    sc = scenarios(got, tables["expect"])
    for name, ok, detail in sc:
        print(f"{'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""), flush=True)
    return not bad and all(ok for _, ok, _ in sc)

"""Самопроверки без БД — до первого запроса. И сверка с синтетикой после прогона.

Проверяется не «работает ли код», а то, из-за чего результату нельзя было бы
верить: автономность папки, фильтр партиции в каждом обращении к ведомостям,
диалект 9.4, приведение номера организации только под маской, тождества
разложения, правила отбора организаций, обезличивание.
"""
from __future__ import annotations

import ast
import json
import re
import sys

import pandas as pd

from . import PKG_DIR, analyze as A, config, months as M, queries as Q, view as V

ALLOWED_TOP = {"pandas", "numpy", "sqlalchemy"}


def _fail(name: str, msg: str) -> None:
    raise AssertionError(f"{name}: {msg}")


def check_self_contained() -> None:
    """Пакет импортирует только stdlib, pandas, numpy, sqlalchemy и себя.
    Правило заказчика: отчёт лежит целиком в своей папке."""
    std = set(getattr(sys, "stdlib_module_names", ()))
    for path in PKG_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:          # относительный импорт — внутри пакета
                    continue
                mods = [node.module or ""]
            for m in mods:
                top = m.split(".")[0]
                if top in ALLOWED_TOP or top == "bank_yoy" or top in std or top == "__future__":
                    continue
                _fail("автономность", f"{path.name} импортирует «{m}» — вне папки отчёта")


def check_partition_filter() -> None:
    """Каждое обращение к ведомостям ограничено ОДНИМ месяцем."""
    for name, sql in Q.all_sql().items():
        n_ref = sql.count("uzp_data_payroll_m")
        if not n_ref:
            continue
        n_flt = len(re.findall(r"report_dt\s*(=\s*CAST\(:m AS date\)|>=\s*CAST\(:d_from)", sql))
        if n_flt < n_ref:
            _fail("партиция", f"{name}: обращений к ведомостям {n_ref}, фильтров по месяцу {n_flt}")


def check_dialect() -> None:
    """Нет конструкций, которых не знает ядро 9.4."""
    bad = {r"=>": "make_interval(months => n) — 9.5+", r"ON CONFLICT": "9.5+",
           r"GROUPING SETS": "не на ядре 9.4", r"\bFILTER\s*\(\s*WHERE[^)]*\)\s*OVER": "FILTER в окне",
           r"count\(DISTINCT[^)]*\)\s*OVER": "DISTINCT в оконной функции"}
    for name, sql in Q.all_sql().items():
        for pat, why in bad.items():
            if re.search(pat, sql, re.IGNORECASE):
                _fail("диалект", f"{name}: {why}")


def check_payroll_once() -> None:
    """Ведомости читает ТОЛЬКО копия t_raw — всё остальное считается из неё."""
    readers = [n for n, sql in Q.all_sql().items() if "uzp_data_payroll_m" in sql]
    if readers != ["T_RAW_MONTH"]:
        _fail("одна копия ведомостей", f"к витрине обращаются: {readers}")
    if "CREATE_TMP" in Q.T_RAW_MONTH or "INSERT" in Q.T_RAW_MONTH:
        _fail("одна копия ведомостей", "T_RAW_MONTH — тело, а не оператор")


def check_no_distinct() -> None:
    """Нет count(DISTINCT) по большим таблицам: на объёме банка он снимал прогон."""
    big = ("t_raw", "t_stage", "t_pairs", "t_tflow", "t_mv", "t_epk ", "t_person")
    for name, sql in Q.all_sql().items():
        if "count(DISTINCT" in sql and any(t in sql for t in big):
            _fail("count(DISTINCT)", f"{name}: уникальный подсчёт по большой таблице — "
                                     f"считайте двухступенчатой группировкой")


def check_no_org_names() -> None:
    """Названия организаций и холдингов отчёт не берёт: у ИП в названии ФИО."""
    for name, sql in Q.all_sql().items():
        for col in ("company_name", "holding_name", "head_holding_name"):
            if col in sql:
                _fail("названия организаций", f"{name}: выбирается {col}")


def check_inn_cast() -> None:
    """CAST(<алиас>.inn AS bigint) — только в запросе, где стоит маска того же алиаса."""
    for name, sql in Q.all_sql().items():
        for alias in set(re.findall(r"CAST\((\w+)\.inn AS bigint\)", sql)):
            if f"{alias}.inn ~ '^[0-9]{{1,12}}$'" not in sql:
                _fail("маска ИНН", f"{name}: приведение {alias}.inn без маски")
        if re.search(r"IN\s*:codes", sql):
            _fail("коды", f"{name}: IN :codes вместо = ANY(:codes)")


def check_placeholders() -> None:
    """После подстановки не остаётся фигурных плейсхолдеров."""
    from . import db
    for name, sql in Q.all_sql().items():
        if name.startswith(("CREATE_", "INSERT_", "ANALYZE_", "DROP_")):
            continue            # шаблоны DDL: {name}/{body} подставляет fetch
        left = re.findall(r"\{(?!1,12\})[a-z_]+\}", db.render(sql))
        if left:
            _fail("плейсхолдеры", f"{name}: {left}")


def _fl(rows):
    cols = ["seg_from", "seg_to", "cause", "tenure", "was_bp1", "in_cn1", "n_epk", "tr_b", "tr_c", "inn_b", "inn_c"]
    return pd.DataFrame(rows, columns=cols)


def check_decomp_identity() -> None:
    """Тождество разложения на игрушечном потоке, включая совместителя."""
    fl = _fl([
        ["КСБ", "КСБ", "stay", "old", None, 1, 10, 12, 11, 11, 11],   # двое потеряли второй ГОСБ
        ["РГС", "КСБ", "stay", "old", None, 1, 3, 3, 3, 3, 3],        # переток между сегментами
        ["КСБ", "—", "left_bank", "new1", None, 0, 2, 2, 0, 2, 0],
        ["РГС", "—", "below_threshold", "old", None, 1, 1, 2, 0, 2, 0],
        ["—", "ММБ", "new_to_bank", None, 0, 1, 4, 0, 4, 0, 4],
    ])
    d = A.decomp(fl)
    if abs(d["residual_tr"]) > 1e-9 or abs(d["residual_epk"]) > 1e-9:
        _fail("разложение", f"невязка {d['residual_tr']}, {d['residual_epk']}")
    if d["inside_tr"] != -1 or d["inside_gosb"] != -1:
        _fail("разложение", f"совместительство {d['inside_tr']} / ГОСБ {d['inside_gosb']}")
    mx = A.flow_matrix(fl)
    if mx.loc["РГС", "КСБ"] != 3 or mx.loc[A.OUTSIDE, "ММБ"] != 4:
        _fail("матрица", "перетоки разложены не туда")


def check_did_identity() -> None:
    """Разница разниц: сумма групп = разности дельт двух переходов."""
    a = A.decomp(_fl([["КСБ", "—", "left_bank", "new2", None, 1, 5, 5, 0, 5, 0],
                      ["—", "КСБ", "new_to_bank", None, 1, 1, 2, 0, 2, 0, 2]]))
    b = A.decomp(_fl([["КСБ", "—", "left_bank", "old", None, 0, 1, 1, 0, 1, 0]]))
    t = A.did_components(a, b, "2026-08-31", "2025-08-31")
    got = t[t["sub"].isna()]["diff"].sum()
    if abs(got - (a["delta_tr"] - b["delta_tr"])) > 1e-9:
        _fail("разница разниц", f"{got} против {a['delta_tr'] - b['delta_tr']}")
    if "пришли в июле" not in set(t["title"]) and "пришли в июне" not in set(t["title"]):
        _fail("разница разниц", "подписи стажа не в предложном падеже")


def check_org_rules() -> None:
    """Правила отбора организаций — та же формула, что в ORG_LIST, на примерах."""
    def pick(base, cur, stopped, reorg_out=0, reorg_in=0, o=None):
        o = o or {"min_base": 20, "min_real": 10, "min_share": 0.05}
        net_ex = cur - base + reorg_out - reorg_in
        real = min(stopped, -net_ex) if net_ex < 0 else 0
        return base >= o["min_base"] and net_ex < 0 and real >= o["min_real"] and real >= o["min_share"] * base
    cases = {"5 ушли из Сбера, 5 пришли": (pick(100, 100, 5), False),
             "реорганизация целиком": (pick(100, 0, 0, reorg_out=100), False),
             "переток в другие организации": (pick(100, 70, 3), False),
             "реальное сокращение": (pick(100, 70, 25), True),
             "ушли 30, пришли 25 новых": (pick(100, 95, 30), False)}
    for k, (got, want) in cases.items():
        if got != want:
            _fail("отбор организаций", k)
    if "LEAST(c.out_stopped, -c.net_ex_reorg)" not in Q.T_ORGSEL:
        _fail("отбор организаций", "формула реального сокращения в SQL разошлась с проверкой")


def check_sanitize() -> None:
    """Слово-блокатор заменяется только целым словом (правило 14)."""
    w = "И" + "НН"
    s = V.sanitize(f"номер {w} 123; длинный; старинный; {w.lower()}")
    if V._WORD.search(s) or "длинный" not in s or "старинный" not in s or s.count("Орг.") != 2:
        _fail("слово-блокатор", s)


def check_months() -> None:
    """Сдвиг месяцев и последний день месяца."""
    if M.iso(M.shift("2026-03", -13)) != "2025-02-28" or M.iso("2024-02") != "2024-02-29":
        _fail("месяцы", "сдвиг или конец месяца посчитан неверно")


CHECKS = [check_self_contained, check_payroll_once, check_no_distinct, check_no_org_names, check_partition_filter, check_dialect, check_inn_cast,
          check_placeholders, check_decomp_identity, check_did_identity, check_org_rules,
          check_sanitize, check_months]


def run_all() -> None:
    for f in CHECKS:
        f()
        print(f"  ✓ {f.__doc__.strip().splitlines()[0] if f.__doc__ else f.__name__}", flush=True)
    print(f"Самопроверки: {len(CHECKS)} из {len(CHECKS)} прошли", flush=True)


# --------------------------------------------------------------------------- #
# Сверка с синтетикой (только открытый контур)
# --------------------------------------------------------------------------- #
def check_against_synth(res: dict) -> None:
    """Находит ли разбор то, ради чего написан: каждое заложенное событие."""
    exp = json.loads((config.OUTPUT_DIR / "synth_expect.json").read_text(encoding="utf-8"))
    last = res["report"][-1]
    errs, ok = [], []

    def need(cond, what):
        (ok if cond else errs).append(what)

    # Картина: июнь +, июль +, август − (так заложено).
    need(res["pattern"].startswith("++−"), f"картина «++−» (получено {res['pattern']})")
    # Растворились пришедшие: убыль новичков июня–июля в августе.
    did = res["did"][last]["table"]
    newc = did[(did["group"] == "lost") & did["sub"].isin(["new1", "new2"])]["diff"].sum()
    gone = exp["influx_gone_aug26"]
    need(newc <= -0.8 * gone, f"растворились пришедшие: {newc:+.0f} против заложенных −{gone}")
    ds = res["dissolved"].set_index("k")
    for k in exp["influx"]:
        if k in ds.index:
            need(ds.loc[k, "surv_cur"] < ds.loc[k, "surv_prev"] - 0.3, f"доживаемость когорты {k} упала")
    # Схлопывание по организациям в августе.
    ins = did[(did["group"] == "inside") & (did["sub"] == "inn")]["diff"].sum()
    need(ins <= -0.8 * exp["second_job_cut_aug26"],
         f"уход со второй работы: {ins:+.0f} против −{exp['second_job_cut_aug26']}")
    # Схлопывание по ГОСБ — уровень, а не август: в разнице переходов его почти нет.
    gs = did[(did["group"] == "inside") & (did["sub"] == "gosb")]["diff"].sum()
    my = res["multi_yoy"]
    ksb = my[(my["report_dt"] == last) & (my["seg"] == "КСБ")]["extra_gosb"].sum()
    need(ksb < -100 and abs(gs) < abs(ksb) / 3, f"сведение ГОСБ КСБ: год к году {ksb:+.0f}, в августе {gs:+.0f}")
    # Сезон: повтор год к году находится.
    st = res["seasonal"].set_index("report_dt")
    need(st.loc[last, "n_seasonal"] >= 0.5 * st.loc[last, "n_gone_base"], "августовский сезон повторяется")
    # Организации.
    lst = res["orgs"][last]["list"]
    inns = set(lst["inn"].astype("int64")) if not lst.empty else set()
    for k, v in exp["real_cut_inns"].items():
        if k <= last:
            miss = [i for i in v if i not in inns]
            need(not miss, f"реальное сокращение ({k}) в списке: не найдены {miss}")
    # «5 ушли — 5 пришли»: заложенный обмен взаимно гасится. В список организация
    # может попасть только за счёт ОБЫЧНОЙ текучести на фоне — тогда пришедшие
    # обязаны уменьшить реальное сокращение относительно ушедших.
    ff = lst[lst["inn"].isin(exp["five_five_inns"])] if not lst.empty else lst
    need(ff.empty or bool((ff["real_cut"] < ff["out_stopped"]).all()),
         f"«5 ушли — 5 пришли»: пришедшие гасят ушедших ({len(ff)} в списке за счёт фона)")
    need(not (inns & {a for a, _ in exp["reorg_pairs"]}), "реорганизации в список не попали")
    # Переток не засчитывается в реальное сокращение. Сама организация в список
    # попасть может — если у неё ещё и обычная текучесть из Сбера выше порога.
    pt = lst[lst["inn"].isin(exp["peretok_inns"])] if not lst.empty else lst
    need(pt.empty or bool(((pt["real_cut"] <= pt["out_stopped"]) & (pt["real_cut"] < -pt["net"])).all()),
         f"переток не засчитан в реальное сокращение ({len(pt)} таких в списке)")
    rg = res["orgs"][last]["reorg"]
    found = {(int(a), int(b)) for a, b in zip(rg["inn_from"], rg["inn_to"])} if not rg.empty else set()
    # Реорганизация была до базового месяца или внутри года — ищем там, где она внутри окна.
    need(all((a, b) in found for a, b in exp["reorg_pairs"]), "реорганизации опознаны")
    # Перенос выплаты: организации найдены, двойная выплата видна, в сокращение не попали.
    shift = set(exp.get("pay_shift_inns", []))
    if shift and res.get("aug_raw"):
        hole = res["aug_raw"]["hole_cur"]
        found = set(hole["inn"].astype("int64")) if not hole.empty else set()
        need(shift <= found, f"перенос выплаты: найдено {len(shift & found)} из {len(shift)} организаций")
        pc, pp = res["aug_raw"]["pay_cur"], res["aug_raw"]["pay_prev"]
        dc = float(pc.loc[pc["grp"] == "gap", "share_double"].iloc[0])
        dp = float(pp.loc[pp["grp"] == "gap", "share_double"].iloc[0])
        need(dc - dp > 0.2, f"двойная выплата у вернувшихся: {dc:.0%} против {dp:.0%} год назад")
        sh = lst[lst["inn"].isin(shift)] if not lst.empty else lst
        need(sh.empty or bool(((sh["out_back"] >= 0.5 * sh["base_fl"])
                               & (sh["real_cut"] <= sh["out_stopped"])).all()),
             f"перенос выплаты не засчитан в сокращение ({len(sh)} в списке за счёт фона)")
    for w in ok:
        print(f"  ✓ {w}", flush=True)
    for w in errs:
        print(f"  ✗ {w}", flush=True)
    if errs:
        raise AssertionError(f"разбор не нашёл заложенное: {len(errs)} из {len(ok) + len(errs)}")
    print(f"Сверка с синтетикой: {len(ok)} из {len(ok)}", flush=True)

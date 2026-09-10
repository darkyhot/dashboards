"""Шаг 2: расчёты. Ни одной строки SQL — только то, что приехало из БД.

Модуль отвечает на три вопроса и ни на один больше: СКОЛЬКО потеряно на самом
деле, КОГДА это произошло и ГДЕ. Всё остальное — вспомогательные проверки, без
которых ответы нельзя предъявлять.

Главное правило здесь одно: **раскладка обязана складываться в целое.** Если
части падения не дают падения, читатель не может проверить ни одну цифру, и
отчёт превращается в набор правдоподобных утверждений. Поэтому сходимость
проверяется в коде (`check_additive`), а не глазами.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from uzp_dash import progress
from uzp_dash.agency import UNKNOWN as AG_UNKNOWN
from uzp_dash.agency import classify_frame as classify_agency

from . import level as LV

# Человекочитаемые имена веток и их смысл. Порядок — от «настоящего оттока» к
# «методологии»: именно так их и надо читать, и именно в таком порядке они
# показываются в отчёте.
# Человекочитаемые имена веток и их смысл.
#
# В описаниях НЕТ слова «ИНН»: готовый документ прогоняется через `sanitize`,
# который меняет это слово на «Орг.», и фраза «по ИНН нет зачислений»
# превратилась бы в «по Орг. нет зачислений». Пишем «номер организации».
#
# Порядок — от настоящей потери к методологии: именно так их и надо читать, и
# именно в таком порядке они показываются в отчёте.
CAUSES: dict[str, tuple[str, str]] = {
    "person_left_bank": ("Человек ушёл из банка",
                         "Ни одного зачисления в банке в отчётном месяце. "
                         "Единственная ветка, где потерян сам человек."),
    "left_rgs":         ("Ушёл из бюджетной сферы",
                         "Зарплата в банке идёт, но организация уже не "
                         "бюджетная: сегмент потерял получателя, банк — нет."),
    "inn_gone":         ("Организация исчезла из ведомостей",
                         "По номеру организации нет ни одного зачисления: "
                         "зарплатный проект потерян целиком."),
    "liquidated":       ("Организация ликвидирована",
                         "По номеру организации в справочнике ЕПК не осталось "
                         "ни одной активной записи. Получатели потеряны, но "
                         "к конкуренту они не уходили — организации больше нет."),
    "below_threshold":  ("Сумма ниже порога",
                         "Тот же человек, та же организация, нужные коды — но "
                         "сумма за месяц не превысила порог. Человек на месте."),
    "code_out_of_list": ("Код зачисления вне списка",
                         "Тот же человек, та же организация, деньги идут — но "
                         "кодом, которого нет в списке. Человек на месте."),
    "multi_collapsed":  ("Схлопнулось совместительство",
                         "Человек получал в нескольких организациях, остался в "
                         "меньшем числе. Ни один человек не потерян."),
    "moved_within_rgs": ("Сменил бюджетную организацию",
                         "Перешёл в другую организацию сегмента. Для сегмента "
                         "потери нет — пара переехала."),
}

GAINS: dict[str, tuple[str, str]] = {
    "person_new_to_rgs": ("Новый в бюджетной сфере",
                          "В базовом месяце пар в сегменте не было."),
    "inn_new":           ("Новая организация сегмента",
                          "У ИНН в базовом месяце не было ни одной пары."),
    "multi_new":         ("Новое совместительство",
                          "Человек добавил организацию к уже имевшимся."),
    "moved_in":          ("Пришёл из другой организации",
                          "Человек был в сегменте, сменил организацию."),
}

# Ветки, где НЕ ПОТЕРЯН НИ ОДИН ПОЛУЧАТЕЛЬ: человек на месте, получает там же,
# а из метрики выпал. Их сумма — ответ на вопрос «сколько из падения настоящее».
#
# Ликвидации и исчезнувшего договора здесь нет намеренно: получателей там
# действительно не стало, хотя к конкуренту они и не уходили. Смешивать «человек
# на месте» с «организации больше нет» значило бы записать в методологию
# настоящую потерю клиентов.
NOT_A_LOSS = ("below_threshold", "code_out_of_list", "multi_collapsed",
              "moved_within_rgs")

# Чем заполняются незаполненные атрибуты организации. Это ЗАГЛУШКИ, а не
# названия: обезличивание документа обязано их знать, иначе проверка утечки
# примет заглушку за настоящее наименование и не даст записать файл. Ровно так и
# вышло на первом прогоне — поэтому список один на оба модуля.
FILL: dict[str, str] = {
    "holding_name":  "Холдинг не указан",
    "industry_name": "Отрасль не указана",
    "tb_short_name": "ТБ неизвестен",
    "region_name":   "Регион неизвестен",
}

# Насколько месяц должен выделяться, чтобы называться обрывом: изменение больше
# этого числа медианных абсолютных отклонений от медианы изменений. Два — мало
# (шум), пять — уже пропускает настоящие ступени.
STEP_MAD = 3.5


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    """Числовая колонка. Отсутствующая — не ошибка, а ноль нужной длины.

    Через `df.get()` отсутствующая колонка вернула бы СКАЛЯР nan, и падение
    случилось бы где-то дальше по коду (ловушка 1 правил платформы).
    """
    if col not in df:
        return pd.Series(0.0, index=df.index)
    return pd.to_numeric(df[col], errors="coerce").fillna(0.0)


# --------------------------------------------------------------------------- #
# Итоги и сходимость
# --------------------------------------------------------------------------- #
def totals(month_totals: pd.DataFrame, lost: pd.DataFrame,
           gained: pd.DataFrame) -> dict:
    """Опорные числа разбора и разложение падения на людей и совместительство.

    Метрика считается ПАРАМИ (человек, ИНН), поэтому её падение складывается из
    двух разных вещей: людей стало меньше и/или у людей стало меньше организаций.
    Смешивать их нельзя — это разные события с разными выводами, а различает их
    только вот эта арифметика.

    Раскладка построена так, чтобы части давали целое ТОЧНО, без остатка:
        Δпар = Δлюдей · k_база  +  людей_отч · Δk
    где k — среднее число организаций на человека.
    """
    mt = month_totals.sort_values("report_dt").reset_index(drop=True)
    base, cur = mt.iloc[0], mt.iloc[-1]

    p_base, p_cur = float(base["n_pairs"]), float(cur["n_pairs"])
    e_base, e_cur = float(base["n_epk"]), float(cur["n_epk"])
    k_base = p_base / e_base if e_base else 0.0
    k_cur = p_cur / e_cur if e_cur else 0.0

    d_pairs = p_cur - p_base
    by_people = (e_cur - e_base) * k_base
    by_multi = e_cur * (k_cur - k_base)

    res = {
        "base_month": pd.Timestamp(base["report_dt"]),
        "report_month": pd.Timestamp(cur["report_dt"]),
        "pairs_base": p_base, "pairs_cur": p_cur, "d_pairs": d_pairs,
        "epk_base": e_base, "epk_cur": e_cur, "d_epk": e_cur - e_base,
        "inn_base": float(base["n_inn"]), "inn_cur": float(cur["n_inn"]),
        "amt_base": float(base["amt"]), "amt_cur": float(cur["amt"]),
        "multi_base": k_base, "multi_cur": k_cur,
        "d_by_people": by_people, "d_by_multi": by_multi,
        "lost": float(_num(lost, "n_pairs").sum()),
        "gained": float(_num(gained, "n_pairs").sum()),
        "lost_epk": float(_num(lost, "n_epk").sum()),
        "gained_epk": float(_num(gained, "n_epk").sum()),
    }
    # Доля совместителей — то самое «7% стало 5%». Считается от ЛЮДЕЙ, а не от
    # пар: доля от пар — другое число, и сравнивать её с отчётностью нельзя.
    res["multi_share_base"] = (p_base - e_base) / e_base if e_base else 0.0
    res["multi_share_cur"] = (p_cur - e_cur) / e_cur if e_cur else 0.0

    not_loss = float(_num(lost[lost["cause"].isin(NOT_A_LOSS)], "n_pairs").sum()) \
        if "cause" in lost else 0.0
    res["lost_not_real"] = not_loss
    res["lost_real"] = res["lost"] - not_loss
    return res


def check_additive(t: dict, lost: pd.DataFrame, gained: pd.DataFrame) -> list[dict]:
    """Проверки сходимости. Не сошлось — читателю сообщается, а не замалчивается.

    Обе проверки тождественные: они обязаны выполняться на любых данных. Если
    хоть одна не выполнилась, где-то в SQL разъехались определения — например,
    рабочий набор посчитан по одним датам, а лестница по другим. Такую ошибку не
    видно по цифрам: они остаются правдоподобными.
    """
    checks: list[dict] = []

    got = t["pairs_base"] - t["lost"] + t["gained"]
    checks.append({
        "name": "Пары: база − потеряно + пришло = отчётный месяц",
        "left": round(got, 2), "right": round(t["pairs_cur"], 2),
        "ok": abs(got - t["pairs_cur"]) < 0.5,
    })

    parts = t["d_by_people"] + t["d_by_multi"]
    checks.append({
        "name": "Падение = вклад людей + вклад совместительства",
        "left": round(parts, 2), "right": round(t["d_pairs"], 2),
        "ok": abs(parts - t["d_pairs"]) < 0.5,
    })

    if "cause" in lost:
        known = set(lost["cause"]) - set(CAUSES)
        checks.append({
            "name": "Все ветки потерь имеют описание",
            "left": len(known), "right": 0, "ok": not known,
        })
    if "cause" in gained:
        known_g = set(gained["cause"]) - set(GAINS)
        checks.append({
            "name": "Все ветки прихода имеют описание",
            "left": len(known_g), "right": 0, "ok": not known_g,
        })

    for c in checks:
        if not c["ok"]:
            progress.warn(f"НЕ СОШЛОСЬ: {c['name']} — {c['left']} против {c['right']}")
    return checks


def causes_table(lost: pd.DataFrame, total_lost: float) -> pd.DataFrame:
    """Лестница причин с описаниями и долями, в осмысленном порядке."""
    if lost.empty:
        return pd.DataFrame()
    df = lost.copy()
    df["title"] = [CAUSES.get(c, (c, ""))[0] for c in df["cause"]]
    df["descr"] = [CAUSES.get(c, ("", ""))[1] for c in df["cause"]]
    df["share"] = _num(df, "n_pairs") / (total_lost or 1)
    df["is_real_loss"] = ~df["cause"].isin(NOT_A_LOSS)
    order = {c: i for i, c in enumerate(CAUSES)}
    df["_o"] = [order.get(c, 99) for c in df["cause"]]
    return df.sort_values("_o").drop(columns="_o").reset_index(drop=True)


def gains_table(gained: pd.DataFrame) -> pd.DataFrame:
    if gained.empty:
        return pd.DataFrame()
    df = gained.copy()
    df["title"] = [GAINS.get(c, (c, ""))[0] for c in df["cause"]]
    total = float(_num(df, "n_pairs").sum()) or 1.0
    df["share"] = _num(df, "n_pairs") / total
    order = {c: i for i, c in enumerate(GAINS)}
    df["_o"] = [order.get(c, 99) for c in df["cause"]]
    return df.sort_values("_o").drop(columns="_o").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Когда
# --------------------------------------------------------------------------- #
def trend(monthly: pd.DataFrame) -> pd.DataFrame:
    """Помесячный ряд с изменениями и коэффициентом совместительства."""
    if monthly.empty:
        return pd.DataFrame()
    df = monthly.sort_values("report_dt").reset_index(drop=True).copy()
    df["report_dt"] = pd.to_datetime(df["report_dt"])
    df["multi"] = _num(df, "n_pairs") / _num(df, "n_epk").replace(0, np.nan)
    df["d_pairs"] = _num(df, "n_pairs").diff()
    df["d_epk"] = _num(df, "n_epk").diff()
    df["d_pairs_pct"] = df["d_pairs"] / _num(df, "n_pairs").shift(1).replace(0, np.nan)
    return df


def steps(tr: pd.DataFrame, col: str = "d_pairs") -> pd.DataFrame:
    """Месяцы-обрывы: изменения, резко выпадающие из обычного шага ряда.

    Отклонение меряется в МЕДИАННЫХ АБСОЛЮТНЫХ ОТКЛОНЕНИЯХ, а не в стандартных:
    одна большая ступень так раздувает стандартное отклонение, что перестаёт
    выделяться сама. Медиана к выбросам нечувствительна — а искать здесь надо
    именно выбросы.

    Ответ на «когда» держится на этой функции: обрыв в одном месяце — событие
    (загрузка, переклассификация, смена кодировки), плавное снижение —
    текучесть. По двум точкам года эти два случая неразличимы, и весь спор
    про −300к ровно об этом.
    """
    if tr.empty or col not in tr or len(tr) < 4:
        return pd.DataFrame()
    d = pd.to_numeric(tr[col], errors="coerce").dropna()
    if d.empty:
        return pd.DataFrame()
    med = float(d.median())
    mad = float((d - med).abs().median())
    if mad <= 0:
        # Ряд с ОДНОЙ ступенью и ровным шагом в остальном даёт нулевой MAD:
        # больше половины изменений совпадают с медианой в точности. Ноль здесь
        # означает не «ступеней нет», а «ступень настолько одна, что медиане
        # нечего мерить» — и ранний выход пропустил бы ровно тот случай, ради
        # которого функция написана. Мера огрубляется до среднего отклонения.
        mad = float((d - med).abs().mean())
    if mad <= 0:
        return pd.DataFrame()       # ряд постоянен: ступеней действительно нет
    out = tr.loc[d.index].copy()
    out["score"] = (d - med).abs() / mad
    out = out[(out["score"] >= STEP_MAD) & (d < med)]
    return out.sort_values("score", ascending=False)


def survival_curve(surv: pd.DataFrame, pairs_base: float) -> pd.DataFrame:
    """Дожитие когорты базового месяца: доля и помесячная убыль."""
    if surv.empty:
        return pd.DataFrame()
    df = surv.sort_values("report_dt").reset_index(drop=True).copy()
    df["report_dt"] = pd.to_datetime(df["report_dt"])
    df["share"] = _num(df, "n_alive") / (pairs_base or 1)
    df["d_alive"] = _num(df, "n_alive").diff()
    return df


def load_health(monthly_all: pd.DataFrame) -> pd.DataFrame:
    """Полнота загрузки партиций по всему банку.

    Считается ДО того, как объяснять падение: месяц, где строк вдвое меньше
    обычного, загружен не полностью, и любой вывод по нему будет выводом про
    загрузку, а не про людей.
    """
    if monthly_all.empty:
        return pd.DataFrame()
    df = monthly_all.sort_values("report_dt").reset_index(drop=True).copy()
    df["report_dt"] = pd.to_datetime(df["report_dt"])
    med = float(_num(df, "n_rows").median()) or 1.0
    df["vs_median"] = _num(df, "n_rows") / med
    df["is_thin"] = df["vs_median"] < 0.5
    return df


# --------------------------------------------------------------------------- #
# Почему
# --------------------------------------------------------------------------- #
def threshold(sens: pd.DataFrame) -> pd.DataFrame:
    """Как выглядит падение при разных порогах получателя.

    Порог фиксирован (2500), а зарплаты индексируются — сам по себе он должен год
    к году ДОБАВЛЯТЬ получателей, а не убавлять. Если падение сохраняется при
    пороге 0, порог ни при чём; если исчезает — объяснение найдено. Проверяется в
    лоб, а не рассуждением.
    """
    if sens.empty or len(sens) < 2:
        return pd.DataFrame()
    df = sens.sort_values("report_dt").reset_index(drop=True)
    cols = [c for c in df.columns if c.startswith("t")]
    base, cur = df.iloc[0], df.iloc[-1]
    rows = []
    for c in cols:
        b, u = float(base[c]), float(cur[c])
        rows.append({"threshold": int(c[1:]), "base": b, "cur": u,
                     "delta": u - b, "delta_pct": (u - b) / b if b else np.nan})
    return pd.DataFrame(rows).sort_values("threshold").reset_index(drop=True)


def code_shift(code_mix: pd.DataFrame, base_month, report_month,
               codes_in: tuple[int, ...]) -> pd.DataFrame:
    """Какие коды зачисления потеряли людей, а какие набрали.

    Считается по ВСЕМ кодам, включая те, что вне списка: исчезнувший из метрики
    код обычно не исчезает из витрины, он переезжает в соседний. Увидеть это можно
    только глядя на оба сразу — а именно так и выглядит смена кодировки выплат,
    самый неочевидный из кандидатов на объяснение падения.
    """
    if code_mix.empty:
        return pd.DataFrame()
    df = code_mix.copy()
    df["report_dt"] = pd.to_datetime(df["report_dt"])
    b = pd.Timestamp(base_month)
    c = pd.Timestamp(report_month)
    sub = df[df["report_dt"].isin([b, c])]
    if sub.empty:
        return pd.DataFrame()
    piv = sub.pivot_table(index=["code", "code_name"], columns="report_dt",
                          values="n_epk", aggfunc="sum").fillna(0.0)
    piv = piv.rename(columns={b: "base", c: "cur"})
    for col in ("base", "cur"):
        if col not in piv:
            piv[col] = 0.0
    piv = piv.reset_index()
    piv["delta"] = piv["cur"] - piv["base"]
    piv["in_list"] = piv["code"].isin(codes_in)
    return piv.sort_values("delta").reset_index(drop=True)


def migration(mig: pd.DataFrame, lost_by_inn: pd.DataFrame,
              attrs: pd.DataFrame, min_share: float = 0.3) -> pd.DataFrame:
    """Реорганизации: ИНН, чьи люди дружно оказались в одном новом ИНН.

    Это единственная проверка, отличающая переоформление организации от оттока.
    Порог по ДОЛЕ, а не по числу людей: двадцать человек из двадцати — это
    переоформление, двадцать из двух тысяч — обычная текучесть, и одним числом
    их не разделить.
    """
    if mig.empty:
        return pd.DataFrame()
    lost_tot = (lost_by_inn.groupby("inn")["n_pairs"].sum()
                if not lost_by_inn.empty else pd.Series(dtype=float))
    df = mig.copy()
    df["lost_from"] = df["inn_from"].map(lost_tot).fillna(0.0)
    df["share"] = _num(df, "n_epk") / df["lost_from"].replace(0, np.nan)
    df = df[df["share"] >= min_share]
    if df.empty:
        return df
    if not attrs.empty and "inn" in attrs:
        names = attrs.set_index("inn")["company_name"]
        df["name_from"] = df["inn_from"].map(names)
        df["name_to"] = df["inn_to"].map(names)
        # ИНН-приёмник часто ещё не размечен как бюджетный — имени у него нет, и
        # это не ошибка, а сам признак свежего переоформления
        df["to_in_segment"] = df["inn_to"].isin(set(attrs["inn"]))
    return df.sort_values("n_epk", ascending=False).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Где
# --------------------------------------------------------------------------- #
def enrich(lost_by_inn: pd.DataFrame, attrs: pd.DataFrame,
           gosb: pd.DataFrame, gmap: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Разметить потери атрибутами организации и территории.

    Ведомство и уровень подчинения выводятся ИЗ ИМЕНИ: отдельных полей для них
    нет ни в одном разрешённом источнике. Доля неразобранных имён считается и
    показывается — приписать их к массовой группе значило бы завысить её потерю
    на неизвестную величину, а весь разбор затевался ровно затем, чтобы такие
    приписки убрать.
    """
    meta: dict = {}
    if lost_by_inn.empty:
        return lost_by_inn, meta

    df = lost_by_inn.copy()
    if not attrs.empty:
        df = df.merge(attrs, on="inn", how="left")

    df["agency"] = classify_agency(df, name_col="company_name",
                                   force_col="is_military",
                                   industry_col="industry_name")
    df["level"] = LV.classify_frame(df, name_col="company_name")

    n_named = int(df["company_name"].notna().sum()) if "company_name" in df else 0
    meta["named_share"] = n_named / max(len(df), 1)
    meta["agency_unknown"] = float(
        _num(df[df["agency"] == AG_UNKNOWN], "n_pairs").sum())
    meta["level_unknown"] = float(
        _num(df[df["level"] == LV.UNKNOWN], "n_pairs").sum())

    # Территория. ГОСБ в ведомостях записан СТАРЫМ кодом, а справочник территории
    # ведётся по новым: без пересчёта часть организаций осталась бы без региона,
    # и разрез показал бы не «где утекло», а «где справочник совпал».
    if not gmap.empty and "old_gosb_id" in gmap:
        m = gmap.drop_duplicates("old_gosb_id").set_index("old_gosb_id")
        df["new_gosb_id"] = df["gosb_id"].map(m["new_gosb_id"])
    else:
        df["new_gosb_id"] = df["gosb_id"]
    if not gosb.empty:
        df = df.merge(gosb, on="new_gosb_id", how="left")

    for col, fill in FILL.items():
        df[col] = df[col].fillna(fill) if col in df else fill

    meta["n_inn"] = int(df["inn"].nunique())
    progress.done(f"разметка потерь: {meta['n_inn']:,} организаций, имя известно у "
                  f"{meta['named_share']:.0%}; без ведомства "
                  f"{meta['agency_unknown']:,.0f} пар, без уровня "
                  f"{meta['level_unknown']:,.0f}")
    return df, meta


def by_dim(df: pd.DataFrame, dim: str, top_n: int = 12) -> pd.DataFrame:
    """Потери в разрезе, с составом причин внутри каждой строки.

    Состав причин здесь не украшение. «Утекло образование» без него остаётся
    утверждением без содержания: неизвестно, ушли ли люди, закрылись ли школы или
    у них сменился код зачисления — а это три разных вывода и три разных решения.

    Строк возвращается top_n, но `total` считается по ВСЕМ: доля обязана быть
    долей от целого, а не от показанного куска.
    """
    if df.empty or dim not in df:
        return pd.DataFrame()
    total = float(_num(df, "n_pairs").sum()) or 1.0
    g = df.groupby(dim, dropna=False)
    out = pd.DataFrame({
        "n_pairs": g["n_pairs"].sum(),
        "n_inn": g["inn"].nunique(),
    })
    for cause in CAUSES:
        sub = df[df["cause"] == cause].groupby(dim)["n_pairs"].sum()
        out[cause] = sub.reindex(out.index).fillna(0.0)
    out["real_loss"] = out[[c for c in CAUSES if c not in NOT_A_LOSS]].sum(axis=1)
    out["not_real"] = out[list(NOT_A_LOSS)].sum(axis=1)
    out["share"] = out["n_pairs"] / total
    out = out.sort_values("n_pairs", ascending=False)
    return out.head(top_n).reset_index()


def top_orgs(df: pd.DataFrame, top_n: int = 15) -> pd.DataFrame:
    """Организации с наибольшей потерей — с преобладающей причиной."""
    if df.empty:
        return pd.DataFrame()
    g = df.groupby("inn", dropna=False)
    out = pd.DataFrame({"n_pairs": g["n_pairs"].sum()})
    name = (df.drop_duplicates("inn").set_index("inn")
            if "company_name" in df else None)
    for col in ("company_name", "holding_name", "tb_short_name", "region_name",
                "agency", "level"):
        out[col] = name[col].reindex(out.index) if name is not None and col in name \
            else None
    top_cause = (df.sort_values("n_pairs", ascending=False)
                 .drop_duplicates("inn").set_index("inn")["cause"])
    out["cause"] = top_cause.reindex(out.index)
    out["cause_title"] = [CAUSES.get(c, (c, ""))[0] for c in out["cause"]]
    return out.sort_values("n_pairs", ascending=False).head(top_n).reset_index()

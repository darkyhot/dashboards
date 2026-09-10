"""Шаг 2: расчёты. Ни одной строки SQL — только то, что приехало из БД.

Модуль отвечает на четыре вопроса и ни на один больше: СКОЛЬКО потеряно на самом
деле, ВЫРОСЛИ ЛИ мы без методологии счёта, КОГДА это произошло и ГДЕ. Всё
остальное — вспомогательные проверки, без которых ответы нельзя предъявлять.

Два правила, на которых всё держится:

1. **Раскладка обязана складываться в целое.** Если части падения не дают
   падения, читатель не может проверить ни одну цифру, и отчёт превращается в
   набор правдоподобных утверждений. Сходимость проверяется в коде
   (`check_additive`), а не глазами.
2. **Приход раскладывается так же, как потери.** Вычитать из ПОЛНОГО прихода
   только НАСТОЯЩИЕ потери — арифметика, не значащая ничего: она завышает рост
   ровно на ту величину, которую мы вычитаем со стороны потерь. Именно поэтому у
   каждой ветки прихода есть свой вид.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from uzp_dash import progress
from uzp_dash.agency import UNKNOWN as AG_UNKNOWN
from uzp_dash.agency import classify_frame as classify_agency

from . import level as LV

# Вид ветки. ДВА, а не три, и вопрос к каждой ветке один: остался ли человек
# получателем зарплаты в сегменте — по определению заказчика, то есть по
# зарплатным кодам и выше порога?
#
#   LOSS   — не остался. Получателя не стало, и неважно, ушёл он из банка, ушёл в
#            другой сегмент или перестал зачислять по зарплатным кодам. Ветка
#            говорит лишь КУДА он делся.
#   INSIDE — остался, но посчитан иначе: перешёл в другую бюджетную организацию,
#            в другое подразделение или сократил число мест работы. Для сегмента
#            это движение внутри, а не убыль.
#
# Раньше веток было три, и в среднюю — «особенности счёта» — попадали порог и
# коды вне списка. Это было неверно: порог и список кодов заданы заказчиком как
# ОПРЕДЕЛЕНИЕ получателя, а не как погрешность измерения. Человек, переставший
# зачислять по зарплатному коду, для зарплатного подразделения пропал, и прятать
# его в «методологию» значило занижать потерю.
LOSS, INSIDE = "loss", "inside"

# Подписи зависят от стороны: одна и та же «остался в сегменте» на потерях читается
# как «не потеряли», а на приходе — как «не привели нового».
KIND_TAG = {
    ("lost", LOSS):     "потеря",
    ("lost", INSIDE):   "остался в сегменте",
    ("gained", LOSS):   "новый приход",
    ("gained", INSIDE): "перешёл внутри сегмента",
}

# --------------------------------------------------------------------------- #
# Ветки лестниц: получатели (тройки человек-организация-подразделение)
# --------------------------------------------------------------------------- #
# В описаниях НЕТ слова «ИНН»: готовый документ прогоняется через `sanitize`,
# который меняет это слово на «Орг.», и фраза «по ИНН нет зачислений»
# превратилась бы в «по Орг. нет зачислений». Пишем «номер организации».
CAUSES: dict[str, tuple[str, str, str]] = {
    "left_bank": (
        "Ушёл из банка совсем",
        "Ни одного зачисления в банке — ни зарплатного, ни любого другого.", LOSS),
    "other_codes": (
        "Получает не по зарплатным кодам",
        "Деньги от бюджетной организации идут, но кодами вне зарплатного списка. "
        "Для зарплатного подразделения получателя нет; чем именно заменились "
        "зачисления — в разделе «Куда делись люди».", LOSS),
    "left_segment": (
        "Ушёл в другой сегмент",
        "Зарплата в банке идёт, но организация уже не бюджетная.", LOSS),
    "below_threshold": (
        "Зарплата ниже порога",
        "Зарплатные зачисления от бюджетной организации есть, но за месяц не "
        "превысили порог. По определению получателя это не получатель.", LOSS),
    "stayed_in_segment": (
        "Остался в бюджетном сегменте",
        "Получателем быть не перестал: сменил организацию, подразделение или "
        "число мест работы. Сегмент никого не потерял, и чем именно этот переход "
        "был, для сегмента неважно.", INSIDE),
}

GAINS: dict[str, tuple[str, str, str]] = {
    "new_to_bank": (
        "Новый в банке совсем",
        "В базовом месяце его не было в ведомостях банка вовсе.", LOSS),
    "back_to_codes": (
        "Начал получать по зарплатным кодам",
        "Деньги от бюджетной организации шли и раньше, но кодами вне списка.", LOSS),
    "from_segment": (
        "Пришёл из другого сегмента",
        "В банке был, зарплату получал в небюджетной организации.", LOSS),
    "above_threshold": (
        "Зарплата поднялась выше порога",
        "Зарплатные зачисления шли и раньше, но за месяц не дотягивали до порога.",
        LOSS),
    "stayed_in_segment": (
        "Был в сегменте и раньше",
        "Получателем бюджетного сегмента он уже был: сменил организацию, "
        "подразделение или число мест работы. Нового получателя сегмент не "
        "привёл.", INSIDE),
}

# --------------------------------------------------------------------------- #
# Ветки лестниц: ЛЮДИ
# --------------------------------------------------------------------------- #
# Здесь ровно те ТРИ СИТУАЦИИ, которые различает зарплатное подразделение, плюс
# уход в другой сегмент:
#   есть зарплатные зачисления, но мало  -> ниже порога
#   есть зачисления, но не зарплатные    -> другие коды
#   нет зачислений вовсе                 -> ушёл из банка
#   зарплата есть, организация не наша   -> другой сегмент
#
# Ветки «остался в сегменте» здесь нет по построению: человек, который остался
# получателем, не потерян вовсе и в лестницу не попадает. Разница между двумя
# лестницами ровно на эту ветку и есть цена того, что метрика считает не людей.
EPK_CAUSES: dict[str, tuple[str, str, str]] = {
    "left_bank":       CAUSES["left_bank"],
    "other_codes":     CAUSES["other_codes"],
    "left_segment":    CAUSES["left_segment"],
    "below_threshold": CAUSES["below_threshold"],
}

EPK_GAINS: dict[str, tuple[str, str, str]] = {
    "new_to_bank":     GAINS["new_to_bank"],
    "back_to_codes":   GAINS["back_to_codes"],
    "from_segment":    GAINS["from_segment"],
    "above_threshold": GAINS["above_threshold"],
}

# Чем заполняются незаполненные атрибуты организации. Это ЗАГЛУШКИ, а не
# названия: обезличивание документа обязано их знать — иначе оно выдаст заглушке
# токен, и «ТБ неизвестен» прочитается как настоящее подразделение. Ровно так и
# вышло на первом прогоне, поэтому список один на оба модуля.
FILL: dict[str, str] = {
    "holding_name":  "Холдинг не указан",
    "industry_name": "Отрасль не указана",
    "tb_short_name": "ТБ неизвестен",
    "region_name":   "Регион неизвестен",
}

# Насколько месяц должен выделяться, чтобы называться обрывом: в медианных
# абсолютных отклонениях. Два — мало (шум), пять — пропускает настоящие ступени.
STEP_MAD = 3.5

# Столько месяцев нужно, чтобы мерить год к году внутри ряда. Меньше — сезонность
# отделить не от чего, и детектор честно переходит на сравнение с соседом.
MIN_MONTHS_FOR_YOY = 15


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    """Числовая колонка. Отсутствующая — не ошибка, а ноль нужной длины.

    Через `df.get()` отсутствующая колонка вернула бы СКАЛЯР nan, и падение
    случилось бы где-то дальше по коду (ловушка 1 правил платформы).
    """
    if col not in df:
        return pd.Series(0.0, index=df.index)
    return pd.to_numeric(df[col], errors="coerce").fillna(0.0)


def _key(s: pd.Series) -> pd.Series:
    """Ключ соединения — к ОДНОМУ числовому типу с обеих сторон.

    Идентификаторы приезжают из разных витрин разными типами: драйвер отдаёт
    `smallint` то как int, то как Decimal, а колонка с единственным NULL
    становится object целиком. Дальше происходит одно из двух, и второе хуже
    первого:

    * `merge` падает с «You are trying to merge on object and int64 columns» —
      шумно, сразу, после одиннадцати минут прогона;
    * `map` и `reindex` НЕ падают, а молча возвращают NaN. Разрез при этом не
      исчезает, а схлопывается в одну строку-заглушку и выглядит как свойство
      данных. Ровно так на проме и пропала вся территория.

    Поэтому каждый идентификатор перед соединением проходит здесь, а не там, где
    его тип случайно совпал. `Int64` (с большой буквы) — нullable, поэтому
    пропуски переживают приведение и не превращаются в мусорные нули.
    """
    return pd.to_numeric(s, errors="coerce").astype("Int64")


def _kinds(df: pd.DataFrame, book: dict, value_col: str) -> dict:
    """Сумма по видам веток: потеря/приход и движение внутри сегмента."""
    out = {LOSS: 0.0, INSIDE: 0.0}
    if df.empty or "cause" not in df:
        return out
    for cause, val in zip(df["cause"], _num(df, value_col)):
        kind = book.get(cause, ("", "", LOSS))[2]
        out[kind] = out.get(kind, 0.0) + float(val)
    return out


# --------------------------------------------------------------------------- #
# Итоги и сходимость
# --------------------------------------------------------------------------- #
def totals(month_totals: pd.DataFrame, lost: pd.DataFrame, gained: pd.DataFrame,
           lost_epk: pd.DataFrame, gained_epk: pd.DataFrame,
           base_month, report_month) -> dict:
    """Опорные числа разбора: обе лестницы и разложение падения.

    Метрика считается ТРОЙКАМИ (человек, организация, подразделение), поэтому её
    падение складывается из двух разных вещей: людей стало меньше и/или у людей
    стало меньше организаций и подразделений. Смешивать их нельзя — это разные
    события с разными выводами, а различает их только вот эта арифметика.

    Раскладка построена так, чтобы части давали целое ТОЧНО, без остатка:
        Δполучателей = Δлюдей · k_база  +  людей_отч · Δk
    где k — среднее число получателей на человека.
    """
    mt = month_totals.copy()
    mt["report_dt"] = pd.to_datetime(mt["report_dt"])
    base = mt[mt["report_dt"] == pd.Timestamp(base_month)]
    cur = mt[mt["report_dt"] == pd.Timestamp(report_month)]
    if base.empty or cur.empty:
        raise RuntimeError(
            f"в рабочем наборе нет одного из опорных месяцев "
            f"({pd.Timestamp(base_month):%m.%Y} или "
            f"{pd.Timestamp(report_month):%m.%Y}) — сравнивать не с чем")
    base, cur = base.iloc[0], cur.iloc[0]

    p_base, p_cur = float(base["n_triples"]), float(cur["n_triples"])
    e_base, e_cur = float(base["n_epk"]), float(cur["n_epk"])
    k_base = p_base / e_base if e_base else 0.0
    k_cur = p_cur / e_cur if e_cur else 0.0

    res = {
        "base_month": pd.Timestamp(base["report_dt"]),
        "report_month": pd.Timestamp(cur["report_dt"]),
        "triples_base": p_base, "triples_cur": p_cur, "d_triples": p_cur - p_base,
        "epk_base": e_base, "epk_cur": e_cur, "d_epk": e_cur - e_base,
        "inn_base": float(base["n_inn"]), "inn_cur": float(cur["n_inn"]),
        "amt_base": float(base["amt"]), "amt_cur": float(cur["amt"]),
        "multi_base": k_base, "multi_cur": k_cur,
        "d_by_people": (e_cur - e_base) * k_base,
        "d_by_multi": e_cur * (k_cur - k_base),
        "lost": float(_num(lost, "n_triples").sum()),
        "gained": float(_num(gained, "n_triples").sum()),
        "lost_epk": float(_num(lost_epk, "n_epk").sum()),
        "gained_epk": float(_num(gained_epk, "n_epk").sum()),
    }
    # Доля совместителей — то самое «7% стало 5%». Считается от ЛЮДЕЙ, а не от
    # получателей: доля от получателей — другое число, с отчётностью его не сверить.
    res["multi_share_base"] = (p_base - e_base) / e_base if e_base else 0.0
    res["multi_share_cur"] = (p_cur - e_cur) / e_cur if e_cur else 0.0

    # Раскладка ОБЕИХ сторон по видам веток — то, ради чего затевался этап.
    lk = _kinds(lost, CAUSES, "n_triples")
    gk = _kinds(gained, GAINS, "n_triples")
    res["lost_kinds"], res["gained_kinds"] = lk, gk
    # РЕАЛЬНЫЕ потери и РЕАЛЬНЫЙ приход — то, что просили показать. Всё, что не
    # реально, — это переходы внутри сегмента: получатель остался, изменилось
    # только то, каким числом строк он посчитан.
    res["real_lost"], res["real_gained"] = lk[LOSS], gk[LOSS]
    res["inside_lost"], res["inside_gained"] = lk[INSIDE], gk[INSIDE]
    res["net_real"] = gk[LOSS] - lk[LOSS]
    res["net_inside"] = gk[INSIDE] - lk[INSIDE]

    lke = _kinds(lost_epk, EPK_CAUSES, "n_epk")
    gke = _kinds(gained_epk, EPK_GAINS, "n_epk")
    res["lost_kinds_epk"], res["gained_kinds_epk"] = lke, gke
    # На уровне человека движений внутри сегмента нет по построению, поэтому
    # реальная потеря людей равна всей потере людей.
    res["real_lost_epk"], res["real_gained_epk"] = lke[LOSS], gke[LOSS]
    res["net_real_epk"] = gke[LOSS] - lke[LOSS]
    return res


def check_additive(t: dict, lost: pd.DataFrame, gained: pd.DataFrame,
                   lost_epk: pd.DataFrame, gained_epk: pd.DataFrame) -> list[dict]:
    """Проверки сходимости. Не сошлось — читателю сообщается, а не замалчивается.

    Проверки тождественные: они обязаны выполняться на любых данных. Если хоть
    одна не выполнилась, где-то в SQL разъехались определения — например, рабочий
    набор посчитан по одним датам, а лестница по другим. Такую ошибку не видно по
    цифрам: они остаются правдоподобными.
    """
    checks: list[dict] = []

    def _add(name, left, right, tol=0.5):
        checks.append({"name": name, "left": round(float(left), 2),
                       "right": round(float(right), 2),
                       "ok": abs(float(left) - float(right)) < tol})

    _add("Получатели: база − потеряно + пришло = отчётный месяц",
         t["triples_base"] - t["lost"] + t["gained"], t["triples_cur"])
    _add("Люди: база − потеряно + пришло = отчётный месяц",
         t["epk_base"] - t["lost_epk"] + t["gained_epk"], t["epk_cur"])
    _add("Падение получателей = вклад людей + вклад совместительства",
         t["d_by_people"] + t["d_by_multi"], t["d_triples"])
    _add("Изменение получателей = реальное движение + переходы внутри сегмента",
         t["net_real"] + t["net_inside"], t["d_triples"])
    _add("Изменение людей = реальное движение (внутри сегмента людей не теряют)",
         t["net_real_epk"], t["d_epk"])

    for df, book, what in ((lost, CAUSES, "потерь"), (gained, GAINS, "прихода"),
                           (lost_epk, EPK_CAUSES, "потерь по людям"),
                           (gained_epk, EPK_GAINS, "прихода по людям")):
        if "cause" in df:
            unknown = set(df["cause"]) - set(book)
            checks.append({"name": f"Все ветки {what} имеют описание",
                           "left": len(unknown), "right": 0, "ok": not unknown})

    for c in checks:
        if not c["ok"]:
            progress.warn(f"НЕ СОШЛОСЬ: {c['name']} — {c['left']} против {c['right']}")
    return checks


def ladder(df: pd.DataFrame, book: dict, value_col: str) -> pd.DataFrame:
    """Лестница с описаниями, видами и долями, в осмысленном порядке."""
    if df.empty or "cause" not in df:
        return pd.DataFrame()
    out = df.copy()
    out["title"] = [book.get(c, (c, "", LOSS))[0] for c in out["cause"]]
    out["descr"] = [book.get(c, ("", "", LOSS))[1] for c in out["cause"]]
    out["kind"] = [book.get(c, ("", "", LOSS))[2] for c in out["cause"]]
    total = float(_num(out, value_col).sum()) or 1.0
    out["share"] = _num(out, value_col) / total
    order = {c: i for i, c in enumerate(book)}
    out["_o"] = [order.get(c, 99) for c in out["cause"]]
    return out.sort_values("_o").drop(columns="_o").reset_index(drop=True)


def side_by_side(lost: pd.DataFrame, lost_epk: pd.DataFrame) -> pd.DataFrame:
    """Две лестницы потерь рядом: по получателям и по людям.

    Смысл таблицы в РАЗНИЦЕ между колонками. Ветки, которых на уровне человека
    нет вовсе, помечаются отдельно — это и есть та часть падения, где не потерян
    никто, и увидеть её иначе нельзя.
    """
    if lost.empty:
        return pd.DataFrame()
    epk = (lost_epk.set_index("cause")["n_epk"].to_dict()
           if not lost_epk.empty and "cause" in lost_epk else {})
    rows = []
    for r in lost.itertuples():
        rows.append({
            "cause": r.cause, "title": r.title, "kind": r.kind,
            "n_triples": float(r.n_triples),
            "n_epk": float(epk[r.cause]) if r.cause in epk else np.nan,
            "epk_applies": r.cause in EPK_CAUSES,
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Когда
# --------------------------------------------------------------------------- #
def trend(monthly: pd.DataFrame) -> pd.DataFrame:
    """Помесячный ряд с изменениями, коэффициентом совместительства и год-к-году.

    Колонка `yoy` — изменение к ТОМУ ЖЕ месяцу год назад. Она и есть главный
    инструмент против сезонности: ряд, где январь проваливается каждый год, по
    соседним месяцам читается как два обрыва, а по год-к-году — как ровная линия,
    на которой видно настоящее изменение.
    """
    if monthly.empty:
        return pd.DataFrame()
    df = monthly.sort_values("report_dt").reset_index(drop=True).copy()
    df["report_dt"] = pd.to_datetime(df["report_dt"])
    df["multi"] = _num(df, "n_triples") / _num(df, "n_epk").replace(0, np.nan)
    df["d_triples"] = _num(df, "n_triples").diff()
    df["d_triples_pct"] = (df["d_triples"]
                           / _num(df, "n_triples").shift(1).replace(0, np.nan))
    df["yoy"] = _num(df, "n_triples").diff(12)
    df["yoy_epk"] = _num(df, "n_epk").diff(12)
    df["yoy_pct"] = df["yoy"] / _num(df, "n_triples").shift(12).replace(0, np.nan)
    return df


def steps(tr: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """Месяцы-обрывы. Возвращает (кадр, чем мерили).

    Меряется по ГОД-К-ГОДУ, а не по соседнему месяцу. Причина конкретная: в этом
    ряду январь проваливается на два миллиона КАЖДЫЙ год, и сравнение с соседом
    честно объявляет обрывом оба января — то есть выдаёт регулярный сезон за
    событие. Ровно эта ошибка попала в первый прогон. При сравнении год к году
    сезон вычитается сам, и остаётся то, что случилось один раз.

    Отклонение меряется в МЕДИАННЫХ абсолютных отклонениях, а не в стандартных:
    одна большая ступень так раздувает стандартное отклонение, что перестаёт
    выделяться сама.

    Истории на год не хватило — детектор переходит на сравнение с соседним
    месяцем и ГОВОРИТ об этом: сезонность в таком режиме не отделена.
    """
    if tr.empty or len(tr) < 4:
        return pd.DataFrame(), "ряда нет"

    col, how = "yoy", "год к году"
    if (len(tr) < MIN_MONTHS_FOR_YOY or col not in tr
            or int(tr[col].notna().sum()) < 3):
        col, how = "d_triples", "к предыдущему месяцу (сезонность не отделена)"

    d = pd.to_numeric(tr[col], errors="coerce").dropna()
    if d.empty:
        return pd.DataFrame(), how
    med = float(d.median())
    mad = float((d - med).abs().median())
    if mad <= 0:
        # Ряд с ОДНОЙ ступенью и ровным шагом в остальном даёт нулевой MAD:
        # больше половины изменений совпадают с медианой в точности. Ноль здесь
        # означает не «ступеней нет», а «ступень настолько одна, что медиане
        # нечего мерить», и ранний выход пропустил бы ровно тот случай, ради
        # которого функция написана.
        mad = float((d - med).abs().mean())
    if mad <= 0:
        return pd.DataFrame(), how
    out = tr.loc[d.index].copy()
    out["score"] = (d - med).abs() / mad
    out["measured"] = how
    out["delta"] = d
    out = out[(out["score"] >= STEP_MAD) & (d < med)]
    return out.sort_values("score", ascending=False), how


def month_compare(tr: pd.DataFrame, report_month, n_prev: int = 2) -> pd.DataFrame:
    """Отчётный месяц против предыдущих и против того же месяца год назад.

    Без этой таблицы вывод о годовой динамике строится по ОДНОЙ точке. Если
    отчётный месяц — сезонная яма, падение год к году может целиком повторять
    обычный летний провал, и отличить одно от другого можно, только положив
    рядом соседние месяцы.
    """
    if tr.empty:
        return pd.DataFrame()
    cur = pd.Timestamp(report_month)
    want = [pd.Timestamp(cur - pd.DateOffset(months=k)).to_period("M").to_timestamp("M")
            for k in list(range(n_prev, -1, -1)) + [12]]
    sub = tr[tr["report_dt"].isin(want)].copy()
    ref = tr[tr["report_dt"] == cur]
    if sub.empty or ref.empty:
        return pd.DataFrame()
    ref = ref.iloc[0]
    sub["vs_report"] = float(ref["n_triples"]) - _num(sub, "n_triples")
    sub["vs_report_epk"] = float(ref["n_epk"]) - _num(sub, "n_epk")
    sub["is_report"] = sub["report_dt"] == cur
    return sub.sort_values("report_dt").reset_index(drop=True)


def survival_curve(surv: pd.DataFrame, base_value: float) -> pd.DataFrame:
    """Дожитие когорты базового месяца: доля и помесячная убыль."""
    if surv.empty:
        return pd.DataFrame()
    df = surv.sort_values("report_dt").reset_index(drop=True).copy()
    df["report_dt"] = pd.to_datetime(df["report_dt"])
    df["share"] = _num(df, "n_alive") / (base_value or 1)
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
def threshold(sens: pd.DataFrame, base_month, report_month) -> pd.DataFrame:
    """Как выглядит падение при разных порогах получателя.

    Порог фиксирован, а зарплаты индексируются — сам по себе он должен год к году
    ДОБАВЛЯТЬ получателей, а не убавлять. Если падение сохраняется при пороге 0,
    порог ни при чём; если исчезает — объяснение найдено.
    """
    if sens.empty or len(sens) < 2:
        return pd.DataFrame()
    df = sens.copy()
    df["report_dt"] = pd.to_datetime(df["report_dt"])
    b = df[df["report_dt"] == pd.Timestamp(base_month)]
    c = df[df["report_dt"] == pd.Timestamp(report_month)]
    if b.empty or c.empty:
        return pd.DataFrame()
    b, c = b.iloc[0], c.iloc[0]
    rows = []
    for col in [x for x in df.columns if x.startswith("t") and x[1:].isdigit()]:
        lo, hi = float(b[col]), float(c[col])
        rows.append({"threshold": int(col[1:]), "base": lo, "cur": hi,
                     "delta": hi - lo,
                     "delta_pct": (hi - lo) / lo if lo else np.nan})
    return pd.DataFrame(rows).sort_values("threshold").reset_index(drop=True)


def seasonality(df: pd.DataFrame, report_month, prev_month) -> dict:
    """Сезонность, подтверждённая ПОВТОРОМ год к году.

    Определение узкое нарочно. «Получал в июле, не получает в августе» — это ещё
    не сезон, а просто пропажа: доказать, что человек вернётся, нечем, следующего
    месяца в данных нет. Сезонным поведение становится, когда ПОВТОРЯЕТСЯ:
    человек получал в предыдущем месяце ОБОИХ лет и не получал в отчётном
    ОБОИХ лет.

    Такие люди в сравнении год к году не участвуют вовсе — их нет ни в базовом
    месяце, ни в отчётном. Но именно они объясняют, почему отчётный месяц ниже
    предыдущего, и без них этот провал читается как потеря.
    """
    if df is None or df.empty:
        return {}
    r = df.iloc[0]
    both = float(r.get("n_prev_both", 0) or 0)
    gone_cur = float(r.get("n_gone_cur", 0) or 0)
    seasonal = float(r.get("n_seasonal", 0) or 0)
    return {
        "prev_month": pd.Timestamp(prev_month), "report_month": pd.Timestamp(report_month),
        "n_prev_both": both,
        "n_gone_cur": gone_cur,
        "n_gone_base": float(r.get("n_gone_base", 0) or 0),
        "n_seasonal": seasonal,
        # Доля от тех, кто пропал в отчётном месяце: сколько из них пропадали и
        # год назад. Это и есть мера сезонности провала.
        "share_of_gone": seasonal / gone_cur if gone_cur else 0.0,
    }


def code_months(df: pd.DataFrame, base_month, prev_month, report_month,
                top_n: int = 20) -> pd.DataFrame:
    """Зарплатные коды: сколько людей получает каждый вид выплаты, и как он изменился.

    Отвечает на вопрос, который не виден ни в одной другой таблице: КАКОЙ ИМЕННО
    ВИД ВЫПЛАТЫ просел. Метрика складывается из восемнадцати кодов, и провал
    одного из них — стипендии в каникулы, премии в конце квартала — выглядит в
    итоге как падение численности, хотя ни один человек никуда не ушёл: у него
    просто в этом месяце нет выплаты этого вида.

    Считаются ТОЛЬКО зарплатные коды: остальные на метрику не влияют по
    построению, а в таблице занимали бы весь верх и сбивали бы вывод.

    Сортировка по МЕСЯЧНОМУ изменению, а не по объёму: вопрос, ради которого
    таблица написана, — что просело между соседними месяцами.
    """
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy()
    d["report_dt"] = pd.to_datetime(d["report_dt"])
    d["n_epk"] = _num(d, "n_epk")

    # Показывается НАЗВАНИЕ, а не код: код — внутренний идентификатор. Кодам без
    # внятной расшифровки (в витрине там встречается сам номер строкой) название
    # собирается из номера, иначе строка выглядела бы безымянной.
    def _name(row) -> str:
        nm = str(row["code_name"] or "").strip()
        return nm if nm and not nm.isdigit() else f"Код {int(row['code'])}"

    d["name"] = d.apply(_name, axis=1)
    piv = d.pivot_table(index="name", columns="report_dt", values="n_epk",
                        aggfunc="sum").fillna(0.0)
    b, pm, c = (pd.Timestamp(base_month), pd.Timestamp(prev_month),
                pd.Timestamp(report_month))
    for col in (b, pm, c):
        if col not in piv:
            piv[col] = 0.0
    out = pd.DataFrame({
        "name": piv.index,
        "base": piv[b].values, "prev": piv[pm].values, "cur": piv[c].values,
    })
    out["d_month"] = out["cur"] - out["prev"]
    out["d_year"] = out["cur"] - out["base"]
    out["d_month_pct"] = out["d_month"] / out["prev"].replace(0, np.nan)
    out["d_year_pct"] = out["d_year"] / out["base"].replace(0, np.nan)
    out = out.sort_values("d_month").reset_index(drop=True)
    return out.head(top_n) if top_n else out


def left_segment(df: pd.DataFrame, seg_big: str) -> pd.DataFrame:
    """В какой сегмент ушли те, кто ушёл из бюджетного.

    Строка «ушёл из сегмента» без адреса — половина ответа. Забрал ли человека
    коммерческий клиент, малый бизнес или он сменил бюджетную работу на
    небюджетную — разные истории с разными выводами.
    """
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out["n_epk"] = _num(out, "n_epk")
    total = float(out["n_epk"].sum()) or 1.0
    out["share"] = out["n_epk"] / total
    # Свой же сегмент в этом списке означал бы, что человек никуда не уходил, —
    # такого быть не может по построению ветки, и если он появился, это ошибка
    # разметки, а не данные.
    out = out[out["segment_name"] != seg_big]
    return out.sort_values("n_epk", ascending=False).reset_index(drop=True)


def left_codes(df: pd.DataFrame, top_n: int = 12) -> pd.DataFrame:
    """Чем заменились зарплатные зачисления у тех, кто перестал их получать.

    Это НЕ разбор кодов витрины — тот выброшен как вредный: его верх занимали
    массовые социальные коды, которых метрика не считала никогда, и по ним
    делался вывод про метрику. Здесь другое: детализация ОДНОЙ ситуации из трёх —
    «зачисления есть, но не по зарплатным кодам».

    Сама ситуация уже посчитана потерей, и таблица её не оправдывает, а
    объясняет: пенсия означает выход на пенсию, пособие на детей — декрет, расчёт
    при увольнении — увольнение. Решения по ним разные вплоть до противоположных.
    """
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out["n_epk"] = _num(out, "n_epk")
    total = float(out["n_epk"].sum()) or 1.0
    out["share"] = out["n_epk"] / total
    return out.sort_values("n_epk", ascending=False).head(top_n).reset_index(drop=True)


def migration(mig: pd.DataFrame, lost_by_inn: pd.DataFrame,
              attrs: pd.DataFrame, min_share: float = 0.3) -> pd.DataFrame:
    """Реорганизации: организации, чьи люди дружно оказались в одной новой.

    Единственная проверка, отличающая переоформление от оттока. Порог по ДОЛЕ, а
    не по числу людей: двадцать человек из двадцати — переоформление, двадцать из
    двух тысяч — обычная текучесть, и одним числом их не разделить.
    """
    if mig.empty:
        return pd.DataFrame()
    lost_tot = pd.Series(dtype=float)
    if not lost_by_inn.empty:
        lt = lost_by_inn.copy()
        lt["inn"] = _key(lt["inn"])
        lost_tot = lt.groupby("inn")["n_triples"].sum()
    df = mig.copy()
    df["inn_from"], df["inn_to"] = _key(df["inn_from"]), _key(df["inn_to"])
    df["lost_from"] = df["inn_from"].map(lost_tot).fillna(0.0)
    df["share"] = _num(df, "n_epk") / df["lost_from"].replace(0, np.nan)
    df = df[df["share"] >= min_share]
    if df.empty:
        return df
    if not attrs.empty and "inn" in attrs:
        at = attrs.copy()
        at["inn"] = _key(at["inn"])
        names = at.drop_duplicates("inn").set_index("inn")["company_name"]
        df["name_from"] = df["inn_from"].map(names)
        df["name_to"] = df["inn_to"].map(names)
        # Организация-приёмник часто ещё не размечена как бюджетная — имени у неё
        # нет, и это не пробел, а сам признак свежего переоформления.
        df["to_in_segment"] = df["inn_to"].isin(set(at["inn"].dropna()))
    return df.sort_values("n_epk", ascending=False).reset_index(drop=True)


TENURE_ORDER = ["0", "1", "2-3", "4-6", "7-12", "13-24", "25+"]


def tenure_table(df: pd.DataFrame) -> pd.DataFrame:
    """Стаж ушедших по корзинам.

    Отвечает на вопрос, которого не задаёт ни одна другая цифра: уходят недавно
    пришедшие (ротация, сезонники) или старожилы (потеря ядра). Это разные
    диагнозы с разными решениями, а по числу «ушло N человек» они одинаковы.
    """
    if df.empty:
        return pd.DataFrame()
    out = df.copy()
    out["n_epk"] = _num(out, "n_epk")
    out["title"] = [EPK_CAUSES.get(c, (c, "", LOSS))[0] for c in out["cause"]]
    piv = out.pivot_table(index="bucket", columns="title", values="n_epk",
                          aggfunc="sum").fillna(0.0)
    piv = piv.reindex([b for b in TENURE_ORDER if b in piv.index])
    piv["Всего"] = piv.sum(axis=1)
    total = float(piv["Всего"].sum()) or 1.0
    piv["Доля"] = piv["Всего"] / total
    return piv.reset_index()


# --------------------------------------------------------------------------- #
# Где
# --------------------------------------------------------------------------- #
def enrich(lost_by_inn: pd.DataFrame, attrs: pd.DataFrame, tb: pd.DataFrame,
           gosb: pd.DataFrame, gosb_key: str | None) -> tuple[pd.DataFrame, dict]:
    """Разметить потери атрибутами организации и территории.

    Ведомство и уровень подчинения выводятся ИЗ ИМЕНИ: отдельных полей для них
    нет ни в одном разрешённом источнике. Доля неразобранных имён считается и
    показывается — приписать их к массовой группе значило бы завысить её потерю
    на неизвестную величину.

    ТБ берётся ПРЯМО ИЗ ВЕДОМОСТЕЙ. На проме `gosb_id` ведомостей со справочником
    не сошёлся вовсе, и весь территориальный разрез схлопнулся в одну строку
    «ТБ неизвестен», которую обезличивание вдобавок выдало за настоящее
    подразделение. Номер ТБ в ведомостях заполнен всегда, и разрез по нему есть
    даже тогда, когда ГОСБ не опознан.
    """
    meta: dict = {}
    if lost_by_inn.empty:
        return lost_by_inn, meta

    df = lost_by_inn.copy()
    df["inn"] = _key(df["inn"])
    if not attrs.empty:
        attrs = attrs.copy()
        attrs["inn"] = _key(attrs["inn"])
        df = df.merge(attrs, on="inn", how="left")

    df["agency"] = classify_agency(df, name_col="company_name",
                                   force_col="is_military",
                                   industry_col="industry_name")
    df["level"] = LV.classify_frame(df, name_col="company_name")

    n_named = int(df["company_name"].notna().sum()) if "company_name" in df else 0
    meta["named_share"] = n_named / max(len(df), 1)
    meta["agency_unknown"] = float(
        _num(df[df["agency"] == AG_UNKNOWN], "n_triples").sum())
    meta["level_unknown"] = float(
        _num(df[df["level"] == LV.UNKNOWN], "n_triples").sum())

    # --- территория: ТБ из ведомостей ---
    if not tb.empty and "tb_id" in df:
        tb = tb.copy()
        tb["tb_id"] = _key(tb["tb_id"])
        df["tb_id"] = _key(df["tb_id"])
        df = df.merge(tb.drop_duplicates("tb_id"), on="tb_id", how="left")
    meta["tb_known"] = (float(_num(df[df["tb_short_name"].notna()], "n_triples").sum())
                        if "tb_short_name" in df else 0.0)

    # ГОСБ — только если разведка нашла ключ, которым он опознаётся. Иначе разрез
    # не строится вовсе: заглушка под видом подразделения хуже отсутствия разреза.
    meta["gosb_key"] = gosb_key
    meta["region_known"] = 0.0
    if gosb_key and not gosb.empty and gosb_key in gosb:
        g = gosb.dropna(subset=[gosb_key]).copy()
        g[gosb_key] = _key(g[gosb_key])
        g = g.drop_duplicates(gosb_key).set_index(gosb_key)
        gid = _key(df["gosb_id"])
        df["region_name"] = gid.map(g["region_name"])
        df["gosb_name"] = gid.map(g["gosb_name"])
        meta["region_known"] = float(
            _num(df[df["region_name"].notna()], "n_triples").sum())
        # Соответствие проверено запросом, а разрез всё равно пуст — значит
        # сломалось не в данных, а по дороге. Молчать об этом нельзя: пустой
        # разрез читается как «в регионах ничего не потеряно».
        if meta["region_known"] == 0:
            progress.warn(
                f"подразделения опознаны справочником по «{gosb_key}», но ни одна "
                f"строка потерь не получила региона — проверьте типы ключа")

    for col, fill in FILL.items():
        df[col] = df[col].fillna(fill) if col in df else fill

    meta["n_inn"] = int(df["inn"].nunique())
    progress.done(f"разметка потерь: {meta['n_inn']:,} организаций, имя известно у "
                  f"{meta['named_share']:.0%}; ТБ известен у "
                  f"{meta['tb_known']:,.0f} получателей, регион у "
                  f"{meta['region_known']:,.0f}")
    return df, meta


def by_dim(df: pd.DataFrame, dim: str, top_n: int = 12) -> pd.DataFrame:
    """Потери в разрезе, с составом по видам внутри каждой строки.

    Состав здесь не украшение. «Утекло образование» без него остаётся
    утверждением без содержания: неизвестно, ушли ли люди, закрылись ли школы,
    сменился ли код зачисления или их просто перевели в другое подразделение — а
    это разные выводы и разные решения.

    Строк возвращается top_n, но итог считается по ВСЕМ: доля обязана быть долей
    от целого, а не от показанного куска.
    """
    if df.empty or dim not in df:
        return pd.DataFrame()
    total = float(_num(df, "n_triples").sum()) or 1.0
    g = df.groupby(dim, dropna=False)
    out = pd.DataFrame({"n_triples": g["n_triples"].sum(),
                        "n_inn": g["inn"].nunique()})
    for kind in (LOSS, INSIDE):
        causes = [c for c, v in CAUSES.items() if v[2] == kind]
        sub = df[df["cause"].isin(causes)].groupby(dim)["n_triples"].sum()
        out[kind] = sub.reindex(out.index).fillna(0.0)
    out["share"] = out["n_triples"] / total
    return out.sort_values("n_triples", ascending=False).head(top_n).reset_index()


def top_orgs(df: pd.DataFrame, gained_inn: pd.DataFrame,
             top_n: int = 15) -> pd.DataFrame:
    """Организации с наибольшим ЧИСТЫМ изменением.

    Именно чистым. Организация, потерявшая двести тысяч получателей и набравшая
    столько же, по одним потерям выглядит катастрофой, хотя не потеряла ничего, —
    и в первом прогоне она возглавила таблицу. Сортировка идёт по нетто, а потери
    и приход стоят рядом, чтобы разницу было видно, а не считать в уме.
    """
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["inn"] = _key(df["inn"])
    g = df.groupby("inn", dropna=False)
    out = pd.DataFrame({"lost": g["n_triples"].sum()})
    if gained_inn is not None and not gained_inn.empty and "inn" in gained_inn:
        gn = gained_inn.copy()
        gn["inn"] = _key(gn["inn"])
        gi = gn.drop_duplicates("inn").set_index("inn")["n_triples"]
        out["gained"] = pd.to_numeric(gi.reindex(out.index), errors="coerce").fillna(0.0)
    else:
        out["gained"] = 0.0
    out["net"] = out["gained"] - out["lost"]

    name = df.drop_duplicates("inn").set_index("inn")
    for col in ("company_name", "holding_name", "tb_short_name", "region_name",
                "agency", "level"):
        out[col] = name[col].reindex(out.index) if col in name else None

    top_cause = (df.sort_values("n_triples", ascending=False)
                 .drop_duplicates("inn").set_index("inn")["cause"])
    out["cause"] = top_cause.reindex(out.index)
    out["cause_title"] = [CAUSES.get(c, (c, "", LOSS))[0] for c in out["cause"]]
    return out.sort_values("net").head(top_n).reset_index()

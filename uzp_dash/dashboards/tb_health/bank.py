"""Единственный проход в БД для дэша tb_health: все данные банка сразу.

Отчёт двухуровневый (СБ + вкладка на каждый ТБ), но данные для него читаются ОДИН
РАЗ по всему банку, а разрез по ТБ делается уже в pandas. Раньше каждый из 13
уровней ходил в БД сам: ~24 запроса на ТБ, плюс повторное чтение справочника
организаций и помесячных активностей на уровне СБ — около 290 запросов и ~45 минут
на проме, притом что данные там были одни и те же.

Здесь же считается и прогноз: его грейн — (ГОСБ, ИНН), от ТБ он не зависит, и
разбивать его по ТБ незачем. Коэффициент реализуемости пайплайна возвращается сразу
для трёх уровней (ГОСБ / ТБ / банк) — у каждого уровня отчёта он свой.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ... import progress
from ...db import read_sql
from . import forecast, queries as Q, segments

RUB_TO_MLN = 1e6
HIST_MONTHS = 24                 # глубина истории витрины под модель сезонности
PIPE_MONTHS = 12                 # закрытых месяцев сделок для коэффициента реализуемости
# Аппарат ТБ — не продающее подразделение, в разборе ему делать нечего. Опознаётся
# по имени, но с обязательной оговоркой: если аппарат — ЕДИНСТВЕННОЕ подразделение
# своего ТБ (Московский банк), исключать его нельзя, иначе ТБ обнулится.
APPARAT_PREFIX = "аппарат"


@dataclass
class Bank:
    """Данные всего банка. Уровни отчёта берут отсюда срезы, в БД больше не ходят."""
    dates: dict
    tbs: pd.DataFrame                 # tb_id / tb_short_name / tb_full_name
    apparat: set                      # new_gosb_id аппаратов (исключаются из разбора)
    tb_of: dict                       # new_gosb_id -> tb_id, единственный источник
    gosb_name: dict                   # new_gosb_id -> имя
    verdict: pd.DataFrame             # план/факт уровней tb и sb за три месяца
    unit_seg: pd.DataFrame            # ГОСБ × сегмент, обе опорные даты
    unit_tot: pd.DataFrame            # итоги ГОСБ, обе опорные даты
    orgs: pd.DataFrame                # (ГОСБ, ИНН) — витрина + воронка + прогноз
    orgs_tb: pd.DataFrame             # (ТБ, ИНН) — строки витрины уровнем выше
    fagg: pd.DataFrame                # агрегат воронки по (ГОСБ, ИНН)
    act_tot: pd.DataFrame             # итоги активностей по ТБ
    act_brk: pd.DataFrame             # разрезы активностей по ТБ
    fmonths: pd.DataFrame             # активности по месяцам (ГОСБ, ИНН, месяц)
    orgs_fc: pd.DataFrame             # прогноз на грейне (ГОСБ, ИНН)
    conv: dict                        # коэффициенты реализуемости трёх уровней
    params: dict = field(default_factory=dict)     # параметры запуска (ctx.params)
    fc_stats: dict = field(default_factory=dict)   # диагностика прогноза


# --------------------------------------------------------------------------- #
def load(ctx) -> Bank:
    """Прочитать всё, что нужно отчёту, и посчитать прогноз по банку."""
    e = ctx.engine
    d = dates(e, ctx.params)
    p_dates = {"ref_cur": d["ref_cur"], "ref_closed": d["ref_closed"],
               "ref_yoy": d["ref_yoy"]}
    p_metric = {"m_fot": Q.METRIC_FOT, "m_rcp": Q.METRIC_RECIPIENTS}

    tbs = read_sql(e, Q.TB_LIST)
    names = [str(r.tb_short_name) for r in tbs.itertuples()]
    tb_ids = {int(x) for x in tbs["tb_id"]}
    progress.done(f"Уровни отчёта: СБ + {len(names)} ТБ ({', '.join(names)})")
    if Q.EXCLUDE_TB:
        progress.done(f"Не считаются территориальными банками и в отчёт не входят: "
                      f"{', '.join(Q.EXCLUDE_TB)} — это не продающая сеть, плана по ФОТ "
                      f"на них нет. Вердикт банка при этом берётся строкой витрины "
                      f"level_name='sb', где они внутри")

    flags = read_sql(e, Q.GOSB_FLAGS)
    apparat, tb_of, gosb_name = _apparat(flags)

    progress.step("Вердикты уровней СБ и ТБ: текущий месяц, закрытый, год назад")
    verdict = read_sql(e, Q.METRICS_VERDICT, {**p_metric, **p_dates})
    verdict["end_dt"] = pd.to_datetime(verdict["end_dt"]).dt.date
    # Под level_name='sb' в профиле прома встречаются level_value 0/1/99. Если строк
    # на метрику окажется больше одной, вердикт банка неоднозначен — про это надо
    # сказать вслух, а не выбрать молча (выбирается строка с наибольшим фактом).
    sb_ids = sorted({int(x) for x in
                     verdict[verdict["level_name"] == "sb"]["level_id"].dropna()})
    if len(sb_ids) > 1:
        progress.done(f"ВНИМАНИЕ: под level_name='sb' несколько level_id {sb_ids} — "
                      f"вердикт банка неоднозначен, берётся строка с наибольшим фактом")

    progress.step("Матрица ГОСБ × сегмент и итоги ГОСБ по всему банку")
    unit_seg = read_sql(e, Q.UNIT_SEG, {"m_rcp": Q.METRIC_RECIPIENTS,
                                        "ref_cur": d["ref_cur"],
                                        "ref_closed": d["ref_closed"]})
    unit_tot = read_sql(e, Q.UNIT_TOTALS, {"m_rcp": Q.METRIC_RECIPIENTS,
                                           "ref_cur": d["ref_cur"],
                                           "ref_closed": d["ref_closed"]})
    for f in (unit_seg, unit_tot):
        f["end_dt"] = pd.to_datetime(f["end_dt"]).dt.date

    progress.step("Витрина организаций по всему банку (потенциал/отток/год к году)")
    orgs = read_sql(e, Q.ORGS_ALL, {"ref_closed": d["ref_closed"]})
    orgs_tb = _only_known_tb(read_sql(e, Q.ORGS_TB, {"ref_closed": d["ref_closed"]}),
                             tb_ids, "строк витрины уровня ТБ")
    orgs = _prepare_orgs(orgs, apparat, tb_of)
    orgs_tb = _prepare_orgs_tb(orgs_tb, orgs)
    _log_orgs(orgs, orgs_tb)

    progress.step("Активности воронки за 3 мес: агрегат по (ГОСБ, ИНН)")
    pf = {"ref_funnel": d["ref_funnel"], "funnel_from": d["funnel_from"],
          "fresh_from": d["fresh_from"]}
    fagg = read_sql(e, Q.FUNNEL_AGG, pf)
    # Активности группируются по СОБСТВЕННОМУ tb_id воронки, а не по справочнику
    # ГОСБ, поэтому фильтр _GMAP их не касается: отсекаем по списку известных ТБ.
    act_tot = _only_known_tb(read_sql(e, Q.ACTIVITY_TOTALS, pf), tb_ids,
                             "строк итогов активностей")
    act_brk = _only_known_tb(read_sql(e, Q.ACTIVITY_BREAKDOWN, pf), tb_ids,
                             "строк разрезов активностей")
    inn_stats = read_sql(e, Q.FUNNEL_INN_STATS,
                         {"plan_from": d["plan_from"], "ref_funnel": d["ref_funnel"]})
    _log_funnel(fagg, act_tot, inn_stats)

    fmonths = _only_known_tb(
        read_sql(e, Q.FUNNEL_MONTHS, {"months_from": d["months_from"],
                                      "ref_funnel": d["ref_funnel"]}),
        tb_ids, "строк помесячных активностей")
    progress.done(f"Активности по месяцам с {d['months_from']}: {len(fmonths)} строк "
                  f"(ГОСБ×организация×месяц) — по ним видно, отрабатывали ли отток тогда")

    orgs = _merge_funnel(orgs, fagg)
    orgs, orgs_fc, conv, stats = forecast_bank(e, orgs, d, tb_of)

    return Bank(dates=d, tbs=tbs, apparat=apparat, tb_of=tb_of, gosb_name=gosb_name,
                verdict=verdict, unit_seg=unit_seg, unit_tot=unit_tot,
                orgs=orgs, orgs_tb=orgs_tb, fagg=fagg, act_tot=act_tot, act_brk=act_brk,
                fmonths=fmonths, orgs_fc=orgs_fc, conv=conv,
                params=dict(ctx.params or {}), fc_stats=stats)


def audit_texts(engine, b: Bank, inns: list) -> pd.DataFrame:
    """Свободный текст активностей по всем аудиторским пулам — ОДНИМ запросом.

    Пул считается на уровне ТБ, и раньше запрос уходил по одному на ТБ: двенадцать
    проходов по самой большой таблице. Списки ИНН всех уровней объединяются, а разбор
    по парам делает уже `analyze._collect_notes`.
    """
    if not inns:
        return pd.DataFrame()
    d = b.dates
    df = read_sql(engine, Q.FUNNEL_TEXT,
                  {"inns": list(inns), "funnel_from": d["funnel_from"],
                   "ref_funnel": d["ref_funnel"]})
    progress.done(f"Тексты активностей: {len(df)} строк по {len(inns)} организациям "
                  f"всех уровней — один запрос вместо двенадцати")
    return df


# --------------------------------------------------------------------------- #
def dates(engine, params: dict) -> dict:
    """Опорные даты дэша. Считаются ОДИН раз на отчёт.

    ПРОГНОЗНЫЙ месяц задаётся параметром `report_month` (синоним — устаревший `date`).
    Если он не задан, берётся самый свежий месяц ежедневной витрины оттока. Витрина
    хранит ВСЕ месяцы, поэтому отчёт можно пересобрать и за прошлый период — но тогда
    дату актуальности надо брать ИМЕННО ЗА ЭТОТ месяц (`ACT_DT_FOR`), а не за
    последний: иначе весь расчёт «сколько выплат уже увидели» считается по чужому
    периоду. Если витрины нет вовсе — откат на закрытый месяц company_holding + 1.
    """
    ref_cur = act_dt = None
    src = ""
    asked = params.get("report_month") or params.get("date")
    if asked:
        ref_cur = (pd.to_datetime(asked) + pd.offsets.MonthEnd(0)).date()
        src = "задан параметром report_month"
    row = read_sql(engine, Q.REF_CUR)
    if not row.empty and pd.notna(row.ref_cur.iloc[0]):
        d_cur = pd.to_datetime(row.ref_cur.iloc[0]).date()
        d_act = (pd.to_datetime(row.act_dt.iloc[0]).date()
                 if pd.notna(row.act_dt.iloc[0]) else d_cur)
        if ref_cur is None:
            ref_cur, act_dt, src = d_cur, d_act, "последний месяц ежедневной витрины"
        elif d_cur == ref_cur:
            act_dt = d_act
        else:
            # заданный месяц не последний — берём дату актуальности ЭТОГО месяца
            a = read_sql(engine, Q.ACT_DT_FOR, {"ref_cur": ref_cur})
            n_rows = int(a.n_rows.iloc[0] or 0) if not a.empty else 0
            if n_rows and pd.notna(a.act_dt.iloc[0]):
                act_dt = pd.to_datetime(a.act_dt.iloc[0]).date()
                progress.done(f"Месяц отчёта {ref_cur} — не последний в витрине "
                              f"(там {d_cur}); дата актуальности взята за этот месяц: "
                              f"{act_dt}, строк {n_rows}")
            else:
                progress.done(f"ВНИМАНИЕ: за {ref_cur} в ежедневной витрине нет строк "
                              f"(последний месяц там {d_cur}) — наблюдаемого оттока не "
                              f"будет, отток посчитается только по модели истории")
    if ref_cur is None:
        closed = pd.to_datetime(read_sql(engine, Q.REF_DATE).iloc[0, 0]).date()
        ref_cur = (pd.Timestamp(closed) + pd.offsets.MonthEnd(1)).date()
        src = "ФОЛБЭК: ежедневной витрины нет — закрытый месяц витрины + 1"
    if act_dt is None:
        act_dt = ref_cur
    cur = pd.Timestamp(ref_cur)
    p_cur = cur.to_period("M")
    p_closed = p_cur - 1
    ref_closed = p_closed.to_timestamp("M").date()
    # Окно воронки — 3 календарных месяца, заканчивая ПРОГНОЗНЫМ: задачи по метрикам
    # идут месяцем позже метрик, поэтому конец окна и есть текущий месяц.
    ref_funnel = ref_cur
    funnel_from = (p_cur - 2).to_timestamp().date()
    # Сделки судим по дате СОЗДАНИЯ СДЕЛКИ: заведённые в двух последних месяцах окна
    # ещё не могли дать зачисления, по ним недоработку не считаем.
    fresh_from = (p_cur - 1).to_timestamp().date()
    hist_from = (p_cur - (HIST_MONTHS + 1)).to_timestamp("M").date()
    # окно помесячных активностей под вопрос «отрабатывали ли отток тогда»: та же
    # глубина, что у месяцев оттока в годовом тренде (forecast.YOY_DEPTH), плюс ещё
    # один месяц вперёд — задачу на отток заводят и в СЛЕДУЮЩЕМ отчётном месяце
    months_from = (p_cur - forecast.YOY_DEPTH).to_timestamp().date()
    # тот же месяц год назад — для прироста «год к году» по закрытому месяцу
    ref_yoy = (p_cur - 13).to_timestamp("M").date()
    # окно сделок для помесячного план/факт: PIPE_MONTHS закрытых месяцев + текущий.
    # Шире окна активностей: коэффициент реализуемости считается по закрытым месяцам.
    plan_from = (p_cur - PIPE_MONTHS).to_timestamp().date()
    # ДВЕ РАЗНЫЕ величины, их нельзя путать:
    #  * observed — какую долю выплатных событий месяца мы уже УВИДЕЛИ. Меряется по
    #    act_dt (дата актуальности витрины оттока) и отвечает за то, сколько риска
    #    оттока уже отыграно;
    #  * days_left — сколько КАЛЕНДАРНОГО времени осталось, чтобы привлечения по
    #    сделкам успели дойти. Меряется по РЕАЛЬНОЙ текущей дате: витрина оттока про
    #    будущие дни ничего не знает, и её act_dt тут ни при чём.
    observed = min(1.0, pd.Timestamp(act_dt).day / cur.day)
    today = params.get("today")
    today = (pd.Timestamp(today).date() if today
             else pd.Timestamp.now().date())
    if today > cur.date():
        days_left = 0                      # месяц уже закончился
    elif today < cur.replace(day=1).date():
        days_left = int(cur.day)           # месяц ещё не начался
    else:
        # сегодняшний день ещё в игре: 31-е из 31 — это 1 оставшийся день, а не 0
        days_left = int(cur.day) - today.day + 1
    pipe_left = days_left / float(cur.day)

    progress.done(f"Прогнозный месяц: {ref_cur} ({src}) · факт зачислений по {act_dt} "
                  f"(выплат месяца отыграно {observed * 100:.0f}%)")
    progress.done(f"Сегодня {today}: до конца месяца {days_left} из {cur.day} дн. "
                  f"({pipe_left * 100:.0f}%) — столько времени осталось у пайплайна")
    progress.done(f"Портфель-база прогноза — закрытый месяц {ref_closed}; "
                  f"история витрины с {hist_from} ({HIST_MONTHS} мес)")
    months = ", ".join((p_cur - k).strftime("%m.%Y") for k in (2, 1, 0))
    progress.done(f"Окно задач воронки: {funnel_from} … {ref_funnel} ({months}) · "
                  f"сделки с {fresh_from} — свежие")

    # Опорные месяцы для свёрнутой истории (queries.OUTFLOW_HIST_AGG). Все — НАЧАЛА
    # месяцев: в запросе ym = date_trunc('month', report_dt).
    p_yoy = p_cur - 12
    first = lambda p: p.to_timestamp().date()          # noqa: E731 — узкий хелпер
    hist_params = {
        "m_closed": first(p_closed), "m_prev": first(p_closed - 1),
        "mon_cur": int(p_cur.month), "mon_cls": int(p_closed.month),
        "m_yoy": first(p_yoy), "m_yoy_prev": first(p_yoy - 1),
        "m_y1": first(p_yoy + 1), "m_y2": first(p_yoy + 2), "m_y3": first(p_yoy + 3),
        "m_out_from": first(p_closed - (forecast.YOY_DEPTH - 1)),
    }
    return {"ref_cur": ref_cur, "ref_closed": ref_closed, "act_dt": act_dt,
            "ref_yoy": ref_yoy,
            "ref_funnel": ref_funnel, "funnel_from": funnel_from,
            "fresh_from": fresh_from, "hist_from": hist_from, "plan_from": plan_from,
            "months_from": months_from,
            "cur_month": int(cur.month), "month_elapsed": float(observed),
            "today": today, "days_left": int(days_left),
            "days_in_month": int(cur.day), "pipe_left": float(pipe_left), "src": src,
            "label": f"{cur.month:02d}.{cur.year}",
            "closed_label": f"{pd.Timestamp(ref_closed).month:02d}."
                            f"{pd.Timestamp(ref_closed).year}",
            **hist_params}


# --------------------------------------------------------------------------- #
def _only_known_tb(df: pd.DataFrame, tb_ids: set, what: str) -> pd.DataFrame:
    """Оставить строки только тех ТБ, которые есть в отчёте.

    Нужно там, где `tb_id` приходит ИЗ САМОЙ ТАБЛИЦЫ (воронка, витрина уровня ТБ), а
    не из справочника ГОСБ: фильтр общего CTE `gmap` такие колонки не затрагивает, и
    без этой отсечки в свод банка попали бы и ЦА, и любой посторонний номер, которого
    в справочнике нет вовсе. Сколько строк убрано — в прогресс.
    """
    if df is None or df.empty or "tb_id" not in df:
        return df
    keep = df["tb_id"].isin(tb_ids)
    n_drop = int((~keep).sum())
    if n_drop:
        dropped = sorted({int(x) for x in df.loc[~keep, "tb_id"].dropna()})
        progress.done(f"Отброшено {n_drop} {what}: ТБ {dropped} не входят в отчёт")
    return df[keep].reset_index(drop=True)


def _apparat(flags: pd.DataFrame) -> tuple[set, dict, dict]:
    """Аппараты, соответствие ГОСБ → ТБ и имена ГОСБ — из одного справочника.

    Правило аппарата: имя начинается на «Аппарат» И это не единственное подразделение
    своего ТБ. Вторая половина обязательна — у Московского банка аппарат единственный,
    и без оговорки этот ТБ остался бы вовсе без единиц разбора.

    Состав печатается в прогресс: молча выкидывать подразделения из отчёта нельзя,
    иначе расхождение с витриной будет выглядеть ошибкой расчёта.
    """
    if flags.empty:
        return set(), {}, {}
    out, kept = set(), []
    tb_of, names = {}, {}
    for r in flags.itertuples():
        nid = int(r.new_gosb_id)
        name = str(r.gosb_name or "").strip()
        tb_of[nid] = int(r.tb_id)
        names[nid] = name
        if not name.lower().startswith(APPARAT_PREFIX):
            continue
        if int(r.n_gosb) <= 1:
            kept.append(name)
            continue
        out.add(nid)
    if out:
        progress.done(f"Из разбора исключены аппараты ТБ: {len(out)} подразделений — "
                      f"они не продающие. Вердикт уровня берётся из витрины целиком, "
                      f"поэтому сумма карточек ему не равна — так и задумано")
    if kept:
        progress.done(f"Оставлены как единственное подразделение своего ТБ: "
                      f"{', '.join(kept)}")
    return out, tb_of, names


def _prepare_orgs(orgs: pd.DataFrame, apparat: set, tb_of: dict) -> pd.DataFrame:
    """Организации банка: убрать аппараты, проставить ТБ и производные колонки.

    ТБ берётся ИЗ СПРАВОЧНИКА ГОСБ (`tb_of`), а не из строки витрины: один ГОСБ обязан
    принадлежать ровно одному ТБ, иначе его организации попали бы в разбор двух ТБ
    сразу и свод по банку задвоился бы.
    """
    if orgs.empty:
        return orgs
    o = orgs.dropna(subset=["new_gosb_id"]).copy()
    o["new_gosb_id"] = o["new_gosb_id"].astype("int64")
    o["inn"] = o["inn"].astype("int64")
    o["tb_id"] = [tb_of.get(int(g)) for g in o["new_gosb_id"]]
    o = o.dropna(subset=["tb_id"])
    # Организации аппаратов ОСТАЮТСЯ в грейне прогноза. База и план уровня ТБ берутся
    # строкой витрины level_name='tb', а аппарат в ней уже учтён, — значит его отток и
    # пайплайн обязаны входить в прогноз, иначе прогноз завышен ровно на них.
    # Из разбора аппараты уходят там, где это действительно нужно: карточки и матрица
    # ГОСБ (`analyze._units`), список к работе и отбор под план (`analyze._candidates`).
    if apparat:
        n_app = int(o["new_gosb_id"].isin(apparat).sum())
        if n_app:
            progress.done(f"Организации аппаратов: {n_app} пар (ГОСБ, ИНН) из {len(o)} "
                          f"остаются в прогнозе (они внутри строки ТБ в витрине), но в "
                          f"карточки ГОСБ и в список к работе не попадут")
    o = o.reset_index(drop=True)
    o["fot_potential_mln"] = o["fot_potential_amt"] / RUB_TO_MLN
    o["fot_outflow_mln"] = o["fot_outflow_amt"] / RUB_TO_MLN
    o["seg_name"] = o["segment_big"].map(segments.short_of_big).fillna("—")
    o["company_name"] = o["company_name"].fillna("")
    o["in_ref"] = o["in_ref"].fillna(False).astype(bool)
    return o


def _prepare_orgs_tb(orgs_tb: pd.DataFrame, orgs: pd.DataFrame) -> pd.DataFrame:
    """Строки витрины уровня ТБ + имя организации и признак эталонной базы.

    Сами числа (годовая дельта, численность) берутся ИЗ ВИТРИНЫ уровня ТБ, а не
    складываются из ГОСБ: витрина эту свёртку уже сделала, а суммирование ГОСБ-строк
    в блоке годового тренда повторяло бы организацию столько раз, в скольких ГОСБ она
    обслуживается. Имя и «закреплена ли в эталонной базе» уровня ТБ в витрине нет —
    их приносим с грейна ГОСБ.
    """
    if orgs_tb is None or orgs_tb.empty:
        return orgs_tb
    t = orgs_tb.copy()
    t["tb_id"] = t["tb_id"].astype("int64")
    t["inn"] = t["inn"].astype("int64")
    if orgs is None or orgs.empty:
        t["company_name"] = ""
        t["in_ref"] = False
        return t
    named = orgs[orgs["company_name"].astype(str) != ""]
    names = named.drop_duplicates("inn").set_index("inn")["company_name"]
    t["company_name"] = t["inn"].map(names).fillna("")
    ref = orgs.groupby(["tb_id", "inn"], as_index=False)["in_ref"].max()
    t = t.merge(ref, on=["tb_id", "inn"], how="left")
    t["in_ref"] = t["in_ref"].fillna(False).astype(bool)
    return t


def _log_orgs(orgs: pd.DataFrame, orgs_tb: pd.DataFrame) -> None:
    n_all = len(orgs)
    n_ref = int(orgs["in_ref"].sum()) if n_all else 0
    if n_all and "n_src_rows" in orgs:
        merged = int((orgs["n_src_rows"] > 1).sum())
        if merged:
            extra = int(orgs["n_src_rows"].sum()) - n_all
            progress.done(f"Свёрнуто до грейна (ГОСБ, ИНН): {merged} пар пришли "
                          f"несколькими строками старых ГОСБ (+{extra} строк) — их "
                          f"числа сложены, иначе организация повторялась бы в отчёте")
    progress.done(f"Эталонная база: закреплено {n_ref} из {n_all} пар (ГОСБ, ИНН) — "
                  f"остальные {n_all - n_ref} исключены из отбора, но остаются в "
                  f"детализации прогноза")
    progress.done(f"Строки витрины уровня ТБ (level_name='tb'): {len(orgs_tb)} пар "
                  f"(ТБ, организация) — по ним считается годовой тренд уровня СБ")


def _log_funnel(fagg: pd.DataFrame, act_tot: pd.DataFrame,
                inn_stats: pd.DataFrame) -> None:
    n_tasks = int(act_tot["n"].sum()) if not act_tot.empty else 0
    with_text = int(fagg["any_text"].sum()) if len(fagg) else 0
    progress.done(f"задач {n_tasks} → пар (ГОСБ,ИНН) {len(fagg)} · из них с текстом "
                  f"{with_text}")
    if inn_stats is not None and not inn_stats.empty:
        r = inn_stats.iloc[0]
        n_all, n_null = int(r.n_all or 0), int(r.n_null_inn or 0)
        if n_null:
            progress.done(f"Строк воронки без ИНН: {n_null} из {n_all} "
                          f"({n_null / max(n_all, 1) * 100:.2f}%) — отброшены: работа "
                          f"с клиентом идёт на грейне (ГОСБ, ИНН), без ИНН строку "
                          f"не к чему отнести")


def _merge_funnel(orgs: pd.DataFrame, fagg: pd.DataFrame) -> pd.DataFrame:
    """Приклеить агрегат воронки на грейне (ГОСБ, ИНН)."""
    num_cols = ["n_tasks", "n_calls", "n_meetings", "n_success", "n_overdue", "n_outflow",
                "n_in_progress", "n_closed",
                "plan_deal", "fact_deal", "plan_deal_old", "fact_deal_old", "unrealized"]
    bool_cols = ["any_success", "any_text", "has_fresh_deal", "deal_expected"]
    if fagg.empty:
        for c in num_cols:
            orgs[c] = 0
        for c in bool_cols:
            orgs[c] = False
        orgs["fresh_deal_dt"] = pd.NaT
        orgs["worked"] = False
        return orgs
    f = fagg.dropna(subset=["new_gosb_id"]).copy()
    f["new_gosb_id"] = f["new_gosb_id"].astype("int64")
    f["inn"] = f["inn"].astype("int64")
    orgs = orgs.merge(f, on=["new_gosb_id", "inn"], how="left")
    orgs["worked"] = orgs["n_tasks"].notna() & (orgs["n_tasks"].fillna(0) > 0)
    for c in num_cols:
        orgs[c] = orgs[c].fillna(0).astype(int)
    for c in bool_cols:
        orgs[c] = orgs[c].fillna(False).astype(bool)
    return orgs


# --------------------------------------------------------------------------- #
def forecast_bank(engine, orgs: pd.DataFrame, d: dict, tb_of: dict):
    """Прогноз по (ГОСБ, ИНН) на весь банк: ожидаемый отток + приход из пайплайна.

    Возвращает (orgs с приклеенным прогнозом, кадр прогноза, коэффициенты, диагностика).
    Отдельно считается ФОТ-эффект: отток пересчитывается по средней ЗП организации,
    а по пайплайну план ФОТа есть свой.
    """
    progress.step(f"Прогноз на {d['ref_cur']}: отток по истории + ежедневный + пайплайн")
    dp = {"ref_cur": d["ref_cur"], "act_dt": d["act_dt"]}
    day = read_sql(engine, Q.DAY_OUTFLOW, dp)
    _log_day_outflow(read_sql(engine, Q.DAY_OUTFLOW_STATS, dp), day, d)
    hist = read_sql(engine, Q.OUTFLOW_HIST_AGG,
                    {"hist_from": d["hist_from"], "ref_closed": d["ref_closed"],
                     **{k: d[k] for k in ("m_closed", "m_prev", "mon_cur", "mon_cls",
                                          "m_yoy", "m_yoy_prev", "m_y1", "m_y2", "m_y3",
                                          "m_out_from")}})
    # План и ФАКТ пайплайна помесячно на грейне (ГОСБ, ИНН, сотрудник): именно на нём
    # план двух сделок одного месяца складывается в одно число, с которым и сравнивается
    # пришедший факт.
    pp = {"plan_from": d["plan_from"], "ref_funnel": d["ref_funnel"]}
    plan_m = read_sql(engine, Q.PIPELINE_PLAN_M, pp)
    fact_m = read_sql(engine, Q.PIPELINE_FACT_M,
                      {"plan_from": d["plan_from"], "ref_cur": d["ref_cur"],
                       "m_np": Q.METRIC_NEW_RECIPIENTS_B2B, "counted": Q.MOTIV_COUNTED})
    fstat = read_sql(engine, Q.PIPELINE_FACT_STATS,
                     {"plan_from": d["plan_from"], "ref_cur": d["ref_cur"],
                      "m_np": Q.METRIC_NEW_RECIPIENTS_B2B, "counted": Q.MOTIV_COUNTED})
    pstat = read_sql(engine, Q.PIPELINE_PLAN_STATS, pp)

    pred = forecast.outflow_model(hist, d["ref_cur"])
    rec = forecast.reconcile(day, pred, d["month_elapsed"])
    conv = forecast.conversion_by_month(plan_m, fact_m, d["ref_cur"], tb_of)
    # у пайплайна своя мера времени — сколько КАЛЕНДАРНЫХ дней осталось до конца
    # месяца (от реальной даты), а не сколько выплат мы увидели в витрине оттока
    pipe_fc = forecast.pipeline_current(plan_m, fact_m, d["ref_cur"], conv["of_gosb"],
                                        conv["sb"], d["pipe_left"])
    due = forecast.deal_due(plan_m, fact_m, d["ref_cur"])
    _log_pipeline(fstat, pstat, conv, d)
    # сегмент из воронки приходит БОЛЬШИМ именем — приводим к короткому,
    # иначе он не совпадёт с сегментами матрицы
    if not pipe_fc.empty:
        short = pipe_fc["seg_funnel"].map(segments.short_of_big)
        pipe_fc["seg_funnel"] = short.fillna(pipe_fc["seg_funnel"])
    seg_of = {int(r.inn): r.seg_name for r in orgs.itertuples()
              if r.seg_name and r.seg_name != "—"}
    fc = forecast.org_forecast(rec, pipe_fc, seg_of)

    # ФОТ-эффект: средняя ЗП из ежедневной витрины, фолбэк — из витрины организаций
    sal_of = {(int(r.new_gosb_id), int(r.inn)): float(r.avg_salary or 0)
              for r in orgs.itertuples() if pd.notna(r.new_gosb_id)}
    sal = [float(s) if float(s or 0) > 0 else sal_of.get((int(g), int(i)), 0.0)
           for s, g, i in zip(fc["avg_salary_m"], fc["new_gosb_id"], fc["inn"])]
    fc["salary"] = sal
    fc["out_fot"] = fc["out_exp"] * fc["salary"]
    fc["in_fot"] = fc["in_exp"] * fc["salary"]
    # ТБ пишем прямо в строку организации: уровню отчёта тогда не нужен обратный
    # маппинг ГОСБ → ТБ, единица разбора задаётся просто именем колонки
    fc["tb_id"] = [tb_of.get(int(g)) if pd.notna(g) else None for g in fc["new_gosb_id"]]

    hd = pred.attrs.get("diag", {}) if not pred.empty else {}
    n_hist = int(hd.get("hist_months", 0))
    classes = fc["out_class"].value_counts().to_dict() if not fc.empty else {}
    stats = {"n_day": len(day), "n_hist_orgs": len(pred), "hist_months": n_hist,
             "n_pipe": len(pipe_fc),
             "classes": classes, "hist": hd,
             "pipe_np": float(fc["pipe_np"].sum()) if not fc.empty else 0.0,
             "pipe_np_raw": float(fc["pipe_np_raw"].sum()) if not fc.empty else 0.0}
    raw = conv["diag_sb"].get("tb_raw")
    conv_txt = (f"коэф. банка {raw:.2f} → поднят до пола {conv['sb']:.2f}"
                if conv["diag_sb"].get("tb_clipped") else f"коэф. банка {conv['sb']:.2f}")
    progress.done(f"История: {n_hist} мес ({hd.get('hist_from','—')}…"
                  f"{hd.get('hist_to','—')}) по {len(pred)} парам · ежедневная витрина: "
                  f"{len(day)} пар · пайплайн на {d['label']}: {len(pipe_fc)} орг, "
                  f"{stats['pipe_np_raw']:.0f} чел заявлено → {stats['pipe_np']:.0f} "
                  f"с поправкой на реализуемость ({conv_txt})")
    n_clip = conv["diag_sb"].get("n_gosb_clipped", 0)
    if n_clip:
        progress.done(f"Коэффициент реализуемости упёрся в границы "
                      f"[{forecast.CONV_MIN}, {forecast.CONV_MAX}] у {n_clip} из "
                      f"{conv['diag_sb'].get('n_gosb', 0)} ТБ — по ним вклад пайплайна "
                      f"в прогноз завышен")
    _log_history(hd, d)
    if classes:
        n_all = sum(classes.values()) or 1
        progress.done("Классы оттока: " + " · ".join(
            f"{k} {v} ({v / n_all * 100:.1f}%)"
            for k, v in sorted(classes.items(), key=lambda x: -x[1])))

    keep = ["new_gosb_id", "inn", "out_exp", "in_exp", "pipe_np", "pipe_np_raw",
            "pipe_fact_mtd", "pipe_fot", "out_observed", "pred", "out_class", "note",
            "why", "settled", "n_deals", "out_months", "has_hist", "recovered"]
    merged = orgs.copy()
    merged["new_gosb_id"] = merged["new_gosb_id"].astype("Int64")
    if not fc.empty:
        f = fc[[c for c in keep if c in fc]].copy()
        f["new_gosb_id"] = f["new_gosb_id"].astype("Int64")
        f["inn"] = f["inn"].astype("int64")
        merged = merged.merge(f, on=["new_gosb_id", "inn"], how="left")
    # план/факт по сделкам за ЗАКРЫТЫЕ месяцы — на них опирается аудит отработки
    if due is not None and not due.empty:
        dd = due.copy()
        dd["new_gosb_id"] = dd["new_gosb_id"].astype("Int64")
        dd["inn"] = dd["inn"].astype("int64")
        merged = merged.merge(dd, on=["new_gosb_id", "inn"], how="left")
    for c in ("out_exp", "in_exp", "pipe_np", "pipe_np_raw", "pipe_fact_mtd", "pipe_fot",
              "out_observed", "pred", "settled", "n_deals",
              "plan_np_due", "fact_np_due", "due_months"):
        merged[c] = forecast.num(merged, c)
    for c in ("out_class", "note", "why"):
        merged[c] = merged.get(c).fillna("") if c in merged else ""
    merged["out_class"] = merged["out_class"].replace("", forecast.CLS_STABLE)
    # колонка-список: у организаций без истории после left join приезжает NaN, а он
    # ПРОХОДИТ проверку `or []` (nan истинно) и роняет list() уже в детализации
    merged["out_months"] = [v if isinstance(v, list) else []
                            for v in merged.get("out_months", pd.Series(dtype=object))] \
        if "out_months" in merged else [[] for _ in range(len(merged))]
    # «истории по паре нет вовсе» — это не то же самое, что «оттока не было»
    merged["has_hist"] = (merged["has_hist"].fillna(False).astype(bool)
                          if "has_hist" in merged else False)
    return merged, fc, conv, stats


def _log_day_outflow(stats: pd.DataFrame, day: pd.DataFrame, d: dict) -> None:
    """Что дала ежедневная ведомость: сколько пар в ней есть и у скольких задача.

    Ноль строк — ШТАТНАЯ ситуация начала месяца: выплатные даты ещё не наступили либо
    задачи на отток пока не завели. Говорим об этом спокойно и отдельной строкой,
    иначе «0 пар» читается как сбой. От «ведомости за месяц нет вовсе» это отличается
    знаменателем: там нет и самих пар, и про это предупреждает `dates()`.
    """
    n_pairs = int(stats.n_pairs.iloc[0] or 0) if stats is not None and not stats.empty else 0
    n_task = len(day)
    if not n_pairs:
        progress.done(f"Ежедневная ведомость на {d['act_dt']}: выплатных дат ещё не "
                      f"наступило — наблюдаемого оттока нет, отток пойдёт только по "
                      f"модели истории. Для начала месяца это нормально")
        return
    if not n_task:
        progress.done(f"Ежедневная ведомость на {d['act_dt']}: {n_pairs} пар "
                      f"(ГОСБ, ИНН), задач на отток НИ ОДНОЙ — наблюдаемого оттока нет, "
                      f"отток пойдёт только по модели истории")
        return
    progress.done(f"Ежедневный отток на {d['act_dt']}: задача выставлена у {n_task} пар "
                  f"из {n_pairs} в ведомости (по последней прошедшей выплате). "
                  f"У остальных {n_pairs - n_task} наблюдения нет — их риск месяца "
                  f"не гасится и войдёт в прогноз по модели целиком")


def _log_pipeline(fstat: pd.DataFrame, pstat: pd.DataFrame, conv: dict, d: dict) -> None:
    """Диагностика пайплайна: что отсеяли фильтрами и на чём стоит коэффициент.

    Обе доли важны для доверия к цифре: фильтр «учтено» убирает фрод, а строки с
    неразрешимым месяцем плана вообще не участвуют в расчёте.
    """
    if fstat is not None and not fstat.empty:
        r = fstat.iloc[0]
        n_all = int(r.n_all or 0)
        if n_all:
            drop = n_all - int(r.n_counted or 0)
            amt_all = float(r.amt_all or 0)
            amt_drop = amt_all - float(r.amt_counted or 0)
            progress.done(
                f"Факт по сделкам: {int(r.n_counted or 0)} из {n_all} строк «учтено» "
                f"(отсеяно {drop}, {drop / n_all * 100:.0f}%) · "
                f"{float(r.amt_counted or 0):.0f} НП из {amt_all:.0f} "
                f"(не в учёте {amt_drop:.0f})")
    if pstat is not None and not pstat.empty:
        r = pstat.iloc[0]
        n_all, n_bad = int(r.n_all or 0), int(r.n_bad or 0)
        if n_bad:
            progress.done(f"Пайплайн: у {n_bad} из {n_all} строк ({n_bad / max(n_all,1)*100:.1f}%) "
                          f"месяц плана вне 3 месяцев жизни сделки — год не восстановить, "
                          f"в расчёт не идут")
        n_multi = int(getattr(r, "n_multi_inn", 0) or 0)
        if n_multi:
            progress.done(f"Пайплайн: {n_multi} кодов сделок встречаются сразу с "
                          f"несколькими ИНН — план такого кода берётся ОДИН раз "
                          f"(организация выбирается детерминированно)")
    sb = conv.get("diag_sb", {})
    if sb.get("months"):
        progress.done(f"Реализуемость: план {sb['plan']:.0f} → факт {sb['fact']:.0f} "
                      f"по {sb['months']} закрытым месяцам, {len(conv.get('by_gosb', {}))} "
                      f"ГОСБ со своим коэффициентом (окно с {d['plan_from']})")
        if sb["months"] < 3:
            progress.done(f"Коэффициент стоит всего на {sb['months']} закрытых мес — "
                          f"мало для устойчивой оценки: в окне нет сделок постарше")
    else:
        progress.done("Реализуемость НЕ рассчитана: закрытых месяцев с планом нет — "
                      "пайплайн войдёт в прогноз без поправки (коэф. 1.0)")


def _log_history(hd: dict, d: dict) -> None:
    """Диагностика истории витрины: хватает ли её модели и что вообще посчиталось.

    Раньше здесь стоял чек `n_hist < 13`, и он был неверен дважды: при ровно 13
    месяцах не срабатывал, а 13 месяцев и не хватает — у ПРОГНОЗНОГО месяца второе
    наблюдение появляется только на 24-м месяце (`forecast.seasonal_depth_needed`).
    Поэтому вместо порога печатаем факт: чем посчитана сезонность и у скольких пар.
    """
    if not hd:
        return
    # молчаливый сбой: если базового месяца нет в истории, отток закрытого месяца
    # везде окажется нулём, и ВСЁ уедет в класс «стабильно» без единой жалобы
    if not hd.get("base_present"):
        progress.done(f"ВНИМАНИЕ: базового месяца {hd.get('base_month')} НЕТ в истории "
                      f"витрины — отток закрытого месяца везде будет нулевым, "
                      f"модель оттока фактически отключена")
    n_pairs = max(int(hd.get("n_pairs", 0)), 1)
    n_out = int(hd.get("n_with_outflow", 0))
    progress.done(f"Отток в закрытом месяце есть у {n_out} из {n_pairs} пар "
                  f"({n_out / n_pairs * 100:.1f}%) — остальные попадут в «стабильно»")
    src = hd.get("seas_src", {})
    need, have = int(hd.get("need_months", 0)), int(hd.get("hist_months", 0))
    n_idx = int(src.get(forecast.SRC_INDEX, 0))
    n_yoy = int(src.get(forecast.SRC_YOY, 0))
    if n_idx:
        progress.done(f"Сезонность: индекс по ≥{forecast.MIN_SEASON_OBS} наблюдениям "
                      f"у {n_idx} пар, переход год назад у {n_yoy}, без сигнала "
                      f"{int(src.get(forecast.SRC_NONE, 0))}")
    elif n_yoy:
        progress.done(f"Сезонность: индекса нет (для месяца {d.get('label','')} нужно "
                      f"{need} мес истории, есть {have}) → считаем по переходу год "
                      f"назад, сигнал у {n_yoy} пар из {n_pairs}")
    else:
        progress.done(f"Сезонность НЕ рассчитана: для месяца {d.get('label','')} нужно "
                      f"{need} мес истории (есть {have}), а перехода год назад нет — "
                      f"работает только модель двух закрытых месяцев")

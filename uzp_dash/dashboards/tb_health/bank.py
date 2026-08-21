"""Единственный проход в БД для дэша tb_health: все данные банка сразу.

Отчёт двухуровневый (СБ + вкладка на каждый ТБ), но данные для него читаются ОДИН
РАЗ по всему банку, а разрез по ТБ делается уже в pandas. Раньше каждый из 13
уровней ходил в БД сам: ~24 запроса на ТБ, плюс повторное чтение справочника
организаций и помесячных активностей на уровне СБ — около 290 запросов и ~45 минут
на проме, притом что данные там были одни и те же.

Сам ПРОГНОЗ дэш не считает — он берёт готовый prediction_amt из витрины метрик.
Здесь считается то, чего в витрине нет на грейне организации: фактический отток за
три закрытых месяца (за вычетом вернувшихся) и ожидаемый приход из пайплайна.
Коэффициент реализуемости пайплайна возвращается сразу для трёх уровней
(ГОСБ / ТБ / банк) — у каждого уровня отчёта он свой.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ... import progress
from ...db import read_sql
from . import forecast, queries as Q, segments

RUB_TO_MLN = 1e6
PIPE_MONTHS = 12                 # закрытых месяцев сделок для коэффициента реализуемости
# Окно фактического оттока: три ЗАКРЫТЫХ месяца (T-1, T-2, T-3). Текущий не берём —
# он не закрыт, и отток по нему был бы неполным.
OUT_MONTHS = 3
# Порог оттока: уход одного-двух человек — текучка, а не потеря клиента. Требование
# бизнеса; ниже порога организация в разбор не попадает.
OUT_MIN_QTY = 3
# Глубина окна помесячных активностей под блок «Портфель год к году»: год плюс месяц,
# потому что задачу на отток заводят и в следующем отчётном месяце.
YOY_DEPTH = 13
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
    orgs: pd.DataFrame                # (ГОСБ, ИНН) — витрина + воронка + отток
    orgs_tb: pd.DataFrame             # (ТБ, ИНН) — строки витрины уровнем выше
    fagg: pd.DataFrame                # агрегат воронки по (ГОСБ, ИНН)
    act_tot: pd.DataFrame             # итоги активностей по ТБ
    act_brk: pd.DataFrame             # разрезы активностей по ТБ
    fmonths: pd.DataFrame             # активности по месяцам (ГОСБ, ИНН, месяц)
    orgs_fc: pd.DataFrame             # отток и пайплайн на грейне (ГОСБ, ИНН)
    conv: dict                        # коэффициенты реализуемости трёх уровней
    params: dict = field(default_factory=dict)     # параметры запуска (ctx.params)
    fc_stats: dict = field(default_factory=dict)   # диагностика оттока и пайплайна


# --------------------------------------------------------------------------- #
def load(ctx) -> Bank:
    """Прочитать всё, что нужно отчёту, и посчитать отток с пайплайном."""
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
    _log_metrics(verdict, d)

    progress.step("Матрица ГОСБ × сегмент и итоги ГОСБ по всему банку")
    unit_seg = read_sql(e, Q.UNIT_SEG, {"m_rcp": Q.METRIC_RECIPIENTS,
                                        "ref_cur": d["ref_cur"],
                                        "ref_closed": d["ref_closed"]})
    unit_tot = read_sql(e, Q.UNIT_TOTALS, {"m_rcp": Q.METRIC_RECIPIENTS,
                                           "ref_cur": d["ref_cur"],
                                           "ref_closed": d["ref_closed"]})
    for f in (unit_seg, unit_tot):
        f["end_dt"] = pd.to_datetime(f["end_dt"]).dt.date
    _log_units(unit_seg, unit_tot, d)

    progress.step("Витрина организаций по всему банку (потенциал/отток/год к году)")
    orgs = read_sql(e, Q.ORGS_ALL, {"ref_closed": d["ref_closed"]})
    orgs_tb = _only_known_tb(read_sql(e, Q.ORGS_TB, {"ref_closed": d["ref_closed"]}),
                             tb_ids, "строк витрины уровня ТБ")
    orgs = _prepare_orgs(orgs, apparat, tb_of)
    orgs_tb = _prepare_orgs_tb(orgs_tb, orgs)
    _log_orgs(orgs, orgs_tb)

    mgr = read_sql(e, Q.ORG_MANAGER, {"role": Q.ROLE_ATTACHED})
    orgs = _merge_manager(orgs, mgr)

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
    # отрабатывали ли отток в месяц ухода — считается ПОСЛЕ прогноза: раньше
    # `out_months` ещё не приклеен к строкам организаций
    orgs = _outflow_worked(orgs, fmonths)
    orgs_fc = _outflow_worked(orgs_fc, fmonths)
    _log_outflow_worked(orgs)

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
    Если он не задан, берётся самый свежий месяц, на который витрина уже посчитала
    прогноз (`REF_CUR`): дэш прогноз не считает, а показывает витринный, и месяц без
    `prediction_amt` показывать нечем. Если прогноза нет вовсе — откат на закрытый
    месяц company_holding + 1, и об этом говорится вслух.
    """
    ref_cur = None
    src = ""
    asked = params.get("report_month") or params.get("date")
    if asked:
        ref_cur = (pd.to_datetime(asked) + pd.offsets.MonthEnd(0)).date()
        src = "задан параметром report_month"
    row = read_sql(engine, Q.REF_CUR, {"m_rcp": Q.METRIC_RECIPIENTS})
    d_cur = (pd.to_datetime(row.ref_cur.iloc[0]).date()
             if not row.empty and pd.notna(row.ref_cur.iloc[0]) else None)
    if ref_cur is None and d_cur is not None:
        ref_cur, src = d_cur, "последний месяц с прогнозом в витрине"
    elif ref_cur is not None and d_cur is not None and ref_cur > d_cur:
        progress.warn(f"На {ref_cur} прогноза по метрике {Q.METRIC_RECIPIENTS} в "
                      f"витрине нет (последний с прогнозом — {d_cur}). Отчёт "
                      f"соберётся, но прогноз будет пустым")
    if ref_cur is None:
        closed = pd.to_datetime(read_sql(engine, Q.REF_DATE).iloc[0, 0]).date()
        ref_cur = (pd.Timestamp(closed) + pd.offsets.MonthEnd(1)).date()
        # якорь отчёта стоит на prediction_amt ИМЕННО метрики портфеля: если у неё
        # прогноза нет вовсе, виновника надо назвать, иначе фолбэк выглядит загадкой
        src = (f"ФОЛБЭК: прогноза по метрике {Q.METRIC_RECIPIENTS} в витрине нет — "
               f"закрытый месяц витрины + 1")
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
    # ТРИ ЗАКРЫТЫХ МЕСЯЦА под фактический отток: T-1, T-2, T-3. Текущий сюда не
    # входит намеренно — он не закрыт, и отток по нему был бы неполным.
    out_months = [(p_closed - k).to_timestamp("M").date() for k in range(OUT_MONTHS)]
    # окно помесячных активностей под вопрос «отрабатывали ли отток тогда»: та же
    # глубина, что у годового тренда портфеля, плюс ещё один месяц вперёд — задачу
    # на отток заводят и в СЛЕДУЮЩЕМ отчётном месяце
    months_from = (p_cur - YOY_DEPTH).to_timestamp().date()
    # тот же месяц год назад — для прироста «год к году» по закрытому месяцу
    ref_yoy = (p_cur - 13).to_timestamp("M").date()
    # окно сделок для помесячного план/факт: PIPE_MONTHS закрытых месяцев + текущий.
    # Шире окна активностей: коэффициент реализуемости считается по закрытым месяцам.
    plan_from = (p_cur - PIPE_MONTHS).to_timestamp().date()
    # days_left — сколько КАЛЕНДАРНОГО времени осталось, чтобы привлечения по сделкам
    # успели дойти. Меряется по РЕАЛЬНОЙ текущей дате.
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

    out_lbl = ", ".join(f"{pd.Timestamp(m).month:02d}.{pd.Timestamp(m).year}"
                        for m in out_months)
    progress.done(f"Прогнозный месяц: {ref_cur} ({src}) — прогноз берётся из витрины")
    progress.done(f"Сегодня {today}: до конца месяца {days_left} из {cur.day} дн. "
                  f"({pipe_left * 100:.0f}%) — столько времени осталось у пайплайна")
    progress.done(f"Портфель — закрытый месяц {ref_closed}; фактический отток за "
                  f"{OUT_MONTHS} закрытых месяца: {out_lbl}")
    months = ", ".join((p_cur - k).strftime("%m.%Y") for k in (2, 1, 0))
    progress.done(f"Окно задач воронки: {funnel_from} … {ref_funnel} ({months}) · "
                  f"сделки с {fresh_from} — свежие")

    return {"ref_cur": ref_cur, "ref_closed": ref_closed,
            "ref_yoy": ref_yoy,
            "ref_funnel": ref_funnel, "funnel_from": funnel_from,
            "fresh_from": fresh_from, "plan_from": plan_from,
            "months_from": months_from,
            "out_months": out_months, "out_label": out_lbl,
            "m_out1": out_months[0], "m_out2": out_months[1], "m_out3": out_months[2],
            "cur_month": int(cur.month),
            "today": today, "days_left": int(days_left),
            "days_in_month": int(cur.day), "pipe_left": float(pipe_left), "src": src,
            "label": f"{cur.month:02d}.{cur.year}",
            "closed_label": f"{pd.Timestamp(ref_closed).month:02d}."
                            f"{pd.Timestamp(ref_closed).year}"}


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


def _log_metrics(verdict: pd.DataFrame, d: dict) -> None:
    """Сколько строк витрина дала по каждой метрике — и порядок величины ФОТ.

    Если метрики за опорный месяц в витрине нет, `analyze._verdict` отдаёт нули, и
    отчёт рисует их как настоящий результат: план 0, факт 0, выполнение «—». Отличить
    это от честного нуля по самому отчёту нельзя, поэтому пустота называется вслух.
    Особенно важно при СМЕНЕ id метрики: новая метрика может быть посчитана не на всех
    уровнях и не за все месяцы.

    Сырой ФОТ печатается до деления на 1e6: если метрика придёт не в рублях, ошибка в
    миллион раз видна сразу, а не после сверки отчёта с чужой выгрузкой.
    """
    what = {Q.METRIC_RECIPIENTS: "портфель", Q.METRIC_FOT: "ФОТ"}
    months = [("прогнозный", d["ref_cur"]), ("закрытый", d["ref_closed"]),
              ("год назад", d["ref_yoy"])]
    for mid, label in what.items():
        sub = verdict[verdict["metric_id"] == mid] if not verdict.empty else verdict
        by_month = [f"{name} {dt}: {int((sub['end_dt'] == dt).sum()) if len(sub) else 0}"
                    for name, dt in months]
        progress.done(f"Метрика {label} ({mid}): строк уровней sb/tb — "
                      + " · ".join(by_month))
        missing = [f"{name} {dt}" for name, dt in months[:2]
                   if not len(sub) or not int((sub["end_dt"] == dt).sum())]
        if missing:
            progress.warn(f"Метрики {mid} ({label}) в витрине нет за: "
                          f"{', '.join(missing)} — план и факт этих месяцев будут "
                          f"нулями, а выполнение пустым. Проверьте id метрики")
    fot = verdict[(verdict["metric_id"] == Q.METRIC_FOT)
                  & (verdict["level_name"] == "sb")
                  & (verdict["end_dt"] == d["ref_closed"])] if not verdict.empty else None
    if fot is not None and not fot.empty:
        raw = float(pd.to_numeric(fot["fact_amt"], errors="coerce").max() or 0)
        # пробел как разделитель разрядов — только В ЧИСЛАХ: replace по всей строке
        # съедал бы и запятые самого текста
        num = f"{raw:,.0f}".replace(",", " ")
        mln = f"{raw / RUB_TO_MLN:,.0f}".replace(",", " ")
        progress.done(f"ФОТ банка за {d['ref_closed']} из витрины: {num} — в отчёте "
                      f"это {mln} млн ₽. Если метрика придёт не в рублях, расхождение "
                      f"будет ровно в 1e6")


def _log_units(unit_seg: pd.DataFrame, unit_tot: pd.DataFrame, d: dict) -> None:
    """Матрица «единица × сегмент» и итоги единиц: сколько строк и по каким уровням.

    Разрез по сегментам (`extended_dim_1`) есть не у каждой метрики витрины. Если его
    нет, матрица приезжает пустой, западающих сегментов не находится, и отчёт молча
    теряет и таблицу сегментов, и весь отбор организаций под план.
    """
    for df, name in ((unit_seg, "матрица единица × сегмент"),
                     (unit_tot, "итоги единиц")):
        if df is None or df.empty:
            progress.warn(f"{name.capitalize()}: строк нет вовсе — метрика портфеля "
                          f"({Q.METRIC_RECIPIENTS}) за {d['ref_cur']}/{d['ref_closed']} "
                          f"в этом разрезе не посчитана")
            continue
        lvl = ", ".join(f"{k}: {v}" for k, v in
                        df["level_name"].value_counts().to_dict().items())
        segs = (df["seg_id"].nunique() if "seg_id" in df else 0)
        tail = f" · сегментов {segs}" if "seg_id" in df else ""
        progress.done(f"{name.capitalize()}: {len(df)} строк ({lvl}){tail}")


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


def _merge_manager(orgs: pd.DataFrame, mgr: pd.DataFrame) -> pd.DataFrame:
    """Приклеить ФИО закреплённого сотрудника на грейне (ГОСБ, ИНН).

    Строка списка к работе говорит, ЧТО сделать; закрепление отвечает, КОМУ это
    поручить, — без него задачу приходится раздавать вручную.

    Соответствие строго по паре (ГОСБ, организация): та же организация в соседнем
    ГОСБ ведётся другим сотрудником, и подставлять его сюда нельзя. Где закрепления
    нет — пусто, и в отчёте будет прочерк.

    Покрытие печатается в прогресс: столбец, пустой у половины строк, без этого
    выглядит сломанным, а не «так в данных».
    """
    out = orgs.copy()
    if orgs.empty:
        out["emp_fio"] = pd.Series(dtype=object)
        out["emp_saphr_id"] = pd.Series(dtype="Int64")
        return out
    if mgr is None or mgr.empty:
        progress.warn("Закреплений сотрудников за организациями не нашлось — столбец "
                      "«Сотрудник» в списке к работе будет пустым")
        out["emp_fio"] = ""
        out["emp_saphr_id"] = pd.NA
        return out
    m = mgr.dropna(subset=["new_gosb_id", "inn"]).copy()
    m["new_gosb_id"] = m["new_gosb_id"].astype("int64")
    m["inn"] = m["inn"].astype("int64")
    m["emp_saphr_id"] = pd.to_numeric(m["saphr_id"], errors="coerce").astype("Int64")
    m["emp_fio"] = m["fio"].fillna("").astype(str).str.strip()
    n_rows = len(m)
    # дедуп уже сделан в SQL; проверяем это здесь, потому что дубль пары размножил бы
    # строки списка к работе — молчаливой такая ошибка быть не должна
    n_pairs = int(m.drop_duplicates(["new_gosb_id", "inn"]).shape[0])
    if n_pairs != n_rows:
        progress.warn(f"Закрепления: {n_rows - n_pairs} пар (ГОСБ, организация) пришли "
                      f"больше одного раза — берётся первая, список не задваивается")
        m = m.drop_duplicates(["new_gosb_id", "inn"])
    n0 = len(out)
    out = out.merge(m[["new_gosb_id", "inn", "emp_saphr_id", "emp_fio"]],
                    on=["new_gosb_id", "inn"], how="left")
    if len(out) != n0:
        progress.warn(f"Склейка закреплений изменила число организаций: {n0} → "
                      f"{len(out)} — в отчёт попали дубли строк")
    out["emp_fio"] = out["emp_fio"].fillna("")
    _log_manager(out, m)
    return out


def _log_manager(orgs: pd.DataFrame, mgr: pd.DataFrame) -> None:
    """Покрытие закреплениями: сколько пар с сотрудником, без ФИО и мимо отчёта."""
    n_all = len(orgs)
    if not n_all:
        return
    has_emp = orgs["emp_saphr_id"].notna()
    n_emp = int(has_emp.sum())
    # закреплённые, но без имени: табельного нет в штатке или ФИО там пустое
    n_no_fio = int((has_emp & (orgs["emp_fio"].astype(str) == "")).sum())
    # закрепления, для которых пары (ГОСБ, организация) в отчёте не нашлось
    keys = set(zip(orgs["new_gosb_id"], orgs["inn"]))
    n_orphan = sum(1 for g, i in zip(mgr["new_gosb_id"], mgr["inn"])
                   if (g, i) not in keys)
    share = n_emp / n_all * 100
    msg = (f"Закреплённые сотрудники: ФИО нашлось у {n_emp - n_no_fio} из {n_all} пар "
           f"(ГОСБ, организация), {share:.0f}% закреплено")
    tail = []
    if n_no_fio:
        tail.append(f"у {n_no_fio} закреплённых нет ФИО в штатке")
    if n_orphan:
        tail.append(f"{n_orphan} закреплений не нашли свою пару в отчёте")
    if tail:
        msg += " · " + "; ".join(tail)
    (progress.warn if share < 50 else progress.done)(
        msg + ("" if share >= 50 else " — у большинства строк списка будет прочерк"))


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
    """Фактический отток и приход из пайплайна по (ГОСБ, ИНН) на весь банк.

    Прогноз здесь БОЛЬШЕ НЕ СЧИТАЕТСЯ: он приходит готовым из витрины метрик
    (prediction_amt). Эта функция отвечает за две вещи, которых в витрине нет на
    грейне организации:
      * сколько людей УЖЕ ушло за три закрытых месяца и не вернулось — по нему
        строится список к работе и блок «крупнейшие оттоки»;
      * сколько людей ждём из пайплайна — с поправкой на реализуемость.
    """
    progress.step(f"Отток за {d['out_label']} и пайплайн на {d['label']}")
    op = {"out_min": OUT_MIN_QTY, "m_out1": d["m_out1"], "m_out2": d["m_out2"],
          "m_out3": d["m_out3"]}
    out = read_sql(engine, Q.FACT_OUTFLOW, op)
    _log_outflow(read_sql(engine, Q.FACT_OUTFLOW_STATS, op), out, d)

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

    conv = forecast.conversion_by_month(plan_m, fact_m, d["ref_cur"], tb_of)
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
    fc = forecast.org_outflow(out, pipe_fc, seg_of)

    # ФОТ-эффект: средняя ЗП из витрины оттока, фолбэк — из витрины организаций
    sal_of = {(int(r.new_gosb_id), int(r.inn)): float(r.avg_salary or 0)
              for r in orgs.itertuples() if pd.notna(r.new_gosb_id)}
    sal = [float(s) if float(s or 0) > 0 else sal_of.get((int(g), int(i)), 0.0)
           for s, g, i in zip(fc["avg_salary_m"], fc["new_gosb_id"], fc["inn"])]
    fc["salary"] = sal
    fc["out_fot"] = fc["out_kept"] * fc["salary"]
    # ТБ пишем прямо в строку организации: уровню отчёта тогда не нужен обратный
    # маппинг ГОСБ → ТБ, единица разбора задаётся просто именем колонки
    fc["tb_id"] = [tb_of.get(int(g)) if pd.notna(g) else None for g in fc["new_gosb_id"]]

    stats = {"n_out": len(out), "n_pipe": len(pipe_fc),
             "out_kept": float(fc["out_kept"].sum()) if not fc.empty else 0.0,
             "out_ret": float(fc["ret_qty"].sum()) if not fc.empty else 0.0,
             "pipe_np": float(fc["pipe_np"].sum()) if not fc.empty else 0.0,
             "pipe_np_raw": float(fc["pipe_np_raw"].sum()) if not fc.empty else 0.0}
    raw = conv["diag_sb"].get("tb_raw")
    conv_txt = (f"коэф. банка {raw:.2f} → поднят до пола {conv['sb']:.2f}"
                if conv["diag_sb"].get("tb_clipped") else f"коэф. банка {conv['sb']:.2f}")
    progress.done(f"Пайплайн на {d['label']}: {len(pipe_fc)} орг, "
                  f"{stats['pipe_np_raw']:.0f} чел заявлено → {stats['pipe_np']:.0f} фл "
                  f"с поправкой на реализуемость ({conv_txt})")
    n_clip = conv["diag_sb"].get("n_gosb_clipped", 0)
    if n_clip:
        progress.done(f"Коэффициент реализуемости упёрся в границы "
                      f"[{forecast.CONV_MIN}, {forecast.CONV_MAX}] у {n_clip} из "
                      f"{conv['diag_sb'].get('n_gosb', 0)} ТБ — по ним вклад пайплайна "
                      f"завышен")

    keep = ["new_gosb_id", "inn", "out_qty", "ret_qty", "out_kept", "out_months",
            "pipe_np", "pipe_np_raw", "pipe_fact_mtd", "pipe_fot", "n_deals"]
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
    for c in ("out_qty", "ret_qty", "out_kept", "pipe_np", "pipe_np_raw",
              "pipe_fact_mtd", "pipe_fot", "n_deals",
              "plan_np_due", "fact_np_due", "due_months"):
        merged[c] = forecast.num(merged, c)
    # колонка-список: у организаций без оттока после left join приезжает NaN, а он
    # ПРОХОДИТ проверку `or []` (nan истинно) и роняет list() уже в детализации
    merged["out_months"] = [v if isinstance(v, list) else []
                            for v in merged.get("out_months", pd.Series(dtype=object))] \
        if "out_months" in merged else [[] for _ in range(len(merged))]
    return merged, fc, conv, stats


def next_month(label: str) -> str:
    """«MM.YYYY» → следующий месяц. Метки месяцев в блоках — строки, а не даты."""
    try:
        mm, yy = label.split(".")
        m, y = int(mm), int(yy)
    except (ValueError, AttributeError):
        return label
    return f"01.{y + 1}" if m == 12 else f"{m + 1:02d}.{y}"


def month_index(fm: pd.DataFrame | None, unit_src: str) -> tuple[dict, set]:
    """Активности по месяцам → индекс {(единица, ИНН, «MM.YYYY»): агрегат} + окно месяцев.

    Окно возвращается отдельно и намеренно: по нему отличается «задач не было» от
    «месяц вне выборки, данных нет». Без него обе ситуации выглядели бы одинаково.

    Единица разбора — ГОСБ или ТБ, поэтому ключ собирается по `unit_src`: у ТБ строки
    воронки нескольких ГОСБ схлопываются в одну пару (ТБ, ИНН).

    Строки без единицы или без организации выбрасываются: ключ по ним не собрать. На
    проме такие есть — у задачи может не оказаться ИНН, а `gosb_id` задачи может не
    найтись в справочнике (join витрины с ним внешний). Раньше это роняло разбор на
    `int(NaN)`. Сколько строк потеряно — печатается: по этому индексу отвечают на
    вопрос «отрабатывали ли отток тогда», и молча недосчитаться активностей значит
    записать организацию в неотработанные без оснований.
    """
    idx: dict = {}
    window: set = set()
    if fm is None or fm.empty:
        return idx, window
    src = unit_src if unit_src in fm else "new_gosb_id"
    keys = [c for c in (src, "inn", "ym") if c in fm]
    n_all = len(fm)
    # пустоты считаем ДО отбрасывания — после него считать уже нечего
    lost = {c: int(fm[c].isna().sum()) for c in keys}
    fm = fm.dropna(subset=keys)
    if len(fm) < n_all:
        progress.warn(
            f"Помесячные активности: отброшено {n_all - len(fm)} строк из {n_all} — "
            f"нечем собрать ключ (пусто: "
            + ", ".join(f"{c} {n}" for c, n in lost.items() if n) + "). "
            f"По этим задачам отработка не учитывается")
    for r in fm.itertuples():
        ym = pd.Timestamp(getattr(r, "ym"))
        label = f"{ym.month:02d}.{ym.year}"
        window.add(label)
        key = (int(getattr(r, src)), int(r.inn), label)
        cur = idx.setdefault(key, {"n_tasks": 0, "n_success": 0, "n_outflow": 0,
                                   "n_out_success": 0})
        cur["n_tasks"] += int(r.n_tasks or 0)
        cur["n_success"] += int(r.n_success or 0)
        cur["n_outflow"] += int(getattr(r, "n_outflow", 0) or 0)
        cur["n_out_success"] += int(getattr(r, "n_out_success", 0) or 0)
    return idx, window


def _outflow_worked(orgs: pd.DataFrame, fmonths: pd.DataFrame) -> pd.DataFrame:
    """Отрабатывали ли отток в месяц ухода — признак для рычага «Вернуть».

    Невозвращённый отток сам по себе не означает, что банк недоработал: по части
    организаций отток отработали, задачу закрыли успешно, а люди всё равно не
    вернулись. Требовать по ним «вернуть» второй раз бессмысленно.

    Смотрим ДВА месяца на каждый месяц ухода M — сам M и M+1: витрина оттока
    закрывается позже, чем он случился, и задачу на отток заводят следующим отчётным
    месяцем. Без M+1 нормально отработанный отток выглядел бы пропущенным.

    Отработанным считается только тот отток, по которому есть УСПЕШНО ЗАКРЫТАЯ задача
    ИМЕННО ТИПА «Отток» (`n_out_success`). Успешная задача о привлечении рядом с
    проваленной задачей об оттоке отработкой оттока не является.

    Считается один раз на банк: признак принадлежит паре (ГОСБ, ИНН), а не уровню
    отчёта, поэтому уровень СБ берёт тот же флаг без пересчёта.
    """
    idx, window = month_index(fmonths, "new_gosb_id")
    tasks, outflow, done, known = [], [], [], []
    for r in orgs.itertuples():
        months = list(getattr(r, "out_months", None) or [])
        g = getattr(r, "new_gosb_id", None)
        t = o = s = 0
        cov = True
        if months and pd.notna(g):
            for m in months:
                look = (m, next_month(m))
                if not any(x in window for x in look):
                    cov = False          # месяц вне окна воронки — судить не о чем
                for x in look:
                    c = idx.get((int(g), int(r.inn), x))
                    if c:
                        t += c["n_tasks"]; o += c["n_outflow"]; s += c["n_out_success"]
        tasks.append(t); outflow.append(o); done.append(s); known.append(cov)
    out = orgs.copy()
    out["out_tasks"] = tasks
    out["out_tasks_outflow"] = outflow
    out["out_done"] = done
    out["out_known"] = known
    # отработан = есть успешно закрытая задача по оттоку; при неизвестном окне
    # (месяц ухода вне выборки воронки) утверждать «не отрабатывали» нельзя
    out["out_worked"] = [bool(s > 0) for s in done]
    return out


def _log_outflow_worked(orgs: pd.DataFrame) -> None:
    """Сколько пар с оттоком отработали, а сколько нет. Молча отсеивать нельзя."""
    with_out = orgs[forecast.num(orgs, "out_kept") > 0]
    n = len(with_out)
    if not n:
        return
    ok = int(with_out["out_worked"].sum())
    fl_ok = float(forecast.num(with_out[with_out["out_worked"]], "out_kept").sum())
    fl_all = float(forecast.num(with_out, "out_kept").sum())
    unknown = int((~with_out["out_known"]).sum())
    progress.done(
        f"Отработка оттока в месяц ухода и следующий: из {n} пар с невозвращённым "
        f"оттоком отработали {ok} ({ok / n * 100:.0f}%, {fl_ok:.0f} из {fl_all:.0f} чел) "
        f"— у них закрыта успешная задача по оттоку, в рычаг «Вернуть» они не идут")
    if unknown:
        progress.warn(f"У {unknown} пар месяц ухода вне окна воронки — про отработку "
                      f"данных нет, они остаются кандидатами на возврат")


def _log_outflow(stats: pd.DataFrame, out: pd.DataFrame, d: dict) -> None:
    """Что дал фактический отток и сколько срезал порог.

    Порог печатается всегда: «оттока мало» и «порог съел почти всё» — разные
    ситуации, а по одному числу строк их не различить.
    """
    if stats is None or stats.empty:
        progress.warn(f"Витрина фактического оттока за {d['out_label']} пуста — "
                      f"список организаций к работе будет только по привлечению")
        return
    s = stats.iloc[0]
    n_all = int(s.n_all or 0)
    n_kept = int(s.n_kept or 0)
    q_all = float(s.qty_all or 0)
    q_kept = float(s.qty_kept or 0)
    share = (q_kept / q_all * 100) if q_all else 0.0
    progress.done(f"Фактический отток за {d['out_label']}: {n_kept} строк из {n_all} "
                  f"прошли порог ≥{OUT_MIN_QTY} чел — это {q_kept:.0f} из {q_all:.0f} "
                  f"человек ({share:.0f}%); свёрнуто в {len(out)} пар (ГОСБ, ИНН, месяц)")



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



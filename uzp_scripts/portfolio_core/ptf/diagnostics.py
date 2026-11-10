"""Короткая диагностика на уже загруженной панели, без БД и полного стенда."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from . import bench, watch
from .inputs import Panel
from .presentation import org_code


def august_calendar_case(p: Panel, year: int = 2026) -> dict:
    """C01: факты июля–сентября двух лет, без БД, детектора и оценки причины.

    Суммируются только атомы. Неизвестная мера среди активных атомов не становится
    нулём или неполной банковской суммой. Доли ФЛ и счётчики совместительства
    сохраняют свои единицы и не выдаются за уникальных людей сегмента.
    """
    months = {m[:7]: t for t, m in enumerate(p.months)}
    stocks = {"pf_recip": "Получатели (тройки)", "pf_fl": "ФЛ (доли)",
              "multi_recip": "Совмест. (тройки)"}
    indicators = {"single_pay_recip": "Одна выплата (тройки)",
                  "m_out_temp": "Пропуск и возврат в М+1", "m_in_returned": "Вернулись извне"}
    measures = {**stocks, **indicators}
    values, issues, checks = [], [], []
    normalized = {f: watch.flow_obs(p, f) for f in ("in_outside", "out_left")}
    for y in (year - 1, year):
        for month in (7, 8, 9):
            key = f"{y}-{month:02d}"
            t = months.get(key)
            row = {"Месяц": key}
            active = np.isfinite(p.x["pf_recip"][p.atoms, t]) if t is not None else np.zeros(len(p.atoms), bool)
            for measure, label in measures.items():
                v = p.x[measure][p.atoms, t] if t is not None else np.full(len(p.atoms), np.nan)
                missing = active & ~np.isfinite(v)
                row[label] = float(v[active].sum()) if active.any() and not missing.any() else np.nan
                if not active.any() or missing.any():
                    why = "нет месяца" if t is None else "нет активных атомов" if not active.any() else "неполная мера"
                    if measure == "m_out_temp" and t == len(p.months) - 1:
                        why = "нет следующего месяца для подтверждения"
                    issues.append({"Месяц": key, "Мера": measure, "Причина": why,
                                   "Нет значений": int(missing.sum())})
            values.append(row)
            check = {"Месяц": key}
            for f, obs in normalized.items():
                for level, rows in (("атом", p.atoms), ("орг", p.orgs)):
                    check[f"{f}: {level}, <0"] = int(np.sum(obs[rows, t] < -1e-6)) if t is not None else np.nan
            checks.append(check)
    frame = pd.DataFrame(values)

    def changes(labels):
        records = []
        for i, month in enumerate((7, 8, 9)):
            before, after = frame.iloc[i], frame.iloc[i + 3]
            records.append({"Месяц": f"{year}-{month:02d}",
                            **{label: after[label] - before[label] for label in labels}})
        return pd.DataFrame(records)

    names = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
    calendar = "; ".join(f"{d:%d.%m.%Y} — {names[d.weekday()]}" for d in
                         (date(year - 1, 8, 1), date(year, 7, 31), date(year, 8, 1)))
    return {"case_id": "C01", "year": year, "calendar": calendar, "run_id": p.run_id,
            "stocks": frame[["Месяц", *stocks.values()]], "stock_changes": changes(stocks.values()),
            "indicators": frame[["Месяц", *indicators.values()]], "indicator_changes": changes(indicators.values()),
            "normalization": pd.DataFrame(checks), "availability": pd.DataFrame(issues),
            "notes": ["Наблюдаемые атомы банка; полнота самой загрузки проверяется по журналу.",
                      "Доли ФЛ и тройки — разные единицы. Счётчик совместителей не равен числу дополнительных работ.",
                      "Одна выплата и совместительство могут пересекаться; размер пересечения в поколении не сохранён.",
                      "m_out_temp охватывает временные уходы разных типов, а не только уход из банка.",
                      "Вернулись извне — часть прихода извне; это не все возвраты временно исчезнувших троек.",
                      "Разности год к году здесь не являются причинным разложением календарного эффекта."]}


def threshold_snapshot(p: Panel, data: dict) -> dict:
    """Один разбор порогов по уже загруженной панели. БД и закладки не запускаются.
    Выбирается месяц самого большого порога среди сохранённых пропусков.
    """
    from . import presentation

    header = data.get("input")
    if header and (header["months"] != p.months or header["atoms"] != len(p.atoms)
                   or header["organizations"] != len(p.orgs)
                   or (header.get("run_id") is not None and p.run_id is not None and header["run_id"] != p.run_id)):
        raise ValueError("Панель и отчёт относятся к разным поколениям. Нужен panel из того же запуска приёмки.")
    data = presentation.mask(data, p)
    selected, maximum = None, -1.
    for item in data.get("missed_details", []):
        table = pd.DataFrame(item["table"])
        flow = presentation.target_flow(item)
        values = table.loc[(table["когда"] == "после") & (table["поток"] == flow), "порог z"]
        if len(values) and np.isfinite(values.iloc[0]) and values.iloc[0] > maximum:
            selected, maximum = item, float(values.iloc[0])
    t = selected["event"]["t"] if selected else len(p.months) - 1
    level = p.meta.at[selected["target"], "lvl"] if selected else ("org" if len(p.orgs) else "atom")
    rows = p.orgs if level == "org" else p.atoms
    legacy = any(m not in p.x or not np.isfinite(p.x[m]).any()
                 for m in ("m_in_temp_outside", "m_out_temp_left"))
    if legacy:
        # Воспроизводим старый JSON, не запуская сторож на неподходящем поколении.
        obs = {f: p.x[col].copy() for f, (col, _) in watch.FLOWS.items()}
        obs["in_outside"][:, 1:] -= np.nan_to_num(p.x["m_out_temp"][:, :-1])
        obs["out_left"] -= watch.calendar_component(p, legacy_replay=True)
    else:
        obs = {f: watch.flow_obs(p, f) for f in watch.FLOWS}
    scans = watch.scan_rows(p, rows, t, obs, details=True, legacy_replay=legacy)
    corrected = watch.scan_rows(p, rows, t, obs, details=True) if legacy else scans
    priority = presentation.target_flow(selected) if selected else None
    summary, sources, comparison = [], [], []
    for flow, s in scans.items():
        c = s["calibration"]
        summary.append({"Поток": flow, "Нормальный z": c["normal_z"],
                        "Исторический z": c["empirical_z"], "Итоговый z": s["zs"]})
        sources.append({"flow": flow, "threshold": s["zs"], "normal": c["normal_z"], "model": c.get("model"),
                        "contributors": pd.DataFrame(c["contributors"])})
        after = corrected[flow]
        comparison.append({"Поток": flow, "Было z": s["zs"] if legacy else np.nan,
                           "Стало z": after["zs"],
                           "Не для фона": after["calibration"]["excluded_count"],
                           "Нет оценки": after["calibration"]["unsupported_current"]})
    sources.sort(key=lambda s: (s["flow"] != priority, -s["threshold"]))
    remaining = []
    for flow, sc in corrected.items():
        for row in sc["calibration"]["contributors"]:
            if row["задаёт порог"]:
                remaining.append({"Поток": flow, "Месяц": row["месяц"], "Ряд": row["ряд"],
                                  "Факт": row["факт"], "Ожидание": row["ожидание"],
                                  "Шум": row["шум"], "z": row["модуль z"]})
    remaining.sort(key=lambda r: -r["z"])
    return {"month": p.months[t], "level": level, "summary": pd.DataFrame(summary), "sources": sources,
            "legacy_replay": legacy, "comparison": pd.DataFrame(comparison), "calibration_revision": 3,
            "remaining": pd.DataFrame(remaining[:1])}


def recode_event(p: Panel, seed: int = 7, months_back: int = 6,
                 sizes=(3.0, 4.0, 6.0), per_cell: int = 10,
                 kinds=bench.EVENT_KINDS, people_sizes=(300, 1000, 3000), per_size=3) -> bench.Event:
    """Повторить только жеребьёвку evaluate, чтобы получить тот же случай смены ГОСБ.
    Параметры должны совпадать с прошедшим стендом. Детектор здесь не вызывается."""
    plan = bench.make_plan(p, seed, months_back, sizes, per_cell, kinds, people_sizes, per_size)
    event = plan["artifacts"].get("recode")
    if event is None:
        raise ValueError("Нет допустимого переноса в другой ГОСБ того же сегмента и ТБ")
    return event


def recode_report(p: Panel, event=None, before=None, after=None, **params) -> dict[str, pd.DataFrame]:
    """Тот же recode, что в evaluate: два запуска детектора вместо полного стенда.
    Вывод: размер закладки, ограничения источника, отбор потоков организации,
    её итоговая карточка и именно те карточки, которые счётчик считает лишними."""
    ev = event if event is not None else recode_event(p, **params)
    t, r = ev.t, ev.row
    src = int(ev.extra["source_atom"])
    if src < 0 or p.meta.at[ev.row2, "lvl"] != "atom":
        raise ValueError("Стенд выбрал некорректный атом для смены ГОСБ: "
                         f"источник={src}, приёмник={ev.row2}. Нужен разбор закладки.")
    q = bench.inject(p, ev)
    before = watch.detect(p, t) if before is None else before
    after = watch.detect(q, t) if after is None else after
    extra = bench.extra_cards(after, before, p=p, changed=q, t=t)
    org_cards = [c for c in after if c.level == "org" and c.key == r]
    facts = [
        ("месяц", p.months[t]), ("организация", org_code(r)),
        ("людей в закладке", ev.n),
        ("источник", watch.where(p, bench.Card("atom", src, "out_other", 0))),
        ("приёмник", watch.where(p, bench.Card("atom", ev.row2, "in_other", 0))),
        ("портфель организации в прошлом месяце", p.x["pf_recip"][r, t - 1]),
        ("портфель атома-источника в прошлом месяце", p.x["pf_recip"][src, t - 1]),
        ("портфель атома-источника после закладки", q.x["pf_recip"][src, t]),
        ("минимум портфеля источника с месяца закладки", float(bench.quiet_nan(np.nanmin, q.x["pf_recip"][src, t:]))),
        ("ГОСБ источника и приёмника совпадают", bool(
            p.meta.at[src, "sys_gosb_id"] == p.meta.at[ev.row2, "sys_gosb_id"])),
        ("строк детализации потоков организации", len(q.org_flows.xs((r, t), level=("row", "t")))
         if not q.org_flows.empty and (r, t) in q.org_flows.index.droplevel("atom") else 0),
        ("лишних по правилу стенда", len(extra)),
    ]
    i = int(np.flatnonzero(p.orgs == r)[0])
    records = []
    scans = {}
    for name, panel in (("до", p), ("после", q)):
        scan = watch.scan_rows(panel, panel.orgs, t, {f: watch.flow_obs(panel, f) for f in watch.FLOWS})
        scans[name] = scan
        picked = {rr: f for rr, f, *_ in watch.pick(scan, panel.orgs)}.get(r)
        for f, s in scan.items():
            score = watch.excess(s)[i]
            records.append({"когда": name, "поток": f, "факт": s["obs"][i],
                            "ожидание": s["E"][i], "отклонение": s["d"][i],
                            "шум": s["s"][i], "порог z": s["zs"], "сверх шума": score,
                            "минимум": watch.floor_of(s["base"][i]), "выбран": picked == f})
    a, b = (float(scans["после"][f]["d"][i]) for f in ("out_other", "in_other"))
    facts.append(("выполнено условие парного прихода и ухода",
                  bool(a > 0 and b > 0 and min(a, b) >= .6 * max(a, b))))
    localized = next((c for c in watch._cell_recodes(q, t) if c.key == r), None)
    facts.append(("переход подтверждён по отдельным ячейкам", localized is not None))
    # Оценка потенциального объяснения — даже если организация не прошла отбор.
    # Она выводится для диагностики, а не используется для изменения карточек.
    candidate = localized or bench.Card("org", r, "out_other", -a, label="recode",
                                        info={"explains": {"out_other": a, "in_other": b}})
    extra_frame = watch.cards_frame(q, extra, t)
    parts = []
    for c in extra:
        d = candidate.info["explains"].get(c.measure, 0.0)
        part = d if watch._contained(q, t, candidate, c.level, c.key, c.measure) else (
            watch._recode_part(q, t, candidate, c.level, c.key, c.measure, d))
        parts.append(part if np.sign(part) == np.sign(c.obs - c.exp) else 0.0)
    extra_frame["оценка объяснения организацией"] = np.round(parts, 1)
    return {
        "1. Случай смены ГОСБ": pd.DataFrame(facts, columns=["показатель", "значение"]),
        "2. Отбор потоков организации": pd.DataFrame(records).round(1),
        "3. Итоговая карточка организации": watch.cards_frame(q, org_cards, t),
        "4. Лишние карточки по правилу стенда": extra_frame,
    }


def tail_shape(p: Panel, months_back: int = 6) -> dict:
    """Форма хвостов реальных потоков: логарифмическая или аддитивная мерка шума.

    Для тех же рядов и месяцев, что проверяет стенд, считаются стандартизованные
    отклонения в двух мерках одной модели: z_log = (log(1+факт) − центр)/σ и
    z_add = (факт − E)/s. Если шум мультипликативный, у z_add раздут верхний хвост
    и сжат нижний, а z_log близок к нормальному; если аддитивный — наоборот.
    Реальные события попадают в обе мерки одинаково, поэтому сравнение честное,
    но частоты выше нормальных сами по себе не означают ложных тревог.

    «Лишних отборов» — ряды в месяц, которые прошли бы первичный отбор вверх при
    границе min(лог, E + z·s), но не проходят при принятой лог-границе (цена гибрида
    до слияния карточек). Расчёт по панели в памяти; стенд и БД не нужны.
    """
    from . import count_model

    ts = list(range(max(13, len(p.months) - months_back), len(p.months)))
    acc: dict = {}
    for level, rows in (("org", p.orgs), ("atom", p.atoms)):
        if not len(rows):
            continue
        for t in ts:
            obs = {f: watch.flow_obs(p, f) for f in watch.FLOWS}
            scans = watch.scan_rows(p, rows, t, obs)
            for flow, s in scans.items():
                ok = np.isfinite(s["z"]) & np.isfinite(s["s"]) & (s["s"] > 0)
                z_log = s["z"][ok]
                z_add = (s["obs"][ok] - s["E"][ok]) / s["s"][ok]
                floor = watch.floor_of(s["base"])
                upper_log = np.maximum(count_model.positive_exp(s["center"] + s["zs"] * s["log_s"]), s["E"])
                upper_hyb = np.maximum(np.fmin(upper_log, s["E"] + s["zs"] * s["s"]), s["E"])
                extra = ((s["obs"] - upper_hyb >= floor) & ~(s["obs"] - upper_log >= floor) & ok).sum()
                a = acc.setdefault((level, flow), {"z_log": [], "z_add": [], "extra": 0})
                a["z_log"].append(z_log)
                a["z_add"].append(z_add)
                a["extra"] += int(extra)
    rows_out = []
    for (level, flow), a in acc.items():
        zl, za = np.concatenate(a["z_log"]), np.concatenate(a["z_add"])
        n = len(zl)

        def share(x, cond):
            return round(100. * float(cond(x).mean()), 2) if n else np.nan

        rows_out.append({
            "уровень": "орг." if level == "org" else "атом", "поток": flow, "точек": n,
            "лог >3, %": share(zl, lambda x: x > 3), "лог <−3, %": share(zl, lambda x: x < -3),
            "адд >3, %": share(za, lambda x: x > 3), "адд <−3, %": share(za, lambda x: x < -3),
            "лог >2, %": share(zl, lambda x: x > 2), "адд >2, %": share(za, lambda x: x > 2),
            "лишних отборов/мес": round(a["extra"] / len(ts), 1)})
    return {"months": [p.months[t] for t in ts], "table": pd.DataFrame(rows_out),
            "normal": {"> 3": 0.13, "> 2": 2.28}}


def slice_sources(connection) -> dict:
    """Готовность срезов шага 2 по последнему месяцу сводки: покрытие ЕПК, отрасли, холдинги.

    Холдинг и отрасль — текущий срез ЕПК, история принадлежности не хранится. Наружу
    уходят только счётчики, доли и названия отраслей; номера организаций и названия
    холдингов не выводятся. Полный запрос — агрегаты в БД, в pandas — десятки строк.
    """
    from . import db, sql as Q

    coverage = db.read_sql(connection, Q.SLICE_COVERAGE)
    industry = db.read_sql(connection, Q.SLICE_INDUSTRY)
    holding = db.read_sql(connection, Q.SLICE_HOLDING_SIZE)
    c = coverage.iloc[0]
    pf = float(c["pf"]) if c["pf"] else np.nan

    def share(v):
        return round(100. * float(v) / pf, 1) if pf and np.isfinite(pf) else np.nan

    summary = pd.DataFrame([
        {"Показатель": "Месяц", "Значение": str(c["report_dt"])[:7]},
        {"Показатель": "Организаций с портфелем", "Значение": int(c["n_org"])},
        {"Показатель": "Получателей (тройки)", "Значение": int(c["pf"])},
        {"Показатель": "Получатели у организаций, найденных в ЕПК, %", "Значение": share(c["pf_in_epk"])},
        {"Показатель": "Получатели с указанной отраслью, %", "Значение": share(c["pf_with_ind"])},
        {"Показатель": "Получатели с указанным холдингом, %", "Значение": share(c["pf_with_hold"])},
        {"Показатель": "Организаций с холдингом", "Значение": int(c["n_org_with_hold"])},
        {"Показатель": "Отраслей", "Значение": int(c["n_ind"])},
        {"Показатель": "Холдингов", "Значение": int(c["n_hold"])}])
    industry = industry.rename(columns={"industry": "Отрасль", "n_org": "Организаций", "pf": "Получателей"})
    industry["Доля, %"] = [share(v) for v in industry["Получателей"]]
    holding = holding.rename(columns={"size_class": "Размер холдинга", "n_hold": "Холдингов",
                                      "n_org": "Организаций", "n_hold_multi_org": "Из них ≥2 организаций",
                                      "pf": "Получателей"})
    holding["Доля, %"] = [share(v) for v in holding["Получателей"]]
    return {"summary": summary, "industry": industry, "holding": holding}

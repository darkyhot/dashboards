"""Короткая диагностика на уже загруженной панели, без БД и полного стенда."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import bench, watch
from .inputs import Panel


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
        ("месяц", p.months[t]), ("id орг", p.meta.at[r, "org_id"]),
        ("людей в закладке", ev.n),
        ("источник", watch.where(p, bench.Card("atom", src, "out_other", 0))),
        ("приёмник", watch.where(p, bench.Card("atom", ev.row2, "in_other", 0))),
        ("портфель организации в прошлом месяце", p.x["pf_recip"][r, t - 1]),
        ("портфель атома-источника в прошлом месяце", p.x["pf_recip"][src, t - 1]),
        ("портфель атома-источника после закладки", q.x["pf_recip"][src, t]),
        ("минимум портфеля источника с месяца закладки", float(np.nanmin(q.x["pf_recip"][src, t:]))),
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
            score = abs(s["d"][i]) - s["zs"] * s["s"][i]
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

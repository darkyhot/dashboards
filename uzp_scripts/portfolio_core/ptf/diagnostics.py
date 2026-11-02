"""Короткая диагностика на уже загруженной панели, без БД и полного стенда."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import bench, watch
from .inputs import Panel


def recode_event(p: Panel, seed: int = 7, months_back: int = 6,
                 sizes=(3.0, 4.0, 6.0), per_cell: int = 20,
                 kinds=bench.EVENT_KINDS) -> bench.Event:
    """Повторить только жеребьёвку evaluate, чтобы получить тот же случай смены ГОСБ.
    Параметры должны совпадать с прошедшим стендом. Детектор здесь не вызывается."""
    rng = np.random.default_rng(seed)
    ts = list(range(max(13, len(p.months) - months_back), len(p.months)))
    if not ts or len(p.orgs) < 2:
        raise ValueError("Для случая смены ГОСБ нужны минимум 14 месяцев и две организации")
    for kind in kinds:
        for k in sizes if kind != "stop" else (0.0,):
            for _ in range(per_cell):
                t = int(rng.choice(ts))
                bench.pick_events(p, rng, kind, k, t, 1)
    for _ in range(per_cell):
        t = int(rng.choice(ts[1:])) if len(ts) > 1 else ts[0]
        bench.pick_events(p, rng, "dip", 4.0, t - 1, 1)
    rng.choice(p.meta.loc[p.atoms, "sys_tb_id"].unique())  # выбор ТБ для недогруза
    t = ts[-1]
    o = p.orgs
    r = int(o[np.nanargmax(np.nan_to_num(p.x["pf_recip"][o, t - 1]))])
    same = p.atoms[(p.meta.loc[p.atoms, "sys_tb_id"] == p.meta.at[r, "sys_tb_id"]).to_numpy()]
    dst = int(rng.choice(same[same != p.meta.at[r, "atom"]])) if len(same) > 1 else r
    n = int(0.5 * np.nan_to_num(p.x["pf_recip"][r, t - 1]))
    return bench.Event("recode", t, n, row=r, row2=dst)


def recode_report(p: Panel, **params) -> dict[str, pd.DataFrame]:
    """Тот же recode, что в evaluate: два запуска детектора вместо полного стенда.
    Вывод: размер закладки, ограничения источника, отбор потоков организации,
    её итоговая карточка и именно те карточки, которые счётчик считает лишними."""
    ev = recode_event(p, **params)
    t, r = ev.t, ev.row
    src = int(p.meta.at[r, "atom"])
    if src < 0 or p.meta.at[ev.row2, "lvl"] != "atom":
        raise ValueError("Стенд выбрал некорректный атом для смены ГОСБ: "
                         f"источник={src}, приёмник={ev.row2}. Нужен разбор закладки.")
    q = bench.inject(p, ev)
    before = watch.detect(p, t)
    after = watch.detect(q, t)
    was = {(c.level, c.key) for c in before}
    extra = [c for c in after if c.label not in bench.ARTIFACTS and (c.level, c.key) not in was]
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
    # Оценка потенциального объяснения — даже если организация не прошла отбор.
    # Она выводится для диагностики, а не используется для изменения карточек.
    candidate = bench.Card("org", r, "out_other", -a, label="recode",
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

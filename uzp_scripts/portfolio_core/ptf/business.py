"""Численный бизнес-разбор готового поколения без статистического фильтра."""
from __future__ import annotations

import html
from pathlib import Path

import numpy as np
import pandas as pd

from .inputs import Panel, IN_M, OUT_M
from .presentation import mask, org_code

TOL = .01  # тот же допуск сумм долевых ФЛ, что у сборки
FLOWS = [("m_in_from_outside", 1, "Пришли извне"),
         ("m_out_left_bank", -1, "Ушли из банка"),
         ("m_in_from_other_org", 1, "Пришли из других организаций"),
         ("m_out_to_other_org", -1, "Ушли в другие организации"),
         ("m_in_codes", 1, "Пришли с других кодов"),
         ("m_out_codes", -1, "Ушли на другие коды")]
REQUIRED = ["pf_recip", "pf_fl", "m_base_pf", *IN_M, *OUT_M,
            "m_fl_in", "m_fl_out", "m_fl_conc"]


def _source(p, rows):
    """Пустая строка вне своего периода — 0; пропуск внутри строки — неизвестно."""
    raw = {m: p.x[m][rows].copy() for m in REQUIRED}
    present = np.logical_or.reduce([~np.isnan(v) for v in raw.values()])
    required = present.copy()
    # Исчезновение строки с положительным остатком не считаем уходом без данных.
    required[:, 1:] |= np.nan_to_num(raw["pf_recip"][:, :-1]) > TOL
    absent_month = ~present.any(axis=0)
    required[:, absent_month] = True
    values = {m: np.where(required, v, 0.) for m, v in raw.items()}
    invalid = np.zeros_like(required)
    for m, v in values.items():
        v[~np.isfinite(v)] = np.nan
        bad = np.isfinite(v) & (v < -TOL) if m != "m_fl_conc" else np.zeros_like(required)
        invalid |= bad
        v[bad] = np.nan
    return values, invalid


def _total(v):
    return float(np.sum(v)) if len(v) else np.nan


def _slice_frame(meta, previous, current, base, incoming, outgoing, name, column):
    frame = pd.DataFrame({name: meta[column].to_numpy(), "Было": previous, "Стало": current,
                          "Приход": incoming, "Уход": outgoing, "Различие баз": base - previous})
    # Обычный groupby.sum скрывает неизвестные значения. Здесь они распространяются.
    frame = frame.groupby(name, dropna=False, sort=False).agg(lambda s: s.sum(skipna=False)).reset_index()
    frame["Вклад (тройки)"] = frame["Стало"] - frame["Было"]
    return frame.sort_values("Вклад (тройки)", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)


def analyze(p: Panel, month: str | None = None) -> dict:
    """Выбирает крупнейшее изменение банка/его частей или разбирает заданный месяц.

    Суммы банка строятся только по атомам. Причина изменения из арифметического
    тождества не выводится. Неизвестные значения и ошибки не превращаются в нули.
    """
    if len(p.months) < 2 or not len(p.atoms):
        raise ValueError("Для бизнес-разбора нужны атомы и минимум два месяца")
    periods = pd.PeriodIndex(p.months, freq="M")
    if periods.has_duplicates or not periods.is_monotonic_increasing:
        raise ValueError("Месяцы панели должны идти по возрастанию без повторений")
    x, invalid = _source(p, p.atoms)
    ox = _source(p, p.orgs)[0] if len(p.orgs) else None
    incoming = sum(x[m] for m in IN_M)
    outgoing = sum(x[m] for m in OUT_M)
    net = incoming - outgoing
    identity = x["pf_recip"] - x["m_base_pf"] - net
    records = []
    for t in range(1, len(p.months)):
        adjacent = periods[t].ordinal - periods[t - 1].ordinal == 1
        delta = _total(x["pf_recip"][:, t]) - _total(x["pf_recip"][:, t - 1]) if adjacent else np.nan
        fl_delta = _total(x["pf_fl"][:, t]) - _total(x["pf_fl"][:, t - 1]) if adjacent else np.nan
        residual = delta - _total(net[:, t])
        atom_volume = .5 * _total(np.abs(x["pf_recip"][:, t] - x["pf_recip"][:, t - 1])) if adjacent else np.nan
        org_volume = (.5 * _total(np.abs(ox["pf_recip"][:, t] - ox["pf_recip"][:, t - 1]))
                      if adjacent and ox is not None else np.nan)
        volumes = [v for v in (abs(delta), atom_volume, org_volume) if np.isfinite(v)]
        fl_residual = fl_delta - _total(x["m_fl_in"][:, t] - x["m_fl_out"][:, t] + x["m_fl_conc"][:, t])
        bad = int(np.sum(np.abs(identity[:, t]) > TOL))
        complete = bool(np.isfinite(identity[:, t]).all() and np.isfinite(residual)
                        and abs(residual) <= TOL and bad == 0 and not invalid[:, t].any())
        records.append({"Месяц": p.months[t], "Изменение (тройки)": delta,
                        "Объём изменений": max(volumes) if volumes else np.nan,
                        "Изменение (доли ФЛ)": fl_delta, "Невязка (тройки)": residual,
                        "Невязка (доли ФЛ)": fl_residual, "Тройки проверены": complete,
                        "Ошибок тождества в атомах": bad,
                        "ФЛ проверены": bool(adjacent and np.isfinite(fl_residual)
                                             and abs(fl_residual) <= TOL and not invalid[:, t].any())})
    history = pd.DataFrame(records)
    if month is None:
        candidates = history.loc[history["Объём изменений"].notna()]
        if candidates.empty:
            raise ValueError("Нет полного сравнения соседних месяцев; проверьте заполнение остатков")
        chosen = candidates["Объём изменений"].idxmax()
        month = history.at[chosen, "Месяц"]
        selection = "Автоматически выбран месяц крупнейшего изменения банка или встречных изменений его частей."
    else:
        matches = [m for m in p.months[1:] if m[:7] == str(month)[:7]]
        if not matches:
            raise ValueError("Выбранный месяц отсутствует или не имеет предыдущего в панели")
        month = matches[0]
        selection = "Выбранный пользователем месяц; самостоятельный поиск показан отдельно в истории."
    t = p.months.index(month)
    if periods[t].ordinal - periods[t - 1].ordinal != 1:
        raise ValueError("Для выбранного месяца нет соседнего предыдущего месяца")
    h = history.loc[history["Месяц"] == month].iloc[0]
    start, end = _total(x["pf_recip"][:, t - 1]), _total(x["pf_recip"][:, t])
    old_fl, new_fl = _total(x["pf_fl"][:, t - 1]), _total(x["pf_fl"][:, t])
    summary = pd.DataFrame([
        {"Мера": "Получатели (тройки)", "Было": start, "Стало": end, "Изменение": end - start,
         "Невязка": h["Невязка (тройки)"], "Проверено": bool(h["Тройки проверены"])},
        {"Мера": "ФЛ (доли)", "Было": old_fl, "Стало": new_fl, "Изменение": new_fl - old_fl,
         "Невязка": h["Невязка (доли ФЛ)"], "Проверено": bool(h["ФЛ проверены"])}])
    movements = pd.DataFrame([{"Движение": label, "Вклад (тройки)": sign * _total(x[m][:, t])}
                              for m, sign, label in FLOWS]
                            + [{"Движение": "Невязка расчёта", "Вклад (тройки)": h["Невязка (тройки)"]}])
    drivers = pd.DataFrame([{"Составляющая": label, "Вклад (тройки)": _total(x[a][:, t] - x[b][:, t])}
        for a, b, label in (("m_in_from_outside", "m_out_left_bank", "Внешние движения троек"),
                            ("m_in_from_other_org", "m_out_to_other_org", "Внутренние движения троек"),
                            ("m_in_codes", "m_out_codes", "Изменение кодов"))]
        + [{"Составляющая": "Невязка расчёта", "Вклад (тройки)": h["Невязка (тройки)"]}])
    fractional = pd.DataFrame([{"Движение": label, "Вклад (доли ФЛ)": sign * _total(x[m][:, t])}
        for m, sign, label in (("m_fl_in", 1, "Приход людей"), ("m_fl_out", -1, "Уход людей"),
                              ("m_fl_conc", 1, "Изменение долей совместительства"))]
        + [{"Движение": "Невязка расчёта", "Вклад (доли ФЛ)": h["Невязка (доли ФЛ)"]}])
    slices = {key: _slice_frame(p.meta.loc[p.atoms], x["pf_recip"][:, t - 1],
                               x["pf_recip"][:, t], x["m_base_pf"][:, t],
                               incoming[:, t], outgoing[:, t], label, col)
              for key, label, col in (("segments", "Сегмент", "seg_rep"), ("tb", "ТБ", "sys_tb_id"),
                                     ("gosb", "ГОСБ", "sys_gosb_id"))}
    issues = []
    if not h["Тройки проверены"]:
        issues.append("Тождество получателей не подтверждено; численный разбор требует проверки данных.")
    if not h["ФЛ проверены"]:
        issues.append("Тождество долевых ФЛ банка не подтверждено.")
    for m, v in x.items():
        count = int(np.sum(~np.isfinite(v[:, t])))
        if count:
            issues.append(f"{m}: неизвестно значений в атомах {count}.")
    if invalid[:, t].any():
        issues.append(f"Отрицательные исходные счётчики: атомов {int(invalid[:, t].sum())}.")
    base_gap = _total(x["m_base_pf"][:, t]) - start
    if not np.isfinite(base_gap) or abs(base_gap) > TOL:
        issues.append("База поколения отличается от предыдущего остатка; это не установленная бизнес-причина.")
    organizations = pd.DataFrame(columns=["Организация", "Вклад (тройки)"])
    if len(p.orgs):
        organizations = pd.DataFrame({"Организация": [org_code(r) for r in p.orgs],
            "Вклад (тройки)": ox["pf_recip"][:, t] - ox["pf_recip"][:, t - 1]})
        organizations = organizations.sort_values("Вклад (тройки)", key=lambda s: s.abs(), ascending=False)
        missing_orgs = int(organizations["Вклад (тройки)"].isna().sum())
        if missing_orgs:
            issues.append(f"Нельзя оценить вклад крупных организаций: {missing_orgs}.")
    return mask({"month": month, "previous_month": p.months[t - 1], "run_id": p.run_id,
                 "selection": selection, "summary": summary, "movements": movements, "drivers": drivers,
                 "fractional": fractional, "slices": slices, "organizations": organizations,
                 "history": history, "base_gap": base_gap,
                 "bank_change": end - start,
                 "issues": issues,
                 "notes": ["Суммируются только атомы; организации не добавляются к итогу банка.",
                           "Тройки и доли ФЛ — разные единицы. Движения не доказывают причину изменения.",
                           "Спорная коррекция возвратов сторожа не используется."]})


def compact(frame, value="Вклад (тройки)", limit=6, total=None):
    """Крупнейшие непересекающиеся вклады и весь остаток, включая неизвестные."""
    frame = frame.copy()
    selected = frame.iloc[:limit]
    rest = frame.iloc[limit:]
    if total is None and rest.empty:
        return selected.reset_index(drop=True)
    record = {c: np.nan for c in frame.columns}
    record[frame.columns[0]] = f"Остальные ({len(rest)})" if total is None else "Остальные, включая малые"
    for c in frame.columns[1:]:
        record[c] = rest[c].sum(skipna=False)
    if total is not None:
        record[value] = total - selected[value].sum(skipna=False)
    return pd.concat([selected, pd.DataFrame([record])], ignore_index=True)


def write_html(report: dict, path) -> Path:
    """Полный обезличенный разбор: все срезы остаются внутри контура."""
    report = mask(report)
    path = Path(path)
    body = ["<!doctype html><html lang='ru'><meta charset='utf-8'>",
            "<title>Разбор портфеля</title><style>body{font:16px sans-serif;margin:24px}"
            "table{border-collapse:collapse;margin-bottom:24px}td,th{padding:6px 12px;border:1px solid #ccc}"
            ".scroll{overflow:auto}p{max-width:1000px}</style>",
            f"<h1>Портфель: {html.escape(report['month'][:7])}</h1>",
            f"<p>Поколение: {html.escape(str(report['run_id']))}. {html.escape(report['selection'])}</p>"]
    for note in [*report["issues"], *report["notes"]]:
        body.append(f"<p>{html.escape(note)}</p>")
    frames = [("Изменение банка", report["summary"]), ("Чистые вклады", report["drivers"]),
              ("Вклады движений", report["movements"]),
              ("Изменение долей ФЛ", report["fractional"]), ("История", report["history"]),
              ("Сегменты", report["slices"]["segments"]), ("ТБ", report["slices"]["tb"]),
              ("ГОСБ", report["slices"]["gosb"]), ("Крупные организации", report["organizations"])]
    for title, frame in frames:
        body += [f"<h2>{title}</h2>", '<div class="scroll">',
                 frame.to_html(index=False, escape=True, border=0, na_rep="неизвестно"), "</div>"]
    body += ["</html>"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(body), encoding="utf-8")
    return path

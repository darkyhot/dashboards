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


def _prepare(p: Panel):
    """Атомы и организации с правилами пустых значений и помесячная история тождеств."""
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
    return periods, x, invalid, ox, incoming, outgoing, net, identity, history


def analyze(p: Panel, month: str | None = None) -> dict:
    """Выбирает крупнейшее изменение банка/его частей или разбирает заданный месяц.

    Суммы банка строятся только по атомам. Причина изменения из арифметического
    тождества не выводится. Неизвестные значения и ошибки не превращаются в нули.
    """
    periods, x, invalid, ox, incoming, outgoing, net, identity, history = _prepare(p)
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


# --------------------------------------------------------------------------- #
# Год к году: что в этом месяце иначе, чем в том же месяце прошлого года
# --------------------------------------------------------------------------- #
# Сезонные изменения (январский уход разовых и премий и т.п.) повторяются каждый год.
# Бизнесу интересно отличие: изменение месяца минус изменение того же месяца год назад.
REASONS = [("m_lb_one_time", -1, "Ушли из банка: были разовыми в прошлом месяце"),
           ("m_lb_seasonal", -1, "Ушли из банка: сезонно, как год назад"),
           ("m_lb_org_stopped", -1, "Ушли из банка: организация перестала платить"),
           ("m_lb_unknown", -1, "Ушли из банка: причина не размечена")]
MARKERS = [("m_out_temp_left", -1, "Ушли из банка и вернулись через месяц"),
           ("m_in_temp_outside", 1, "Пришли извне после месячного пропуска")]
STOCK_MARKERS = [("single_pay_recip", "Одна выплата за месяц"),
                 ("one_time_pf", "Разовые в портфеле"), ("multi_recip", "Совместители (тройки)")]
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _known_total(p, measure, t):
    """Сумма меры по активным атомам; пропуск у активного атома — неизвестно, не ноль."""
    if measure not in p.x:
        return np.nan
    rows = p.atoms
    active = np.isfinite(p.x["pf_recip"][rows, t]) | np.isfinite(p.x["m_base_pf"][rows, t])
    v = p.x[measure][rows, t]
    if not active.any() or (active & ~np.isfinite(v)).any():
        return np.nan
    return float(v[active].sum())


def _comparison(rows, this_year, last_year, label_col):
    frame = pd.DataFrame(rows)
    frame["Сверх прошлого года"] = frame[this_year] - frame[last_year]
    return frame[[label_col, this_year, last_year, "Сверх прошлого года"]]


def yoy(p: Panel, month: str | None = None) -> dict:
    """Изменение месяца против изменения того же месяца прошлого года.

    Сверх прошлого года = (остаток M − остаток M−1) − (остаток M−12 − остаток M−13).
    Автоматический выбор — максимум |сверх прошлого года| по банку. Отличие раскладывается по шести потокам (точное
    тождество при пройденных проверках), причинам ухода и признакам календаря.
    Признаки и причины — наблюдаемые свойства, а не доказанная причина отличия.
    """
    periods, x, invalid, ox, incoming, outgoing, net, identity, history = _prepare(p)
    hist = history.set_index("Месяц")
    ordinal = {periods[t].ordinal: t for t in range(len(p.months))}

    def pair(t):
        """(t, t−1, t−12, t−13) только при подряд идущих месяцах."""
        o = periods[t].ordinal
        idx = [ordinal.get(o - k) for k in (0, 1, 12, 13)]
        return None if any(i is None for i in idx) else idx

    def stock_delta(values, a, b):
        return values[:, a] - values[:, b]

    records = []
    for t in range(len(p.months)):
        idx = pair(t)
        if idx is None:
            continue
        t0, t1, t12, t13 = idx
        this_d = hist.at[p.months[t0], "Изменение (тройки)"]
        last_d = hist.at[p.months[t12], "Изменение (тройки)"]
        atom_excess = stock_delta(x["pf_recip"], t0, t1) - stock_delta(x["pf_recip"], t12, t13)
        org_excess = (stock_delta(ox["pf_recip"], t0, t1) - stock_delta(ox["pf_recip"], t12, t13)
                      if ox is not None else np.array([]))
        volumes = [v for v in (abs(this_d - last_d), .5 * _total(np.abs(atom_excess)),
                               .5 * _total(np.abs(org_excess)) if len(org_excess) else np.nan)
                   if np.isfinite(v)]
        records.append({
            "Месяц": p.months[t0], "Этот год": this_d, "Прошлый год": last_d,
            "Сверх прошлого года": this_d - last_d,
            "Объём отличий": max(volumes) if volumes else np.nan,
            "ФЛ: сверх прошлого года": (hist.at[p.months[t0], "Изменение (доли ФЛ)"]
                                        - hist.at[p.months[t12], "Изменение (доли ФЛ)"]),
            "Проверено": bool(hist.at[p.months[t0], "Тройки проверены"]
                              and hist.at[p.months[t12], "Тройки проверены"])})
    comparisons = pd.DataFrame(records)
    if comparisons.empty:
        raise ValueError("Для сравнения год к году нужны месяцы M, M−1, M−12 и M−13 подряд")
    if month is None:
        # Только банк: сумма модулей по атомам растёт вместе с сезонным объёмом месяца
        # и снова выбирала бы январь. Встречные отличия частей — справочно в истории.
        candidates = comparisons.loc[comparisons["Сверх прошлого года"].notna()]
        if candidates.empty:
            raise ValueError("Нет полного сравнения год к году; проверьте заполнение остатков")
        month = comparisons.at[candidates["Сверх прошлого года"].abs().idxmax(), "Месяц"]
        selection = ("Автоматически выбран месяц, где изменение банка сильнее всего отличается "
                     "от того же месяца прошлого года.")
    else:
        matches = [m for m in comparisons["Месяц"] if m[:7] == str(month)[:7]]
        if not matches:
            raise ValueError("Для выбранного месяца нет сравнения год к году (нужны M−1, M−12, M−13)")
        month = matches[0]
        selection = "Выбранный пользователем месяц; самостоятельный поиск показан в истории."
    t0, t1, t12, t13 = pair(p.months.index(month))
    this_label = f"{p.months[t0][:7]} к {p.months[t1][:7]}"
    last_label = f"{p.months[t12][:7]} к {p.months[t13][:7]}"

    def bank(measure, t, values=x):
        return _total(values[measure][:, t])

    summary = []
    for measure, label, verified in (("pf_recip", "Получатели (тройки)", "Тройки проверены"),
                                     ("pf_fl", "ФЛ (доли)", "ФЛ проверены")):
        this_d = bank(measure, t0) - bank(measure, t1)
        last_d = bank(measure, t12) - bank(measure, t13)
        summary.append({"Мера": label, "Этот год": this_d, "Прошлый год": last_d,
                        "Сверх прошлого года": this_d - last_d,
                        "Проверено": bool(hist.at[p.months[t0], verified] and hist.at[p.months[t12], verified])})
    summary = pd.DataFrame(summary)

    flows = [{"Движение": label, "Этот год": sign * bank(m, t0), "Прошлый год": sign * bank(m, t12)}
             for m, sign, label in FLOWS]
    # Невязка включает и различие месячной базы с прошлым остатком: строки складываются в итог.
    flows.append({"Движение": "Невязка расчёта", "Этот год": hist.at[p.months[t0], "Невязка (тройки)"],
                  "Прошлый год": hist.at[p.months[t12], "Невязка (тройки)"]})
    flows = _comparison(flows, "Этот год", "Прошлый год", "Движение")

    reasons = [{"Признак": label, "Этот год": sign * _known_total(p, m, t0),
                "Прошлый год": sign * _known_total(p, m, t12)} for m, sign, label in REASONS + MARKERS]
    reasons += [{"Признак": f"{label}: изменение остатка",
                 "Этот год": _known_total(p, m, t0) - _known_total(p, m, t1),
                 "Прошлый год": _known_total(p, m, t12) - _known_total(p, m, t13)} for m, label in STOCK_MARKERS]
    reasons = _comparison(reasons, "Этот год", "Прошлый год", "Признак")

    meta = p.meta.loc[p.atoms]
    slices = {}
    for key, label, col in (("segments", "Сегмент", "seg_rep"), ("tb", "ТБ", "sys_tb_id"),
                            ("gosb", "ГОСБ", "sys_gosb_id")):
        frame = pd.DataFrame({label: meta[col].to_numpy(),
                              "Этот год": stock_delta(x["pf_recip"], t0, t1),
                              "Прошлый год": stock_delta(x["pf_recip"], t12, t13)})
        frame = frame.groupby(label, dropna=False, sort=False).agg(lambda s: s.sum(skipna=False)).reset_index()
        frame["Сверх прошлого года"] = frame["Этот год"] - frame["Прошлый год"]
        slices[key] = frame.sort_values("Сверх прошлого года", key=lambda s: s.abs(),
                                        ascending=False).reset_index(drop=True)
    organizations = pd.DataFrame(columns=["Организация", "Этот год", "Прошлый год", "Сверх прошлого года"])
    if ox is not None:
        organizations = pd.DataFrame({"Организация": [org_code(r) for r in p.orgs],
                                      "Этот год": stock_delta(ox["pf_recip"], t0, t1),
                                      "Прошлый год": stock_delta(ox["pf_recip"], t12, t13)})
        organizations["Сверх прошлого года"] = organizations["Этот год"] - organizations["Прошлый год"]
        organizations = organizations.sort_values("Сверх прошлого года", key=lambda s: s.abs(),
                                                  ascending=False).reset_index(drop=True)

    issues = []
    for t, name in ((t0, "этого года"), (t12, "прошлого года")):
        if not hist.at[p.months[t], "Тройки проверены"]:
            issues.append(f"Тождество получателей {name} ({p.months[t][:7]}) не подтверждено.")
        if not hist.at[p.months[t], "ФЛ проверены"]:
            issues.append(f"Тождество долевых ФЛ {name} ({p.months[t][:7]}) не подтверждено.")
    unknown = reasons.loc[reasons[["Этот год", "Прошлый год"]].isna().any(axis=1), "Признак"].tolist()
    if unknown:
        issues.append("Неизвестно (пустые меры, не ноль): " + "; ".join(unknown) + ".")
    if t0 == len(p.months) - 1:
        issues.append("Последний месяц: возврат в следующем месяце ещё не наблюдался.")
    first_days = [pd.Timestamp(p.months[t]).replace(day=1) for t in (t0, t12)]
    calendar = "; ".join(f"1-е число {d:%m.%Y} — {WEEKDAYS[d.weekday()]}" for d in first_days)
    explained = flows.loc[flows["Движение"] != "Невязка расчёта", "Сверх прошлого года"]
    return mask({
        "month": month, "previous_month": p.months[t1], "last_year_month": p.months[t12],
        "run_id": p.run_id, "selection": selection, "this_label": this_label, "last_label": last_label,
        "calendar": calendar, "summary": summary, "flows": flows, "reasons": reasons,
        "slices": slices, "organizations": organizations, "history": comparisons,
        "excess": float(summary.at[0, "Сверх прошлого года"]),
        "flows_total": float(explained.sum(skipna=False)), "issues": issues,
        "notes": ["Сверх прошлого года = изменение этого месяца − изменение того же месяца год назад.",
                  "Повторяющийся каждый год сезон взаимно сокращается; остаётся то, что изменилось.",
                  "Причины ухода и календарные признаки — наблюдения, а не доказанная причина.",
                  "Тройки и доли ФЛ — разные единицы. Суммируются только атомы."]})


def write_yoy_html(report: dict, path) -> Path:
    """Полный обезличенный разбор год к году: все срезы и вся история."""
    report = mask(report)
    path = Path(path)
    body = ["<!doctype html><html lang='ru'><meta charset='utf-8'>",
            "<title>Портфель год к году</title><style>body{font:16px sans-serif;margin:24px}"
            "table{border-collapse:collapse;margin-bottom:24px}td,th{padding:6px 12px;border:1px solid #ccc}"
            ".scroll{overflow:auto}p{max-width:1000px}</style>",
            f"<h1>Портфель: {html.escape(report['this_label'])} против {html.escape(report['last_label'])}</h1>",
            f"<p>Поколение: {html.escape(str(report['run_id']))}. {html.escape(report['selection'])}</p>",
            f"<p>{html.escape(report['calendar'])}</p>"]
    for note in [*report["issues"], *report["notes"]]:
        body.append(f"<p>{html.escape(note)}</p>")
    frames = [("Изменение банка", report["summary"]), ("Движения", report["flows"]),
              ("Причины ухода и признаки", report["reasons"]), ("Сегменты", report["slices"]["segments"]),
              ("ТБ", report["slices"]["tb"]), ("ГОСБ", report["slices"]["gosb"]),
              ("Крупные организации", report["organizations"]), ("Все месяцы", report["history"])]
    for title, frame in frames:
        body += [f"<h2>{title}</h2>", '<div class="scroll">',
                 frame.to_html(index=False, escape=True, border=0, na_rep="неизвестно"), "</div>"]
    body += ["</html>"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(body), encoding="utf-8")
    return path

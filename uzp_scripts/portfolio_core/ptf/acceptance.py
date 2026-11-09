"""Один запуск приёмки: метрики, карточки и причины провалов в HTML и JSON без сети."""
from __future__ import annotations

import hashlib
import html
import json
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from . import bench, diagnostics, presentation, watch
from .inputs import Panel


def _code_signature(code):
    """Семантика функции без номеров строк, с рекурсивным учётом вложенного кода."""
    return (code.co_code, tuple(_code_signature(v) if hasattr(v, "co_code") else v
                               for v in code.co_consts), code.co_names, code.co_varnames,
            code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount,
            code.co_freevars, code.co_cellvars, code.co_flags, getattr(code, "co_exceptiontable", b""))


def _default_signature(value):
    if isinstance(value, float) and not np.isfinite(value):
        return ("float", str(value))
    if isinstance(value, (tuple, list)):
        return tuple(_default_signature(v) for v in value)
    if isinstance(value, dict):
        return {k: _default_signature(v) for k, v in value.items()}
    return value


def method_signature():
    """Код модели и обычных закладок до/после reload. Артефакты можно исправлять
    отдельно, но изменение отбора или событий запрещает повторное использование."""
    from . import count_model, inputs
    signatures = {}
    for module in (watch, count_model, inputs, bench):
        for name, value in vars(module).items():
            if module is bench and name in ("_inject_artifact", "_cell_changes", "evaluate"):
                continue
            if name.isupper():
                try:
                    signatures[(module.__name__, name)] = json.dumps(_plain(value), sort_keys=True)
                except TypeError:
                    if isinstance(value, (set, frozenset)):
                        signatures[(module.__name__, name)] = tuple(sorted(value))
            if getattr(value, "__module__", None) != module.__name__:
                continue
            members = vars(value).items() if isinstance(value, type) else [("", value)]
            for member, function in members:
                function = function.fget if isinstance(function, property) else function
                if hasattr(function, "__code__"):
                    signatures[(module.__name__, name, member)] = (
                        _code_signature(function.__code__), _default_signature(function.__defaults__),
                        _default_signature(function.__kwdefaults__))
    return signatures


def recover_artifact_failure(previous_signature):
    """Прочитать завершённые этапы только из последней ошибки данного стенда.
    Объекты остаются в памяти текущего ядра; файлы и данные наружу не передаются.
    Старый аварийный результат не считается завершённой приёмкой.
    """
    if previous_signature != method_signature():
        return None
    traceback = getattr(sys, "last_traceback", None)
    if traceback is None:
        traceback = getattr(getattr(sys, "last_exc", None), "__traceback__", None)
    params = {}
    while traceback is not None:
        frame = traceback.tb_frame
        local = frame.f_locals
        if (frame.f_code.co_name == "run"
                and Path(frame.f_code.co_filename).resolve() == Path(__file__).resolve()):
            params = dict(local.get("params", {}))
        if (frame.f_code.co_name == "evaluate"
                and Path(frame.f_code.co_filename).resolve() == Path(bench.__file__).resolve()
                and local.get("name") in ("calendar", "underload", "recode")
                and isinstance(local.get("recall"), pd.DataFrame)
                and not local.get("invalid")):
            plan = local["plan"]
            count = sum(2 if e.kind == "reorg" else 1 for e in plan["trials"])
            if len(local["recall"]) != count or len(local.get("echo", [])) != len(plan["echoes"]):
                return None
            p = local["p"]
            if local["detect"].__module__ != watch.__name__:
                return None
            return {"panel": p, "params": params, "known_clean": local["clean_is_known"],
                    "completed": {"plan": plan,
                    "backgrounds": local["backgrounds"], "recall": local["recall"],
                    "clean_cards": local["clean"], "echo_values": local["echo"]}}
        traceback = traceback.tb_next
    return None


def _plain(value):
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, pd.DataFrame):
        return _plain(value.to_dict("records"))
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_plain(v) for v in value]
    if isinstance(value, np.generic):
        return _plain(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def fingerprint() -> dict[str, str]:
    folder = Path(__file__).parent
    return {name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
            for name in ("inputs.py", "bench.py", "watch.py", "count_model.py", "diagnostics.py", "acceptance.py", "presentation.py")}


def quality(p: Panel) -> pd.DataFrame:
    active = np.isfinite(p.x["m_base_pf"])
    records = []
    for name, x in p.x.items():
        relevant = active.copy()
        if name in ("m_out_temp", "m_out_temp_left"):
            relevant[:, -1] = False  # нет следующего месяца — не дефект заполнения
        records.append({"мера": name, "сравнимых ячеек": int(relevant.sum()),
                        "пустых среди сравнимых": int((relevant & ~np.isfinite(x)).sum()),
                        "отрицательных": int((np.isfinite(x) & (x < -1e-6)).sum())})
    return pd.DataFrame(records)


def reason_partition(p: Panel) -> pd.DataFrame:
    """Полнота исходной разметки причин ухода."""
    records = []
    for level, rows in (("atom", p.atoms), ("org", p.orgs)):
        total = p.x["m_out_left_bank"][rows]
        parts = np.array([p.x[m][rows] for m in
                          ("m_lb_unknown", "m_lb_seasonal", "m_lb_org_stopped", "m_lb_one_time")])
        complete = (np.all(np.isfinite(parts) & (parts >= 0), axis=0)
                    & np.isclose(parts.sum(axis=0), total, rtol=1e-8, atol=1e-6))
        for t, month in enumerate(p.months):
            active = np.isfinite(total[:, t])
            records.append({"месяц": month, "уровень": level, "строк с потоком": int(active.sum()),
                            "полностью размечены причины": int((active & complete[:, t]).sum())})
    return pd.DataFrame(records)


def _checks(result):
    return pd.DataFrame([{"проверка": name, "пройдена": bool(ok), "результат": detail}
                         for name, ok, detail in bench.verdict(result)])


def _event_of(plan, row):
    """Закладка, давшая цель recall; у реорганизации цель — любая из двух сторон."""
    t, target, kind, n = int(row["t"]), int(row["row"]), row["kind"], int(row["n"])
    return next((ev for ev in plan["trials"] if ev.t == t and ev.kind == kind
                 and ev.n == n and target in (ev.row, ev.row2)), None)


def _target_scan(p, ev, target, cache):
    """Нужный ряд до и после закладки — тем же первичным отбором, что у сторожа.

    «Запас» — watch.excess: превышение факта над прогнозной границей (положительная
    модель). Ряд проходит первичный отбор, если запас не меньше минимума. Скан «до»
    зависит только от месяца и уровня, поэтому кэшируется между целями.
    """
    lvl = p.meta.at[target, "lvl"]
    rows = p.orgs if lvl == "org" else p.atoms
    i = int(np.flatnonzero(rows == target)[0])
    if (ev.t, lvl) not in cache:
        obs = {f: watch.flow_obs(p, f) for f in watch.FLOWS}
        cache[(ev.t, lvl)] = watch.scan_rows(p, rows, ev.t, obs)
    q = bench.inject(p, ev)
    obs = {f: watch.flow_obs(q, f) for f in watch.FLOWS}
    after = watch.scan_rows(q, rows, ev.t, obs, details=True)
    records = []
    for when, scans in (("до", cache[(ev.t, lvl)]), ("после", after)):
        picked = {rr: f for rr, f, *_ in watch.pick(scans, rows)}.get(target)
        for f, s in scans.items():
            records.append({"когда": when, "поток": f, "факт": s["obs"][i], "ожидание": s["E"][i],
                            "граница": s["upper"][i] if "upper" in s else np.nan,
                            "шум": s["s"][i], "порог z": s["zs"], "сверх шума": watch.excess(s)[i],
                            "минимум": watch.floor_of(s["base"][i]), "выбран": picked == f})
    return {"table": pd.DataFrame(records).round(2),
            "calibration": {f: s["calibration"] for f, s in after.items()},
            "where": watch.where(p, bench.Card(lvl, target, "net", 0))}


def _missed_details(p, result, limit, select=None):
    r = result["recall"]
    if r.empty or limit <= 0:
        return []
    missed = r.loc[~r["found"].astype(bool) & (True if select is None else select)]
    missed = missed.sort_values("n", ascending=False)
    reports, seen, cache = [], set(), {}
    for _, row in missed.iterrows():
        key = (int(row["t"]), int(row["row"]), row["kind"], int(row["n"]))
        if key in seen:
            continue
        seen.add(key)
        ev = _event_of(result["plan"], row)
        if ev is None:
            continue
        target = int(row["row"])
        scan = _target_scan(p, ev, target, cache)
        reports.append({"event": ev, "target": target, "size_sigma": row["effective_k"],
                        "measure": row["measure"], **scan})
        if len(reports) >= limit:
            break
    return reports


def saved_result(p, data):
    """Цели и план стенда без повторного расчёта: recall из отчёта, план — той же
    детерминированной жеребьёвкой. Отчёт обязан относиться к этой панели."""
    header = data["input"]
    if (header["months"] != p.months or header["atoms"] != len(p.atoms)
            or header["organizations"] != len(p.orgs)
            or (header.get("run_id") is not None and p.run_id is not None and str(header["run_id"]) != str(p.run_id))):
        raise ValueError("Панель и отчёт относятся к разным поколениям. Нужен panel того же запуска приёмки.")
    recall = pd.DataFrame(data["recall"])
    plan = bench.make_plan(p, **(data.get("params") or {}))
    lost = [i for i, row in recall.iterrows() if _event_of(plan, row) is None]
    if lost:
        raise ValueError(f"План стенда не восстановился: {len(lost)} целей без закладки. "
                         "Нужен res_acceptance в памяти того же ядра.")
    return {"recall": recall, "plan": plan}


def weak_misses(p, res, progress=print) -> dict:
    """Разбор целей «около трёх шумов, ≥ 300 чел.» — ровно тех, что считает приёмка.

    Обзор по всем целям группы и подробная таблица по каждому пропуску. Стенд и сборка
    не запускаются: результат берётся из res_acceptance в памяти, а после перезапуска
    ядра — из сохранённого отчёта и восстановленного плана.
    """
    result = res.get("result") or saved_result(p, res["data"])
    r = result["recall"]
    _, mid = bench.acceptance_groups(r)
    targets = r.loc[mid].sort_values(["found", "n"], ascending=[True, False])
    overview, details, cache = [], [], {}
    for number, (_, row) in enumerate(targets.iterrows(), start=1):
        if progress:
            progress(f"Слабые цели: {number}/{len(targets)}")
        ev = _event_of(result["plan"], row)
        target = int(row["row"])
        scan = _target_scan(p, ev, target, cache)
        flow = bench.FLOW_ALIAS[row["measure"]]
        table = scan["table"]
        after = table.loc[(table["когда"] == "после") & (table["поток"] == flow)].iloc[0]
        calibration = scan["calibration"][flow]
        found = bool(row["found"])
        level = row.get("level")
        overview.append({
            "№": number, "тип": row["kind"], "месяц": p.months[int(row["t"])][:7],
            "ряд": "орг." if p.meta.at[target, "lvl"] == "org" else "атом",
            "людей": int(row["n"]), "шумов стенда": round(float(row["effective_k"]), 2),
            "найдено": level if found else ("нет" if level is None or pd.isna(level) else f"нет ({level})"),
            "факт": after["факт"], "ожидание": after["ожидание"], "граница": after["граница"],
            "запас": after["сверх шума"], "минимум": after["минимум"],
            "отбор": bool(np.isfinite(after["сверх шума"]) and after["сверх шума"] >= after["минимум"]),
            "выбран поток": bool(after["выбран"]),
            "масштаб": round(float(calibration.get("scale", np.nan)), 2),
            "порог z": round(float(calibration.get("critical_z", np.nan)), 2)})
        if not found:
            details.append({"event": ev, "target": target, "size_sigma": row["effective_k"],
                            "measure": row["measure"], **scan})
    return presentation.mask({"overview": pd.DataFrame(overview), "details": details,
                              "months": p.months}, p)


def _table(frame):
    if frame.empty:
        return "<p>Нет строк.</p>"
    return '<div class="scroll">' + frame.to_html(index=False, escape=True, border=0) + "</div>"


def _write_html(path, data):
    data = presentation.mask(data)
    check = data["checks"]
    passed = bool(check["пройдена"].all())
    body = ["<h1>Приёмка сторожа</h1>",
            f'<p class="status">{"Критерии выполнены" if passed else "Есть невыполненные критерии"}</p>',
            f'<p>Поколение {html.escape(str(data["input"]["run_id"]))}; '
            f'{html.escape(data["input"]["months"][0])} — {html.escape(data["input"]["months"][-1])}. '
            f'Протокол {data["protocol"]}. Время: {data["seconds"]:.1f} с.</p>',
            "<p>Протокол 3 проверяет появление или усиление сигнала нужного потока в той же совокупности относительно исходного фона; размер когорты ограничен её собственным остатком. "
            "Результаты прежнего протокола напрямую с ним не сравниваются. Шум измеряется по изменяемому потоку; "
            "если сработал минимум размера, показан фактический размер в шумах.</p>",
            "<h2>Проверки</h2>", _table(check),
            f'<p>Карточек на исходном фоне в среднем: {data["background_mean"]:.1f}. '
            + ("Фон заведомо чистый." if data["known_clean"] else
               "На реальных данных это число включает настоящие события и не является числом ложных тревог.") + "</p>",
            "<h2>Полнота по типу и размеру</h2>", _table(data["summary"]),
            "<h2>Карточки за последние месяцы</h2>", _table(data["cards"])]
    if data.get("baseline_checks") is not None:
        body += ["<details><summary>Соперник на тех же событиях</summary>",
                 _table(data["baseline_checks"]), _table(data["baseline_summary"]), "</details>"]
    body += ["<h2>Артефакты</h2>"]
    for name, item in data["artifacts"].items():
        body += [f"<h3>{html.escape(name)}</h3>",
                 "<pre>" + html.escape(json.dumps(_plain(item["event"]), ensure_ascii=False, indent=2)) + "</pre>",
                 _table(item["extra"])]
    for name, frame in data["recode_diagnostics"].items():
        body += [f"<details><summary>{html.escape(name)}</summary>", _table(frame), "</details>"]
    body += ["<h2>Разбор пропущенных событий</h2>"]
    for item in data["missed_details"]:
        ev = item["event"]
        body += ["<details><summary>" + html.escape(
            f'{ev["kind"]}, {data["input"]["months"][ev["t"]]}, {ev["n"]} человек, {item["where"]}') + "</summary>",
            _table(item["table"])]
        calibration = item.get("calibration", {}).get(presentation.target_flow(item))
        if calibration:
            body += ["<p>Источники исторического порога:</p>",
                     _table(pd.DataFrame(calibration["contributors"]))]
        body.append("</details>")
    for title, frame in (("Ошибки закладки", data["invalid"]), ("Пропущенные попытки закладки", data["skipped"]),
                         ("Заполнение мер", data["quality"]),
                         ("Полнота разметки причин ухода", data["reason_partition"]), ("Все цели испытаний", data["recall"]),
                         ("Дополнительные потоки карточек", data["secondary_flows"])):
        body += [f"<details><summary>{html.escape(title)}</summary>", _table(frame), "</details>"]
    body += ["<details><summary>Версии файлов</summary><pre>",
             html.escape(json.dumps(data["files"], ensure_ascii=False, indent=2)), "</pre></details>",
             "<p>Факт в карточке — исходный поток. Ожидание включает учтённые возвраты и общий календарный "
             "компонент. Отклонение вклада в портфель — разница с ожиданием со знаком потока. "
             "Карточки разных уровней, а также полный атом и его «прочие организации», "
             "могут пересекаться; их нельзя суммировать.</p>"]
    document = '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Приёмка сторожа</title>'
    document += "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:1500px;margin:32px auto;padding:0 24px;color:#182b32}" 
    document += "h1,h2{color:#174e45}.status{font-weight:bold}.scroll{overflow:auto;margin:12px 0}table{border-collapse:collapse;font-size:13px}"
    document += "td,th{padding:7px 10px;border-bottom:1px solid #dce4e5;text-align:left;white-space:nowrap}th{background:#edf4f2}"
    document += "details{margin:16px 0}summary{cursor:pointer;font-weight:bold}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><body>"
    path.write_text(document + "".join(body) + "</body></html>", encoding="utf-8")


def run(p: Panel, out_dir="output/watch_acceptance", compare=True, known_clean=False,
        max_details=12, progress=print, completed=None, **params) -> dict:
    """Сохранить весь результат одним запуском. Подложка и настройки БД не меняются."""
    start = time.monotonic()
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    plan = bench.make_plan(p, **params) if completed is None else completed["plan"]
    if progress:
        progress(f"Приёмка: {len(plan['trials'])} событий; пропущено попыток {len(plan['skipped'])}")
    result = bench.evaluate(watch.detect, p, plan=plan, clean_is_known=known_clean,
                            progress=progress, completed=completed)
    frames, secondary = [], []
    for t in plan["ts"]:
        cards = result["backgrounds"][t]
        frames.append(watch.cards_frame(p, cards, t))
        for c in cards:
            for f, values in c.info.get("flows", {}).items():
                secondary.append({"месяц": p.months[t], "где": watch.where(p, c), "поток": f, **values})
    artifacts = {}
    for name, item in result["artifact_details"].items():
        q = bench.inject(p, item["event"])
        artifacts[name] = {"event": item["event"], "extra": pd.concat(
            [watch.cards_frame(q, frame["extra"], frame["t"]) for frame in item["frames"]], ignore_index=True)}
    recode = result["artifact_details"].get("recode")
    recode_details = diagnostics.recode_report(
        p, event=recode["event"], before=recode["frames"][0]["before"], after=recode["frames"][0]["after"]
    ) if recode else {}
    data = {"protocol": 3, "model": "positive_count_and_cell_remainder", "input": {"run_id": p.run_id, "months": p.months,
             "atoms": len(p.atoms), "organizations": len(p.orgs)}, "params": params,
            "files": fingerprint(), "known_clean": known_clean, "checks": _checks(result),
            "summary": bench.summary(result), "recall": result["recall"],
            "cards": pd.concat(frames, ignore_index=True), "secondary_flows": pd.DataFrame(secondary),
            "background_mean": float(np.mean(result["clean_cards"])), "echo": result["echo"],
            "leak": result["leak"], "quality": quality(p), "artifacts": artifacts,
            "reason_partition": reason_partition(p),
            "recode_diagnostics": recode_details, "missed_details": _missed_details(p, result, max_details),
            "invalid": result["invalid"], "skipped": result["skipped"],
            "recovered_stages": ["background", "events", "echo"] if completed is not None else []}
    data = presentation.mask(data, p)
    # Сохраняем результат сторожа до сравнения: уже есть полный файл, даже если
    # последующий долгий расчёт соперника будет остановлен пользователем.
    paths = {"html": folder / "report.html", "json": folder / "report.json"}

    def save():
        data["seconds"] = round(time.monotonic() - start, 2)
        _write_html(paths["html"], data)
        paths["json"].write_text(json.dumps(_plain(data), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")

    save()
    if compare:
        if progress:
            progress("Сторож: отчёт сохранён. Сравнение с соперником на том же наборе.")
        rival = bench.evaluate(bench.baseline, p, plan=plan, clean_is_known=known_clean, progress=progress)
        data["baseline_checks"] = _checks(rival)
        data["baseline_summary"] = bench.summary(rival)
        save()
    if progress:
        progress(f"Готово: {paths['html']} и {paths['json']}")
    return {"data": data, "paths": paths, "result": result}

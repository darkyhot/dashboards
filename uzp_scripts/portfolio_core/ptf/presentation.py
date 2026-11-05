"""Обезличенный вывод отчёта небольшими страницами для фотографирования."""
from __future__ import annotations

import re
from dataclasses import asdict, is_dataclass

import pandas as pd


def org_code(row) -> str:
    """Код строки панели; исходный номер организации в него не входит."""
    return f"Орг. R{int(row):05d}"


class OrgAliases:
    def __init__(self, panel=None):
        self.labels = {}
        if panel is not None:
            for row in panel.orgs:
                self.labels[self._key(panel.meta.at[row, "org_id"])] = org_code(row)

    @staticmethod
    def _key(value):
        return re.sub(r"\.0$", "", str(value).strip())

    def label(self, value):
        if value is None or pd.isna(value):
            return "—"
        key = self._key(value)
        if re.fullmatch(r"Орг\. [RA]\d+", key):
            return key
        if key not in self.labels:
            self.labels[key] = f"Орг. A{len(self.labels) + 1:05d}"
        return self.labels[key]

    def text(self, value):
        # Поддерживается и старый отчёт, где номер встроен в описание места.
        return re.sub(r"\bорганизация\s+([0-9xX*]+)(?=\s|\(|$)",
                      lambda m: self.label(m[1]), value, flags=re.IGNORECASE)


def mask(data, panel=None):
    """Копия для показа/записи. Работает также с уже рассчитанным старым отчётом.

    Числовые меры и служебные индексы строк сохраняются. Заменяются только поля
    номера организации и номера внутри подписей «организация …».
    """
    aliases = OrgAliases(panel)

    def identifier(key):
        key = str(key).lower()
        return key == "id орг" or key == "org_id" or key.endswith("_org_id")

    def visit(value):
        if is_dataclass(value):
            return visit(asdict(value))
        if isinstance(value, pd.DataFrame):
            frame = value.copy(deep=True)
            for col in frame:
                frame[col] = frame[col].map(aliases.label if identifier(col) else visit)
            if {"показатель", "значение"}.issubset(frame.columns):
                rows = frame["показатель"].map(identifier)
                frame.loc[rows, "значение"] = frame.loc[rows, "значение"].map(aliases.label)
            return frame
        if isinstance(value, dict):
            result = {k: aliases.label(v) if identifier(k) else visit(v) for k, v in value.items()}
            if identifier(value.get("показатель", "")) and "значение" in result:
                result["значение"] = aliases.label(value["значение"])
            return result
        if isinstance(value, (list, tuple)):
            return type(value)(visit(v) for v in value)
        return aliases.text(value) if isinstance(value, str) else value

    return visit(data)


def _frame(value):
    return value.copy() if isinstance(value, pd.DataFrame) else pd.DataFrame(value)


def _cards(frame, start):
    if frame.empty:
        return [("Карточек нет", frame)]
    frame = frame.copy()
    frame.insert(0, "№", range(start + 1, start + len(frame) + 1))
    frame["где"] = frame["где"].str.replace(" (главная ячейка: ", " / ", regex=False).str.rstrip(")")
    frame["что"] = frame["что"].str.replace(": больше ожидания", " ↑", regex=False).str.replace(
        ": меньше ожидания", " ↓", regex=False)
    first = frame[["№", "где", "что", "объяснение"]].rename(
        columns={"где": "Место", "что": "Поток", "объяснение": "Пояснение"})
    second = frame[["№", "факт", "ожидание", "отклонение вклада в портфель", "сверх шума, не меньше"]].rename(
        columns={"факт": "Факт", "ожидание": "Ожидание", "отклонение вклада в портфель": "Δ вклада",
                 "сверх шума, не меньше": "Сверх шума"})
    return [("Места и потоки", first), ("Числа тех же карточек", second)]


def target_flow(item):
    from .bench import EVENT_MEASURE, FLOW_ALIAS

    if "measure" in item:
        return FLOW_ALIAS[item["measure"]]
    ev = item["event"]
    if ev["kind"] == "reorg" and item["target"] == ev["row2"]:
        return "in_other"
    return FLOW_ALIAS[EVENT_MEASURE[ev["kind"]]]


def photo_page(data, section="итог", page=1, month=None, panel=None):
    """Одна страница, без БД и детектора. Принимает DataFrame или загруженный JSON."""
    from .bench import ACCEPT

    if not isinstance(page, int) or page < 1:
        raise ValueError("Номер страницы — целое число от 1")
    data = mask(data, panel)
    blocks, notes = [], []
    pages, title = 1, section
    if section == "итог":
        records = []
        rival = {r["проверка"]: r for r in _frame(data.get("baseline_checks", [])).to_dict("records")}
        for row in _frame(data["checks"]).to_dict("records"):
            name = row["проверка"]
            if name.startswith("покрытие типа"):
                continue
            label, need = name, ""
            if name.startswith("полнота: ≥ 4"):
                label, need = "Крупные ≥4 шума, ≥2000", f"≥{ACCEPT['recall_4']:.0%}"
            elif name.startswith("полнота: 3"):
                label, need = "Три шума, ≥300", f"≥{ACCEPT['recall_3']:.0%}"
            elif name == "эхо провала":
                need = f"≤{ACCEPT['echo_max']:.0%}"
            elif "ложных карточек" in name:
                need = f"≤{ACCEPT['clean_max']}"
            elif name.startswith("артефакт"):
                label, need = name.split("«")[1].split("»")[0], "0 лишних"
            elif name.startswith("закладки без"):
                label, need = "Ошибки закладки", "0"
            def result(check):
                if not check:
                    return "—"
                return ("✓ " if check["пройдена"] else "× ") + check["результат"].split(" (все размеры:")[0]
            records.append({"Проверка": label, "Нужно": need, "Сторож": result(row),
                            "Соперник": result(rival.get(name))})
        blocks = [("Приёмка", pd.DataFrame(records))]
        notes.append(f"Карточек фона в среднем: {data['background_mean']:.1f}/мес.")
        if not data["known_clean"]:
            notes.append("Фон содержит реальные события; это не число ложных тревог.")
    elif section in ("карточки", "календарь"):
        if section == "карточки":
            frame = _frame(data["cards"])
            if not frame.empty:
                chosen = month or frame["месяц"].max()
                if chosen != "все":
                    frame = frame.loc[frame["месяц"] == chosen]
                title = f"Карточки: {chosen}"
        else:
            frame = _frame(data["artifacts"].get("calendar", {}).get("extra", []))
            title = "Лишние карточки календарного сдвига"
        pages = max(1, (len(frame) + 4) // 5)
        start = (page - 1) * 5
        blocks = _cards(frame.iloc[start:start + 5], start)
        notes.append(f"Показано {len(frame.iloc[start:start + 5])} из {len(frame)}; по 5 карточек на странице.")
        notes.append("↑ / ↓ — больше / меньше ожидания по данному потоку.")
    elif section == "пропуски":
        details = data.get("missed_details", [])
        pages = max(1, len(details))
        if 1 <= page <= len(details):
            item = details[page - 1]
            ev = item["event"]
            flow = target_flow(item)
            table = _frame(item["table"])
            table = table.loc[table["поток"] == flow]
            title = f"{ev['kind']}: {ev['n']} человек; {item['size_sigma']:.2f} шума"
            notes += [data["input"]["months"][ev["t"]] + "; " + item["where"],
                      "Граница должна быть не меньше минимума, чтобы пройти первичный отбор."]
            blocks = [("Нужный поток до и после закладки", table.drop(columns=["поток"]).rename(
                columns={"когда": "Когда", "факт": "Факт", "ожидание": "Ожидание", "шум": "Шум",
                         "порог z": "Порог z", "сверх шума": "Граница", "минимум": "Минимум", "выбран": "Выбран"}))]
            calibration = item.get("calibration", {}).get(flow, {})
            if calibration:
                blocks.append(("Источники исторического порога", _frame(calibration.get("contributors", []))))
                notes.append(f"Поправка на число проверок: {calibration['normal_z']:.2f}; "
                             f"исторический порог: {calibration['empirical_z']:.2f}.")
            else:
                notes.append("В этом отчёте источники исторического порога ещё не записаны.")
            notes.append(f"Сохранено разборов: {len(details)}. Это выборка крупнейших пропусков.")
    elif section == "бизнес":
        from .business import compact

        report = data["business"]
        issues = report["issues"]
        pages = 6 + max(1, (len(issues) + 7) // 8)
        title = f"Бизнес-разбор: {report['month'][:7]}"
        notes = [report["selection"]]
        if issues:
            notes.append("Есть ограничения данных: раздел проверки на странице 7.")
        if page == 1:
            blocks = [("Изменение банка", _frame(report["summary"])),
                      ("Из чего сложилось изменение получателей", _frame(report["drivers"]))]
            notes.append("Вклады движений не доказывают календарную или другую бизнес-причину.")
        elif page == 2:
            history = _frame(report["history"]).sort_values("Объём изменений", ascending=False).head(6)
            blocks = [("Люди и совместительство", _frame(report["fractional"])),
                      ("Крупнейшие изменения за всё поколение", history[["Месяц", "Объём изменений", "Изменение (тройки)",
                          "Изменение (доли ФЛ)", "Тройки проверены", "ФЛ проверены"]])]
        elif page in (3, 4, 5):
            key, label = {3: ("segments", "Сегменты"), 4: ("tb", "ТБ"), 5: ("gosb", "ГОСБ")}[page]
            frame = _frame(report["slices"][key])
            blocks = [(label, compact(frame))]
            notes.append(f"Крупнейшие вклады и весь остаток; всего срезов {len(frame)}.")
            notes.append("Вклад = изменение остатков; различие баз выделено отдельно от движений.")
        elif page == 6:
            blocks = [("Организации", compact(_frame(report["organizations"]),
                                              total=report["bank_change"]))]
            notes.append("Итоги организаций по всем территориям; главная ячейка не используется.")
            notes.append("Остаток включает все непоказанные организации, в том числе малые.")
        else:
            start = (page - 7) * 8
            blocks = [("Исходные потоки", _frame(report["movements"])),
                      ("Ограничения", pd.DataFrame({"Сообщение": issues[start:start + 8]}))]
            notes += report["notes"]
    elif section == "кейс-август":
        case = data.get("calendar_case")
        if case is None:
            raise ValueError("Разбор C01 ещё не рассчитан в ячейке просмотра")
        issues = _frame(case["availability"])
        pages = 2 + max(1, (len(issues) + 7) // 8)
        title = f"C01: июль–сентябрь {case['year']}; банк"
        notes = [case["calendar"], *case["notes"]]
        if page == 1:
            blocks = [("Портфель", _frame(case["stocks"])),
                      ("Изменение к тому же месяцу прошлого года", _frame(case["stock_changes"]))]
        elif page == 2:
            blocks = [("Календарные признаки", _frame(case["indicators"])),
                      ("Изменение к тому же месяцу прошлого года", _frame(case["indicator_changes"]))]
        else:
            start = (page - 3) * 8
            blocks = [("Отрицательные потоки после коррекции: число строк", _frame(case["normalization"])),
                      ("Недоступные значения", issues.iloc[start:start + 8])]
            notes.append(f"Недоступные значения: показано {len(issues.iloc[start:start + 8])} из {len(issues)}.")
    elif section == "пороги":
        snapshot = data.get("threshold_snapshot")
        if snapshot is None:
            raise ValueError("Разбор порогов ещё не рассчитан в ячейке просмотра")
        pages = 1 + len(snapshot["sources"])
        title = f"Пороги: {snapshot['month']}, {snapshot['level']}"
        if snapshot.get("legacy_replay"):
            notes.append("Воспроизведение старого отчёта: прежняя коррекция возвратов, только диагностика.")
        notes.append("Новая модель оценивает log(1 + поток); старый и новый z имеют разные шкалы. "
                     "Качество сравнивается по найденным событиям и ложным карточкам.")
        if page == 1:
            if "comparison" in snapshot:
                blocks = [("Проверка правки калибровки", _frame(snapshot["comparison"]))]
                remaining = _frame(snapshot.get("remaining", []))
                if not remaining.empty:
                    blocks.append(("Наибольший оставшийся исторический порог", remaining))
                notes += ["Не для фона — крупные отклонения с ожиданием меньше 5 или без достаточной истории.",
                          "Нет оценки — строки без достаточных данных для собственного статистического сигнала."]
                if snapshot.get("legacy_replay"):
                    notes.append("Проверяются порог и достаточность истории; возвраты пока по прежним данным. Это не полная приёмка.")
            else:
                blocks = [("Пороги потоков", _frame(snapshot["summary"]))]
        elif page <= pages:
            source = snapshot["sources"][page - 2]
            blocks = [(f"{source['flow']}: источники порога", _frame(source["contributors"]))]
            notes.append(f"Поправка на число проверок: {source['normal']:.2f}; "
                         f"итоговый порог: {source['threshold']:.2f}.")
            notes.append("Крупные прошлые отклонения показаны для проверки. В новой модели "
                         "порог задаёт масштаб центра ошибок, а не отдельная строка хвоста."
                         if source.get("model") == "positive_count" else
                         "Первые три строки хвоста и строка, определяющая исторический порог.")
        notes.append("Условные коды строк не содержат исходных номеров организаций.")
    elif section == "качество":
        frame = _frame(data["reason_partition"])
        if not frame.empty:
            frame = frame.loc[frame["месяц"] >= data["input"]["months"][-6]]
        pages = max(1, (len(frame) + 7) // 8)
        blocks = [("Разметка причин: последние шесть месяцев", frame.iloc[(page - 1) * 8:page * 8])]
    else:
        raise ValueError("Раздел: бизнес, итог, карточки, пропуски, календарь, кейс-август, пороги или качество")
    if page > pages:
        raise ValueError(f"Страница должна быть от 1 до {pages}")
    return {"title": title, "page": page, "pages": pages, "blocks": blocks, "notes": notes}


def show(data, display, section="итог", page=1, month=None, panel=None):
    view = photo_page(data, section, page, month, panel)
    print(f"{view['title']} — страница {view['page']}/{view['pages']}")
    for note in view["notes"]:
        print(note)
    with pd.option_context("display.max_rows", 12, "display.max_columns", None, "display.max_colwidth", 100):
        for title, frame in view["blocks"]:
            print(title)
            display(frame.reset_index(drop=True).round(2))

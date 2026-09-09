"""Самопроверки. Запускаются из тетрадки ПЕРЕД прогоном, без обращения к БД.

Лежат внутри пакета намеренно: отдельный каталог `tests/` на проме перехватывает
первый попавшийся одноимённый пакет из окружения, и тетрадка падает на импорте.

Проверяется не «работает ли код», а то, из-за чего РЕЗУЛЬТАТУ НЕЛЬЗЯ БЫЛО БЫ
ВЕРИТЬ: разъехавшийся словарь сегментов, порядок правил классификатора,
подглядывание прогноза в будущее, несходящиеся суммы, диалект SQL.

Запуск: `from rgs import selfcheck; selfcheck.run_all()` или `python -m rgs.selfcheck`.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from . import agency as AG
from . import analyze as A
from . import forecast as F
from . import narrative as N
from . import queries as LQ


class CheckFailed(AssertionError):
    pass


def _ok(name: str) -> None:
    print(f"  ✓ {name}")


def _all_sql() -> str:
    """Весь SQL модуля одной строкой — по нему идут проверки диалекта."""
    return "\n".join(v for k, v in vars(LQ).items()
                     if isinstance(v, str) and k.isupper())


# --------------------------------------------------------------------------- #
def check_sql_dialect() -> None:
    """SQL обязан быть валиден для ядра PostgreSQL 9.4.

    `make_interval(months => n)` — самая частая ловушка: локально на 16-й версии
    запрос работает, а на проме падает, потому что 9.4 разбирает «=>» как оператор.
    """
    sql = _all_sql()
    if re.search(r"make_interval\s*\(", sql):
        raise CheckFailed("в SQL есть make_interval — на ядре 9.4 не работает; "
                          "сдвиг на месяцы пишется как n * interval '1 month'")
    if "=>" in sql:
        raise CheckFailed("в SQL есть «=>» — ядро 9.4 разбирает это как оператор")
    _ok("SQL: нет конструкций новее ядра 9.4")


def check_sql_braces() -> None:
    """Фигурные скобки в SQL обязаны быть экранированы.

    `read_sql` прогоняет текст через `str.format()`. Незакрытый `{2}` из регулярки
    роняет запрос с «IndexError: Replacement index 2 out of range» — по такому
    сообщению про SQL не догадаешься. Проверяем прямо форматированием.
    """
    for name, sql in vars(LQ).items():
        if not (isinstance(sql, str) and name.isupper()):
            continue
        try:
            sql.format(schema="s", schema_t="t")
        except (IndexError, KeyError) as ex:
            raise CheckFailed(
                f"{name}: SQL не переживает str.format() ({type(ex).__name__}: {ex}). "
                f"Экранируйте фигурные скобки: {{2}} вместо {2}") from ex
    _ok("SQL: фигурные скобки экранированы")


def check_no_lookahead() -> None:
    """В запросах не должно быть колонок, смотрящих в будущее.

    Прогноз, прочитавший `next_m_fl_val`, показал бы блестящую точность и не
    значил бы ровно ничего.
    """
    sql = _all_sql()
    hits = [c for c in LQ.FORBIDDEN_COLUMNS if re.search(rf"\b{c}\b", sql)]
    if hits:
        raise CheckFailed(f"запросы читают будущее: {', '.join(hits)} — "
                          f"это не прогноз, а чтение ответа")
    _ok("прогноз: запросы не читают колонки будущего")


def check_segment_dictionary() -> None:
    """Словари сегментов дэша и отчёта не разъехались.

    Три витрины называют бюджетную сферу по-разному. Если словарь дэша изменится,
    отчёт обязан упасть здесь, а не показать пустой раздел на проме.
    """
    if LQ.SEG_SHORT != "РГС":
        raise CheckFailed(f"короткий код сегмента стал '{LQ.SEG_SHORT}' — "
                          f"проверьте uzp_dash...segments.SHORT")
    from uzp_dash.dashboards.tb_health import segments as S
    if S.short_of_big(LQ.SEG_BIG) != LQ.SEG_SHORT:
        raise CheckFailed(f"'{LQ.SEG_BIG}' больше не соответствует '{LQ.SEG_SHORT}'")
    if ":seg" not in LQ.RGS_FACT:
        raise CheckFailed("основной запрос перестал фильтровать сегмент")
    _ok("сегменты: словари дэша и отчёта совпадают")


def check_agency_rules() -> None:
    """Классификатор ведомств: порядок правил и границы слов.

    Каждый случай здесь — ошибка, которая уже возможна и которую глазами
    в отчёте не поймать: она просто переносит отток в чужое ведомство.
    """
    cases = {
        # порядок правил: силовые и юстиция ДО органов власти
        "УПРАВЛЕНИЕ МВД № 3": "Силовые ведомства",
        "УПРАВЛЕНИЕ ФССП № 7": "Суды и юстиция",
        "УПРАВЛЕНИЕ РОСГВАРДИИ № 2": "Силовые ведомства",
        "УПРАВЛЕНИЕ ОБРАЗОВАНИЯ АДМИНИСТРАЦИИ": "Органы власти",
        # границы слов: подстрока не считается признаком
        "СУДОРЕМОНТНЫЙ ЗАВОД": AG.UNKNOWN,
        "ДКБ ХОЛДИНГ": AG.UNKNOWN,
        # обычные случаи
        "МБОУ СОШ № 12": "Образование",
        "ГБУЗ ЦРБ № 4": "Здравоохранение",
        "МУП ВОДОКАНАЛ № 1": "ЖКХ и благоустройство",
        "МБУК ДОМ КУЛЬТУРЫ № 2": "Культура",
        "МБУ ДЮСШ № 5": "Спорт",
        "ГБУ КЦСОН № 10": "Социальная защита",
        "УФНС № 9": "Налоги и казначейство",
        # нормализация: регистр, «ё», кавычки
        'мбоу "гимназия" № 1': "Образование",
        "": AG.UNKNOWN,
    }
    for name, expect in cases.items():
        got = AG.classify(name)
        if got != expect:
            raise CheckFailed(f"«{name}» → «{got}», ожидалось «{expect}»")

    if AG.classify("ФКУ № 12", is_force=True) != "Силовые ведомства":
        raise CheckFailed("флаг силовой организации должен перевешивать имя")
    if AG.classify("ФКУ № 12", industry_name="Органы гос. и мун. управления") \
            != "Органы власти":
        raise CheckFailed("подсказка по отрасли не сработала")
    if AG.classify("МБОУ СОШ № 1", industry_name="Органы гос. и мун. управления") \
            != "Образование":
        raise CheckFailed("подсказка по отрасли не должна перебивать разбор имени")
    _ok(f"ведомства: {len(cases) + 3} случая, порядок правил соблюдён")


def check_agency_frame() -> None:
    """Отсутствующие колонки не должны ронять классификацию.

    У отсутствующей колонки `df.get()` возвращает скаляр nan — на этом код уже
    падал, причём не здесь, а страницей ниже.
    """
    df = pd.DataFrame({"company_name": ["МБОУ СОШ № 1", None, "ООО Ромашка"]})
    got = AG.classify_frame(df)          # ни is_force, ни industry_name нет
    if list(got) != ["Образование", AG.UNKNOWN, AG.UNKNOWN]:
        raise CheckFailed(f"классификация без флагов сломалась: {list(got)}")
    known, total, share = AG.coverage(got)
    if (known, total) != (1, 3):
        raise CheckFailed(f"покрытие посчитано неверно: {known} из {total}")
    _ok("ведомства: работают без колонок флагов, покрытие считается")


def check_causes_additive() -> None:
    """Раскладка причин обязана быть аддитивной: части = падение.

    Иначе суммы по ведомствам и регионам не сойдутся с итогом, а расхождение
    будет выглядеть как «немного разные цифры в разных таблицах».
    """
    rng = np.random.default_rng(7)
    n, months = 60, 8
    ym = pd.date_range("2025-11-30", periods=months, freq="ME")
    rows = []
    for i in range(n):
        fl, emp = float(rng.integers(10, 400)), 0.0
        emp = fl * float(rng.uniform(1.1, 2.2))
        for j, m in enumerate(ym):
            # три ветки: штат идёт за получателями, штат стоит, роста нет
            if i % 3 == 0:
                fl *= 0.95; emp *= 0.95            # сокращение штата
            elif i % 3 == 1:
                fl *= 0.93                          # уход к конкуренту, штат стоит
            rows.append({"inn": 1000 + i, "ym": m, "fl": round(fl),
                         "emp": round(emp), "fot": fl * 50000, "pot": 0.0,
                         "new_fl": 0, "np": 0, "out_q": 0})
    panel = pd.DataFrame(rows)
    meta = pd.DataFrame({"inn": panel["inn"].unique()})
    meta["agency"] = ["Образование" if i % 2 else "Культура"
                      for i in range(len(meta))]

    org, by_ag, chk = A.causes(panel, meta, ym[-1], window=6)
    if org.empty:
        raise CheckFailed("раскладка причин ничего не вернула на фикстуре")
    if not chk["ok"]:
        raise CheckFailed(f"части не равны падению: невязка {chk['residual']:+.4f}")
    if abs(by_ag["drop"].sum() - org["drop"].sum()) > 0.5:
        raise CheckFailed("сумма по ведомствам не равна итогу падения")
    seen = set(org["cause"])
    for need in (A.CAUSE_STAFF, A.CAUSE_COMPETITOR):
        if need not in seen:
            raise CheckFailed(f"ветка «{need}» не встретилась на фикстуре — "
                              f"значит она не проверена")
    _ok(f"причины: аддитивны, ветки {sorted(seen)} покрыты")


def check_forecast_identity() -> None:
    """Прогноз обязан воспроизводить историю и сходиться в водопаде.

    Проверяется главное свойство расчётного прихода: среднее «приход − отток»
    равно среднему изменению численности. Если это перестанет выполняться,
    прогноз поедет мимо факта, а метрика останется приличной.
    """
    ym = pd.date_range("2025-01-31", periods=12, freq="ME")
    fl = np.linspace(1000, 880, len(ym))
    out = np.full(len(ym), 20.0)
    panel = pd.DataFrame({"inn": 1, "ym": ym, "fl": fl, "emp": fl * 1.5,
                          "fot": fl * 50000, "pot": 0.0,
                          "new_fl": 999, "np": 999, "out_q": out})   # витрина врёт
    fact = pd.DataFrame({"inn": 1, "report_dt": ym, "out_kept": out,
                         "out_qty": out, "ret_qty": 0.0, "agency": "Образование"})
    meta = pd.DataFrame({"inn": [1], "agency": ["Образование"]})

    s = F._series(panel, fact, meta)                      # noqa: SLF001
    obs = s["in_impl"].dropna() - s["out_kept"].iloc[1:]
    d_fl = s["d_fl"].dropna()
    if abs(float(obs.mean()) - float(d_fl.mean())) > 1e-6:
        raise CheckFailed("тождество «приход − отток = Δчисленности» нарушено")

    fc, end, diag = F.run(panel, fact, meta, ym[-1], "2026-06")
    if end.empty:
        raise CheckFailed("прогноз не построился на фикстуре")
    for c in diag["checks"]:
        if not c["ok"]:
            raise CheckFailed(f"{c['name']}: невязка {c['residual']:+.4f}")
    if set(end["scenario"]) != set(F.SCENARIOS):
        raise CheckFailed(f"не все сценарии посчитаны: {sorted(end['scenario'])}")
    # приход витрины (999) в прогнозе участвовать не должен
    if diag.get("inflow_mart_ratio") and abs(diag["inflow_mart_ratio"]) < 5:
        raise CheckFailed("витринный приход не отличили от расчётного — "
                          "проверка расхождения не работает")
    _ok("прогноз: тождество истории выполняется, водопад сходится")


def check_forecast_no_negative() -> None:
    """На длинном горизонте численность не должна уходить в минус."""
    ym = pd.date_range("2025-01-31", periods=8, freq="ME")
    fl = np.linspace(300, 100, len(ym))
    out = np.full(len(ym), 90.0)
    panel = pd.DataFrame({"inn": 1, "ym": ym, "fl": fl, "emp": fl * 2,
                          "fot": fl * 1000, "pot": 0.0, "new_fl": 0, "np": 0,
                          "out_q": out})
    fact = pd.DataFrame({"inn": 1, "report_dt": ym, "out_kept": out, "out_qty": out,
                         "ret_qty": 0.0, "agency": "Образование"})
    meta = pd.DataFrame({"inn": [1], "agency": ["Образование"]})
    fc, end, _ = F.run(panel, fact, meta, ym[-1], "2027-12")
    if fc.empty:
        raise CheckFailed("прогноз не построился")
    if (fc["fl_end"] < -0.5).any():
        raise CheckFailed("численность ушла в минус — отток не ограничен базой")
    _ok("прогноз: численность не уходит в минус на длинном горизонте")


def check_materiality() -> None:
    """Свёртка мелких строк не должна терять сумму и прятать «не классифицировано»."""
    df = pd.DataFrame({
        "agency": ["Образование", "Культура", AG.UNKNOWN, "Спорт", "Мелочь"],
        "out_kept": [900.0, 80.0, 3.0, 12.0, 5.0],
        "n_org": [10, 5, 1, 2, 1],
    })
    out, cut = A.materialize(df, "agency", "out_kept", keep_last=AG.UNKNOWN)
    if abs(out["out_kept"].sum() - df["out_kept"].sum()) > 1e-9:
        raise CheckFailed("свёртка потеряла часть оттока")
    if AG.UNKNOWN not in set(out["agency"]):
        raise CheckFailed("«не классифицировано» свернулось в «прочие» — "
                          "неполнота разбора оказалась спрятана")
    if cut["hidden"] == 0:
        raise CheckFailed("мелкие строки не свернулись, проверка бессмысленна")
    _ok("материальность: суммы сохраняются, неполнота разбора видна")


def check_anonymization() -> None:
    """Настоящие названия не должны попадать в промпт, а токены — в отчёт."""
    from uzp_dash.anonymize import Aliases
    al = Aliases()
    df = pd.DataFrame({"region_name": ["Республика Северная Осетия - Алания",
                                       "Ленинградская область"],
                       "out_kept": [10.0, 20.0]})
    masked = N.mask_frame(df, "region_name", "Регион", al)
    prompt = "Отток: " + "; ".join(masked["region_name"])
    leaks = N.check_masked(prompt, list(df["region_name"]))
    if leaks:
        raise CheckFailed(f"в промпт попали настоящие названия: {leaks}")
    restored = al.restore("Хуже всего Регион-01, лучше Регион-02.")
    if "Республика Северная Осетия - Алания" not in restored:
        raise CheckFailed("настоящие названия не вернулись в текст ответа")
    if "Регион-0" in restored:
        raise CheckFailed("в тексте остались токены — читателю они непонятны")
    # проверка самой проверки: незамаскированное имя обязано находиться
    if not N.check_masked("Отток в Ленинградская область вырос", 
                          ["Ленинградская область"]):
        raise CheckFailed("check_masked не ловит настоящее название")
    _ok("обезличивание: имена не уходят наружу и возвращаются в ответ")


def check_llm_fallback() -> None:
    """Сбой LLM обязан давать текст по правилам, а не пустой раздел."""
    def boom(prompt, temperature=0.2):
        raise RuntimeError("шлюз недоступен")

    nar = N.Narrator(complete=boom, max_calls=5)
    text, used = nar.section("тест", "промпт", "запасной текст")
    if not used or text != "запасной текст":
        raise CheckFailed("фолбэк не сработал при исключении в LLM")

    def empty(prompt, temperature=0.2):
        return "   "

    nar2 = N.Narrator(complete=empty, max_calls=5)
    _, used2 = nar2.section("тест", "промпт", "запасной текст")
    if not used2:
        raise CheckFailed("пустой ответ модели не ушёл в фолбэк")

    # защёлка: после серии отказов остальные разделы не тратят вызовы
    nar3 = N.Narrator(complete=boom, max_calls=10)
    for i in range(3):
        nar3.section(f"раздел {i}", "промпт", "запас")
    if not nar3.gateway_down:
        raise CheckFailed("защёлка недоступного шлюза не сработала")
    if nar3.calls > N.ZERO_STREAK_ABORT:
        raise CheckFailed(f"после защёлки потрачено {nar3.calls} вызовов — "
                          f"должно быть не больше {N.ZERO_STREAK_ABORT}")
    _ok("LLM: фолбэк на правила и защёлка недоступного шлюза работают")


def check_row_limit() -> None:
    """Лимит строк обязан кидать исключение, а не молча обрезать выборку."""
    from . import fetch
    df = pd.DataFrame({"a": range(10)})
    try:
        fetch.guard_rows(df, "тест", limit=5)
    except fetch.RowLimitError:
        _ok("лимит строк: выборка сверх лимита останавливает прогон")
        return
    raise CheckFailed("guard_rows пропустил выборку больше лимита")


def run_all() -> None:
    print("Самопроверки rgs_outflow:")
    for fn in (check_sql_dialect, check_sql_braces, check_no_lookahead,
               check_segment_dictionary, check_agency_rules, check_agency_frame,
               check_causes_additive, check_forecast_identity,
               check_forecast_no_negative, check_materiality,
               check_anonymization, check_llm_fallback, check_row_limit):
        fn()
    print("Все проверки пройдены.")


if __name__ == "__main__":
    run_all()

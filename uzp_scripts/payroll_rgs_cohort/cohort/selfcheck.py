"""Самопроверки. Запускаются из тетрадки ПЕРЕД прогоном, без обращения к БД.

Лежат внутри пакета намеренно: отдельный каталог `tests/` на проме перехватывает
первый попавшийся одноимённый пакет из окружения, и тетрадка падает на импорте.

Проверяется не «работает ли код», а то, из-за чего РЕЗУЛЬТАТУ НЕЛЬЗЯ БЫЛО БЫ
ВЕРИТЬ: диалект SQL, отсутствие фильтра партиции, приведение номера организации,
аддитивность и взаимоисключаемость ОБЕИХ лестниц, порядок веток, отделение
сезонности, обезличивание документа и то, что показанный читателю запрос
действительно можно выполнить.

Запуск: `from cohort import selfcheck; selfcheck.run_all()`
        или `python -m cohort.selfcheck`.
"""
from __future__ import annotations

import re

import pandas as pd

from . import analyze as A
from . import fetch
from . import level as LV
from . import narrative as N
from . import queries as LQ
from . import report_text as RT
from . import view as V


class CheckFailed(AssertionError):
    pass


def _ok(name: str) -> None:
    print(f"  ✓ {name}")


def _all_sql() -> list[tuple[str, str]]:
    """Весь SQL модуля — парами (имя, текст). По ним идут проверки диалекта."""
    return [(k, v) for k, v in vars(LQ).items()
            if isinstance(v, str) and (k.isupper() or k.startswith("_T_"))]


def _ws() -> fetch.Workspace:
    """Рабочий набор без БД — только чтобы собирать тексты запросов."""
    return fetch.Workspace(None, None, "enrollment_type", {}, use_temp=False)


# --------------------------------------------------------------------------- #
# SQL
# --------------------------------------------------------------------------- #
def check_sql_dialect() -> None:
    """SQL обязан быть валиден для ядра PostgreSQL 9.4.

    `make_interval(months => n)` — самая частая ловушка: локально на 16-й версии
    запрос работает, а на проме падает, потому что 9.4 разбирает «=>» как оператор.
    """
    sql = "\n".join(v for _, v in _all_sql())
    if re.search(r"make_interval\s*\(", sql):
        raise CheckFailed("в SQL есть make_interval — на ядре 9.4 не работает; "
                          "сдвиг на месяцы пишется как n * interval '1 month'")
    if "=>" in sql:
        raise CheckFailed("в SQL есть «=>» — ядро 9.4 разбирает это как оператор")
    _ok("SQL: нет конструкций новее ядра 9.4")


def check_sql_braces() -> None:
    """Фигурные скобки в SQL обязаны быть экранированы.

    `read_sql` прогоняет текст через `str.format()`. Незакрытый `{1,12}` из
    регулярки роняет запрос с «IndexError: Replacement index out of range» — по
    такому сообщению про SQL не догадаешься.
    """
    known = {"schema", "schema_t", "code_col", "amt_cond", "name", "body", "dist"}
    for name, sql in _all_sql():
        for field in re.findall(r"(?<!\{)\{([^{}]*)\}(?!\})", sql):
            if field not in known:
                raise CheckFailed(
                    f"{name}: одинарные фигурные скобки «{{{field}}}» — str.format "
                    f"попробует их подставить и запрос упадёт. Удвойте: "
                    f"{{{{{field}}}}}")
    _ok("SQL: фигурные скобки экранированы")


def check_partition_filter() -> None:
    """Каждое обращение к ведомостям обязано иметь фильтр по report_dt.

    Витрина партиционирована по отчётной дате. Запрос без неё читает всю историю:
    на проме это не «медленно», это «никогда». Ошибка не диагностируется по
    результату — запрос просто не возвращается.
    """
    for name, sql in _all_sql():
        if "uzp_data_payroll_m" not in sql:
            continue
        if "report_dt" not in sql:
            raise CheckFailed(
                f"{name}: обращается к uzp_data_payroll_m без report_dt — "
                f"запрос прочитает всю историю партиций")
        tail = sql[sql.index("uzp_data_payroll_m"):]
        if not re.search(r"WHERE[\s\S]*?report_dt", tail, re.IGNORECASE):
            raise CheckFailed(
                f"{name}: report_dt упоминается, но не в WHERE после обращения "
                f"к витрине — партиции не отсекутся")
    _ok("SQL: каждое обращение к ведомостям отсекает партиции по report_dt")


def check_inn_cast_guarded() -> None:
    """Приведение номера организации к числу — только под маской.

    В ведомостях колонка текстовая, и в ней встречается то, что в bigint не
    приводится. Голый CAST роняет ВЕСЬ запрос, а не одну строку.
    """
    for name, sql in _all_sql():
        if "CAST(p.inn AS bigint)" not in sql:
            continue
        if "p.inn ~ " not in sql:
            raise CheckFailed(
                f"{name}: приводит p.inn к bigint без маски «~ '^[0-9]...'» — "
                f"нечисловое значение уронит весь запрос")
    _ok("SQL: номер организации приводится к числу только под маской")


def check_codes_binding() -> None:
    """Список кодов уходит параметром, а не склейкой, и сравнивается через ANY.

    `IN :codes` через `text()` подставил бы кортеж ОДНИМ значением — условие
    сравнивало бы smallint с кортежем. Ошибка ловится только на проме.
    """
    sql = "\n".join(v for _, v in _all_sql())
    if re.search(r"IN\s+:codes", sql):
        raise CheckFailed("в SQL есть «IN :codes» — через text() кортеж "
                          "подставится одним значением; пишите «= ANY(:codes)»")
    for code in LQ.CODES:
        if re.search(rf"\b{code}\s*,\s*{code}\b", sql):
            raise CheckFailed("коды зачисления склеены в текст запроса")
    _ok(f"SQL: {len(LQ.CODES)} кодов зачисления уходят параметром через = ANY")


def check_triple_grain() -> None:
    """Получатель считается ТРОЙКОЙ, а порог — по организации.

    Это две разные группировки, и перепутать их легко: сгруппировав порог по
    тройке, база разойдётся с отчётностью — и разойдётся правдоподобно.
    """
    body = LQ._T_PAIRS                                          # noqa: SLF001
    if "GROUP BY p.report_dt, p.epk_id, CAST(p.inn AS bigint), p.gosb_id" not in body:
        raise CheckFailed("t_pairs группируется не по тройке (человек, ИНН, ГОСБ)")
    if "OVER (PARTITION BY p.report_dt, p.epk_id" not in body:
        raise CheckFailed("сумма по организации не считается оконной функцией — "
                          "порог по организации при группировке по тройке иначе "
                          "не взять, HAVING фильтрует только свою группу")
    if "min(p.gosb_id)" in body:
        raise CheckFailed("ГОСБ схлопывается через min() — грейн снова стал парой")
    for scope, cond in LQ.AMT_COND.items():
        if not cond.startswith("x."):
            raise CheckFailed(f"условие порога «{scope}» ссылается не на выборку x")
    _ok(f"грейн: получатель — тройка, порог — по организации "
        f"(вариантов порога {len(LQ.AMT_COND)})")


def check_probe_placeholders() -> None:
    """Запросы разведки не должны уносить с собой неподставленные скобки.

    Разведка идёт МИМО рабочего набора: она работает до его сборки, и подстановку
    имени колонки кода делает сама. Забытая скобка не роняет прогон — `KeyError`
    ловится общим `except` разведки и печатается как «запрос разведки
    недоступен», то есть выглядит как отсутствие прав на витрину. Раздел молча
    пропадает, а причина ищется не там. Так и вышло с проверкой порога.

    Проверяется то же, что делает разведка: подставить схему и имя колонки — и
    убедиться, что скобок не осталось.
    """
    from uzp_dash import db

    for name, sql in _all_sql():
        if not name.startswith("PROBE_"):
            continue
        built = db.render(sql.replace("{code_col}", "enrollment_type"))
        left = re.findall(r"(?<!\{)\{([a-z_]+)\}(?!\})", built)
        if left:
            raise CheckFailed(
                f"{name}: после подстановки остались скобки {left} — разведка "
                f"упадёт с KeyError, а напечатает «запрос недоступен»")
    _ok("разведка: в запросах не осталось неподставленных скобок")


def check_workset_order() -> None:
    """Порядок рабочего набора: выборка не может ссылаться на объявленную позже.

    В режиме CTE это не стилистика: CTE видит только то, что объявлено выше него,
    и перестановка двух строк в WORKSET уронила бы запасной путь — тот самый,
    который включается сам и без объявления.
    """
    seen: set[str] = set()
    names = [n for n, _, _ in LQ.WORKSET]
    for name, body, _ in LQ.WORKSET:
        for other in names:
            if other != name and other in body and other not in seen:
                raise CheckFailed(
                    f"{name} ссылается на {other}, объявленную ПОЗЖЕ — "
                    f"в режиме CTE запрос не разберётся")
        seen.add(name)
    _ok(f"рабочий набор: {len(names)} выборок объявлены в порядке зависимостей")


def check_prelude_minimal() -> None:
    """Запасной путь подставляет только нужные выборки и всё подставляет.

    На ядре 9.4 CTE — барьер оптимизации: объявленная выборка считается всегда.
    Полный набор в запросе про справочник ТБ означал бы лишние сканы витрины.
    """
    ws = _ws()
    only_tb = ws._prelude(LQ.TB_DIM)                            # noqa: SLF001
    if "t_pairs" in only_tb or "uzp_data_payroll_m" in only_tb:
        raise CheckFailed("запрос справочника ТБ тянет за собой рабочий набор")

    lost = ws._prelude(LQ.LOST_TOTALS)                          # noqa: SLF001
    for need in ("t_seg", "t_pairs", "t_seen", "t_seen_epk", "t_inn_seen",
                 "t_recent", "t_epk_month"):
        if f"{need} AS (" not in lost:
            raise CheckFailed(f"лестница причин осталась без выборки {need}")
    if lost.count("WITH ") != 1:
        raise CheckFailed("в запросе два WITH подряд — оператор не разберётся")

    # Имя колонки кода и условие порога обязаны быть подставлены и в САМОМ
    # запросе, а не только в телах выборок: неподставленная скобка роняет запрос
    # в слое БД с «KeyError». Ошибка срабатывает ТОЛЬКО на запасном пути — то
    # есть только там, где её нельзя отладить.
    for name, sql in (("MONTHLY", LQ.MONTHLY), ("SURVIVAL", LQ.SURVIVAL),
                      ("THRESHOLD_SENS", LQ.THRESHOLD_SENS),
                      ("CODE_SPLIT", LQ.CODE_SPLIT), ("TENURE", LQ.TENURE),
                      ("LOST_TOTALS", LQ.LOST_TOTALS)):
        built = ws._body(ws._prelude(sql))                      # noqa: SLF001
        for ph in ("{code_col}", "{amt_cond}"):
            if ph in built:
                raise CheckFailed(
                    f"{name}: на запасном пути {ph} не подставлено — запрос "
                    f"упадёт с KeyError в слое БД")
    _ok("запасной путь: нужные выборки, WITH один, подстановки выполнены")


# --------------------------------------------------------------------------- #
# Лестницы
# --------------------------------------------------------------------------- #
def check_causes_exhaustive() -> None:
    """Все четыре лестницы полны и взаимоисключающи.

    Проверяется по САМОМУ ТЕКСТУ SQL, а не по данным: ветка, которую забыли
    описать, показалась бы в отчёте пустой строкой и выглядела бы как свойство
    данных, а не как забытое описание.
    """
    pairs = [
        ("потери получателей", LQ._LOST_CASE, A.CAUSES, "moved_within_rgs"),
        ("приход получателей", LQ._GAINED_CASE, A.GAINS, "moved_in"),
        ("потери людей", LQ._LOST_EPK_CASE, A.EPK_CAUSES, "left_rgs"),
        ("приход людей", LQ._GAINED_EPK_CASE, A.EPK_GAINS, "returned_to_rgs"),
    ]
    for what, case, book, else_branch in pairs:      # noqa: SLF001
        branches = set(re.findall(r"THEN\s+'([a-z_]+)'", case))
        branches.add(else_branch)
        if branches != set(book):
            raise CheckFailed(
                f"{what}: ветки SQL и описания разъехались — "
                f"{sorted(branches ^ set(book))}")
        for cause, (title, descr, kind) in book.items():
            if kind not in (A.REAL, A.METHOD, A.GAP):
                raise CheckFailed(f"{what}: у ветки «{cause}» неизвестный вид {kind}")
            if not title or not descr:
                raise CheckFailed(f"{what}: ветка «{cause}» без названия/описания")

    # На уровне ЧЕЛОВЕКА веток про получателей быть не может: это и есть смысл
    # второй лестницы. Если они туда просочатся, обе лестницы станут одинаковыми,
    # и разница между ними — главный вывод отчёта — исчезнет.
    for cause in ("multi_collapsed", "moved_within_rgs", "gosb_moved"):
        if cause in A.EPK_CAUSES:
            raise CheckFailed(
                f"ветка «{cause}» попала в лестницу по ЛЮДЯМ, а там она "
                f"бессмысленна: ни один человек так не теряется")
    _ok(f"лестницы: {len(A.CAUSES)}+{len(A.GAINS)} веток по получателям и "
        f"{len(A.EPK_CAUSES)}+{len(A.EPK_GAINS)} по людям, все описаны")


def check_ladder_priority() -> None:
    """Порядок веток лестницы значим и обязан быть именно таким.

    Каждая перестановка ниже делает разбор бессмысленным, и все — молча:

    * «ликвидирована» ПОСЛЕ «организация исчезла» никогда не сработает:
      у ликвидированной организации зачислений и так нет;
    * «ушёл из банка» ПОСЛЕ «перерыв» перестанет отличать уход от пропущенного
      месяца — а в сезонной яме это миллионы человек;
    * «перевод в другое подразделение» ПОСЛЕ «ушёл из сегмента» не сработает:
      переведённый выглядит ушедшим;
    * «схлопнулось совместительство» ПОСЛЕ «сменил организацию» не сработает
      тоже — а это ветка, ради которой разбор опускается до физлица.
    """
    order = re.findall(r"THEN\s+'([a-z_]+)'", LQ._LOST_CASE)     # noqa: SLF001
    pos = {name: i for i, name in enumerate(order)}
    pos.setdefault("moved_within_rgs", len(order))
    for first, second in [("liquidated", "inn_gone"),
                          ("left_bank", "gap_only"),
                          ("gap_only", "below_threshold"),
                          ("below_threshold", "gosb_moved"),
                          ("code_out_of_list", "gosb_moved"),
                          ("gosb_moved", "left_rgs"),
                          ("left_rgs", "multi_collapsed"),
                          ("multi_collapsed", "moved_within_rgs")]:
        if pos.get(first, 99) >= pos.get(second, -1):
            raise CheckFailed(
                f"ветка «{first}» обязана проверяться РАНЬШЕ «{second}» — "
                f"иначе она не сработает никогда")

    # В приходе методологические ветки идут РАНЬШЕ «новых»: человек, который в
    # базовом месяце получал здесь же, но ниже порога, — это порог, а не новый
    # сотрудник. Назвать его новым значило бы завысить рост ровно на ту величину,
    # которую мы вычитаем со стороны потерь.
    g_pos = {n: i for i, n in
             enumerate(re.findall(r"THEN\s+'([a-z_]+)'", LQ._GAINED_CASE))}
    for meth in ("crossed_threshold", "code_came_into_list", "gosb_moved_in"):
        if g_pos.get(meth, 99) >= g_pos.get("person_new_to_bank", -1):
            raise CheckFailed(
                f"в приходе «{meth}» обязана проверяться РАНЬШЕ "
                f"«person_new_to_bank» — иначе рост будет завышен")
    _ok("лестницы: порядок веток соблюдён в потерях и в приходе")


def _fake_totals() -> tuple:
    """Подставные данные для проверок тождеств."""
    mt = pd.DataFrame({
        "report_dt": [pd.Timestamp("2025-08-31"), pd.Timestamp("2026-08-31")],
        "n_triples": [1000.0, 700.0], "n_epk": [940.0, 672.0],
        "n_inn": [50.0, 44.0], "amt": [1e7, 7e6],
    })
    lost = pd.DataFrame({
        "cause": ["left_bank", "below_threshold", "multi_collapsed", "gap_only"],
        "n_triples": [200.0, 90.0, 60.0, 40.0],
        "n_epk": [200.0, 90.0, 60.0, 40.0], "amt": [1e6, 2e5, 3e5, 1e5]})
    gained = pd.DataFrame({
        "cause": ["person_new_to_bank", "crossed_threshold"],
        "n_triples": [50.0, 40.0], "n_epk": [50.0, 40.0], "amt": [5e5, 1e5]})
    lost_e = pd.DataFrame({
        "cause": ["left_bank", "gap_only", "below_threshold"],
        "n_epk": [200.0, 40.0, 50.0]})
    gained_e = pd.DataFrame({
        "cause": ["person_new_to_bank", "crossed_threshold"],
        "n_epk": [12.0, 10.0]})
    return mt, lost, gained, lost_e, gained_e


def check_additive() -> None:
    """Обе раскладки обязаны складываться в целое на любых данных.

    Тождества проверяются на подставных числах: если они не выполняются здесь,
    они не выполнятся и на проме, а там расхождение выглядело бы правдоподобно.
    """
    mt, lost, gained, lost_e, gained_e = _fake_totals()
    t = A.totals(mt, lost, gained, lost_e, gained_e,
                 pd.Timestamp("2025-08-31"), pd.Timestamp("2026-08-31"))
    bad = [c["name"] for c in A.check_additive(t, lost, gained, lost_e, gained_e)
           if not c["ok"]]
    if bad:
        raise CheckFailed(f"не сошлось: {bad}")

    if abs((t["d_by_people"] + t["d_by_multi"]) - t["d_triples"]) > 1e-6:
        raise CheckFailed("вклады людей и совместительства не дают падения")

    # Реальное движение обязано считаться ПО ОБЕИМ сторонам: приход по настоящим
    # причинам минус уход по настоящим. Если бы вычиталось из ПОЛНОГО прихода,
    # число было бы завышено ровно на методологию — ошибка первого этапа.
    if abs(t["net_real"] - (50.0 - 200.0)) > 1e-6:
        raise CheckFailed(f"чистое реальное движение посчитано как {t['net_real']}, "
                          f"ждали -150 (пришло 50 настоящих, ушло 200)")
    if abs(t["net_gap"] - (0.0 - 40.0)) > 1e-6:
        raise CheckFailed(f"перерывы посчитаны как {t['net_gap']}, ждали -40")
    _ok("раскладка: обе лестницы сходятся, реальное движение считается "
        "по обеим сторонам")


def check_epk_ladder_shorter() -> None:
    """Лестница по людям обязана быть КОРОЧЕ лестницы по получателям.

    Если они совпали, вторая лестница не считает ничего своего, и главный вывод
    отчёта — разница между ними — становится нулём по построению, а не по данным.
    """
    mt, lost, gained, lost_e, gained_e = _fake_totals()
    t = A.totals(mt, lost, gained, lost_e, gained_e,
                 pd.Timestamp("2025-08-31"), pd.Timestamp("2026-08-31"))
    if t["lost_epk"] >= t["lost"]:
        raise CheckFailed(
            f"по людям потеряно {t['lost_epk']}, по получателям {t['lost']} — "
            f"вторая лестница обязана быть короче")

    both = A.side_by_side(A.ladder(lost, A.CAUSES, "n_triples"), lost_e)
    if both.empty:
        raise CheckFailed("таблица «получатели и люди рядом» пуста")
    mc = both[both["cause"] == "multi_collapsed"]
    if mc.empty or bool(mc.iloc[0]["epk_applies"]):
        raise CheckFailed("«схлопнулось совместительство» помечено как имеющее "
                          "смысл на уровне человека — а там оно бессмысленно")
    _ok("лестница по людям короче и помечает ветки, которых у человека нет")


def check_ladder_table() -> None:
    """Лестница: доли от целого, порядок и виды."""
    lost = pd.DataFrame({
        "cause": ["multi_collapsed", "left_bank", "inn_gone"],
        "n_triples": [100.0, 300.0, 100.0], "n_epk": [100.0, 300.0, 100.0],
        "amt": [1.0, 2.0, 3.0]})
    tbl = A.ladder(lost, A.CAUSES, "n_triples")
    if abs(tbl["share"].sum() - 1.0) > 1e-9:
        raise CheckFailed("доли причин не дают единицы")
    if list(tbl["cause"])[0] != "left_bank":
        raise CheckFailed("порядок веток не по смыслу: настоящий уход обязан "
                          "идти первым")
    if tbl[tbl["cause"] == "multi_collapsed"]["kind"].iloc[0] != A.METHOD:
        raise CheckFailed("схлопнувшееся совместительство помечено не как счёт")
    _ok("лестница: доли от целого, порядок и виды верны")


# --------------------------------------------------------------------------- #
# Когда
# --------------------------------------------------------------------------- #
def _seasonal(years: int = 3, dip_month: int = 1) -> pd.DataFrame:
    """Ряд с РЕГУЛЯРНЫМ сезонным провалом и без единого настоящего события."""
    idx = pd.date_range("2024-02-29", periods=12 * years, freq="ME")
    vals = [1000.0 - 2.0 * i - (300.0 if dt.month == dip_month else 0.0)
            for i, dt in enumerate(idx)]
    return pd.DataFrame({"report_dt": idx, "n_triples": vals,
                         "n_epk": [v * 0.95 for v in vals], "n_inn": 50.0,
                         "amt": [v * 1000 for v in vals]})


def check_steps_seasonal() -> None:
    """Регулярный сезонный провал обрывом НЕ является.

    Ровно эта ошибка попала в первый прогон по проме: январь проваливается на два
    миллиона каждый год, и оба января были объявлены событием — то есть
    регулярный сезон выдан за смену кодировки. Сравнение год к году вычитает
    сезон само.
    """
    tr = A.trend(_seasonal())
    st, how = A.steps(tr)
    if "год к году" not in how:
        raise CheckFailed(f"при {len(tr)} месяцах мерилось «{how}», а не год к году")
    if not st.empty:
        months = [f"{pd.Timestamp(r.report_dt):%m.%Y}" for r in st.itertuples()]
        raise CheckFailed(f"регулярный сезонный провал объявлен обрывом: {months}")

    # А вот НАСТОЯЩАЯ ступень поверх той же сезонности обязана найтись.
    df = _seasonal()
    hit = 30
    df.loc[hit:, "n_triples"] = df.loc[hit:, "n_triples"] - 400.0
    df["n_epk"] = df["n_triples"] * 0.95
    st2, _ = A.steps(A.trend(df))
    if st2.empty:
        raise CheckFailed("настоящая ступень поверх сезонности не найдена")
    got = pd.Timestamp(st2.iloc[0]["report_dt"])
    want = pd.Timestamp(df.loc[hit, "report_dt"])
    if got != want:
        raise CheckFailed(f"найден не тот месяц: {got:%m.%Y}, ждали {want:%m.%Y}")
    _ok("обрывы: сезон не выдаётся за событие, настоящая ступень находится")


def check_steps_short_series() -> None:
    """Короткий ряд честно говорит, что сезонность не отделена."""
    _, how = A.steps(A.trend(_seasonal(years=1)))
    if "сезонность не отделена" not in how:
        raise CheckFailed(f"на коротком ряду мерилось «{how}» без оговорки о "
                          f"сезонности — читатель примет сезон за событие")
    _ok("обрывы: на коротком ряду оговорка о сезонности есть")


def check_month_compare() -> None:
    """Сравнение отчётного месяца с соседями и с годом назад."""
    tr = A.trend(_seasonal())
    cur = pd.Timestamp(tr["report_dt"].iloc[-1])
    cmp = A.month_compare(tr, cur, n_prev=2)
    if cmp.empty:
        raise CheckFailed("сравнение с соседними месяцами не построилось")
    if int(cmp["is_report"].sum()) != 1:
        raise CheckFailed("отчётный месяц в сравнении не один")
    year_ago = pd.Timestamp(pd.Timestamp(cur) - pd.DateOffset(months=12)) \
        .to_period("M").to_timestamp("M")
    if year_ago not in set(cmp["report_dt"]):
        raise CheckFailed("в сравнении нет того же месяца год назад — "
                          "сезонность не с чем сопоставить")
    _ok("сравнение месяцев: соседи и год назад на месте")


# --------------------------------------------------------------------------- #
# Разрезы, стаж, коды
# --------------------------------------------------------------------------- #
def check_by_dim() -> None:
    """Разрез обязан считать долю от ЦЕЛОГО, а не от показанного куска."""
    df = pd.DataFrame({
        "inn": [1, 1, 2, 3, 4, 5],
        "cause": ["left_bank", "multi_collapsed", "left_bank", "inn_gone",
                  "below_threshold", "gap_only"],
        "n_triples": [100.0, 50.0, 80.0, 60.0, 40.0, 20.0],
        "agency": ["Образование", "Образование", "Культура", "Спорт", "Спорт",
                   "Прочее"],
    })
    out = A.by_dim(df, "agency", top_n=2)
    if len(out) != 2:
        raise CheckFailed("разрез вернул не top_n строк")
    if out["share"].sum() >= 1.0:
        raise CheckFailed("доли показанных строк дают единицу — значит целое "
                          "посчитано по обрезанному кадру")
    edu = out[out["agency"] == "Образование"].iloc[0]
    if abs(edu[A.REAL] - 100.0) > 1e-9 or abs(edu[A.METHOD] - 50.0) > 1e-9:
        raise CheckFailed("состав по видам внутри разреза посчитан неверно")
    _ok("разрезы: доля от целого, состав по видам внутри строки верен")


def check_join_key_dtypes() -> None:
    """Соединения по идентификатору обязаны работать при РАЗНЫХ типах ключа.

    Драйвер отдаёт один и тот же smallint то как int, то как Decimal, а колонка
    с единственным NULL приезжает object целиком. Дальше происходит одно из двух,
    и второе хуже первого:

    * `merge` падает — шумно, сразу, но после многих минут прогона (так и вышло
      на проме: одиннадцать минут выгрузки и ValueError на разметке);
    * `map` и `reindex` НЕ падают, а молча дают NaN. Разрез не исчезает, а
      схлопывается в одну строку-заглушку и читается как свойство данных.

    Поэтому проверка кормит расчёты кадрами с НАМЕРЕННО разными типами ключей и
    требует, чтобы соединение всё равно состоялось.
    """
    # Ключи слева — строки и Decimal, справа — int. Ровно то, что приходит с прома.
    lost = pd.DataFrame({
        "inn": ["7701", "7702"], "cause": ["left_bank", "left_bank"],
        "gosb_id": ["38", "38"], "tb_id": ["17", "17"],
        "n_triples": [100.0, 50.0]})
    attrs = pd.DataFrame({
        "inn": [7701, 7702],
        "company_name": ["МБОУ СОШ № 1", "ГБУЗ БОЛЬНИЦА № 2"],
        "holding_name": [None, None], "industry_name": [None, None],
        "is_educational": [True, False], "is_military": [False, False],
        "is_liquidated": [False, False]})
    tb = pd.DataFrame({"tb_id": [17], "tb_short_name": ["СРБ"]})
    gosb = pd.DataFrame({"old_gosb_id": [38], "new_gosb_id": [38],
                         "tb_id": [17], "tb_short_name": ["СРБ"],
                         "gosb_name": ["ГОСБ-1"], "region_name": ["Регион-1"]})

    marked, meta = A.enrich(lost, attrs, tb, gosb, "old_gosb_id")
    if marked.empty:
        raise CheckFailed("разметка потеряла все строки на разных типах ключа")
    if marked["company_name"].isna().any():
        raise CheckFailed("соединение со справочником организаций не состоялось: "
                          "ключ строкой слева и числом справа")
    if float(meta.get("tb_known", 0)) <= 0:
        raise CheckFailed("ТБ не определился — соединение по tb_id не состоялось")
    if float(meta.get("region_known", 0)) <= 0:
        raise CheckFailed("регион не определился — map по gosb_id дал NaN молча, "
                          "и разрез схлопнулся бы в заглушку")

    # Тот же разнобой в топе организаций и в миграции.
    gained = pd.DataFrame({"inn": [7701.0], "n_triples": [40.0], "n_real": [40.0]})
    top = A.top_orgs(marked, gained, top_n=5)
    if float(top[top["inn"] == 7701].iloc[0]["gained"]) != 40.0:
        raise CheckFailed("приход не подтянулся к организации: ключ float против int")

    mig = pd.DataFrame({"inn_from": ["7701"], "inn_to": ["7799"], "n_epk": [90.0]})
    out = A.migration(mig, lost, attrs, min_share=0.5)
    if out.empty:
        raise CheckFailed("миграция не нашла переоформление: доля не посчиталась "
                          "из-за типов ключа")
    if pd.isna(out.iloc[0].get("name_from")):
        raise CheckFailed("название организации-источника не подтянулось")
    _ok("соединения по идентификатору: разные типы ключа сводятся, "
        "молчаливого NaN не остаётся")


def check_top_orgs_net() -> None:
    """Топ организаций сортируется по НЕТТО, а не по потерям.

    Организация, потерявшая много и набравшая столько же, ничего не потеряла — а
    по одним потерям возглавила бы таблицу. В первом прогоне так и вышло.
    """
    marked = pd.DataFrame({
        "inn": [1, 2], "cause": ["left_bank", "left_bank"],
        "n_triples": [1000.0, 300.0],
        "company_name": ["Большая", "Малая"], "agency": ["А", "Б"],
        "level": ["Ф", "М"], "tb_short_name": ["Т", "Т"],
        "holding_name": ["Х", "Х"], "region_name": ["Р", "Р"]})
    gained = pd.DataFrame({"inn": [1, 2], "n_triples": [990.0, 0.0],
                           "n_real": [990.0, 0.0]})
    out = A.top_orgs(marked, gained, top_n=2)
    if int(out.iloc[0]["inn"]) != 2:
        raise CheckFailed(
            "первой стоит организация, потерявшая больше всех, хотя она почти "
            "всё вернула: сортировка идёт по потерям, а не по нетто")
    if abs(float(out[out["inn"] == 1].iloc[0]["net"]) + 10.0) > 1e-9:
        raise CheckFailed("нетто посчитано неверно")
    _ok("топ организаций: сортировка по нетто, приход учтён")


def check_code_split() -> None:
    """Коды делятся на зарплатные и все прочие, и вывод строится по первым."""
    b, c = pd.Timestamp("2025-08-31"), pd.Timestamp("2026-08-31")
    df = pd.DataFrame({
        "report_dt": [b, b, c, c], "grp": ["in", "out", "in", "out"],
        "n_epk": [1000.0, 5000.0, 990.0, 0.0],
        "n_rows": [1.0] * 4, "amt": [1e6, 5e6, 0.99e6, 0.0]})
    out = A.code_split(df, b, c)
    if len(out) != 2:
        raise CheckFailed("раскладка кодов вернула не две строки")
    if abs(float(out[out["grp"] == "in"].iloc[0]["d_epk"]) + 10.0) > 1e-9:
        raise CheckFailed("изменение по зарплатным кодам посчитано неверно")

    # Обнулившийся код ВНЕ списка на метрику не влияет по построению — таблица
    # исчезнувших кодов обязана говорить это колонкой, а не подразумевать.
    mix = pd.DataFrame({
        "report_dt": [b, c], "code": [3, 3],
        "code_name": ["Пенсия", "Пенсия"], "n_epk": [50000.0, 0.0],
        "amt": [1e6, 0.0]})
    gone = A.vanished_codes(mix, b, c, LQ.CODES)
    if gone.empty:
        raise CheckFailed("исчезнувший код не найден")
    if bool(gone.iloc[0]["in_list"]):
        raise CheckFailed("код 3 помечен как входящий в метрику, а его там нет")
    _ok("коды: зарплатные отделены от прочих, исчезнувшие помечены верно")


def check_tenure() -> None:
    """Стаж ушедших раскладывается по корзинам в осмысленном порядке."""
    df = pd.DataFrame({
        "cause": ["left_bank"] * 3 + ["gap_only"] * 2,
        "bucket": ["1", "13-24", "2-3", "1", "25+"],
        "n_epk": [100.0, 50.0, 30.0, 20.0, 10.0]})
    out = A.tenure_table(df)
    if out.empty:
        raise CheckFailed("таблица стажа не построилась")
    order = [b for b in A.TENURE_ORDER if b in set(out["bucket"])]
    if list(out["bucket"]) != order:
        raise CheckFailed(f"корзины стажа не в порядке: {list(out['bucket'])}")
    if abs(float(out["Доля"].sum()) - 1.0) > 1e-9:
        raise CheckFailed("доли корзин не дают единицы")
    _ok("стаж ушедших: корзины в порядке, доли от целого")


# --------------------------------------------------------------------------- #
# Показ запроса читателю
# --------------------------------------------------------------------------- #
def check_shown_self_contained() -> None:
    """Показанный читателю запрос обязан быть выполнимым.

    На проме разбор идёт по временным таблицам, и текст `FROM t_pairs` читателю
    выполнить негде — таблица жила в чужой сессии. Значок «как проверить цифру»
    обещал бы проверку, которой нет.
    """
    ws = _ws()
    for name, sql in (("lost_totals", LQ.LOST_TOTALS),
                      ("gained_totals", LQ.GAINED_TOTALS),
                      ("lost_epk", LQ.LOST_EPK_TOTALS),
                      ("month_totals", LQ.MONTH_TOTALS),
                      ("tenure", LQ.TENURE)):
        text = ws._body(ws._prelude(sql))                       # noqa: SLF001
        # Обращение к рабочей выборке допустимо ТОЛЬКО если она же тут и
        # объявлена: тогда запрос самодостаточен.
        for tname, _, _ in LQ.WORKSET:
            if re.search(rf"\b{tname}\b", text) and f"{tname} AS (" not in text:
                raise CheckFailed(
                    f"{name}: показывается запрос с обращением к {tname}, "
                    f"которое в нём не объявлено — выполнить его нельзя")
    _ok("показ запроса: тексты самодостаточны, временных таблиц не осталось")


def check_shown_recorded() -> None:
    """В `ws.shown` попадает то, что исполнялось, вместе с параметрами."""
    ws = _ws()
    ws.params = {"d_cur": "2026-08-31", "amt_min": 2500, "codes": [1, 2]}
    ws.shown["demo"] = (ws._body(ws._prelude(LQ.MONTH_TOTALS)),  # noqa: SLF001
                        dict(ws.params))
    html = V.sql_info(ws.shown, "demo")
    if "<details" not in html or "qbox-i" not in html:
        raise CheckFailed("значок «i» не собрался")
    for key in ws.params:
        if f":{key} =" not in html:
            raise CheckFailed(f"значение параметра :{key} не показано — цифру по "
                              f"такому запросу не воспроизвести")
    if V.sql_info(ws.shown, "нет-такого") != "":
        raise CheckFailed("значок «i» показан для запроса, которого не было")
    _ok("показ запроса: параметры в шапке, отсутствующий запрос не выдумывается")


# --------------------------------------------------------------------------- #
# Обезличивание и устойчивость
# --------------------------------------------------------------------------- #
def check_anonymize_doc() -> None:
    """Обезличивание: имена уходят, номера удаляются, ЗАГЛУШКИ остаются."""
    df = pd.DataFrame({
        "inn": [7701234567, 7809876543],
        "company_name": ["МБОУ СОШ № 1 ГОРОДА ЗАРЕЧНЫЙ", "ГБУЗ БОЛЬНИЦА № 3"],
        "holding_name": ["Холдинг Минобразования", A.FILL["holding_name"]],
        "tb_short_name": ["СРБ", A.FILL["tb_short_name"]],
        "n_triples": [100.0, 50.0]})
    al = RT.Aliases()
    masked = RT.mask(df, al)

    if "inn" in masked.columns:
        raise CheckFailed("номер организации остался в обезличенном кадре")
    for v in masked["company_name"]:
        if not str(v).startswith("Орг-"):
            raise CheckFailed(f"название не заменено токеном: {v}")
    # Заглушка — не имя. Выдав ей токен, документ показал бы «ТБ-01» там, где на
    # самом деле ничего не известно, и пробел прочитался бы как подразделение.
    if masked["tb_short_name"].iloc[1] != A.FILL["tb_short_name"]:
        raise CheckFailed("заглушка «ТБ неизвестен» заменена токеном — читатель "
                          "примет её за настоящее подразделение")
    if masked["holding_name"].iloc[1] != A.FILL["holding_name"]:
        raise CheckFailed("заглушка холдинга заменена токеном")

    names = RT.collect_names([df])
    if A.FILL["tb_short_name"] in names:
        raise CheckFailed("заглушка попала в список настоящих названий — "
                          "проверка утечки завалит чистый документ")
    if "СРБ" not in names:
        raise CheckFailed("аббревиатура из трёх букв не попала в список названий — "
                          "сокращённое имя подразделения уедет наружу")

    # Свободный текст выводов приходит с ВОССТАНОВЛЕННЫМИ названиями: в HTML они
    # нужны настоящими, в документе — нет.
    said = "Больше всех потеряли СРБ и МБОУ СОШ № 1 ГОРОДА ЗАРЕЧНЫЙ."
    out = RT.mask_text(said, al, names)
    if RT.check_leak(out, names):
        raise CheckFailed(f"обезличивание свободного текста не сработало: {out}")
    if al.alias("ТБ", "СРБ") not in out:
        raise CheckFailed("текст и таблица получили РАЗНЫЕ токены для одного ТБ")
    _ok("обезличивание: имена заменены, заглушки сохранены, номера удалены")


def check_llm_fallback() -> None:
    """Раздел обязан собираться и при полностью недоступном шлюзе."""
    def dead(prompt, **kw):
        raise RuntimeError("шлюз недоступен")

    nar = N.Narrator(complete=dead, max_calls=4)
    text, fb = nar.section("проверка", "любой промпт", "текст по правилам")
    if text != "текст по правилам" or not fb:
        raise CheckFailed("фолбэк на правила не сработал")
    for _ in range(3):
        nar.section("проверка", "промпт", "правила")
    if not nar.gateway_down:
        raise CheckFailed("защёлка недоступного шлюза не сработала")
    if nar.calls > N.ZERO_STREAK_ABORT:
        raise CheckFailed(f"после защёлки шлюз дёргали ещё раз: {nar.calls} вызовов")

    # Главный вывод отчёта обязан быть и без модели: «выросли или нет» — то, ради
    # чего всё считается.
    mt, lost, gained, lost_e, gained_e = _fake_totals()
    t = A.totals(mt, lost, gained, lost_e, gained_e,
                 pd.Timestamp("2025-08-31"), pd.Timestamp("2026-08-31"))
    txt = N.fb_net("08.2025", "08.2026", t)
    if not txt or "сократил" not in txt:
        raise CheckFailed(f"фолбэк раздела «выросли или нет» не дал вывода: {txt}")
    _ok("LLM: фолбэк на правила, защёлка шлюза, главный вывод без модели")


def check_row_limit() -> None:
    """Выборка сверх лимита обязана останавливать прогон, а не обрезаться молча."""
    try:
        fetch.guard_rows(pd.DataFrame({"x": range(10)}), "проверка", limit=5)
    except fetch.RowLimitError:
        _ok("лимит строк: выборка сверх лимита останавливает прогон")
        return
    raise CheckFailed("выборка сверх лимита прошла молча")


def check_level_rules() -> None:
    """Уровень подчинения: порядок правил и поиск по целым словам.

    «ФГБОУ» содержит «ГБОУ» как подстроку. Разбор подстрокой отнёс бы федеральный
    вуз к региональным — ошибка молчаливая, и разрез «уровень подчинения» стал бы
    показывать не то, чем подписан.
    """
    cases = {
        "ФГБОУ ВО УНИВЕРСИТЕТ № 5": LV.FEDERAL,
        "ГБОУ ЛИЦЕЙ № 3": LV.REGIONAL,
        "МБОУ СОШ № 1": LV.MUNICIPAL,
        "ГБУЗ ГОРОДСКАЯ БОЛЬНИЦА № 2": LV.REGIONAL,
        "УМВД № 4": LV.FEDERAL,
        "МУП ВОДОКАНАЛ № 1": LV.MUNICIPAL,
        'ООО "РОМАШКА"': LV.COMMERCIAL,
        "НЕПОНЯТНОЕ НАЗВАНИЕ": LV.UNKNOWN,
        "": LV.UNKNOWN,
    }
    for name, want in cases.items():
        got = LV.classify(name)
        if got != want:
            raise CheckFailed(f"«{name}»: получили «{got}», ждали «{want}»")
    _ok(f"уровень подчинения: {len(cases)} случаев, ФГБОУ не путается с ГБОУ")


def check_empty_frames() -> None:
    """Пустые кадры не должны ронять расчёты.

    На проме любой необязательный источник может не прочитаться, и тогда в
    расчёты приезжает пустой кадр. Падение здесь означало бы, что один
    недоступный раздел уносит весь прогон.
    """
    e = pd.DataFrame()
    ts = (pd.Timestamp("2025-08-31"), pd.Timestamp("2026-08-31"))
    for fn, args in ((A.trend, (e,)), (A.threshold, (e,) + ts),
                     (A.load_health, (e,)), (A.survival_curve, (e, 0.0)),
                     (A.top_orgs, (e, e)), (A.by_dim, (e, "agency")),
                     (A.ladder, (e, A.CAUSES, "n_triples")),
                     (A.side_by_side, (e, e)), (A.tenure_table, (e,)),
                     (A.code_split, (e,) + ts),
                     (A.vanished_codes, (e,) + ts + (LQ.CODES,)),
                     (A.month_compare, (e, ts[1])),
                     (A.migration, (e, e, e))):
        out = fn(*args)
        if out is None or not isinstance(out, pd.DataFrame) or not out.empty:
            raise CheckFailed(f"{fn.__name__} на пустом кадре вернула {type(out)}")

    st, how = A.steps(e)
    if not st.empty or not how:
        raise CheckFailed("поиск обрывов на пустом ряду вернул не пустой результат")
    marked, _ = A.enrich(e, e, e, e, None)
    if not marked.empty:
        raise CheckFailed("разметка пустых потерь вернула непустой кадр")
    _ok("пустые кадры: расчёты не падают, недоступный источник не уносит прогон")


# --------------------------------------------------------------------------- #
# Проверка против синтетики
# --------------------------------------------------------------------------- #
def check_against_synth(res: dict, expect_path: str | None = None) -> None:
    """Нашёл ли разбор то, что заложено в синтетику.

    Всё остальное в этом файле проверяет, что код не падает и не врёт в
    арифметике. Эта проверка — единственная, которая отвечает на вопрос «а
    находит ли он вообще то, ради чего написан».

    Вызывается ОТДЕЛЬНО, из тетрадки, после прогона на синтетике. В закрытом
    контуре не запускается — там синтетики нет.
    """
    import json
    from pathlib import Path

    from uzp_dash import config

    path = (Path(expect_path) if expect_path
            else config.OUTPUT_DIR / "synth_payroll_expect.json")
    if not path.exists():
        raise CheckFailed(
            f"нет файла ожиданий {path}. Он пишется генератором синтетики: "
            f"выполните `python3 -m synth`. В закрытом контуре эта проверка не "
            f"применяется — синтетики там нет.")
    exp = json.loads(path.read_text(encoding="utf-8"))

    causes = res.get("causes")
    if causes is None or causes.empty:
        raise CheckFailed("лестница причин пуста")
    got = set(causes["cause"])
    for cause in ("code_out_of_list", "below_threshold", "multi_collapsed",
                  "liquidated", "inn_gone", "left_bank", "gosb_moved"):
        if cause not in got:
            raise CheckFailed(
                f"ветка «{cause}» в синтетику заложена, но разбором не найдена — "
                f"значит на проме она тоже не сработает, и это будет незаметно")

    gains = res.get("gains")
    if gains is None or gains.empty:
        raise CheckFailed("лестница прихода пуста")
    if len(set(gains["cause"])) < 2:
        raise CheckFailed(
            f"весь приход попал в одну ветку ({set(gains['cause'])}) — раскладка "
            f"прихода не работает, и «выросли или нет» посчитано зря")
    if "gosb_moved_in" not in set(gains["cause"]):
        raise CheckFailed("перевод между подразделениями не найден в приходе, "
                          "хотя в синтетику он заложен")

    bad = [c["name"] for c in res.get("checks", []) if not c["ok"]]
    if bad:
        raise CheckFailed(f"не сошлись проверки: {bad}")

    # Численность базового месяца сходится не точно: генератор считает до
    # маскирования грязных номеров организаций, а разбор — после (нечисловой
    # номер в сегмент не попадает). Поэтому сравнение по порядку величины.
    key = str(pd.Timestamp(res["totals"]["base_month"]).date())
    want = (exp.get("pairs_by_month") or {}).get(key)
    if want:
        ratio = float(res["totals"]["triples_base"]) / float(want)
        if not 0.5 <= ratio <= 1.0:
            raise CheckFailed(
                f"получателей в базовом месяце: разбор насчитал "
                f"{res['totals']['triples_base']:,.0f}, генератор заложил "
                f"{want:,.0f} (отношение {ratio:.2f}). Разбор считает после "
                f"отсева непригодных номеров, поэтому его число обязано быть "
                f"чуть МЕНЬШЕ, но не вдвое.")

    mig = res.get("migration")
    n_mig = 0 if mig is None or mig.empty else len(mig)
    _ok(f"синтетика: найдены заложенные ветки — {len(got)} причин потерь, "
        f"{len(set(gains['cause']))} источников прихода, {n_mig} переоформлений")


# --------------------------------------------------------------------------- #
ALL = [
    check_sql_dialect, check_sql_braces, check_partition_filter,
    check_inn_cast_guarded, check_codes_binding, check_triple_grain,
    check_probe_placeholders, check_workset_order, check_prelude_minimal,
    check_causes_exhaustive, check_ladder_priority, check_additive,
    check_epk_ladder_shorter, check_ladder_table,
    check_steps_seasonal, check_steps_short_series, check_month_compare,
    check_by_dim, check_join_key_dtypes, check_top_orgs_net, check_code_split, check_tenure,
    check_shown_self_contained, check_shown_recorded,
    check_anonymize_doc, check_llm_fallback, check_row_limit,
    check_level_rules, check_empty_frames,
]


def run_all() -> None:
    print("Самопроверки payroll_rgs_cohort:")
    for fn in ALL:
        fn()
    print("Все проверки пройдены.")


if __name__ == "__main__":
    run_all()

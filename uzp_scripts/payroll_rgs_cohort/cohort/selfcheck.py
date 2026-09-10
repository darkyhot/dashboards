"""Самопроверки. Запускаются из тетрадки ПЕРЕД прогоном, без обращения к БД.

Лежат внутри пакета намеренно: отдельный каталог `tests/` на проме перехватывает
первый попавшийся одноимённый пакет из окружения, и тетрадка падает на импорте.

Проверяется не «работает ли код», а то, из-за чего РЕЗУЛЬТАТУ НЕЛЬЗЯ БЫЛО БЫ
ВЕРИТЬ: диалект SQL, отсутствие фильтра партиции, приведение номера организации,
аддитивность и взаимоисключаемость лестницы причин, порядок правил уровня
подчинения, обезличивание документа.

Запуск: `from cohort import selfcheck; selfcheck.run_all()`
        или `python -m cohort.selfcheck`.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from . import analyze as A
from . import fetch
from . import level as LV
from . import narrative as N
from . import queries as LQ
from . import report_text as RT


class CheckFailed(AssertionError):
    pass


def _ok(name: str) -> None:
    print(f"  ✓ {name}")


def _all_sql() -> list[tuple[str, str]]:
    """Весь SQL модуля — парами (имя, текст). По ним идут проверки диалекта."""
    return [(k, v) for k, v in vars(LQ).items()
            if isinstance(v, str) and (k.isupper() or k.startswith("_T_"))]


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
    known = {"schema", "schema_t", "code_col", "name", "body", "dist"}
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
        # Обращение к витрине есть — значит и дата обязана быть в том же запросе.
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
    """Запасной путь подставляет только нужные выборки, а не весь набор.

    На ядре 9.4 CTE — барьер оптимизации: объявленная выборка считается всегда.
    Полный набор в запросе про справочник ГОСБ означал бы лишние сканы
    партиционированной витрины.
    """
    ws = fetch.Workspace(None, None, "enrollment_type", {}, use_temp=False)
    only_gosb = ws._prelude(LQ.GOSB_DIM)                       # noqa: SLF001
    if "t_pairs" in only_gosb or "uzp_data_payroll_m" in only_gosb:
        raise CheckFailed("запрос справочника ГОСБ тянет за собой рабочий набор")

    lost = ws._prelude(LQ.LOST_TOTALS)                         # noqa: SLF001
    for need in ("t_seg", "t_pairs", "t_seen", "t_seen_epk", "t_seen_inn",
                 "t_cur_epk", "t_base_epk"):
        if f"{need} AS (" not in lost:
            raise CheckFailed(f"лестница причин осталась без выборки {need}")
    if lost.count("WITH ") != 1:
        raise CheckFailed("в запросе два WITH подряд — оператор не разберётся")

    # Имя колонки кода обязано быть подставлено и в САМОМ запросе, а не только в
    # телах выборок: `p.{code_col}` стоит и там и там, а неподставленная скобка
    # роняет запрос в слое БД с «KeyError: code_col». Ошибка срабатывает ТОЛЬКО
    # на запасном пути — то есть только там, где её нельзя отладить.
    for name, sql in (("MONTHLY", LQ.MONTHLY), ("SURVIVAL", LQ.SURVIVAL),
                      ("THRESHOLD_SENS", LQ.THRESHOLD_SENS),
                      ("CODE_MIX", LQ.CODE_MIX), ("LOST_TOTALS", LQ.LOST_TOTALS)):
        built = ws._body(ws._prelude(sql))                     # noqa: SLF001
        if "{code_col}" in built:
            raise CheckFailed(
                f"{name}: на запасном пути имя колонки кода не подставлено — "
                f"запрос упадёт с KeyError: code_col")
    _ok("запасной путь: нужные выборки, WITH один, имя колонки подставлено")


# --------------------------------------------------------------------------- #
def check_causes_exhaustive() -> None:
    """Лестница причин обязана быть полной и взаимоисключающей.

    Проверяется по САМОМУ ТЕКСТУ SQL, а не по данным: ветка, которую забыли
    описать, показалась бы в отчёте пустой строкой «nan», и это выглядело бы как
    свойство данных, а не как забытое описание.
    """
    branches = set(re.findall(r"THEN\s+'([a-z_]+)'", LQ._LOST_CASE))  # noqa: SLF001
    branches.add("moved_within_rgs")     # ветка ELSE
    missing = branches - set(A.CAUSES)
    if missing:
        raise CheckFailed(f"ветки лестницы без описания: {sorted(missing)}")
    extra = set(A.CAUSES) - branches
    if extra:
        raise CheckFailed(f"описания без веток в SQL: {sorted(extra)} — "
                          f"раскладка и её расшифровка разъехались")

    g_branches = set(re.findall(r"THEN\s+'([a-z_]+)'", LQ._GAINED_BASE))  # noqa: SLF001
    g_branches.add("moved_in")
    if g_branches != set(A.GAINS):
        raise CheckFailed(f"ветки прихода разъехались с описаниями: "
                          f"{sorted(g_branches ^ set(A.GAINS))}")

    for c in A.NOT_A_LOSS:
        if c not in A.CAUSES:
            raise CheckFailed(f"«{c}» помечена как не-отток, но такой ветки нет")
    _ok(f"лестница причин: {len(branches)} веток потерь и {len(g_branches)} "
        f"прихода, все описаны")


def check_ladder_priority() -> None:
    """Порядок веток лестницы значим и обязан быть именно таким.

    Две перестановки делают разбор бессмысленным, обе — молча:

    * «ликвидирована» ПОСЛЕ «организация исчезла» никогда не сработает:
      у ликвидированной организации зачислений и так нет;
    * «схлопнулось совместительство» ПОСЛЕ «сменил организацию» не сработает
      тоже — совместитель, потерявший одну из организаций, выглядит как
      сменивший её. А это ровно та ветка, ради которой затевался разбор.
    """
    case = LQ._LOST_CASE                                        # noqa: SLF001
    order = re.findall(r"THEN\s+'([a-z_]+)'", case)
    pos = {name: i for i, name in enumerate(order)}
    pairs = [
        ("liquidated", "inn_gone"),
        ("person_left_bank", "below_threshold"),
        ("below_threshold", "left_rgs"),
        ("code_out_of_list", "left_rgs"),
        ("left_rgs", "multi_collapsed"),
    ]
    for first, second in pairs:
        if pos.get(first, 99) >= pos.get(second, -1):
            raise CheckFailed(
                f"ветка «{first}» обязана проверяться РАНЬШЕ «{second}» — "
                f"иначе она не сработает никогда")
    _ok("лестница причин: порядок веток соблюдён")


def check_additive() -> None:
    """Раскладка обязана складываться в целое на любых данных.

    Тождество проверяется на подставных числах: если оно не выполняется здесь,
    оно не выполнится и на проме, а там расхождение выглядело бы правдоподобно.
    """
    mt = pd.DataFrame({
        "report_dt": [pd.Timestamp("2025-08-31"), pd.Timestamp("2026-08-31")],
        "n_pairs": [1000.0, 700.0], "n_epk": [940.0, 672.0],
        "n_inn": [50.0, 44.0], "amt": [1e7, 7e6],
    })
    lost = pd.DataFrame({
        "cause": ["person_left_bank", "below_threshold", "multi_collapsed"],
        "n_pairs": [200.0, 90.0, 60.0], "n_epk": [200.0, 90.0, 60.0],
        "amt": [1e6, 2e5, 3e5]})
    gained = pd.DataFrame({"cause": ["person_new_to_rgs"], "n_pairs": [50.0],
                           "n_epk": [50.0], "amt": [5e5]})

    t = A.totals(mt, lost, gained)
    checks = A.check_additive(t, lost, gained)
    bad = [c for c in checks if not c["ok"]]
    if bad:
        raise CheckFailed(f"не сошлось: {[c['name'] for c in bad]}")

    if abs(t["lost_not_real"] - 150.0) > 1e-6:
        raise CheckFailed(f"не-отток посчитан как {t['lost_not_real']}, ждали 150")

    # Раскладка на людей и совместительство обязана давать ровно падение —
    # без остатка. Остаток здесь означал бы, что часть падения не объяснена ничем.
    if abs((t["d_by_people"] + t["d_by_multi"]) - t["d_pairs"]) > 1e-6:
        raise CheckFailed("вклады людей и совместительства не дают падения")
    _ok("раскладка: тождества выполняются, не-отток отделён")


def check_causes_table() -> None:
    """Таблица причин: доли считаются от целого, порядок — осмысленный."""
    lost = pd.DataFrame({
        "cause": ["multi_collapsed", "person_left_bank", "inn_gone"],
        "n_pairs": [100.0, 300.0, 100.0], "n_epk": [100.0, 300.0, 100.0],
        "amt": [1.0, 2.0, 3.0]})
    tbl = A.causes_table(lost, 500.0)
    if abs(tbl["share"].sum() - 1.0) > 1e-9:
        raise CheckFailed("доли причин не дают единицы")
    if list(tbl["cause"])[0] != "person_left_bank":
        raise CheckFailed("порядок веток не по смыслу: настоящий отток обязан "
                          "идти первым")
    if bool(tbl[tbl["cause"] == "multi_collapsed"]["is_real_loss"].iloc[0]):
        raise CheckFailed("схлопнувшееся совместительство помечено как отток")
    _ok("таблица причин: доли от целого, порядок и пометки верны")


def check_steps() -> None:
    """Поиск месяца-обрыва обязан находить ступень и не находить её в ровном ряду.

    Меряется в медианных абсолютных отклонениях, а не в стандартных: одна большая
    ступень так раздувает стандартное отклонение, что перестаёт выделяться сама.
    """
    even = pd.DataFrame({
        "report_dt": pd.date_range("2025-01-31", periods=12, freq="ME"),
        "n_pairs": np.linspace(1000, 900, 12), "n_epk": np.linspace(950, 860, 12)})
    if not A.steps(A.trend(even)).empty:
        raise CheckFailed("в ровно убывающем ряду найден обрыв, которого нет")

    vals = list(np.linspace(1000, 950, 12))
    vals[7] -= 300                                   # ступень
    step = pd.DataFrame({
        "report_dt": pd.date_range("2025-01-31", periods=12, freq="ME"),
        "n_pairs": vals, "n_epk": [v * 0.95 for v in vals]})
    found = A.steps(A.trend(step))
    if found.empty:
        raise CheckFailed("ступень в ряду не найдена")
    if pd.Timestamp(found.iloc[0]["report_dt"]).month != 8:
        raise CheckFailed(f"найден не тот месяц: "
                          f"{pd.Timestamp(found.iloc[0]['report_dt']):%m.%Y}")
    _ok("поиск обрывов: ступень найдена, ровный ряд не даёт ложных срабатываний")


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
        "МБДОУ ДЕТСКИЙ САД № 7": LV.MUNICIPAL,
        "ГБУЗ ГОРОДСКАЯ БОЛЬНИЦА № 2": LV.REGIONAL,
        "УМВД № 4": LV.FEDERAL,
        "УФНС № 9": LV.FEDERAL,
        "МУП ВОДОКАНАЛ № 1": LV.MUNICIPAL,
        'ООО "РОМАШКА"': LV.COMMERCIAL,
        "НЕПОНЯТНОЕ НАЗВАНИЕ": LV.UNKNOWN,
        "": LV.UNKNOWN,
    }
    for name, want in cases.items():
        got = LV.classify(name)
        if got != want:
            raise CheckFailed(f"«{name}»: получили «{got}», ждали «{want}»")

    df = pd.DataFrame({"company_name": ["МБОУ СОШ № 1", None, "ЧТО-ТО"]})
    got = LV.classify_frame(df)
    if list(got) != [LV.MUNICIPAL, LV.UNKNOWN, LV.UNKNOWN]:
        raise CheckFailed(f"разбор кадра сломался: {list(got)}")
    known, total, share = LV.coverage(got)
    if (known, total) != (1, 3) or abs(share - 1 / 3) > 1e-9:
        raise CheckFailed(f"покрытие посчитано неверно: {known}/{total}")
    _ok(f"уровень подчинения: {len(cases)} случаев, ФГБОУ не путается с ГБОУ")


def check_by_dim() -> None:
    """Разрез обязан считать долю от ЦЕЛОГО, а не от показанного куска."""
    df = pd.DataFrame({
        "inn": [1, 1, 2, 3, 4, 5],
        "cause": ["person_left_bank", "multi_collapsed", "person_left_bank",
                  "inn_gone", "below_threshold", "person_left_bank"],
        "n_pairs": [100.0, 50.0, 80.0, 60.0, 40.0, 20.0],
        "agency": ["Образование", "Образование", "Культура", "Спорт", "Спорт",
                   "Прочее"],
    })
    out = A.by_dim(df, "agency", top_n=2)
    if len(out) != 2:
        raise CheckFailed("разрез вернул не top_n строк")
    total = float(df["n_pairs"].sum())
    if abs(float(out["n_pairs"].sum()) / total - out["share"].sum()) > 1e-9:
        raise CheckFailed("доля считается не от целого")
    if out["share"].sum() >= 1.0:
        raise CheckFailed("доли показанных строк дают единицу — значит целое "
                          "посчитано по обрезанному кадру")
    edu = out[out["agency"] == "Образование"].iloc[0]
    if abs(edu["real_loss"] - 100.0) > 1e-9 or abs(edu["not_real"] - 50.0) > 1e-9:
        raise CheckFailed("состав причин внутри разреза посчитан неверно")
    _ok("разрезы: доля от целого, состав причин внутри строки верен")


# --------------------------------------------------------------------------- #
def check_anonymize_doc() -> None:
    """Обезличивание документа: имена уходят, номера удаляются, утечка ловится."""
    df = pd.DataFrame({
        "inn": [7701234567, 7809876543],
        "company_name": ["МБОУ СОШ № 1 ГОРОДА ЗАРЕЧНЫЙ", "ГБУЗ БОЛЬНИЦА № 3"],
        "holding_name": ["Холдинг Минобразования", "Холдинг Минздрава"],
        "n_pairs": [100.0, 50.0]})
    al = RT.Aliases()
    masked = RT.mask(df, al)

    if "inn" in masked.columns:
        raise CheckFailed("номер организации остался в обезличенном кадре")
    for v in masked["company_name"]:
        if not str(v).startswith("Орг-"):
            raise CheckFailed(f"название не заменено токеном: {v}")
    if masked["holding_name"].iloc[0] == df["holding_name"].iloc[0]:
        raise CheckFailed("холдинг не заменён токеном")

    names = RT.collect_names([df])
    if "МБОУ СОШ № 1 ГОРОДА ЗАРЕЧНЫЙ" not in names:
        raise CheckFailed("сбор настоящих названий пропустил название")

    good = "Орг-01 потеряла 100 пар, Орг-02 — 50."
    if RT.check_leak(good, names):
        raise CheckFailed("проверка утечки срабатывает на чистом тексте")
    bad = good + " Это была ГБУЗ БОЛЬНИЦА № 3."
    if not RT.check_leak(bad, names):
        raise CheckFailed("проверка утечки пропустила настоящее название")

    # Свободный текст выводов приходит с ВОССТАНОВЛЕННЫМИ названиями: в HTML они
    # нужны настоящими, в документе — нет. Без отдельного шага названия уехали бы
    # наружу в абзацах при полностью замаскированных таблицах рядом.
    tb = pd.DataFrame({"tb_short_name": ["СРБ", "СибБ"],
                       "region_name": ["Мурманская область", "Ярославская область"],
                       "n_pairs": [1.0, 2.0]})
    al2 = RT.Aliases()
    RT.mask(tb, al2)
    names2 = RT.collect_names([tb])
    if "СРБ" not in names2:
        raise CheckFailed("аббревиатура из трёх букв не попала в список названий — "
                          "сокращённое имя подразделения уедет наружу")
    said = ("Больше всех потеряли СРБ и СибБ, а также Мурманская область.")
    masked = RT.mask_text(said, al2, names2)
    if RT.check_leak(masked, names2):
        raise CheckFailed(f"обезличивание свободного текста не сработало: {masked}")
    if "ТБ-" not in masked or "Регион-" not in masked:
        raise CheckFailed(f"названия в тексте не заменены токенами: {masked}")
    # Токен в тексте обязан совпасть с токеном в таблице: иначе один и тот же
    # объект в абзаце и в таблице читался бы как два разных.
    if al2.alias("ТБ", "СРБ") not in masked:
        raise CheckFailed("текст и таблица получили РАЗНЫЕ токены для одного ТБ")
    _ok("обезличивание документа: имена, аббревиатуры и абзацы выводов "
        "заменены одними и теми же токенами")


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
    _ok("LLM: фолбэк на правила и защёлка недоступного шлюза работают")


def check_row_limit() -> None:
    """Выборка сверх лимита обязана останавливать прогон, а не обрезаться молча."""
    df = pd.DataFrame({"x": range(10)})
    try:
        fetch.guard_rows(df, "проверка", limit=5)
    except fetch.RowLimitError:
        _ok("лимит строк: выборка сверх лимита останавливает прогон")
        return
    raise CheckFailed("выборка сверх лимита прошла молча")


def check_empty_frames() -> None:
    """Пустые кадры не должны ронять расчёты.

    На проме любой необязательный источник может не прочитаться, и тогда в
    расчёты приезжает пустой кадр. Падение здесь означало бы, что один
    недоступный раздел уносит весь прогон.
    """
    empty = pd.DataFrame()
    for fn, args in ((A.trend, (empty,)), (A.threshold, (empty,)),
                     (A.gains_table, (empty,)), (A.load_health, (empty,)),
                     (A.survival_curve, (empty, 0.0)),
                     (A.steps, (empty,)), (A.top_orgs, (empty,)),
                     (A.by_dim, (empty, "agency")),
                     (A.causes_table, (empty, 0.0)),
                     (A.migration, (empty, empty, empty))):
        out = fn(*args)
        if out is None or not isinstance(out, pd.DataFrame) or not out.empty:
            raise CheckFailed(f"{fn.__name__} на пустом кадре вернула {type(out)}")

    marked, meta = A.enrich(empty, empty, empty, empty)
    if not marked.empty:
        raise CheckFailed("разметка пустых потерь вернула непустой кадр")
    _ok("пустые кадры: расчёты не падают, недоступный источник не уносит прогон")



# --------------------------------------------------------------------------- #
# Проверка против синтетики: единственная, которой нужен готовый прогон
# --------------------------------------------------------------------------- #
def check_against_synth(res: dict, expect_path: str | None = None) -> None:
    """Нашёл ли разбор то, что заложено в синтетику.

    Всё остальное в этом файле проверяет, что код не падает и не врёт в
    арифметике. Эта проверка — единственная, которая отвечает на вопрос «а
    находит ли он вообще то, ради чего написан».

    Генератор синтетики закладывает в ряд четыре события с ИЗВЕСТНЫМИ месяцами:
    смену кода зачисления, обрыв у группы организаций, ликвидацию и
    реорганизацию. Разбор обязан найти каждое. Держать эти месяцы в голове
    нельзя, сверять глазами — то же самое, что не сверять, поэтому генератор
    пишет их в файл, а проверка читает.

    Вызывается ОТДЕЛЬНО, из тетрадки, после прогона на синтетике:
        selfcheck.check_against_synth(res)
    В закрытом контуре не запускается — там синтетики нет.
    """
    import json
    from pathlib import Path

    from uzp_dash import config

    path = Path(expect_path) if expect_path else \
        config.OUTPUT_DIR / "synth_payroll_expect.json"
    if not path.exists():
        raise CheckFailed(
            f"нет файла ожиданий {path}. Он пишется генератором синтетики: "
            f"выполните `python3 -m synth`. В закрытом контуре эта проверка "
            f"не применяется — синтетики там нет.")
    exp = json.loads(path.read_text(encoding="utf-8"))

    # --- 1. месяцы-обрывы ---
    st = res.get("steps")
    if st is None or st.empty:
        raise CheckFailed("разбор не нашёл ни одного месяца-обрыва, а в синтетику "
                          "их заложено три")
    found = {f"{pd.Timestamp(d):%Y-%m-%d}" for d in st["report_dt"]}
    for key, human in (("codeswitch_month", "смена кода зачисления"),
                       ("cliff_month", "обрыв у группы организаций"),
                       ("reorg_month", "реорганизация")):
        want = exp.get(key)
        if want and want not in found:
            raise CheckFailed(
                f"событие «{human}» заложено в {want}, но разбор этот месяц "
                f"обрывом не назвал. Найдены: {sorted(found)}")

    # --- 2. реорганизация: пары «откуда→куда» ---
    mig = res.get("migration")
    want_map = exp.get("reorg_map") or {}
    if want_map:
        if mig is None or mig.empty:
            raise CheckFailed(
                f"в синтетику заложено {len(want_map)} переоформлений, разбор не "
                f"нашёл ни одного")
        got = {str(a): str(b) for a, b in zip(mig["inn_from"], mig["inn_to"])}
        missed = {k: v for k, v in want_map.items() if got.get(k) != v}
        # Часть переоформлений мельче порога миграции — это НЕ ошибка, порог
        # для того и стоит. Ошибка — если не нашлось ни одного крупного.
        if len(missed) == len(want_map):
            raise CheckFailed(
                f"ни одно из {len(want_map)} переоформлений не найдено; "
                f"разбор нашёл: {got}")

    # --- 3. ветки лестницы, которые синтетика заведомо содержит ---
    causes = res.get("causes")
    if causes is None or causes.empty:
        raise CheckFailed("лестница причин пуста")
    got_causes = set(causes["cause"])
    for cause in ("code_out_of_list", "below_threshold", "multi_collapsed",
                  "liquidated", "inn_gone", "person_left_bank"):
        if cause not in got_causes:
            raise CheckFailed(
                f"ветка «{cause}» в синтетику заложена, но разбором не найдена — "
                f"значит на проме она тоже не сработает, и это будет незаметно")

    # --- 4. численность базового месяца ---
    # Сходится не точно: генератор считает пары до маскирования грязных номеров
    # организаций, а разбор — после (нечисловой номер в сегмент не попадает).
    # Поэтому сравнение по порядку величины, а не по равенству.
    want_pairs = (exp.get("pairs_by_month") or {}).get(exp.get("base_month"))
    if want_pairs:
        got_pairs = float(res["totals"]["pairs_base"])
        ratio = got_pairs / float(want_pairs)
        if not 0.5 <= ratio <= 1.0:
            raise CheckFailed(
                f"пар в базовом месяце: разбор насчитал {got_pairs:,.0f}, "
                f"генератор заложил {want_pairs:,.0f} (отношение {ratio:.2f}). "
                f"Разбор считает после отсева непригодных номеров организаций, "
                f"поэтому его число обязано быть чуть МЕНЬШЕ, но не вдвое.")

    _ok(f"синтетика: найдены все заложенные события "
        f"({len(found)} обрывов, {0 if mig is None or mig.empty else len(mig)} "
        f"переоформлений, {len(got_causes)} веток причин)")


# --------------------------------------------------------------------------- #
ALL = [
    check_sql_dialect, check_sql_braces, check_partition_filter,
    check_inn_cast_guarded, check_codes_binding, check_workset_order,
    check_prelude_minimal, check_causes_exhaustive, check_ladder_priority,
    check_additive, check_causes_table, check_steps, check_level_rules,
    check_by_dim, check_anonymize_doc, check_llm_fallback, check_row_limit,
    check_empty_frames,
]


def run_all() -> None:
    print("Самопроверки payroll_rgs_cohort:")
    for fn in ALL:
        fn()
    print("Все проверки пройдены.")


if __name__ == "__main__":
    run_all()

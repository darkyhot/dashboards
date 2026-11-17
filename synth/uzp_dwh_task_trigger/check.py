"""Проверка функций витрины uzp_dwh_task_trigger на локальной БД по сценариям спек.

Запуск из корня репозитория:
    .venv/bin/python synth/uzp_dwh_task_trigger/check.py
Грузит DDL и функции из openspec/marts/uzp_dwh_task_trigger/sql (storage-секции Greenplum
убираются), заливает fixtures.sql, запускает расчёты и печатает несовпадения.
"""
import json
import pathlib
import re
import sys

import psycopg2

ROOT = pathlib.Path(__file__).resolve().parents[2]
MART = ROOT / "openspec/marts/uzp_dwh_task_trigger/sql"
T_UZP = "s_grnplm_ld_salesntwrk_pcap_sn_t_uzp"
UZP = "s_grnplm_ld_salesntwrk_pcap_sn_uzp"
TBL = f"{T_UZP}.uzp_dwh_task_trigger"

MONTH_NAMES = {
    "neg": "Отрицательный потенциал", "kp": "Кп первое зачисление",
    "season": "Сезонный тип деятельности", "churn": "Высокая текучка",
    "agr": "Есть договор нет зачисления", "contact": "Контактная политика 4 месяца",
    "check": "Ср чек ниже среднего", "share": "Доля по клиенту больше 90%",
    "out1": "По клиенту был отток в прошлом месяце, численность",
    "out3": "По клиенту был отток в последние три месяца", "epp": "ЕПП флаг",
    "pap1": "Бумажные заявления из ВСП или МП СБОЛ за прошлый месяц",
    "ele1": "Электронные заявления из ВСП или МП СБОЛ за прошлый месяц",
    "pap3": "Бумажные заявления из ВСП или МП СБОЛ за последние три месяца",
    "ele3": "Электронные заявления из ВСП или МП СБОЛ за последние три месяца",
}
W_NO = "Заявление из ВСП без договора и зачислений"
W_YES = "Заявление из ВСП с договором без зачислений"

# (ИНН, триггер, ожидаемые ключи JSON или None — строки быть не должно)
MONTH_EXPECT = [
    (1, "neg", {"month": "2026-10-31", "np_cnt": 55}), (2, "neg", None),
    (3, "kp", {"fl_cnt": 12}), (4, "kp", None),
    (5, "season", {"fl_uplift": 12}), (6, "season", None),
    (7, "churn", {"nfl_cnt": 12, "np_cnt": 2, "portf_last_m": 145, "portf_cur_m": 140}), (8, "churn", None),
    (9, "agr", {"month": "2026-10-31"}), (10, "agr", None), (11, "agr", None),
    (12, "contact", {"task_close_date": "2026-06-20"}), (13, "contact", None),
    (14, "contact", None), (15, "contact", {"task_close_date": None}),
    (16, "check", {"avg_salary": 55000, "client_salary": 33000}), (17, "check", None),
    (19, "share", {"fl_cnt": 12, "fl_dolay": 0.93}), (20, "share", None),
    (21, "out1", {"portf_last_m": 400, "portf_cur_m": 388, "churn_cnt": 12, "churn_pct": 0.03}),
    (22, "out1", None),
    (23, "out3", {"portf_3m_ago": 430, "portf_cur_m": 388, "churn_pct_3m": 0.1}),
    (24, "out3", None), (24, "out1", {"churn_cnt": 19}),
    (25, "epp", {"epp_cnt": 14}), (26, "epp", None),
    (27, "pap1", {"app_cnt": 10, "app_with_zachisl_cnt": 3, "first_app_date": "2026-10-01"}),
    (27, "pap3", {"app_cnt_3m": 27, "app_with_zachisl_cnt_3m": 9, "first_app_date": "2026-08-04"}),
    (27, "ele1", {"app_cnt": 2, "app_with_zachisl_cnt": 1, "first_app_date": "2026-10-11"}),
    (27, "ele3", {"app_cnt_3m": 3, "app_with_zachisl_cnt_3m": 1, "first_app_date": "2026-09-11"}),
    (28, "pap1", {"app_cnt": 4, "app_with_zachisl_cnt": 0, "first_app_date": "2026-10-05"}), (28, "ele1", None), (28, "ele3", None),
    (29, "pap1", None), (29, "ele1", None),
]
WEEK_EXPECT = [
    (31, W_NO, {"app_cnt": 10, "app_with_zachisl_cnt": 3, "app_no_zachisl_cnt": 7,
                "first_app_date": "2026-11-09"}),
    (32, W_NO, None), (33, W_YES, {"app_cnt": 10, "app_no_zachisl_cnt": 7}), (33, W_NO, None),
    (34, W_YES, None), (35, W_NO, None), (36, W_NO, {"app_cnt": 1, "app_no_zachisl_cnt": 1}),
]

errors = []


def gp_to_pg(sql):
    sql = re.sub(r"with\s*\(\s*appendonly[^)]*\)", "", sql, flags=re.I)
    sql = re.sub(r"distributed\s+(by\s*\([^)]*\)|randomly)", "", sql, flags=re.I)
    return re.sub(r"partition\s+by\s+range\s*\([^)]*\)\s*\((?:[^()]|\([^()]*\))*\)", "", sql, flags=re.I)


def check(cond, msg):
    if not cond:
        errors.append(msg)


def run(cur, func, load_id, end_dt):
    cur.execute(f"select {T_UZP}.{func}(%s, now()::timestamp, %s::timestamp)", (load_id, end_dt))
    cur.execute(f"select state_id from {T_UZP}.etl_state where etl_load_id = %s order by state_dttm desc, state_id desc limit 1", (load_id,))
    return cur.fetchone()[0]


def rows(cur, start_dt, end_dt):
    cur.execute(f"select inn, gosb_id, trigger_name, trigger_type, attributes_json, modified_dttm from {TBL} "
                "where start_dt = %s and end_dt = %s", (start_dt, end_dt))
    return cur.fetchall()


def compare(got, expect, label):
    by_key = {(r[0], r[2]): r for r in got}
    for nn, name, exp in expect:
        inn = 990000000000 + nn
        r = by_key.get((inn, name))
        if exp is None:
            check(r is None, f"{label}: {inn} '{name}' — строки быть не должно")
            continue
        if r is None:
            errors.append(f"{label}: {inn} '{name}' — нет строки")
            continue
        js = r[4]
        for k, v in exp.items():
            g = js.get(k, "<нет ключа>")
            ok = abs(float(g) - v) < 1e-9 if isinstance(v, (int, float)) and isinstance(g, (int, float)) else g == v
            check(ok, f"{label}: {inn} '{name}' {k} = {g!r}, ожидалось {v!r}")


def main():
    con = psycopg2.connect("host=127.0.0.1 port=55433 user=dev dbname=dev")
    con.autocommit = True
    cur = con.cursor()
    cur.execute(f"drop table if exists {TBL}")
    for f in [MART / "ddl/uzp_dwh_task_trigger.sql", *sorted((MART / "functions").glob("*.sql"))]:
        cur.execute(gp_to_pg(f.read_text(encoding="utf-8")))
    cur.execute((pathlib.Path(__file__).parent / "fixtures.sql").read_text(encoding="utf-8"))

    # Месяц: дата внутри месяца даёт полный месяц
    st = run(cur, "uzp_dwh_task_trigger_month_calc", 9001, "2026-10-15")
    check(st == 99, f"месяц: статус {st}, ожидался 99")
    got = rows(cur, "2026-10-01", "2026-10-31")
    compare(got, [(nn, MONTH_NAMES[k], e) for nn, k, e in MONTH_EXPECT], "месяц")
    types = {"Отрицательный потенциал": "Основной", "ЕПП флаг": "Вспомогательный"}
    for r in got:
        if r[2] in types:
            check(r[3] == types[r[2]], f"месяц: тип '{r[2]}' = {r[3]}")
    check(len({r[5] for r in got}) == 1, "месяц: modified_dttm не одно на запуск")
    check(all(r[1] == 8644 for r in got if r[0] == 990000000027), "месяц: ГОСБ заявлений 027 не 8644")
    cur.execute(f"select count(*) from {TBL} where start_dt <> date_trunc('month', end_dt) and end_dt = '2026-10-31'")
    check(cur.fetchone()[0] == 0, "месяц: строки с другим периодом")
    n1 = len(got)
    cur.execute(f"select count(*) from {UZP}.uzp_dwh_company_holding_metric where report_dt = '2026-10-31' "
                "and np_cnt > emp_potential_qty and level_name = 'gosb' and org_type = 'inn'")
    check(sum(1 for r in got if r[2] == MONTH_NAMES["neg"]) == cur.fetchone()[0], "месяц: сверка 'отрицательный потенциал'")

    # Повторный запуск
    st = run(cur, "uzp_dwh_task_trigger_month_calc", 9002, "2026-10-31")
    got2 = rows(cur, "2026-10-01", "2026-10-31")
    check(st == 99 and len(got2) == n1, f"повтор: статус {st}, строк {len(got2)} вместо {n1}")
    cur.execute(f"select count(*) from (select 1 from {TBL} group by inn, gosb_id, trigger_name, start_dt, end_dt having count(*) > 1) d")
    check(cur.fetchone()[0] == 0, "дубли ключа в витрине")

    # 98: нет holding_metric за T
    st = run(cur, "uzp_dwh_task_trigger_month_calc", 9003, "2030-01-15")
    check(st == 98, f"98: статус {st}")
    cur.execute(f"select log_message from {T_UZP}.etl_log where etl_load_id = 9003 and log_message like '98%'")
    msg = cur.fetchone()
    check(msg is not None and "uzp_dwh_company_holding_metric" in msg[0] and "последняя дата" in msg[0], f"98: лог {msg}")

    # 97: дубль строки holding_metric даёт дубль ключа
    cur.execute(f"insert into {UZP}.uzp_dwh_company_holding_metric (report_dt, level_name, level_id, org_type, org_id, "
                "current_fot_amt, current_fl_qty, np_cnt, emp_potential_qty, total_emp_qty, new_fl_cnt, zp_fl_perc) "
                "values ('2026-10-31', 'gosb', 9001, 'inn', 990000000001, 100, 1, 55, 40, 10, 0, 0)")
    st = run(cur, "uzp_dwh_task_trigger_month_calc", 9004, "2026-10-31")
    check(st == 97, f"97: статус {st}")
    check(len(rows(cur, "2026-10-01", "2026-10-31")) == n1, "97: витрина изменилась")
    cur.execute(f"delete from {UZP}.uzp_dwh_company_holding_metric where org_id = 990000000001 and report_dt = '2026-10-31'")
    cur.execute(f"insert into {UZP}.uzp_dwh_company_holding_metric (report_dt, level_name, level_id, org_type, org_id, "
                "current_fot_amt, current_fl_qty, np_cnt, emp_potential_qty, total_emp_qty, new_fl_cnt, zp_fl_perc) "
                "values ('2026-10-31', 'gosb', 9001, 'inn', 990000000001, 100, 1, 55, 40, 10, 0, 0)")

    # Неделя: дата внутри недели даёт Пн–Вс; месяц не меняется
    month_before = sorted((r[0], r[2], r[5]) for r in rows(cur, "2026-10-01", "2026-10-31"))
    st = run(cur, "uzp_dwh_task_trigger_week_calc", 9005, "2026-11-12")
    check(st == 99, f"неделя: статус {st}")
    wk = rows(cur, "2026-11-09", "2026-11-15")
    compare(wk, WEEK_EXPECT, "неделя")
    check(all(r[3] == "Основной" for r in wk), "неделя: тип не 'Основной'")
    check(all(r[1] == 8644 for r in wk if 990000000000 < r[0] < 990000000100), "неделя: ГОСБ не 8644")
    st = run(cur, "uzp_dwh_task_trigger_week_calc", 9006, "2026-11-15")
    check(len(rows(cur, "2026-11-09", "2026-11-15")) == len(wk), "неделя: повтор изменил число строк")
    month_after = sorted((r[0], r[2], r[5]) for r in rows(cur, "2026-10-01", "2026-10-31"))
    check(month_before == month_after, "неделя изменила строки месяца")

    print(f"месяц: {n1} строк, неделя: {len(wk)} строк")
    if errors:
        print("НЕ СОВПАЛО:\n  " + "\n  ".join(errors))
        sys.exit(1)
    print("Все проверки пройдены")


if __name__ == "__main__":
    main()

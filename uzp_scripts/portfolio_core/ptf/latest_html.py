"""Пользовательский HTML отчёта за месяц. Прямой код организации виден только здесь."""
from __future__ import annotations

from html import escape
import json

from . import months as M

ORG_LABEL = "\u0418\u041d\u041d"
BANDS = ("До 30 тыс. руб.", "30–60 тыс. руб.", "60–100 тыс. руб.", "100–200 тыс. руб.", "От 200 тыс. руб.")

CSS = """
:root{color-scheme:light;--ink:#142538;--muted:#465a6d;--line:#d7e0e8;--green:#075a47;--red:#983333}
*{box-sizing:border-box}body{margin:0;background:#f3f6f8;color:var(--ink);font:15px/1.55 Arial,"Liberation Sans","DejaVu Sans",system-ui,sans-serif}
header,main,footer{width:min(1140px,100% - 32px);margin:auto}header{padding:32px 0 20px}
h1,h2,h3,p{margin:0}h1{font-size:clamp(27px,4vw,42px);letter-spacing:-.035em}h2{font-size:25px}
h3{font-size:18px;margin-bottom:8px}p{max-width:80ch}nav{display:flex;flex-wrap:wrap;gap:9px 22px;margin-top:15px}
a{color:#14604f;text-underline-offset:3px}section{background:#fff;border:1px solid var(--line);border-radius:16px;padding:22px;margin-bottom:20px}
section>p{margin:4px 0 16px;color:var(--muted)}.kpis{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:13px}
.kpi{background:#edf5f3;border:1px solid #cfe1dc;border-radius:12px;padding:15px}.kpi strong{display:block;font-size:clamp(23px,4vw,32px)}
.kpi>span{display:block;color:var(--muted);font-size:13px}.positive{color:var(--green)}.negative{color:var(--red)}
.block{margin-top:20px}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;text-align:left}
th,td{border-bottom:1px solid var(--line);vertical-align:top;padding:10px}th{font-size:13px;color:var(--muted);background:#f3f6f8}
td.num,th.num{white-space:nowrap;font-variant-numeric:tabular-nums;text-align:right}.code{white-space:nowrap}
.share{display:block;background:#e7edf1;border-radius:6px;height:6px;min-width:58px;margin-top:3px}
.share b{display:block;background:var(--green);height:100%;border-radius:6px}
details{border-top:1px solid var(--line);padding:12px 0}details summary{cursor:pointer;font-weight:650}
details .scroll{margin-top:8px}details p{margin-top:8px}.note{color:var(--muted);margin-top:13px;font-size:14px}
.alert{padding:10px 14px;background:#fff5df;border-left:4px solid #a56400}.note + .note{margin-top:6px}
footer{padding:0 0 24px;color:var(--muted);font-size:13px}
select{font:inherit;color:inherit;background:#fff;border:1px solid #8da0ad;border-radius:7px;padding:6px 10px}
.controls{display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px;margin:12px 0}
[hidden]{display:none!important}
@media(max-width:650px){.kpis{grid-template-columns:1fr}section{padding:14px}th,td{padding:7px}header{padding-top:20px}}
"""


def _number(n):
    return "—" if n is None else f"{n:,.0f}".replace(",", " ")


def _money(n):
    if n is None:
        return "—"
    scale, unit = next(((d, u) for d, u in ((10**12, "трлн"), (10**9, "млрд"), (10**6, "млн"))
                        if abs(n) >= d), (1, ""))
    if scale == 1:
        return f"{_number(n)} руб."
    amount = f"{n / scale:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return f'<span title="{_number(n)} руб.">{amount} {unit} руб.</span>'


def _change(n, currency=False):
    if n is None:
        return "—"
    label = _money(abs(n)) if currency else _number(abs(n))
    sign = "+" if n > 0 else "−" if n < 0 else ""
    css = "positive" if n > 0 else "negative" if n < 0 else ""
    return f'<span class="{css}">{sign}{label}</span>'


def _table(headers, rows, numeric=(), thresholds=None):
    head = "".join(f'<th scope="col"{" class=num" if i in numeric else ""}>{escape(h)}</th>'
                   for i, h in enumerate(headers))
    data = list(rows)
    tags = (' data-impact="' + str(abs(n)) + '"' for n in thresholds) if thresholds is not None else ("",) * len(data)
    body = "".join(f"<tr{attrs}>" + "".join(f'<td{" class=num" if i in numeric else ""}>{c}</td>'
                   for i, c in enumerate(row)) + "</tr>" for attrs, row in zip(tags, data))
    container = ' id="real-rows"' if thresholds is not None else ""
    return f'<div class="scroll"{container}><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _percent(part, total):
    return f"{part / total:.1%}".replace(".", ",") if total else "—"


def _portfolio_sections(report):
    portfolio = report.get("portfolio", {})
    periods = portfolio.get("periods")
    if not periods:
        return ('<section id="portfolio"><h2>Портфель за месяц</h2>'
                '<p>Полная сводка показателей появится после расчёта отчёта.</p></section>')
    now, previous, year, _ = periods
    bands = {(r["mi"], r["band"]): r for r in portfolio["bands"]}

    def band(mi, index):
        return bands.get((mi, index), {"people": 0, "amount": 0})

    def people(mi):
        return sum(band(mi, i)["people"] for i in range(1, len(BANDS) + 1))

    kpis = (
        ("Люди в зарплатном портфеле", people(0), people(1), people(3), False),
        ("ФОТ по ведомостям", now["fot"], previous["fot"], year["fot"], True),
        ("Портфельные зачисления", now["amount"], previous["amount"], year["amount"], True),
    )
    metrics = []
    for title, value, before, last_year, currency in kpis:
        metric = _money(value) if currency else _number(value)
        metrics.append(
            f'<article class="kpi">{title}<strong>{metric}</strong>'
            f'<span>К прошлому месяцу {_change(value - before, currency)}</span>'
            f'<span>К тому же месяцу год назад {_change(value - last_year, currency)}</span></article>'
        )
    rows = []
    for i, label in enumerate(BANDS, 1):
        before, actual = band(1, i), band(0, i)
        portion = actual["people"] / people(0) * 100 if people(0) else 0
        share = f'{_percent(actual["people"], people(0))}<span class="share" aria-hidden="true"><b style="width:{portion:.2f}%"></b></span>'
        rows.append((label, _number(actual["people"]), share, _change(actual["people"] - before["people"]),
                     _money(actual["amount"]), _change(actual["amount"] - before["amount"], currency=True)))
    income = _table(("Зачисления на человека за месяц", "Людей", "Доля людей", "Людей к прошлому месяцу",
                     "Зачисления", "Зачисления к прошлому месяцу"), rows, numeric=range(1, 6))

    places = portfolio.get("gosb", [])
    rows = [(str(g["gosb_id"]) if g["gosb_id"] >= 0 else "ГОСБ не указан",
             _money(g["actual"]), _percent(g["actual"], now["amount"]),
             _change(g["actual"] - g["prior"], currency=True)) for g in places[:6]]
    others = places[6:]
    if others:
        amount = sum(g["actual"] for g in others)
        rows.append(("Остальные ГОСБ", _money(amount), _percent(amount, now["amount"]),
                     _change(amount - sum(g["prior"] for g in others), currency=True)))
    geography = _table(("ГОСБ", "Зачисления", "Доля зачислений", "К прошлому месяцу"), rows, numeric=(1, 2, 3))
    return (
        '<section id="portfolio"><h2>Портфель за месяц</h2>'
        '<p>Каждый человек учитывается один раз по всему банку.</p>'
        f'<div class="kpis">{"".join(metrics)}</div>'
        '<p class="note">ФОТ и портфельные зачисления считаются по разным наборам видов выплат.</p></section>'
        + _bank_change(report)
        + '<section id="structure"><h2>Как изменилась структура</h2>'
        '<p>Сумма на человека включает его зачисления от всех работодателей по правилам портфеля.</p>'
        f'{income}<div class="block"><h3>Зачисления по ГОСБ</h3>{geography}</div></section>'
    )


def _bank_change(report):
    changes = report.get("portfolio", {}).get("changes")
    if not changes:
        return ""
    raw = changes["net"]
    direction = "стал меньше" if raw < 0 else "стал больше" if raw > 0 else "не изменился"
    how_much = f" на {_number(abs(raw))} человек" if raw else ""
    rows = [
        ("Получили портфельные выплаты в этом месяце после отсутствия в прошлом", _change(changes["entered"])),
        ("В этом месяце остались без портфельных выплат в банке", _change(-changes["left"])),
        ("Изменение за месяц", _change(raw)),
    ]
    early = changes.get("early")
    status = changes.get("early_status")
    if early is not None:
        d = str(changes["early_date"])
        if early > 0:
            rows += [
                (f"Из них найден перенос выплат на {d[8:10]}.{d[5:7]}", _number(early)),
                ("Изменение с учётом найденного переноса", _change(raw + early)),
            ]
        context = ("Перенос нашли по совпадению выплаты в последний будний день с выплатами первого числа "
                   "в двух предыдущих месяцах. Первое число отчётного месяца было выходным."
                   if early > 0 else "Переносов первого числа на предыдущий будний день по этому правилу не найдено.")
    elif status == "weekday":
        context = "Первое число было будним. Другие переносы этой проверкой не оцениваются."
    elif status == "no_reference":
        context = "Для проверки переноса первого числа недостаточно сопоставимых рабочих дат."
    else:
        context = "Проверка переноса первого числа не завершена." if changes["left"] else ""
    status_note = ""
    if changes["left"] > (early or 0):
        status_note = (f'<p class="note">По {_number(changes["left"] - (early or 0))} людям причина '
                       'отсутствия выплат пока открыта. Переходы между ГОСБ оставляют человека в портфеле банка.</p>')
    return ('<section id="people-change"><h2>Почему изменилось число людей</h2>'
            f'<p>Портфель за месяц {direction}{how_much}.</p>'
            + _table(("Что произошло", "Людей"), rows, numeric=(1,))
            + (f'<p class="note">{context}</p>' if context else "") + status_note + '</section>')


def _org(report, card):
    return escape(str(report.get("organization_ids", {}).get(card["pair"]) or "—"))


def _group(card):
    group = card["group"].replace("₽", "руб.")
    if group == "Вся пара":
        return "Все получатели в этом ГОСБ"
    return escape(group.replace("Были в двух предыдущих месяцах", "Получали выплаты оба прошлых месяца")
                  .replace("Были только в предыдущем месяце", "Получали выплаты только в прошлом месяце"))


def _followup(card):
    state = card["state"]
    if "витрины расходятся" in state:
        return "Сверить дневную и месячную ведомости."
    if "сводкой" in state or "полноты" in state:
        return "Проверить полноту данных и суммы в сводке."
    return "Проверить крупные выплаты и изменения состава получателей."


def _unique(cards, limit):
    selected = {}
    for card in cards:
        selected.setdefault(card["pair"], card)
    return list(selected.values())[:limit]


RUB_THRESHOLDS = (
    ("0", "Все суммы"), ("100000", "От 100 тыс. руб."), ("200000", "От 200 тыс. руб."),
    ("500000", "От 500 тыс. руб."), ("1000000", "От 1 млн руб."),
    ("3000000", "От 3 млн руб."), ("5000000", "От 5 млн руб."),
    ("10000000", "От 10 млн руб."), ("gt-10000000", "Больше 10 млн руб."),
)

FILTER_SCRIPT = """
(() => {
  const filter = document.getElementById('amount-limit');
  if (!filter) return;
  const rows = [...document.querySelectorAll('#real-rows tr[data-impact]')];
  const output = document.getElementById('amount-status');
  const table = document.getElementById('real-rows');
  function update() {
    const strict = filter.value.startsWith('gt-');
    const limit = Number(strict ? filter.value.slice(3) : filter.value);
    let seen = 0;
    for (const row of rows) {
      const amount = Number(row.dataset.impact);
      const visible = strict ? amount > limit : amount >= limit;
      row.hidden = !visible;
      if (visible) seen++;
    }
    table.hidden = seen === 0;
    output.textContent = seen ? 'Показано ' + seen + ' из ' + rows.length : 'По выбранной сумме ничего нет.';
  }
  filter.addEventListener('change', update);
  update();
})();
"""


def _findings(report):
    cards = report["cards"]
    drops = sorted((c for c in cards if c["confirmed"] is not None and c["confirmed"] < 0),
                   key=lambda c: (c["confirmed"], c["pair"]))
    gains = sorted((c for c in cards if c["confirmed"] is not None and c["confirmed"] > 0),
                   key=lambda c: (-c["confirmed"], c["pair"]))
    visible = _unique(drops, len(drops)) + _unique(gains, len(gains))
    if not visible:
        confirmed_html = '<p class="note">Среди проверенных карточек подтверждённых изменений выплат нет.</p>'
    else:
        options = "".join(f'<option value="{value}">{label}</option>' for value, label in RUB_THRESHOLDS)
        rows = [(_org(report, c), str(c["gosb"]), _group(c), _change(c["confirmed"], currency=True),
                 "Уточнить состав начислений." if c["confirmed"] < 0 else "Проверить, повторяется ли прирост.")
                for c in visible]
        confirmed_html = (
            '<div class="controls"><label for="amount-limit">Показывать изменения на сумму</label>'
            f'<select id="amount-limit">{options}</select><span id="amount-status" role="status"></span></div>'
            + _table((ORG_LABEL, "ГОСБ", "Группа получателей", "Изменение выплат", "Действие"), rows,
                     numeric=(3,), thresholds=[c["confirmed"] for c in visible])
        )
    return (
        '<section id="findings"><h2>Подтверждённые отклонения</h2>'
        '<p>Выплаты тем же людям выросли или снизились относительно ожидания с учётом сезона и календаря.</p>'
        + confirmed_html + '</section>'
    )


def _explanations(report):
    cards = report["cards"]
    transfers = report.get("routes", [])
    transfer_rows = [
        (escape(str(report.get("organization_ids", {}).get(r["pair_code"]) or "—")),
         str(r["source_gosb"]), str(r["target_gosb"]), _number(r["people"]), _money(r["amount"]))
        for r in transfers
    ]
    open_cases = sorted((c for c in cards if c["residual"] is not None and c["residual"] < 0),
                        key=lambda c: (c["residual"], c["pair"]))
    open_rows = [(_org(report, c), str(c["gosb"]), _group(c), _change(c["residual"], currency=True), _followup(c))
                 for c in _unique(open_cases, 6)]
    stopped = []
    for key, branches in report.get("organization_checks", {}).items():
        organization = report.get("organization_ids", {}).get(key)
        origin = next(b["stopped_gosb"] for b in branches)
        previous = sum(b["previous_amount"] for b in branches)
        actual = sum(b["current_amount"] for b in branches)
        action = ("Сверить, какие выплаты продолжились в других ГОСБ." if actual
                  else "Уточнить у организации график платежей.")
        stopped.append((actual - previous, organization or "—", origin, previous, actual, action))
    stopped.sort(key=lambda row: (row[0], row[1], row[2]))
    rows = []
    seen = set()
    for delta, organization, gosb, previous, actual, action in stopped:
        if organization != "—" and organization in seen:
            continue
        seen.add(organization)
        rows.append((escape(organization), str(gosb), _money(previous), _money(actual), _change(delta, currency=True), action))
    if not (transfer_rows or rows or open_rows):
        return ""
    return (
        '<section id="explanations"><h2>Пояснения и открытые вопросы</h2>'
        + ('<details><summary>Выплаты перешли между ГОСБ</summary>'
           '<p>Получатели остались в портфеле банка.</p>'
           + _table((ORG_LABEL, "Раньше платили через ГОСБ", "Теперь платят через ГОСБ", "Людей", "Выплаты"),
                    transfer_rows, numeric=(3, 4))
           + '</details>' if transfer_rows else "")
        + ('<details><summary>В одном из ГОСБ прекратились выплаты</summary>'
           '<p>Суммы по всей организации за два месяца. Причину остановки ещё нужно подтвердить.</p>'
           + _table((ORG_LABEL, "ГОСБ, где выплаты прекратились", "Прошлый месяц", "Текущий месяц", "Разница", "Действие"),
                    rows[:6], numeric=(2, 3, 4)) + '</details>' if rows else "")
        + ('<details><summary>Причину снижения ещё предстоит проверить</summary>'
           + _table((ORG_LABEL, "ГОСБ", "Группа", "Пока без объяснения", "Действие"), open_rows, numeric=(3,))
           + '</details>' if open_rows else "")
        + '</section>'
    )


def _details(report):
    cards = []
    checks = report.get("organization_checks", {})
    for c in report["cards"]:
        title = f'{ORG_LABEL} {_org(report, c)} · ГОСБ {escape(str(c["gosb"]))} · {_group(c)}'
        rows = (
            ("Зачисления за месяц", _money(c["amount"])),
            ("Ожидаемые зачисления", _money(c["expected"])),
            ("Отклонение от ожидания", _change(c["raw"], currency=True)),
            ("Эффект границы расчётного месяца", _change(c["boundary"], currency=True)),
            ("Поправка на распределение выплат по ГОСБ", _change(c["moved"], currency=True)),
            ("Изменение выплат тем же людям", _change(c["confirmed"], currency=True)),
            ("Часть без подтверждённой причины", _change(c["residual"], currency=True)),
        )
        flows = c["flows"]
        previous = [v["previous"] for v in flows.values()]
        total = sum(previous) if all(v is not None for v in previous) else None
        movements = [(escape(label), _number(v["people"]), _money(v["previous"]), _percent(v["previous"], total))
                     for label, v in flows.items()]
        paid = ""
        if c["group"] == "Вся пара" and c["pair"] in checks:
            paid = '<h3 class="block">Выплаты этой организации по ГОСБ</h3>' + _table(
                ("ГОСБ", "Прошлый месяц", "Текущий месяц"),
                [(str(b["gosb"]), _money(b["previous_amount"]), _money(b["current_amount"])) for b in checks[c["pair"]]],
                numeric=(1, 2))
        expected = f', ожидали {_number(c["expected_people"])}' if c["expected_people"] is not None else ""
        cards.append(
            f'<details><summary>{title}</summary><p>Получателей в этом ГОСБ или группе: {_number(c["people"])}{expected}.</p>'
            f'<p>{escape(c["state"])}</p>' + _table(("Показатель", "Сумма"), rows, numeric=(1,))
            + ('<h3 class="block">Что видно по выбывшим</h3>' + _table(
                ("Наблюдение", "Людей", "Их выплаты в прошлом месяце", "Доля прошлых выплат выбывшим"),
                movements, numeric=(1, 2, 3)) if movements else "")
            + paid + '</details>'
        )
    return (
        f'<section><details><summary>Расчёты и проверки по всем случаям ({len(cards)})</summary>'
        '<p>Группы могут пересекаться. Суммы отдельных карточек отражают разные срезы одних зачислений.</p>'
        + "".join(cards) + '</details></section>'
    )


def render(report):
    month = escape(M.long(report["month"]).capitalize())
    warning_keys = ("calendar_warning", "organization_warning", "pair_audit_warning", "portfolio_warning")
    warnings = [report[k] for k in warning_keys if report.get(k)]
    alert = ('<p class="alert">Отчёт предварительный. ' + escape(" ".join(warnings)) + "</p>") if warnings else ""
    coverage = report["totals"]
    foot = (
        f'Денежное сравнение применимо к {_percent(coverage["comparable_actual"], coverage["actual"])} зачислений. '
        'Возвраты в следующем месяце будут видны после его загрузки.'
    )
    if report.get("monthly_audit") and any(not a["ok"] for a in report["monthly_audit"]):
        foot += " Ведомости и сохранённая сводка расходятся. Показатели портфеля взяты из ведомостей."
    # Тетрадка открывает этот блок для фото. Прямые коды остаются в видимом HTML.
    photo = {key: report[key] for key in ("month", "cards", "organization_checks")}
    changes = report.get("portfolio", {}).get("changes")
    if changes:
        photo["portfolio"] = {"changes": changes}
    structure_link = '<a href="#structure">Структура</a>' if report.get("portfolio", {}).get("periods") else ""
    people_link = '<a href="#people-change">Почему изменилось</a>' if changes else ""
    payload = json.dumps(photo, ensure_ascii=False, allow_nan=False).replace("<", r"\u003c")
    return (
        f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Зарплатный портфель · {month}</title><style>{CSS}</style></head><body>'
        f'<header><h1>Зарплатный портфель</h1><p>{month}</p>'
        f'<nav><a href="#portfolio">Портфель</a>{people_link}{structure_link}<a href="#findings">Отклонения</a></nav></header>'
        f'<main>{alert}{_portfolio_sections(report)}{_findings(report)}'
        f'{_explanations(report)}{_details(report)}</main>'
        f'<footer>{escape(foot)}</footer>'
        f'<script id="ptf-review" type="application/json">{payload}</script><script>{FILTER_SCRIPT}</script></body></html>'
    )

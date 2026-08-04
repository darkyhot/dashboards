"""Apple-design токены и базовый CSS (по скиллу apple-design).

Принципы, зашитые здесь:
- системный шрифт (оптический размер уже встроен), size-specific tracking:
  крупный текст — отрицательный трекинг, body — около 0;
- полупрозрачные материалы (backdrop-filter) для чрома;
- сдержанные тени, глубина через слои; светлая/тёмная тема;
- пружиноподобные, короткие переходы; уважение prefers-reduced-motion.
Рендерим самодостаточную страницу: весь CSS инлайн, без внешних ресурсов
(в закрытом контуре нет внешней сети).
"""

# Цветовые токены (light / dark) в виде CSS-переменных.
BASE_CSS = """
:root {
  --bg: #f5f5f7;
  --surface: rgba(255,255,255,0.72);
  --surface-solid: #ffffff;
  --elevated: rgba(255,255,255,0.85);
  --text: #1d1d1f;
  --text-2: #6e6e73;
  --separator: rgba(0,0,0,0.08);
  --accent: #0071e3;
  --good: #34c759;
  --warn: #ff9f0a;
  --bad: #ff3b30;
  --shadow: 0 1px 2px rgba(0,0,0,0.04), 0 8px 24px rgba(0,0,0,0.06);
  --radius: 18px;
  --spring: 420ms cubic-bezier(0.22, 1, 0.36, 1);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #000000;
    --surface: rgba(28,28,30,0.72);
    --surface-solid: #1c1c1e;
    --elevated: rgba(44,44,46,0.85);
    --text: #f5f5f7;
    --text-2: #98989d;
    --separator: rgba(255,255,255,0.10);
    --accent: #0a84ff;
    --good: #30d158;
    --warn: #ff9f0a;
    --bad: #ff453a;
    --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 8px 30px rgba(0,0,0,0.5);
  }
}

* { box-sizing: border-box; }
html { -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 400 17px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", system-ui, sans-serif;
  letter-spacing: 0;
}

.wrap { max-width: 1120px; margin: 0 auto; padding: 56px 24px 96px; }

/* Заголовки: отрицательный трекинг тем сильнее, чем крупнее текст */
h1 { font-size: clamp(32px, 5vw, 52px); line-height: 1.05; letter-spacing: -0.022em; font-weight: 700; margin: 0 0 8px; }
h2 { font-size: 25px; line-height: 1.14; letter-spacing: -0.018em; font-weight: 650; margin: 34px 0 14px; }
h3 { font-size: 20px; line-height: 1.2; letter-spacing: -0.01em; font-weight: 600; margin: 0 0 12px; }
.sub { color: var(--text-2); font-size: 19px; letter-spacing: -0.004em; margin: 0 0 8px; }
.eyebrow { color: var(--accent); font-weight: 600; font-size: 13px; letter-spacing: 0.02em; text-transform: uppercase; }

/* Карточка-материал */
.card {
  background: var(--surface);
  backdrop-filter: saturate(180%) blur(20px);
  -webkit-backdrop-filter: saturate(180%) blur(20px);
  border: 1px solid var(--separator);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 22px 24px;
}

.grid { display: grid; gap: 18px; }
.grid.cols-2 { grid-template-columns: repeat(2, 1fr); }
.grid.cols-3 { grid-template-columns: repeat(3, 1fr); }
.grid.cols-4 { grid-template-columns: repeat(4, 1fr); }
@media (max-width: 820px) { .grid.cols-2, .grid.cols-3, .grid.cols-4 { grid-template-columns: 1fr; } }

/* KPI */
.kpi .label { color: var(--text-2); font-size: 14px; letter-spacing: -0.003em; }
.kpi .value { font-size: 40px; line-height: 1.05; letter-spacing: -0.02em; font-weight: 680; margin: 6px 0 2px; font-variant-numeric: tabular-nums; }
.kpi .delta { font-size: 15px; font-weight: 560; letter-spacing: -0.005em; }
/* подвал карточки: факты ЗАКРЫТОГО месяца — отделены линией от прогноза сверху */
.kpi .foot { font-size: 13px; line-height: 1.45; color: var(--text-2); margin-top: 12px;
             padding-top: 10px; border-top: 1px solid var(--separator);
             font-variant-numeric: tabular-nums; }

/* Бэйдж статуса */
.badge { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 600; letter-spacing: -0.003em; padding: 4px 11px; border-radius: 980px; }
.badge::before { content: ""; width: 7px; height: 7px; border-radius: 50%; }
.badge.good { color: var(--good); background: color-mix(in srgb, var(--good) 14%, transparent); }
.badge.good::before { background: var(--good); }
.badge.warn { color: var(--warn); background: color-mix(in srgb, var(--warn) 14%, transparent); }
.badge.warn::before { background: var(--warn); }
.badge.bad  { color: var(--bad);  background: color-mix(in srgb, var(--bad) 14%, transparent); }
.badge.bad::before  { background: var(--bad); }

/* Прогресс выполнения плана */
.meter { height: 8px; border-radius: 980px; background: var(--separator); overflow: hidden; margin-top: 12px; }
.meter > span { display: block; height: 100%; border-radius: 980px; transition: width var(--spring); }

/* Таблица */
table { width: 100%; border-collapse: collapse; font-size: 15px; }
th, td { text-align: left; padding: 12px 14px; border-bottom: 1px solid var(--separator); letter-spacing: -0.004em; }
th { color: var(--text-2); font-weight: 560; font-size: 13px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr:last-child td { border-bottom: none; }

.footer { margin-top: 64px; color: var(--text-2); font-size: 13px; letter-spacing: -0.003em; }

/* Hero-вердикт */
.hero { padding: 30px 32px; }
.hero .verdict { font-size: clamp(26px, 3.4vw, 40px); line-height: 1.1; letter-spacing: -0.02em; font-weight: 680; margin: 8px 0 6px; }
.hero .big { font-variant-numeric: tabular-nums; }
.hero .row2 { color: var(--text-2); font-size: 17px; letter-spacing: -0.005em; margin-top: 6px; }

/* Тепловая карта */
table.matrix { font-size: 14px; }
table.matrix th, table.matrix td { padding: 9px 12px; white-space: nowrap; }
td.heat { font-weight: 600; font-variant-numeric: tabular-nums; border-radius: 6px; }

/* Проекция закрытия плана */
.proj { position: relative; display: flex; height: 46px; border-radius: 12px; overflow: hidden; background: var(--separator); margin: 8px 0 30px; }
.proj-seg { display: flex; align-items: center; justify-content: center; min-width: 0; transition: width var(--spring); }
.proj-seg span { font-size: 12px; font-weight: 600; color: #fff; letter-spacing: -0.003em; padding: 0 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.proj-seg.fact { background: #48484a; }
.proj-seg.attract { background: var(--good); }
.proj-seg.retention { background: var(--accent); }
.proj-seg.rest { background: var(--separator); }
.proj-plan { position: absolute; top: -6px; bottom: -6px; width: 2px; background: var(--text); }
.proj-plan span { position: absolute; top: -20px; left: 50%; transform: translateX(-50%); font-size: 11px; font-weight: 600; color: var(--text); white-space: nowrap; }

/* Легенда проекции */
.proj-legend { display: flex; flex-wrap: wrap; align-items: center; gap: 14px; margin: -18px 0 4px; font-size: 13px; letter-spacing: -0.003em; }
.proj-legend .lg { display: inline-flex; align-items: center; gap: 6px; color: var(--text-2); }
.proj-legend .lg b { color: var(--text); font-variant-numeric: tabular-nums; }
.proj-legend .lg i { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
.proj-legend .lg i.attract { background: var(--good); }
.proj-legend .lg i.retention { background: var(--accent); }
.proj-legend .lg i.rest { background: var(--separator); border: 1px solid var(--separator); }
.proj-legend .lg-note { color: var(--text-2); font-size: 12px; margin-left: auto; }

/* Мини горизонтальные полоски */
.hbar { display: grid; grid-template-columns: 140px 1fr 64px; align-items: center; gap: 12px; margin: 7px 0; }
.hbar-l { font-size: 14px; color: var(--text-2); letter-spacing: -0.003em; }
.hbar-track { height: 8px; border-radius: 980px; background: var(--separator); overflow: hidden; }
.hbar-track > span { display: block; height: 100%; background: var(--accent); border-radius: 980px; transition: width var(--spring); }
.hbar-v { font-size: 13px; text-align: right; font-variant-numeric: tabular-nums; color: var(--text); }
@media (max-width: 620px) { .hbar { grid-template-columns: 100px 1fr 52px; } }

/* Карточки проблемных ГОСБ */
/* 360px, а не 300: в карточке четыре числовые колонки с пятизначными числами.
   min(360px, 100%) — чтобы на узком экране колонка не вылезала за контейнер
   и страница не ехала горизонтально. */
.gcards { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(360px, 100%), 1fr)); gap: 14px; }
.gcard { position: relative; overflow: hidden; padding: 16px 18px; }
.gcard::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; background: var(--bad); }
.gcard.warn::before { background: var(--warn); }
.gcard.good::before { background: var(--good); }
/* Карточка кликабельна целиком — открывает разбор прогноза */
.gcard { cursor: pointer; transition: box-shadow var(--spring), transform var(--spring); }
.gcard:hover { transform: translateY(-1px); }
.gcard:focus-visible { outline: 2px solid var(--good); outline-offset: 2px; }
.g-more { margin-top: 10px; font-size: 12.5px; font-weight: 600; color: var(--good);
          letter-spacing: -0.004em; }
/* Переход на уровень ниже (карточка ТБ в отчёте банка) — отдельное действие,
   а не открытие оверлея, поэтому и выглядит как ссылка, а не как подпись */
.g-drill { border-top: 1px solid var(--separator); padding-top: 8px; cursor: pointer; }
.g-drill:hover { text-decoration: underline; }

/* Вкладки уровней: СБ и каждый ТБ. Липкие — разбор длинный, а переключаться
   между уровнями надо из любого места страницы. */
.lvls { position: sticky; top: 0; z-index: 5; display: flex; flex-wrap: wrap; gap: 6px;
        padding: 10px 0; margin: 0 0 8px; background: var(--bg);
        border-bottom: 1px solid var(--separator); }
.lvl-tab { border: 0; border-radius: 980px; padding: 6px 14px; cursor: pointer;
           font-size: 13px; font-weight: 600; letter-spacing: -0.006em;
           background: var(--separator); color: var(--text-2);
           transition: background var(--spring), color var(--spring); }
.lvl-tab:hover { color: var(--text); }
.lvl-tab.on { background: var(--text); color: var(--bg); }
.lvl-back { display: flex; align-items: center; gap: 12px; margin: 0 0 14px; }
.lvl-back button { border: 0; background: var(--separator); color: var(--text);
                   border-radius: 980px; padding: 5px 12px; cursor: pointer;
                   font-size: 12.5px; font-weight: 600; }
.lvl-back span { font-size: 13px; color: var(--text-2); }
.gcard .g-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.gcard .g-head h3 { font-size: 17px; }
.gcard .g-ex { font-variant-numeric: tabular-nums; font-weight: 680; font-size: 20px; letter-spacing: -0.02em; }
/* Строки водопада и таблицы НЕ привязаны к .gcard: те же блоки рендерятся
   в разделе «Из чего складывается прогноз» по ТБ и в оверлее по ГОСБ. */
.g-seg { font-size: 13.5px; line-height: 1.5; letter-spacing: -0.004em; margin: 5px 0; display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px; }
.g-seg b { font-variant-numeric: tabular-nums; }
.g-hint { color: var(--text-2); font-size: 12.5px; letter-spacing: -0.003em; }
/* Вывод LLM в конце своего раздела — тише данных, но с явной пометкой авторства */
.ai { margin-top: 14px; border-left: 3px solid var(--accent); }
.ai-head { font-size: 12px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase;
           color: var(--accent); margin-bottom: 6px; }
/* Таблица «прогноз / план / недобор / орг»: строка «Всего» по ГОСБ и строки сегментов
   в одних колонках — выравнивание делает сравнение за читателя. */
.g-tbl { margin: 12px 0 0; overflow-x: auto; }
/* первая колонка вмещает бейдж целиком («КСБ 99.9%»), числовые — своё значение;
   minmax(0,...) для чисел не годится: ячейка сжалась бы уже содержимого */
.g-tbl .g-row { display: grid; grid-template-columns: minmax(104px, 1.25fr) repeat(4, minmax(46px, 1fr));
                gap: 6px; align-items: baseline; font-size: 13px; line-height: 1.5;
                letter-spacing: -0.004em; padding: 3px 0; }
.g-tbl .g-row > span:not(:first-child) { text-align: right; font-variant-numeric: tabular-nums; }
/* nowrap ТОЛЬКО у бейджей таблицы: «КСБ» и «104%» всегда в одной строке.
   Глобально нельзя — длинные бейджи («план выполняется, но западает …») должны переноситься. */
.g-tbl .badge { white-space: nowrap; }
/* в оверлее к таблице добавляются отток и пайплайн — разбор на грейне (ГОСБ, сегмент) */
.g-tbl.wide .g-row { grid-template-columns: minmax(104px, 1.25fr) repeat(6, minmax(46px, 1fr)); }
.g-tbl .g-row.head { color: var(--text-2); font-size: 11.5px; letter-spacing: 0; padding-bottom: 1px; }
.g-tbl .g-row.total { font-weight: 620; border-bottom: 1px solid var(--separator); padding-bottom: 6px; margin-bottom: 2px; }
.g-tbl .g-row.rest, .g-tbl .g-row.ok { color: var(--text-2); }

/* Оверлей с разбором прогноза по ГОСБ */
dialog.gd { border: 0; padding: 0; background: transparent; max-width: 760px; width: 92vw;
            max-height: 88vh; color: var(--text); }
dialog.gd::backdrop { background: rgba(0, 0, 0, 0.45); backdrop-filter: blur(6px); }
dialog.gd[open] { animation: gd-in 0.22s cubic-bezier(0.22, 1, 0.36, 1); }
@keyframes gd-in { from { opacity: 0; transform: scale(0.97) translateY(6px); } }
/* лист непрозрачный (--surface полупрозрачна и на размытой подложке «плывёт») */
.gd-sheet { background: var(--surface-solid); border-radius: 20px; padding: 22px 24px;
            max-height: 88vh; overflow-y: auto; box-shadow: 0 24px 60px rgba(0,0,0,0.28); }
.gd-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px;
           position: sticky; top: -22px; background: var(--surface-solid); padding: 4px 0 8px;
           margin: -4px 0 4px; }
.gd-close { border: 0; background: var(--separator); color: var(--text); cursor: pointer;
            border-radius: 980px; width: 30px; height: 30px; font-size: 16px; line-height: 1; }
.gd-block { margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--separator); }
.gd-block h4 { margin: 0 0 8px; font-size: 14px; letter-spacing: -0.01em; }
.gd-note { font-size: 13px; color: var(--text-2); line-height: 1.45; margin: 6px 0 0; }
.gd-row { display: grid; grid-template-columns: 1fr 72px 1.35fr; gap: 10px;
          align-items: baseline; font-size: 13px; line-height: 1.45; padding: 4px 0;
          border-top: 1px solid var(--separator); }
.gd-row:first-of-type { border-top: 0; }
.gd-row > span:nth-child(2) { text-align: right; font-variant-numeric: tabular-nums;
                              font-weight: 620; }
.gd-row .gd-why { color: var(--text-2); font-size: 12.5px; }

/* Группы оттока по причине: нативный <details>, шапка в колонках строки организации */
.gd-grp { border-top: 1px solid var(--separator); }
.gd-grp:first-of-type { border-top: 0; }
.gd-grp > summary { display: grid; grid-template-columns: 1fr 72px 1.35fr; gap: 10px;
                    align-items: baseline; font-size: 13px; line-height: 1.45;
                    padding: 7px 8px; margin: 0 -8px; cursor: pointer; border-radius: 8px;
                    list-style: none;
                    /* полоска доли блока — фон под шапкой, ширина из --share */
                    background: linear-gradient(to right, var(--separator) var(--share),
                                                transparent var(--share)); }
.gd-grp > summary::-webkit-details-marker { display: none; }
/* подсветка через inset-тень, а не background: иначе она стёрла бы полоску доли */
.gd-grp > summary:hover { box-shadow: inset 0 0 0 999px rgba(127, 127, 127, 0.09); }
.gd-grp > summary > span:nth-child(2) { text-align: right; font-variant-numeric: tabular-nums;
                                        font-weight: 620; }
.gd-gt { font-weight: 620; letter-spacing: -0.006em; }
.gd-gt::before { content: "›"; display: inline-block; width: 12px; color: var(--text-2);
                 transition: transform 0.18s ease; }
.gd-grp[open] > summary .gd-gt::before { transform: rotate(90deg); }
.gd-gt i { font-style: normal; font-weight: 400; color: var(--text-2); margin-left: 8px;
           font-variant-numeric: tabular-nums; }
.gd-sub { color: var(--text-2); font-size: 12.5px; }
.gd-rows { padding: 2px 0 8px 20px; }
.g-do { font-size: 14px; line-height: 1.4; letter-spacing: -0.004em; margin: 12px 0 0; padding-top: 10px; border-top: 1px solid var(--separator); }
.g-do b { font-variant-numeric: tabular-nums; }
.g-act { font-size: 12.5px; color: var(--text-2); margin-top: 8px; line-height: 1.35; }

/* Чипы западающих сегментов */
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 2px; }
.chips .clab { font-size: 12px; color: var(--text-2); align-self: center; margin-right: 2px; }
.chip { display: inline-block; font-size: 12px; padding: 3px 10px; border-radius: 980px; background: var(--separator); color: var(--text-2); margin: 3px 5px 0 0; letter-spacing: -0.003em; }
.chip.bad { color: var(--bad); background: color-mix(in srgb, var(--bad) 13%, transparent); }

/* Фильтры и пагинация интерактивной таблицы */
.filters { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 14px; }
.filters input, .filters select {
  font: inherit; font-size: 14px; padding: 9px 13px; border-radius: 11px;
  border: 1px solid var(--separator); background: var(--surface-solid); color: var(--text);
  letter-spacing: -0.003em; outline: none;
}
.filters input { flex: 1; min-width: 190px; }
.filters input:focus, .filters select:focus { border-color: var(--accent); }
.tbl-scroll { overflow-x: auto; }
.pager { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 14px; }
.pager .btns { display: flex; gap: 8px; }
.pager button {
  font: inherit; font-size: 14px; padding: 8px 15px; border-radius: 11px;
  border: 1px solid var(--separator); background: var(--surface-solid); color: var(--text); cursor: pointer;
  transition: transform 100ms ease-out;
}
.pager button:active { transform: scale(0.97); }
.pager button:disabled { opacity: 0.4; cursor: default; }
.pager .info { font-size: 13px; color: var(--text-2); font-variant-numeric: tabular-nums; }

@media (prefers-reduced-motion: reduce) {
  .meter > span, .proj-seg, .hbar-track > span { transition: none; }
  .pager button { transition: none; }
  .gcard { transition: none; }
  .gcard:hover { transform: none; }
  dialog.gd[open] { animation: none; }
  .gd-gt::before { transition: none; }
  .lvl-tab { transition: none; }
}
"""

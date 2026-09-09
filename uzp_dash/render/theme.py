"""Apple-design токены и базовый CSS (по скиллу apple-design).

Отчёт читает руководитель, а не аналитик: страница устроена как презентация с
вертикальным скроллом — крупные числа, один блок на экран, красное и зелёное.
Поэтому базовые размеры здесь заметно больше обычных «дэшбордных».

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
  /* 18px, а не 17: отчёт читают с проектора и с ноутбука люди, которым мелкий
     текст неудобен. Ниже по файлу нет ни одного видимого текста меньше 15px. */
  font: 400 18px/1.55 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", system-ui, sans-serif;
  letter-spacing: 0;
}

.wrap { max-width: 1180px; margin: 0 auto; padding: 28px 24px 120px; }

/* Заголовки: отрицательный трекинг тем сильнее, чем крупнее текст */
h1 { font-size: clamp(30px, 4vw, 44px); line-height: 1.06; letter-spacing: -0.022em; font-weight: 700; margin: 0 0 6px; }
h2 { font-size: clamp(26px, 3vw, 36px); line-height: 1.14; letter-spacing: -0.018em; font-weight: 680; margin: 0 0 10px; }
h3 { font-size: 21px; line-height: 1.25; letter-spacing: -0.01em; font-weight: 620; margin: 0 0 12px; }
.sub { color: var(--text-2); font-size: 19px; letter-spacing: -0.004em; margin: 0 0 8px; }
.eyebrow { color: var(--accent); font-weight: 600; font-size: 15px; letter-spacing: 0.02em; text-transform: uppercase; }

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

/* Бэйдж статуса */
.badge { display: inline-flex; align-items: center; gap: 6px; font-size: 15px; font-weight: 600; letter-spacing: -0.003em; padding: 4px 11px; border-radius: 980px; }
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
table { width: 100%; border-collapse: collapse; font-size: 16px; }
th, td { text-align: left; padding: 12px 14px; border-bottom: 1px solid var(--separator); letter-spacing: -0.004em; }
th { color: var(--text-2); font-weight: 560; font-size: 15px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr:last-child td { border-bottom: none; }

.footer { margin-top: 64px; color: var(--text-2); font-size: 15px; letter-spacing: -0.003em; }

/* Тепловая карта */
table.matrix { font-size: 15px; }
table.matrix th, table.matrix td { padding: 8px 11px; white-space: nowrap; }
td.heat { font-weight: 600; font-variant-numeric: tabular-nums; border-radius: 6px; }

/* Строки блоков и таблицы живут и на слайде уровня, и в разборе отделения —
   поэтому не привязаны ни к какому контейнеру. */
.g-seg { font-size: 17px; line-height: 1.6; letter-spacing: -0.004em; margin: 5px 0; display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px; }
.g-seg b { font-variant-numeric: tabular-nums; }
.g-hint { color: var(--text-2); font-size: 15px; letter-spacing: -0.003em; }
/* Вывод LLM в конце своего раздела — тише данных, но с явной пометкой авторства */
.ai { margin-top: 14px; border-left: 3px solid var(--accent); }
.ai-head { font-size: 15px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase;
           color: var(--accent); margin-bottom: 6px; }
/* Таблица «прогноз / план / недобор / орг»: строка «Всего» по ГОСБ и строки сегментов
   в одних колонках — выравнивание делает сравнение за читателя. */
.g-tbl { margin: 12px 0 0; overflow-x: auto; }
/* первая колонка вмещает бейдж целиком («КСБ 99.9%»), числовые — своё значение;
   minmax(0,...) для чисел не годится: ячейка сжалась бы уже содержимого */
.g-tbl .g-row { display: grid; grid-template-columns: minmax(104px, 1.25fr) repeat(4, minmax(46px, 1fr));
                gap: 8px; align-items: baseline; font-size: 16px; line-height: 1.6;
                letter-spacing: -0.004em; padding: 5px 0; }
.g-tbl .g-row > span:not(:first-child) { text-align: right; font-variant-numeric: tabular-nums; }
/* nowrap ТОЛЬКО у бейджей таблицы: «КСБ» и «104%» всегда в одной строке.
   Глобально нельзя — длинные бейджи («план выполняется, но западает …») должны переноситься. */
.g-tbl .badge { white-space: nowrap; }
/* в оверлее к таблице добавляются отток и пайплайн — разбор на грейне (ГОСБ, сегмент) */
.g-tbl.wide .g-row { grid-template-columns: minmax(104px, 1.25fr) repeat(6, minmax(46px, 1fr)); }
.g-tbl .g-row.head { color: var(--text-2); font-size: 15px; letter-spacing: 0; padding-bottom: 2px; }
.g-tbl .g-row.total { font-weight: 620; border-bottom: 1px solid var(--separator); padding-bottom: 6px; margin-bottom: 2px; }
.g-tbl .g-row.rest, .g-tbl .g-row.ok { color: var(--text-2); }

/* Разбор отделения — раскрывается прямо в ленте, без модального окна */
.gd-block { margin-top: 26px; padding-top: 18px; border-top: 1px solid var(--separator); }
.gd-block:first-child { border-top: 0; padding-top: 6px; }
.gd-block h4 { margin: 0 0 12px; font-size: 19px; font-weight: 640; letter-spacing: -0.012em; }
.gd-note { font-size: 15px; color: var(--text-2); line-height: 1.5; margin: 8px 0 0; }
/* строка организации: имя · сколько человек · что произошло */
.gd-row { display: grid; grid-template-columns: 1fr 90px 1.35fr; gap: 14px;
          align-items: baseline; font-size: 16px; line-height: 1.5; padding: 8px 0;
          border-top: 1px solid var(--separator); }
.gd-row:first-of-type { border-top: 0; }
.gd-row > span:nth-child(2) { text-align: right; font-variant-numeric: tabular-nums;
                              font-weight: 640; }
.gd-row .gd-why { color: var(--text-2); font-size: 15px; }
/* закреплённый сотрудник — подпись под названием организации */
.gd-row .gd-emp { display: block; color: var(--text-2); font-size: 15px; margin-top: 2px; }
@media (max-width: 720px) {
  .gd-row { grid-template-columns: 1fr 80px; }
  .gd-row .gd-why { grid-column: 1 / -1; }
}

/* Группы оттока по причине: нативный <details>, шапка в колонках строки организации */
.gd-grp { border-top: 1px solid var(--separator); }
.gd-grp:first-of-type { border-top: 0; }
.gd-grp > summary { display: grid; grid-template-columns: 1fr 90px 1.35fr; gap: 14px;
                    align-items: baseline; font-size: 16px; line-height: 1.5;
                    padding: 11px 10px; margin: 0 -10px; cursor: pointer; border-radius: 10px;
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
.gd-sub { color: var(--text-2); font-size: 15px; }
.gd-rows { padding: 2px 0 8px 20px; }
.g-do { font-size: 17px; line-height: 1.5; letter-spacing: -0.004em; margin: 16px 0 0; padding-top: 14px; border-top: 1px solid var(--separator); }
.g-do b { font-variant-numeric: tabular-nums; }
.g-act { font-size: 15px; color: var(--text-2); margin-top: 10px; line-height: 1.5; }

/* Фильтры и пагинация интерактивной таблицы */
.filters { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 14px; }
.filters input, .filters select {
  font: inherit; font-size: 16px; padding: 9px 13px; border-radius: 11px;
  border: 1px solid var(--separator); background: var(--surface-solid); color: var(--text);
  letter-spacing: -0.003em; outline: none;
}
.filters input { flex: 1; min-width: 190px; }
.filters input:focus, .filters select:focus { border-color: var(--accent); }
.tbl-scroll { overflow-x: auto; }
.pager { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 14px; }
.pager .btns { display: flex; gap: 8px; }
.pager button {
  font: inherit; font-size: 16px; padding: 8px 15px; border-radius: 11px;
  border: 1px solid var(--separator); background: var(--surface-solid); color: var(--text); cursor: pointer;
  transition: transform 100ms ease-out;
}
.pager button:active { transform: scale(0.97); }
.pager button:disabled { opacity: 0.4; cursor: default; }
.pager .info { font-size: 15px; color: var(--text-2); font-variant-numeric: tabular-nums; }

/* ============ Презентация: экран = слайд, скролл защёлкивается ============= */
/* Листание на уровне документа: видимый уровень — единственный в потоке, поэтому
   снап работает по странице целиком. scroll-padding — под закреплённой полосой
   крошек, иначе верх слайда уезжал бы под неё. */
html { scroll-snap-type: y mandatory; scroll-behavior: smooth; scroll-padding-top: 0; }
body { overflow-x: hidden; }
.wrap { max-width: none; margin: 0; padding: 0; }

/* Закреплённая полоса: где читатель и как перейти к другому банку */
.topbar { position: fixed; top: 0; left: 0; right: 0; z-index: 20; height: 56px;
          display: flex; align-items: center; gap: 20px; padding: 0 28px;
          background: var(--elevated); border-bottom: 1px solid var(--separator);
          backdrop-filter: saturate(180%) blur(20px);
          -webkit-backdrop-filter: saturate(180%) blur(20px); }
.crumbs { display: flex; align-items: center; gap: 8px; }
.crumb { border: 0; background: none; color: var(--accent); cursor: pointer;
         font: inherit; font-size: 16px; font-weight: 600; padding: 4px 2px;
         border-radius: 8px; }
.crumb:hover { text-decoration: underline; }
.crumb.on { color: var(--text); font-weight: 700; }
.crumb-sep { color: var(--text-2); font-size: 16px; }
.topbar-name { font-size: 17px; font-weight: 650; letter-spacing: -0.01em;
               white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.topbar-when { font-size: 15px; color: var(--text-2); white-space: nowrap; }
@media (max-width: 900px) { .topbar-when, .topbar-name { display: none; } }
.picker { display: flex; align-items: center; gap: 10px; margin-left: auto;
          font-size: 15px; color: var(--text-2); }
.picker select { font: inherit; font-size: 15px; padding: 6px 12px; border-radius: 10px;
                 border: 1px solid var(--separator); background: var(--surface-solid);
                 color: var(--text); }

/* СЛАЙД = ровно одно окно. Тело прокручивается внутри, шапка и подвал закреплены —
   иначе длинный разбор отделения растянул бы слайд и листание сломалось бы. */
.slide {
  height: 100vh; height: 100svh;
  scroll-snap-align: start; scroll-snap-stop: always;
  display: flex; flex-direction: column;
  padding: 76px 28px 20px; max-width: 1320px; margin: 0 auto;
  border-top: 1px solid var(--separator);
}
.slide:first-of-type { border-top: 0; }
.slide-head { flex: none; }
.slide-q { margin: 0 0 6px; }
.slide-hint { color: var(--text-2); font-size: 16px; line-height: 1.45;
              max-width: 1000px; margin: 0 0 14px; }
.slide-body { flex: 1; min-height: 0; overflow-y: auto; padding-right: 4px; }
.slide-foot { flex: none; display: flex; gap: 12px; align-items: center;
              padding-top: 14px; margin-top: 10px;
              border-top: 1px solid var(--separator); }

/* Точки-индикатор: сколько слайдов и где мы сейчас */
.dots { position: fixed; right: 14px; top: 50%; transform: translateY(-50%);
        z-index: 15; display: flex; flex-direction: column; gap: 10px; }
.dots .dot { border: 0; background: none; padding: 4px; cursor: pointer; line-height: 0; }
.dots .dot i { display: block; width: 10px; height: 10px; border-radius: 50%;
               background: var(--separator); border: 1px solid var(--text-2);
               transition: background var(--spring), transform var(--spring); }
.dots .dot.on i { background: var(--accent); border-color: var(--accent);
                  transform: scale(1.35); }
@media (max-width: 900px) { .dots { display: none; } }

/* Два главных числа одного кегля: процент плана и сколько людей не хватает */
.hero2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
         gap: 22px; margin: 0 0 22px; }
.hero-one { background: var(--surface); border: 1px solid var(--separator);
            border-radius: var(--radius); box-shadow: var(--shadow); padding: 22px 26px; }
.hero-v { font-size: clamp(44px, 5.5vw, 68px); line-height: 1.0; font-weight: 700;
          letter-spacing: -0.03em; font-variant-numeric: tabular-nums; }
.hero-c { font-size: 20px; font-weight: 600; line-height: 1.35; margin-top: 8px; }
.hero-sub { font-size: 16px; color: var(--text-2); margin-top: 6px; line-height: 1.45; }

/* Три числа портфеля */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
         gap: 16px; margin: 0 0 18px; }
.stat { background: var(--surface); border: 1px solid var(--separator);
        border-radius: var(--radius); padding: 16px 20px; }
.stat-v { font-size: 34px; line-height: 1.05; font-weight: 680;
          letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
.stat-c { font-size: 17px; line-height: 1.4; margin-top: 6px; }
.stat-sub { font-size: 15px; color: var(--text-2); margin-top: 4px; }

/* Твёрдые факты и легенда цветов */
.facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
         gap: 8px 26px; margin: 0 0 16px; }
.fact { font-size: 16px; color: var(--text-2); line-height: 1.45; }
.fact b { color: var(--text); }
.legend { display: flex; flex-wrap: wrap; gap: 18px; font-size: 15px; }
.legend .lg { display: inline-flex; align-items: center; gap: 8px; color: var(--text-2); }
.legend .lg::before { content: ""; width: 12px; height: 12px; border-radius: 4px; }
.legend .lg.good::before { background: var(--good); }
.legend .lg.warn::before { background: var(--warn); }
.legend .lg.bad::before { background: var(--bad); }

.note { font-size: 17px; line-height: 1.5; max-width: 1000px; margin: 14px 0 0; }
.note.dim { color: var(--text-2); font-size: 16px; }
.card-sub { color: var(--text-2); font-size: 15px; margin: -6px 0 12px; }
.matrix-pair { margin-top: 22px; }

/* ==================== Рейтинг единиц ==================== */
.rk { margin-top: 2px; }
.rk-row {
  display: grid; align-items: center; gap: 16px;
  grid-template-columns: minmax(150px, 1.1fr) minmax(90px, 0.9fr) 76px minmax(180px, 1.3fr) auto;
  padding: 12px 16px; border-radius: 12px; border: 1px solid var(--separator);
  background: var(--surface); margin-bottom: 8px; border-left: 5px solid var(--text-2);
}
.rk-row.good { border-left-color: var(--good); }
.rk-row.warn { border-left-color: var(--warn); }
.rk-row.bad  { border-left-color: var(--bad); }
.rk-name { font-size: 18px; font-weight: 620; letter-spacing: -0.008em; }
.rk-sub { display: block; font-size: 15px; color: var(--text-2); font-weight: 400; }
.rk-bar { height: 10px; border-radius: 980px; background: var(--separator); overflow: hidden; }
.rk-bar > span { display: block; height: 100%; border-radius: 980px;
                 transition: width var(--spring); }
.rk-pct { font-size: 24px; font-weight: 700; text-align: right;
          font-variant-numeric: tabular-nums; letter-spacing: -0.02em; }
.rk-right { font-size: 16px; color: var(--text-2); }
.rk-right b { color: var(--text); font-variant-numeric: tabular-nums; }
.ok-txt { color: var(--good); font-weight: 600; }
.rk-act { text-align: right; }
.go { font: inherit; font-size: 15px; font-weight: 620; padding: 9px 16px;
      border-radius: 980px; border: 0; cursor: pointer;
      background: var(--accent); color: #fff; white-space: nowrap;
      transition: filter var(--spring); }
.go:hover { filter: brightness(1.08); }
.go.ghost { background: var(--separator); color: var(--text); }
@media (max-width: 860px) {
  .rk-row { grid-template-columns: 1fr auto; }
  .rk-bar { display: none; }
  .rk-right, .rk-act { grid-column: 1 / -1; text-align: left; }
}

/* Слайд разбора отделения: цветная полоса статуса слева */
.slide.unit { border-left: 6px solid var(--text-2); }
.slide.unit.good { border-left-color: var(--good); }
.slide.unit.warn { border-left-color: var(--warn); }
.slide.unit.bad  { border-left-color: var(--bad); }

/* Печать: слайды становятся обычными страницами, снап не нужен */
@media print {
  html { scroll-snap-type: none; }
  .slide { height: auto; page-break-after: always; }
  .slide-body { overflow: visible; }
  .dots, .topbar { display: none; }
}

@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  .meter > span, .rk-bar > span, .dots .dot i { transition: none; }
  .pager button, .crumb, .go { transition: none; }
  .gd-gt::before { transition: none; }
}
"""

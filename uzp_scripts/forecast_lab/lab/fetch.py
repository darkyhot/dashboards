"""Шаг 1: выгрузка панели из витрины в кэш на диске.

Делается ОДИН раз и долго (на проме порядка часа), дальше перебор гоняется по кэшу
сколько угодно раз — в том числе после того, как в сетку добавят новые варианты.

Две вещи определяют конструкцию.

1. **Лимит строк.** Правило 12 Datalab — не больше 1 млн строк в одной выборке, а
   пар (ГОСБ, организация) по банку порядка 5.7 млн на месяц. Поэтому выгрузка
   режется на чанки по ГОСБ, размер чанка планируется ЗАРАНЕЕ по счётчику строк
   (`PANEL_COUNTS`), а перед сборкой стоит проверка, которая ПАДАЕТ с ошибкой, а не
   обрезает выборку молча: тихо усечённая панель дала бы правдоподобные, но неверные
   итоги.

2. **Форма кэша.** Хранить длинную таблицу (пара × месяц) невыгодно: перебор
   обращается к истории «все месяцы до M» тысячи раз. Поэтому каждый чанк
   раскладывается в ШИРОКИЕ матрицы `(пара × месяц)` по одной на признак, float32.
   Тогда история до месяца j — это срез `arr[:, :j]` без единого пересчёта.

Кэш пишется в формате npz (в стандартной поставке, pyarrow на проме может не быть).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from uzp_dash import db, progress
from uzp_dash.dashboards.tb_health import segments

from . import queries as LQ

# Столько строк в одной выборке считаем предельным (правило 12 Datalab).
MAX_ROWS = 1_000_000
# Планируем чанк с запасом: счётчик строк снят раньше выгрузки, и между ними
# витрина может подрасти.
CHUNK_TARGET = int(MAX_ROWS * 0.75)

# Признаки панели: имя в кэше -> колонка запроса. Все — ИСТОРИЧЕСКИЕ, ни одной
# смотрящей вперёд (список запрета — LQ.FORBIDDEN_COLUMNS).
PANEL_VARS = ("fl", "out_q", "new_fl", "np", "fot", "emp", "pot")
FO_VARS = ("fo_out", "fo_plan", "fo_fact", "fo_calc")


class RowLimitError(RuntimeError):
    """Выборка больше лимита. Обрезать нельзя — только сузить запрос."""


def guard_rows(df: pd.DataFrame, name: str, limit: int | None = None) -> pd.DataFrame:
    """Не дать выборке съесть память ядра.

    Лимит разрешается ВНУТРИ функции, а не в значении по умолчанию: иначе он
    зафиксируется в момент импорта, и правка MAX_ROWS перед прогоном молча ни на
    что не повлияет — то есть защита окажется выключенной ровно тогда, когда её
    решили ужесточить.
    """
    limit = MAX_ROWS if limit is None else limit
    if len(df) > limit:
        raise RowLimitError(
            f"{name}: {len(df):,} строк — больше лимита {limit:,}. "
            f"Сузьте чанк (уменьшите CHUNK_TARGET) или окно месяцев.")
    return df


# --------------------------------------------------------------------------- #
@dataclass
class Chunk:
    """Кусок панели: пары строк, месяцы столбцов.

    `present` отделяет «ноль» от «строки не было»: у витрины это разные вещи, и
    средняя по k месяцам считается только по тем, где пара вообще была.
    """
    gosb: np.ndarray            # (n,) int32  — new_gosb_id
    tb: np.ndarray              # (n,) int32
    inn: np.ndarray             # (n,) int64
    seg: np.ndarray             # (n,) int16  — код сегмента (extended_dim_1), -1 неизвестен
    months: list                # список str YYYY-MM
    present: np.ndarray         # (n, m) bool
    var: dict                   # имя -> (n, m) float32

    @property
    def n(self) -> int:
        return len(self.inn)

    def hist(self, name: str, j: int) -> np.ndarray:
        """История признака СТРОГО до месяца j. Единственный способ дотянуться до
        прошлого: срез делается здесь, и подглядеть в месяц j через него нельзя."""
        return self.var[name][:, :j]


# --------------------------------------------------------------------------- #
def pack_chunks(olds_of: dict, rows_of: dict, target: int) -> list[dict]:
    """Разложить ГОСБ по чанкам, ни один из которых не превышает `target` строк.

    Чистая функция без БД — самая хитрая часть выгрузки, и проверяется отдельно.

    Мелкие ГОСБ складываются в общий чанк, крупный (сам по себе больше `target`)
    режется по остатку от деления ИНН на равные части. Части одного ГОСБ вместе
    дают ровно его целиком: остатки не пересекаются и ничего не теряют.
    """
    chunks: list[dict] = []
    cur: dict = {"old_ids": [], "gosb": [], "rows": 0, "n_parts": 1, "part": 0}
    for gid in sorted(olds_of):
        n = int(rows_of.get(gid, 0))
        if n > target:
            parts = int(np.ceil(n / target))
            for p in range(parts):
                chunks.append({"old_ids": list(olds_of[gid]), "gosb": [gid],
                               "rows": n // parts, "n_parts": parts, "part": p})
            continue
        if cur["rows"] + n > target and cur["old_ids"]:
            chunks.append(cur)
            cur = {"old_ids": [], "gosb": [], "rows": 0, "n_parts": 1, "part": 0}
        cur["old_ids"].extend(olds_of[gid])
        cur["gosb"].append(gid)
        cur["rows"] += n
    if cur["old_ids"]:
        chunks.append(cur)
    return chunks


def plan_chunks(engine, d_from, d_to) -> list[dict]:
    """Разложить ГОСБ по чанкам так, чтобы ни один запрос не превысил лимит строк."""
    progress.step("Планирование чанков выгрузки")
    gmap = db.read_sql(engine, LQ.GOSB_MAP)
    if gmap.empty:
        raise RuntimeError("справочник ГОСБ пуст — выгружать нечего")
    cnt = db.read_sql(engine, LQ.PANEL_COUNTS, {"d_from": d_from, "d_to": d_to})
    rows_of = {int(r.new_gosb_id): int(r.n_rows) for r in cnt.itertuples()} \
        if not cnt.empty else {}

    olds_of: dict[int, list] = {}
    for r in gmap.itertuples():
        olds_of.setdefault(int(r.new_gosb_id), []).append(int(r.old_gosb_id))

    chunks = pack_chunks(olds_of, rows_of, CHUNK_TARGET)
    total = sum(c["rows"] for c in chunks)
    progress.done(f"Чанков: {len(chunks)}, строк ожидается {total:,} "
                  f"(в самом крупном {max(c['rows'] for c in chunks):,})")
    return chunks


def org_segments(engine, n_parts: int = 8) -> dict:
    """ИНН -> код сегмента (extended_dim_1). Справочник крупный, читаем частями."""
    progress.step("Сегменты организаций из справочника")
    out: dict[int, int] = {}
    unknown_names: set = set()
    for part in range(n_parts):
        df = db.read_sql(engine, LQ.ORG_SEG, {"n_parts": n_parts, "part": part})
        guard_rows(df, f"ORG_SEG part {part}")
        for inn, big in zip(df["inn"], df["segment_big"]):
            code = segments.code_of_big(big)
            if code is None:
                if big:
                    unknown_names.add(str(big))
                continue
            out[int(inn)] = int(code)
    progress.done(f"Сегмент известен у {len(out):,} организаций"
                  + (f"; не разобраны названия: {sorted(unknown_names)[:5]}"
                     if unknown_names else ""))
    return out


def fact_outflow_available(engine) -> bool:
    try:
        df = db.read_sql(engine, LQ.FACT_OUTFLOW_PROBE)
        return not df.empty and int(df["n_rows"].iloc[0] or 0) > 0
    except Exception:
        return False


def pipeline_plan(engine, months: list) -> pd.DataFrame:
    """План привлечения по (ГОСБ, ИНН, месяц), доступный НА НАЧАЛО этого месяца.

    Отбираются только сделки, заведённые РАНЬШЕ планируемого месяца: прогноз
    строится 1–5 числа, и сделки самого месяца к этому моменту ещё нет.

    Точность здесь ограничена по природе источника, и об этом сказано в отчёте:
    воронка — снимок на сегодня, а не на момент прогноза. Сделки, заведённые год
    назад и с тех пор удалённые, в выборке отсутствуют.
    """
    d_from = pd.Period(months[0], freq="M").to_timestamp().date()
    d_to = pd.Period(months[-1], freq="M").to_timestamp("M").date()
    progress.step("Пайплайн: план привлечения по месяцам")
    try:
        df = db.read_sql(engine, LQ.PIPELINE_PLAN, {"d_from": d_from, "d_to": d_to})
    except Exception as ex:
        progress.warn(f"пайплайн недоступен: {type(ex).__name__}: {str(ex)[:200]}")
        return pd.DataFrame()
    guard_rows(df, "PIPELINE_PLAN")
    if df.empty:
        progress.warn("пайплайн пуст — семейство кандидатов по нему пропускается")
        return df
    m0 = pd.PeriodIndex(pd.to_datetime(df["m0_month"]), freq="M")
    pm = pd.PeriodIndex(pd.to_datetime(df["plan_month"]), freq="M")
    keep = m0 < pm
    n_drop = int((~keep).sum())
    df = df[keep].copy()
    df["ym"] = pm[keep].astype(str)
    df = df.groupby(["new_gosb_id", "inn", "ym"], as_index=False)["plan_np"].sum()
    progress.done(f"Пайплайн: {len(df):,} строк (ГОСБ, ИНН, месяц); отброшено "
                  f"{n_drop:,} строк со сделками, заведёнными В планируемом месяце — "
                  f"на 1–5 число их ещё не существует")
    return df


def pipeline_realization(engine, months: list) -> dict:
    """Доля реализуемости пайплайна по месяцам: сколько НП пришло против плана.

    Пулированная величина (одна на банк за месяц) — по ней перебор строит вариант
    «план × историческая доля», где доля считается только по месяцам < M.
    """
    d_from = pd.Period(months[0], freq="M").to_timestamp().date()
    d_to = pd.Period(months[-1], freq="M").to_timestamp("M").date()
    try:
        df = db.read_sql(engine, LQ.PIPELINE_FACT_TOTAL,
                         {"m_np": LQ.METRIC_NEW_RECIPIENTS_B2B,
                          "counted": LQ.MOTIV_COUNTED,
                          "d_from": d_from, "d_to": d_to})
    except Exception as ex:
        progress.warn(f"факт пайплайна недоступен: {type(ex).__name__}")
        return {}
    if df.empty:
        return {}
    ym = pd.PeriodIndex(pd.to_datetime(df["ym"]), freq="M").astype(str)
    return {str(a): float(b) for a, b in zip(ym, df["fact_np"])}


# --------------------------------------------------------------------------- #
def fetch_all(engine, cache_dir: Path, months: list, use_fact_outflow: bool = True,
              force: bool = False) -> dict:
    """Выгрузить панель по всем чанкам в кэш. Возвращает манифест.

    Повторный запуск переиспользует уже выгруженные чанки — прогон на проме долгий,
    и обрыв на середине не должен означать «начать сначала».
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    d_from = pd.Period(months[0], freq="M").to_timestamp().date()
    d_to = pd.Period(months[-1], freq="M").to_timestamp("M").date()
    mi_of = {m: i for i, m in enumerate(months)}

    man_path = cache_dir / "manifest.json"
    if man_path.exists() and not force:
        man = json.loads(man_path.read_text(encoding="utf-8"))
        if man.get("months") == months:
            progress.done(f"Кэш уже собран: {len(man['chunks'])} чанков в {cache_dir}")
            return man
        progress.warn("окно месяцев изменилось — кэш пересобирается заново")

    chunks = plan_chunks(engine, d_from, d_to)
    seg_of = org_segments(engine)
    has_fo = use_fact_outflow and fact_outflow_available(engine)
    if use_fact_outflow and not has_fo:
        progress.warn("uzp_dwh_fact_outflow недоступна — семейство кандидатов по ней "
                      "в переборе участвовать не будет")
    pipe = pipeline_plan(engine, months)
    has_pipe = not pipe.empty
    pipe_fact = pipeline_realization(engine, months) if has_pipe else {}

    man = {"months": months, "chunks": [], "has_fact_outflow": has_fo,
           "has_pipeline": has_pipe,
           "pipe_plan_total": ({} if not has_pipe else
                               {k: float(v) for k, v in
                                pipe.groupby("ym")["plan_np"].sum().items()}),
           "pipe_fact_total": pipe_fact,
           "vars": list(PANEL_VARS) + (list(FO_VARS) if has_fo else [])
                   + (["pipe_fwd"] if has_pipe else []),
           "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    t0 = time.time()
    for i, ch in enumerate(chunks, 1):
        name = f"chunk_{i:04d}.npz"
        path = cache_dir / name
        if path.exists() and not force:
            progress.done(f"[{i}/{len(chunks)}] {name} уже в кэше")
            man["chunks"].append(name)
            continue
        progress.step(f"[{i}/{len(chunks)}] выгрузка ГОСБ {ch['gosb'][:3]}"
                      f"{'…' if len(ch['gosb']) > 3 else ''} "
                      f"(ожидается {ch['rows']:,} строк)")
        params = {"old_ids": ch["old_ids"], "d_from": d_from, "d_to": d_to,
                  "n_parts": ch["n_parts"], "part": ch["part"]}
        df = db.read_sql(engine, LQ.PANEL, params)
        guard_rows(df, f"PANEL chunk {i}")
        if df.empty:
            progress.warn(f"[{i}/{len(chunks)}] пусто — чанк пропущен")
            continue
        fo = pd.DataFrame()
        if has_fo:
            fo = db.read_sql(engine, LQ.FACT_OUTFLOW, params)
            guard_rows(fo, f"FACT_OUTFLOW chunk {i}")
        _save_chunk(path, df, fo, pipe, months, mi_of, seg_of, has_fo, has_pipe)
        man["chunks"].append(name)
        progress.done(f"[{i}/{len(chunks)}] {len(df):,} строк -> {name} "
                      f"({path.stat().st_size / 1e6:.1f} МБ)")

    size = sum((cache_dir / c).stat().st_size for c in man["chunks"]) / 1e6
    man["size_mb"] = round(size, 1)
    man_path.write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
    progress.done(f"Кэш собран за {time.time() - t0:.0f} с: {len(man['chunks'])} "
                  f"чанков, {size:.0f} МБ в {cache_dir}")
    return man


def _save_chunk(path: Path, df: pd.DataFrame, fo: pd.DataFrame, pipe: pd.DataFrame,
                months: list, mi_of: dict, seg_of: dict, has_fo: bool,
                has_pipe: bool = False) -> None:
    """Разложить длинную выборку в широкие матрицы (пара × месяц) и записать."""
    df = df.dropna(subset=["new_gosb_id", "inn", "ym"]).copy()
    df["new_gosb_id"] = df["new_gosb_id"].astype("int64")
    df["inn"] = df["inn"].astype("int64")
    ym = pd.PeriodIndex(pd.to_datetime(df["ym"]), freq="M").astype(str)
    df["_mi"] = [mi_of.get(v, -1) for v in ym]
    df = df[df["_mi"] >= 0]

    codes, uniq = pd.factorize(pd.MultiIndex.from_frame(df[["new_gosb_id", "inn"]]))
    n, m = len(uniq), len(months)
    gosb = np.asarray([k[0] for k in uniq], dtype="int64")
    inn = np.asarray([k[1] for k in uniq], dtype="int64")

    mi = df["_mi"].to_numpy()
    present = np.zeros((n, m), dtype=bool)
    present[codes, mi] = True
    out = {}
    for v in PANEL_VARS:
        a = np.zeros((n, m), dtype="float32")
        a[codes, mi] = pd.to_numeric(df[v], errors="coerce").fillna(0).to_numpy("float32")
        out[v] = a

    # ТБ у пары один (грейн отчёта это гарантирует) — берём первое вхождение
    tb = np.zeros(n, dtype="int32")
    tb[codes] = pd.to_numeric(df["tb_id"], errors="coerce").fillna(0).to_numpy("int32")
    seg = np.asarray([seg_of.get(int(i), -1) for i in inn], dtype="int16")

    if has_fo and fo is not None and not fo.empty:
        fo = fo.dropna(subset=["new_gosb_id", "inn", "ym"]).copy()
        fo["new_gosb_id"] = fo["new_gosb_id"].astype("int64")
        fo["inn"] = fo["inn"].astype("int64")
        fym = pd.PeriodIndex(pd.to_datetime(fo["ym"]), freq="M").astype(str)
        fo["_mi"] = [mi_of.get(v, -1) for v in fym]
        fo = fo[fo["_mi"] >= 0]
        # выравнивание на тот же индекс пар: строки, которых нет в панели,
        # отбрасываются — прогноз строится только по парам, известным витрине
        pid_of = pd.Series(np.arange(n), index=pd.MultiIndex.from_arrays([gosb, inn]))
        key = pd.MultiIndex.from_frame(fo[["new_gosb_id", "inn"]])
        pid = pid_of.reindex(key).to_numpy()
        ok = ~pd.isna(pid)
        pid = pid[ok].astype("int64")
        fmi = fo["_mi"].to_numpy()[ok]
        for v in FO_VARS:
            a = np.zeros((n, m), dtype="float32")
            vals = pd.to_numeric(fo[v], errors="coerce").fillna(0).to_numpy("float32")
            a[pid, fmi] = vals[ok]
            out[v] = a
    elif has_fo:
        for v in FO_VARS:
            out[v] = np.zeros((n, m), dtype="float32")

    if has_pipe:
        # СДВИНУТАЯ раскладка: столбец t хранит план на месяц t+1. Так перебор
        # читает план прогнозного месяца обычным лагом 1, и правило «только
        # столбцы < j» остаётся без единого исключения — иначе пришлось бы
        # разрешить читать столбец j, и защита от подглядывания ослабла бы.
        a = np.zeros((n, m), dtype="float32")
        if pipe is not None and not pipe.empty:
            pid_of = pd.Series(np.arange(n), index=pd.MultiIndex.from_arrays([gosb, inn]))
            key = pd.MultiIndex.from_frame(pipe[["new_gosb_id", "inn"]].astype("int64"))
            pid = pid_of.reindex(key).to_numpy()
            col = np.asarray([mi_of.get(v, -1) - 1 for v in pipe["ym"]])
            ok = (~pd.isna(pid)) & (col >= 0)
            if ok.any():
                np.add.at(a, (pid[ok].astype("int64"), col[ok]),
                          pd.to_numeric(pipe["plan_np"], errors="coerce")
                          .fillna(0).to_numpy("float32")[ok])
        out["pipe_fwd"] = a

    np.savez_compressed(path, gosb=gosb.astype("int32"), tb=tb, inn=inn, seg=seg,
                        present=present, **out)


def load_chunk(cache_dir: Path, name: str, months: list) -> Chunk:
    z = np.load(cache_dir / name)
    var = {k: z[k] for k in z.files
           if k not in ("gosb", "tb", "inn", "seg", "present")}
    return Chunk(gosb=z["gosb"], tb=z["tb"], inn=z["inn"], seg=z["seg"],
                 months=list(months), present=z["present"], var=var)


def iter_chunks(cache_dir: Path, man: dict):
    for name in man["chunks"]:
        yield load_chunk(cache_dir, name, man["months"])


# --------------------------------------------------------------------------- #
def fetch_units(engine, cache_dir: Path, months: list, force: bool = False
                ) -> pd.DataFrame:
    """План, факт и собственный прогноз витрины по единицам. Выборка крошечная."""
    path = cache_dir / "units.csv"
    if path.exists() and not force:
        return pd.read_csv(path)
    d_from = pd.Period(months[0], freq="M").to_timestamp("M").date()
    d_to = pd.Period(months[-1], freq="M").to_timestamp("M").date()
    progress.step("Витрина метрик: план, факт и собственный прогноз по единицам")
    df = db.read_sql(engine, LQ.UNITS, {"m_fot": LQ.METRIC_FOT,
                                        "m_rcp": LQ.METRIC_RECIPIENTS,
                                        "d_from": d_from, "d_to": d_to})
    guard_rows(df, "UNITS")
    # уровень gosb лежит на СТАРОМ id — сворачиваем в новый, как весь отчёт
    gmap = db.read_sql(engine, LQ.GOSB_MAP)
    new_of = {int(r.old_gosb_id): int(r.new_gosb_id) for r in gmap.itertuples()}
    tb_of = {int(r.new_gosb_id): int(r.tb_id) for r in gmap.itertuples()}
    is_gosb = df["level_name"] == "gosb"
    df.loc[is_gosb, "unit_id"] = [new_of.get(int(v), -1)
                                  for v in df.loc[is_gosb, "level_id"]]
    df.loc[~is_gosb, "unit_id"] = df.loc[~is_gosb, "level_id"]
    df = df[df["unit_id"] >= 0]
    df["unit_id"] = df["unit_id"].astype("int64")
    df = (df.groupby(["level_name", "unit_id", "metric_id", "end_dt", "seg_id"],
                     as_index=False)
            .agg(plan_amt=("plan_amt", "sum"), fact_amt=("fact_amt", "sum"),
                 pred_amt=("pred_amt", "sum")))
    df["ym"] = pd.PeriodIndex(pd.to_datetime(df["end_dt"]), freq="M").astype(str)
    df["tb_id"] = [tb_of.get(int(u), int(u)) if lv == "gosb" else int(u)
                   for lv, u in zip(df["level_name"], df["unit_id"])]
    df.to_csv(path, index=False)
    progress.done(f"Единицы: {len(df):,} строк -> {path}")
    return df

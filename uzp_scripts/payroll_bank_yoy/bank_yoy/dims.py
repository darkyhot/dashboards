"""Отрасли и районы: получатели год к году в разрезе сегментов.

Две оценки отрасли — справочник ЕПК (`industry_name`) и правила по названию
(`industry.py`) — и сравнение их между собой. Территория — субъект РФ и район по
ОКТМО (если его нет — по ОКАТО) из справочника ЕПК по связке (id орг, ГОСБ).

Каждая таблица складывается в банк: сумма изменений по строкам = изменение
получателей банка за тот же переход (проверяется в `run`).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import geo as G
from . import industry as I
from . import segments as S

TOTAL = "Итого"
NO_IND = "Отрасль не указана"
# Строки, которые не участвуют в сравнении двух оценок: у одной из них нет ответа.
_NOT_KNOWN_EPK = {NO_IND, I.NO_NAME, "Прочее"}


def _num(df: pd.DataFrame, c: str) -> pd.Series:
    return pd.to_numeric(df[c], errors="coerce").fillna(0.0) if c in df else pd.Series(0.0, index=df.index)


def yoy_table(df: pd.DataFrame, key: str) -> pd.DataFrame:
    """Строка — значение разреза; колонки: изменение по сегментам, «Итого», было, стало,
    изменение в %, вклад в изменение банка. Сортировка — по изменению (хуже сверху)."""
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    x["n_b"], x["n_c"] = _num(x, "n_b"), _num(x, "n_c")
    x["d"] = x["n_c"] - x["n_b"]
    pv = x.pivot_table(index=key, columns="seg", values="d", aggfunc="sum").fillna(0)
    pv = pv.reindex(columns=S.ordered(pv.columns))
    out = pv.copy()
    out[TOTAL] = pv.sum(axis=1)
    g = x.groupby(key)[["n_b", "n_c"]].sum()
    out["was"], out["now"] = g["n_b"], g["n_c"]
    out["pct"] = out[TOTAL] / out["was"].where(out["was"] > 0)
    bank = float(x["d"].sum())
    out["share_bank"] = out[TOTAL] / bank if bank else np.nan
    return out.sort_values(TOTAL)


def seg_cols(t: pd.DataFrame) -> list[str]:
    return [c for c in t.columns if c not in (TOTAL, "was", "now", "pct", "share_bank")]


def industry(raw: pd.DataFrame) -> dict:
    if raw is None or raw.empty:
        return {}
    x = raw.copy()
    x["n_b"], x["n_c"] = _num(x, "n_b"), _num(x, "n_c")
    out = {"epk": yoy_table(x, "ind_epk"), "name": yoy_table(x, "ind_name")}
    # Сравнение двух оценок — по получателям месяца c.
    cross = x.pivot_table(index="ind_epk", columns="ind_name", values="n_c", aggfunc="sum").fillna(0)
    out["cross"] = cross
    known = x[~x["ind_epk"].isin(_NOT_KNOWN_EPK) & (x["ind_name"] != I.UNKNOWN)
              & (x["ind_name"] != I.NO_NAME)]
    rows = []
    for seg, z in [(TOTAL, x)] + [(s, x[x["seg"] == s]) for s in S.ordered(x["seg"].unique())]:
        k = known if seg == TOTAL else known[known["seg"] == seg]
        tot = float(z["n_c"].sum())
        rows.append({"seg": seg, "n": tot,
                     "epk_known": float(z.loc[~z["ind_epk"].isin(_NOT_KNOWN_EPK), "n_c"].sum()) / tot if tot else np.nan,
                     "name_known": float(z.loc[~z["ind_name"].isin({I.UNKNOWN, I.NO_NAME}), "n_c"].sum()) / tot if tot else np.nan,
                     "both": float(k["n_c"].sum()),
                     "agree": float(k.loc[k["ind_epk"] == k["ind_name"], "n_c"].sum()) / float(k["n_c"].sum())
                     if float(k["n_c"].sum()) else np.nan})
    out["agree"] = pd.DataFrame(rows)
    return out


def _labels(x: pd.DataFrame) -> pd.DataFrame:
    """Субъект и район: ОКТМО, а где его нет — ОКАТО."""
    okt = x["oktmo5"].where(x["oktmo5"].notna(), None)
    oka = x["okato5"].where(x["okato5"].notna(), None)
    x["src"] = np.where(okt.notna(), "ОКТМО", np.where(oka.notna(), "ОКАТО", "нет"))
    x["subject"] = [G.subject_of(a, "oktmo") if isinstance(a, str) else
                    (G.subject_of(b, "okato") if isinstance(b, str) else G.NO_GEO) for a, b in zip(okt, oka)]
    x["district"] = [G.district(a, "oktmo") if isinstance(a, str) else
                     (G.district(b, "okato") if isinstance(b, str) else G.NO_GEO) for a, b in zip(okt, oka)]
    return x


def territory(raw: pd.DataFrame) -> dict:
    if raw is None or raw.empty:
        return {}
    x = raw.copy()
    x["n_b"], x["n_c"] = _num(x, "n_b"), _num(x, "n_c")
    for c in ("oktmo5", "okato5"):
        x[c] = [v if isinstance(v, str) and len(v) == 5 else None for v in x.get(c, pd.Series(index=x.index))]
    x = _labels(x)
    tot = float(x["n_c"].sum())
    cov = {"total": tot,
           "pair": float(x.loc[x["match"] == "pair", "n_c"].sum()),
           "inn_only": float(x.loc[x["match"] == "inn_only", "n_c"].sum()),
           "none": float(x.loc[x["match"] == "none", "n_c"].sum()),
           "oktmo": float(x.loc[x["src"] == "ОКТМО", "n_c"].sum()),
           "okato": float(x.loc[x["src"] == "ОКАТО", "n_c"].sum()),
           "district": float(x.loc[(x["src"] != "нет") & ~x["district"].str.endswith(G.NO_DISTRICT), "n_c"].sum())}
    return {"subject": yoy_table(x, "subject"), "district": yoy_table(x, "district"), "cov": cov}

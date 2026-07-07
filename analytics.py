"""Analytics on top of the daily screen:
- signals log (every day's Grade A/B rows appended to data/signals.csv)
- persistence counts (repeat appearances = accumulation campaigns)
- self-scorecard (how did past ACT/WATCH signals actually perform?)
- sector clusters (several signals in one industry = rotation tell)
- distribution watch (heavy delivery on down days in YOUR holdings)
"""

import os
import pandas as pd
import config as C

BASE = os.path.dirname(os.path.abspath(__file__))
SIGNALS_PATH = os.path.join(BASE, "data", "signals.csv")
HOLDINGS_PATH = os.path.join(BASE, "data", "holdings.txt")


# ---------------------------------------------------------------- signals log
def append_signals(res: dict) -> pd.DataFrame:
    """Append today's shortlist to the signals log (idempotent per date)."""
    rows = []
    for grade, df in (("A", res["grade_a"]), ("B", res["grade_b"])):
        for sym, r in df.iterrows():
            rows.append(dict(date=res["latest"].date().isoformat(),
                             symbol=sym, grade=grade, action=r["action"],
                             score=int(r["score"]), close=float(r["close"])))
    today = pd.DataFrame(rows)
    if os.path.exists(SIGNALS_PATH):
        log = pd.read_csv(SIGNALS_PATH)
        log = log[log["date"] != res["latest"].date().isoformat()]
        log = pd.concat([log, today], ignore_index=True)
    else:
        log = today
    os.makedirs(os.path.dirname(SIGNALS_PATH), exist_ok=True)
    log.to_csv(SIGNALS_PATH, index=False)
    return log


# ---------------------------------------------------------------- persistence
def persistence_counts(log: pd.DataFrame) -> dict:
    """symbol -> number of distinct sessions it signalled, within the last
    PERSIST_WINDOW signal dates."""
    if log is None or log.empty:
        return {}
    recent_dates = sorted(log["date"].unique())[-C.PERSIST_WINDOW:]
    rec = log[log["date"].isin(recent_dates)]
    return rec.groupby("symbol")["date"].nunique().to_dict()


# ---------------------------------------------------------------- scorecard
def scorecard(log: pd.DataFrame, history: pd.DataFrame) -> pd.DataFrame:
    """Forward returns of past signals vs the universe median, by action
    label. Only signals old enough for each horizon are counted."""
    if log is None or log.empty:
        return pd.DataFrame()
    hist = history[history["SERIES"] == "EQ"]
    closes = hist.pivot_table(index="DATE", columns="SYMBOL",
                              values="CLOSE_PRICE", aggfunc="last")
    closes.index = pd.to_datetime(closes.index).astype(str)
    closes = closes.sort_index()
    pos = {d: i for i, d in enumerate(closes.index)}
    med = closes.median(axis=1)

    cutoff = sorted(closes.index)[-C.SCORECARD_LOOKBACK_DAYS] \
        if len(closes.index) > C.SCORECARD_LOOKBACK_DAYS else closes.index[0]
    sig = log[(log["date"] >= cutoff) & log["date"].isin(pos)
              & log["action"].isin(["ACT", "WATCH"])]
    if sig.empty:
        return pd.DataFrame()

    recs = []
    for _, s in sig.iterrows():
        i = pos[s["date"]]
        base = closes.at[s["date"], s["symbol"]] \
            if s["symbol"] in closes.columns else None
        if base is None or pd.isna(base) or base <= 0:
            continue
        rec = dict(action=s["action"])
        for h in C.SCORECARD_HORIZONS:
            if i + h < len(closes.index):
                d2 = closes.index[i + h]
                px = closes.at[d2, s["symbol"]]
                if pd.notna(px):
                    rec[f"r{h}"] = (px / base - 1) * 100
                    rec[f"x{h}"] = rec[f"r{h}"] - \
                        (med.iloc[i + h] / med.iloc[i] - 1) * 100
        if len(rec) > 1:
            recs.append(rec)
    if not recs:
        return pd.DataFrame()
    f = pd.DataFrame(recs)
    out = []
    for a, gdf in f.groupby("action"):
        row = {"action": a, "signals": len(gdf)}
        for h in C.SCORECARD_HORIZONS:
            col = f"r{h}"
            if col in gdf and gdf[col].notna().any():
                row[f"avg_{h}d"] = round(gdf[col].mean(), 1)
                row[f"hit_{h}d"] = round((gdf[col] > 0).mean() * 100, 0)
                row[f"excess_{h}d"] = round(gdf[f"x{h}"].mean(), 1)
        out.append(row)
    return pd.DataFrame(out).sort_values("action")


# ---------------------------------------------------------------- sectors
def sector_clusters(res: dict, sector_map: pd.DataFrame) -> list:
    """[(sector, [symbols])] where >=2 shortlisted names share an industry."""
    if sector_map is None or sector_map.empty:
        return []
    syms = list(res["grade_a"].index) + list(res["grade_b"].index)
    m = sector_map.set_index("SYMBOL")["SECTOR"]
    tagged = [(s, m.get(s)) for s in syms if pd.notna(m.get(s))]
    groups = {}
    for s, sec in tagged:
        groups.setdefault(sec, []).append(s)
    return sorted([(k, v) for k, v in groups.items() if len(v) >= 2],
                  key=lambda kv: -len(kv[1]))


# ---------------------------------------------------------------- holdings
def load_holdings() -> list:
    if not os.path.exists(HOLDINGS_PATH):
        return []
    with open(HOLDINGS_PATH) as f:
        return [ln.strip().upper() for ln in f
                if ln.strip() and not ln.startswith("#")]


def distribution_watch(history: pd.DataFrame, holdings: list) -> pd.DataFrame:
    """Flag holdings showing heavy delivery on DOWN days in the last 3
    sessions - the footprint of smart money exiting."""
    if not holdings:
        return pd.DataFrame()
    df = history[(history["SERIES"] == "EQ")
                 & history["SYMBOL"].isin(holdings)].copy()
    if df.empty:
        return pd.DataFrame()
    df["DATE"] = pd.to_datetime(df["DATE"])
    df = df.sort_values(["SYMBOL", "DATE"])
    g = df.groupby("SYMBOL")
    df["dq20"] = g["DELIV_QTY"].transform(
        lambda s: s.rolling(C.LOOKBACK_AVG_DAYS, min_periods=15).mean())
    df["prev_close"] = g["CLOSE_PRICE"].shift(1)
    tail = g.tail(3)
    hits = tail[(tail["CLOSE_PRICE"] < tail["prev_close"])
                & (tail["DELIV_QTY"] > C.DISTRIB_DELIV_MULT * tail["dq20"])]
    if hits.empty:
        return pd.DataFrame()
    out = hits.groupby("SYMBOL").agg(
        down_days=("DATE", "size"),
        last_close=("CLOSE_PRICE", "last"),
        max_deliv_x=("DELIV_QTY", "max"))
    dq = hits.groupby("SYMBOL")["dq20"].last()
    out["max_deliv_x"] = (out["max_deliv_x"] / dq).round(2)
    return out.sort_values("max_deliv_x", ascending=False)

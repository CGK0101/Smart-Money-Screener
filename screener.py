"""
Three-layer Smart Money screener.

Layer 1  Accumulation gate  (price / volume / delivery, hardened)
Layer 2  Price-structure classification (Base / Continuation / Extended / Neutral)
Layer 3  Event overlay (bulk, block, insider, pledge) + composite score 0-100
"""

import re
import numpy as np
import pandas as pd
import config as C


# --------------------------------------------------------------- helpers
def _norm_symbol_col(df: pd.DataFrame) -> str | None:
    """Find the symbol column in an NSE event frame (names vary by endpoint)."""
    for c in df.columns:
        if re.fullmatch(r"(?i)(bd_)?symbol", str(c)):
            return c
    return None


def _col_like(df: pd.DataFrame, *patterns) -> str | None:
    for p in patterns:
        for c in df.columns:
            if re.search(p, str(c), re.I):
                return c
    return None


# --------------------------------------------------------------- Layer 3 prep
def build_event_flags(events: dict) -> pd.DataFrame:
    """Collapse raw event frames into one row per symbol with boolean flags
    and short descriptions."""
    recs = {}

    def add(sym, key, desc):
        sym = str(sym).strip().upper()
        if not sym or sym == "NAN":
            return
        r = recs.setdefault(sym, {"promoter_buy": False, "promoter_sell": False,
                                  "block_deal": False, "bulk_buy": False,
                                  "bulk_circular": False, "pledge_creation": False,
                                  "pledge_release": False, "notes": []})
        r[key] = True
        if desc and len(r["notes"]) < 6:
            r["notes"].append(desc)

    # ---- bulk deals: flag buys; detect same-client both-sides (circular)
    bulk = events.get("bulk", pd.DataFrame())
    if len(bulk):
        sc = _norm_symbol_col(bulk)
        side = _col_like(bulk, r"buy.?sell", r"^bd_buy_sell$", r"deal.?type")
        client = _col_like(bulk, r"client", r"name")
        qty = _col_like(bulk, r"qty|quantity")
        if sc and side:
            b = bulk.copy()
            b["_side"] = b[side].astype(str).str.upper().str.strip()
            if client:
                # circular: same client buying AND selling same symbol in window
                g = b.groupby([sc, client])["_side"].nunique()
                for (sym, cl) in g[g > 1].index:
                    add(sym, "bulk_circular", f"Circular bulk: {cl}")
            for _, row in b[b["_side"].str.startswith("B")].iterrows():
                cl = str(row[client]) if client else "?"
                q = f" {row[qty]}" if qty else ""
                add(row[sc], "bulk_buy", f"Bulk buy: {cl}{q}")

    # ---- block deals: institutional by definition
    block = events.get("block", pd.DataFrame())
    if len(block):
        sc = _norm_symbol_col(block)
        client = _col_like(block, r"client", r"name")
        if sc:
            for _, row in block.iterrows():
                cl = str(row[client]) if client else ""
                add(row[sc], "block_deal", f"Block deal: {cl}".strip(": "))

    # ---- insider (PIT): promoter acquisition vs disposal
    ins = events.get("insider", pd.DataFrame())
    if len(ins):
        sc = _norm_symbol_col(ins)
        person_cat = _col_like(ins, r"person.?cat|category")
        acq_type = _col_like(ins, r"acq.?mode|transaction.?type|acqui?sition.*disposal|tdp")
        who = _col_like(ins, r"acquirer|person.?name|^name$")
        if sc and acq_type:
            for _, row in ins.iterrows():
                cat = str(row[person_cat]).upper() if person_cat else ""
                if "PROMOTER" not in cat:
                    continue
                t = str(row[acq_type]).upper()
                nm = str(row[who]) if who else "Promoter"
                if any(k in t for k in ("ACQ", "BUY", "PURCH")):
                    add(row[sc], "promoter_buy", f"Promoter buy: {nm}")
                elif any(k in t for k in ("DISP", "SELL", "SALE")):
                    add(row[sc], "promoter_sell", f"Promoter sell: {nm}")

    # ---- pledge (SAST): creation vs release
    pl = events.get("pledge", pd.DataFrame())
    if len(pl):
        sc = _norm_symbol_col(pl)
        ptype = _col_like(pl, r"type|purpose|event")
        if sc and ptype:
            for _, row in pl.iterrows():
                t = str(row[ptype]).upper()
                if any(k in t for k in ("CREAT", "INVOC", "INCREASE")):
                    add(row[sc], "pledge_creation", "Pledge created/increased")
                elif any(k in t for k in ("RELEASE", "REVOK", "DECREASE")):
                    add(row[sc], "pledge_release", "Pledge released")

    if not recs:
        return pd.DataFrame(columns=["SYMBOL"])
    out = pd.DataFrame.from_dict(recs, orient="index").reset_index(
        names="SYMBOL")
    out["notes"] = out["notes"].apply(lambda n: " | ".join(n))
    return out


# --------------------------------------------------------------- main screen
def run_screen(history: pd.DataFrame, events: dict,
               fo_oi: pd.DataFrame | None = None) -> dict:
    """Returns dict with grade_a, grade_b dataframes + metadata."""
    df = history[history["SERIES"].isin(C.SERIES_ALLOWED)].copy()
    df["DATE"] = pd.to_datetime(df["DATE"])
    df = df.sort_values(["SYMBOL", "DATE"])

    dates = np.sort(df["DATE"].unique())
    if len(dates) < C.MIN_HISTORY_DAYS:
        raise RuntimeError(
            f"Only {len(dates)} days of history stored; need "
            f"{C.MIN_HISTORY_DAYS}. Run scripts/backfill.py first.")
    latest = dates[-1]

    g = df.groupby("SYMBOL")
    # rolling stats computed per symbol on trading-day series
    df["vol20"] = g["TTL_TRD_QNTY"].transform(
        lambda s: s.rolling(C.LOOKBACK_AVG_DAYS, min_periods=15).mean())
    df["dq20"] = g["DELIV_QTY"].transform(
        lambda s: s.rolling(C.LOOKBACK_AVG_DAYS, min_periods=15).mean())
    df["to20"] = g["TURNOVER_LACS"].transform(
        lambda s: s.rolling(C.LOOKBACK_AVG_DAYS, min_periods=15).mean())
    df["sma21"] = g["CLOSE_PRICE"].transform(
        lambda s: s.rolling(21, min_periods=15).mean())
    df["sma50"] = g["CLOSE_PRICE"].transform(
        lambda s: s.rolling(C.SMA_SHORT, min_periods=30).mean())
    df["sma200"] = g["CLOSE_PRICE"].transform(
        lambda s: s.rolling(C.SMA_LONG, min_periods=100).mean())
    df["ret_rs"] = g["CLOSE_PRICE"].transform(
        lambda s: s / s.shift(min(C.RS_LOOKBACK, max(1, len(s) - 1))) - 1)
    df["low52"] = g["LOW_PRICE"].transform(
        lambda s: s.rolling(250, min_periods=60).min())
    df["hi52"] = g["HIGH_PRICE"].transform(
        lambda s: s.rolling(250, min_periods=60).max())

    # last 4 trading rows per symbol -> wide
    tail = g.tail(4).copy()
    tail["rk"] = tail.groupby("SYMBOL")["DATE"].rank(
        method="first", ascending=False).astype(int)   # 1 = latest
    full = tail[tail.groupby("SYMBOL")["rk"].transform("max") == 4]
    # symbol must have traded on the latest session
    on_latest = set(full.loc[(full["rk"] == 1) & (full["DATE"] == latest),
                             "SYMBOL"])
    full = full[full["SYMBOL"].isin(on_latest)]

    w = full.pivot(index="SYMBOL", columns="rk",
                   values=["CLOSE_PRICE", "LOW_PRICE", "TTL_TRD_QNTY",
                           "DELIV_QTY", "DELIV_PER"])
    stats = full[full["rk"] == 1].set_index("SYMBOL")[
        ["vol20", "dq20", "to20", "sma21", "sma50", "sma200", "low52", "hi52",
         "ret_rs", "TURNOVER_LACS", "NO_OF_TRADES"]]

    def col(m, k):
        return w[(m, k)]

    p1, p2, p3, p4 = (col("CLOSE_PRICE", k) for k in (1, 2, 3, 4))
    v1, v2, v3 = (col("TTL_TRD_QNTY", k) for k in (1, 2, 3))
    d1, d2, d3 = (col("DELIV_QTY", k) for k in (1, 2, 3))
    dp1, dp2, dp3 = (col("DELIV_PER", k) for k in (1, 2, 3))
    low2 = col("LOW_PRICE", 2)

    out = pd.DataFrame(index=w.index)
    out["close"] = p1
    out["chg3d_pct"] = (p1 / p4 - 1) * 100

    # ---- Layer 1 conditions
    price_strict = (p1 > p2) & (p2 > p3) & (p3 > p4)
    price_soft = (((p1 > p2).astype(int) + (p2 > p3).astype(int)
                   + (p3 > p4).astype(int)) >= 2) & (p1 > p4) & (p1 >= low2)
    vol_strict = (v1 > v2) & (v2 > v3)
    days_above = ((v1 > stats["vol20"]).astype(int)
                  + (v2 > stats["vol20"]).astype(int)
                  + (v3 > stats["vol20"]).astype(int))
    vol_expanded = ((v1 + v2 + v3) > C.VOL_3DAY_MULTIPLE * 3 * stats["vol20"]) \
        & (days_above >= C.VOL_DAYS_ABOVE_AVG_MIN)
    dp_strict = (dp1 > dp2) & (dp2 > dp3)
    deliv_qty_ok = (d1 > d3) & (d1 > stats["dq20"])
    deliv_strong = dp_strict & (d1 > d2) & (d2 > d3) & (d1 > stats["dq20"])

    liquid = (stats["to20"] >= C.MIN_AVG_TURNOVER_LACS) \
        & (p1 >= C.MIN_PRICE)

    out["grade_a"] = liquid & price_strict & vol_strict & deliv_strong
    out["grade_b"] = liquid & price_soft & vol_expanded & dp_strict \
        & deliv_qty_ok & ~out["grade_a"]

    # ---- Layer 2 structure
    ext = (out["chg3d_pct"] > C.EXTENDED_3DAY_PCT) | \
          (p1 > stats["sma50"] * (1 + C.EXTENDED_ABOVE_SMA50_PCT / 100))
    base = (p1 <= stats["low52"] * (1 + C.BASE_NEAR_52WLOW_PCT / 100)) | \
           ((abs(p1 / stats["sma200"] - 1) <= C.BASE_NEAR_SMA200_PCT / 100)
            & (stats["sma50"] < stats["sma200"]))
    cont = (p1 > stats["sma50"]) & (stats["sma50"] > stats["sma200"])
    out["structure"] = np.select(
        [ext, base.fillna(False), cont.fillna(False)],
        ["Extended", "Base accumulation", "Continuation"], default="Neutral")
    out.loc[stats["sma200"].isna(), "structure"] = \
        out.loc[stats["sma200"].isna(), "structure"].replace(
            {"Base accumulation": "Neutral", "Continuation": "Neutral"})

    # ---- context columns
    out["vol_x_20d"] = (v1 / stats["vol20"]).round(2)
    out["dq_x_20d"] = (d1 / stats["dq20"]).round(2)
    out["deliv_pct"] = dp1.round(1)
    out["deliv_pct_prev"] = dp3.round(1)
    out["from_52w_high_pct"] = ((p1 / stats["hi52"] - 1) * 100).round(1)
    out["turnover_lacs"] = stats["TURNOVER_LACS"].round(0)

    # ---- Layer 3 events + score
    flags = build_event_flags(events).set_index("SYMBOL") \
        if events else pd.DataFrame()
    for f in ["promoter_buy", "promoter_sell", "block_deal", "bulk_buy",
              "bulk_circular", "pledge_creation", "pledge_release"]:
        out[f] = flags[f].reindex(out.index).fillna(False).astype(bool) \
            if len(flags) and f in flags else False
    out["event_notes"] = flags["notes"].reindex(out.index).fillna("") \
        if len(flags) and "notes" in flags else ""

    W = C.SCORE_WEIGHTS
    deliv_pts = pd.Series(
        np.clip((out["dq_x_20d"] - 1) / (C.DELIV_FULL_SCORE_MULTIPLE - 1),
                0, 1) * W["delivery_max"], index=out.index).fillna(0)
    vol_pts = pd.Series(
        np.clip((out["vol_x_20d"] - 1) / (C.VOL_FULL_SCORE_MULTIPLE - 1),
                0, 1) * W["volume_max"], index=out.index).fillna(0)
    struct_pts = out["structure"].map(W["structure"]).fillna(0)
    ev_pts = (out["promoter_buy"] * W["promoter_buy"]
              + out["block_deal"] * W["block_deal"]
              + out["bulk_buy"] * W["bulk_buy"]
              + out["pledge_release"] * W["pledge_release"]
              + out["promoter_sell"] * W["promoter_sell"]
              + out["pledge_creation"] * W["pledge_creation"]
              + out["bulk_circular"] * W["bulk_circular"])
    total = (deliv_pts + vol_pts + struct_pts + ev_pts).fillna(0)
    out["score"] = np.clip(total, 0, 100).round(0).astype(int)
    out["red_flag"] = out["promoter_sell"] | out["pledge_creation"] \
        | out["bulk_circular"]

    # ---- vs 21 DMA + technical posture + action label
    out["vs_21dma_pct"] = ((p1 / stats["sma21"] - 1) * 100).round(1)
    above21 = (p1 > stats["sma21"]).fillna(False)
    above50 = (p1 > stats["sma50"]).fillna(False)
    above200 = (p1 > stats["sma200"]).fillna(False)

    def posture(i):
        n = int(above21[i]) + int(above50[i]) + int(above200[i])
        if pd.isna(stats.loc[i, "sma200"]):
            return "> 21D" if above21[i] else "< 21D"
        return {3: "Above 21/50/200D", 0: "Below all DMAs",
                2: "Mixed (2 of 3)", 1: "Mixed (1 of 3)"}[n]
    out["tech_posture"] = [posture(i) for i in out.index]

    # Action rules (rule-based triage, not advice):
    #   IGNORE  red flag, or weak score
    #   LATE    structure Extended - move likely already happened
    #   ACT     score >= 55 AND trading above its 21 DMA
    #   WATCH   score 35-54, or strong score but below 21 DMA
    ACT_MIN_SCORE, WATCH_MIN_SCORE = 55, 35
    out["action"] = np.select(
        [out["red_flag"],
         out["structure"].eq("Extended"),
         (out["score"] >= ACT_MIN_SCORE) & above21,
         out["score"] >= WATCH_MIN_SCORE],
        ["IGNORE", "LATE", "ACT", "WATCH"], default="IGNORE")

    # ---- relative strength percentile within the liquid universe
    rs_rank = stats.loc[liquid[liquid].index, "ret_rs"].rank(pct=True) * 100
    out["rs_pct"] = rs_rank.reindex(out.index).round(0)

    # ---- futures OI overlay (F&O stocks only)
    out["fut_oi_chg_pct"] = np.nan
    if fo_oi is not None and len(fo_oi):
        m = fo_oi.set_index("SYMBOL")["oi_chg_pct"]
        out["fut_oi_chg_pct"] = m.reindex(out.index)

    out["seen"] = 1  # persistence count; overwritten by run_daily

    ga = out[out["grade_a"]].sort_values("score", ascending=False)
    gb = out[out["grade_b"]].sort_values("score", ascending=False)

    # ---- Stealth scan: elevated delivery, dormant price (pre-markup)
    t5 = g.tail(5).copy()
    agg5 = t5.groupby("SYMBOL").agg(dq5=("DELIV_QTY", "mean"),
                                    p_first=("CLOSE_PRICE", "first"),
                                    p_last=("CLOSE_PRICE", "last"),
                                    dp_last=("DELIV_PER", "last"),
                                    n5=("CLOSE_PRICE", "size"))
    agg5 = agg5[agg5["n5"] == 5].join(stats[["dq20", "sma21"]], how="inner")
    agg5["deliv5_x"] = agg5["dq5"] / agg5["dq20"]
    agg5["chg5_pct"] = (agg5["p_last"] / agg5["p_first"] - 1) * 100
    agg5["vs_21dma_pct"] = (agg5["p_last"] / agg5["sma21"] - 1) * 100
    shortlisted = set(ga.index) | set(gb.index)
    stealth = agg5[
        agg5.index.isin(liquid[liquid].index)
        & ~agg5.index.isin(shortlisted)
        & (agg5["deliv5_x"] >= C.STEALTH_DELIV_MULT)
        & (agg5["chg5_pct"].abs() <= C.STEALTH_MAX_ABS_5D_PCT)
        & agg5["dq20"].notna()
    ].sort_values("deliv5_x", ascending=False).head(C.STEALTH_TOP_N)
    stealth = stealth.rename(columns={"p_last": "close",
                                      "dp_last": "deliv_pct"})
    stealth[["deliv5_x", "chg5_pct", "vs_21dma_pct"]] = \
        stealth[["deliv5_x", "chg5_pct", "vs_21dma_pct"]].round(2)

    return {"grade_a": ga, "grade_b": gb, "latest": pd.Timestamp(latest),
            "n_days": len(dates), "universe": int(liquid.sum()),
            "stealth": stealth,
            "event_flags": flags.reset_index() if len(flags) else pd.DataFrame()}

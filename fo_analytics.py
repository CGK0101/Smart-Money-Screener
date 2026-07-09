"""F&O analytics engine (leg 2).

EOD analysis of index options from the NSE F&O bhavcopy:
- posture (futures OI read, trend), OI walls, max pain, PCR
- ATM straddle -> expected move; ATM IV via Black-Scholes; IV rank
- rule-based strategy-environment VERDICT per index/expiry
- expiry radar (last 2 sessions), stock-options bridge to the equity leg

Metrics persist to data/fo_metrics.csv so IV rank / trend build over time.
"""

import io
import math
import os
import datetime as dt
import pandas as pd
import numpy as np

import nse_fetch as nf
import config as C

BASE = os.path.dirname(os.path.abspath(__file__))
METRICS_PATH = os.path.join(BASE, "data", "fo_metrics.csv")

INDICES = ["NIFTY", "BANKNIFTY"]
RISK_FREE = 0.07

# ---- verdict thresholds (tunable)
IVR_SELL_MIN = 60          # IV rank needed for premium-selling GREEN
IVR_DEBIT_MAX = 30         # IV rank ceiling for debit/calendar GREEN
IV_OVER_RV_MIN = 1.10      # IV must exceed realized vol by 10% to sell
MIN_METRIC_ROWS_FOR_RANK = 20


# ------------------------------------------------------------- fetch/parse
def fetch_fo_bhav(d: dt.date, session=None) -> pd.DataFrame:
    """Full F&O bhavcopy (UDiFF) for one day. Empty frame on failure."""
    s = session or nf._session()
    url = ("https://nsearchives.nseindia.com/content/fo/"
           f"BhavCopy_NSE_FO_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip")
    try:
        r = s.get(url, timeout=60)
        if r.status_code != 200 or len(r.content) < 1000:
            return pd.DataFrame()
        df = pd.read_csv(io.BytesIO(r.content), compression="zip",
                         low_memory=False)
        df.columns = [str(c).strip() for c in df.columns]
        return df
    except Exception as e:  # noqa: BLE001
        print(f"[warn] fo bhav {d}: {e}")
        return pd.DataFrame()


def _col(df, *pats):
    import re
    for p in pats:
        for c in df.columns:
            if re.fullmatch(p, c, re.I):
                return c
    return None


def _std(df: pd.DataFrame) -> pd.DataFrame:
    """Standardize UDiFF columns we need."""
    m = {"typ": _col(df, "FinInstrmTp"), "sym": _col(df, "TckrSymb"),
         "exp": _col(df, "XpryDt"), "k": _col(df, "StrkPric"),
         "opt": _col(df, "OptnTp"), "close": _col(df, "ClsPric"),
         "oi": _col(df, "OpnIntrst"), "oic": _col(df, "ChngInOpnIntrst"),
         "val": _col(df, "TtlTrfVal"), "und": _col(df, "UndrlygPric")}
    if not all(m[k] for k in ("typ", "sym", "exp", "close", "oi")):
        return pd.DataFrame()
    out = pd.DataFrame({
        "typ": df[m["typ"]].astype(str).str.upper().str.strip(),
        "sym": df[m["sym"]].astype(str).str.upper().str.strip(),
        "exp": pd.to_datetime(df[m["exp"]], errors="coerce"),
        "k": pd.to_numeric(df[m["k"]], errors="coerce") if m["k"] else np.nan,
        "opt": df[m["opt"]].astype(str).str.upper().str.strip()
        if m["opt"] else "",
        "close": pd.to_numeric(df[m["close"]], errors="coerce"),
        "oi": pd.to_numeric(df[m["oi"]], errors="coerce"),
        "oic": pd.to_numeric(df[m["oic"]], errors="coerce")
        if m["oic"] else 0.0,
        "val": pd.to_numeric(df[m["val"]], errors="coerce")
        if m["val"] else 0.0,
        "und": pd.to_numeric(df[m["und"]], errors="coerce")
        if m["und"] else np.nan,
    })
    return out.dropna(subset=["exp", "close"])


# ------------------------------------------------------------- BS implied vol
def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs(S, K, T, r, sigma, call=True):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, (S - K) if call else (K - S))
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if call:
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def implied_vol(price, S, K, T, r=RISK_FREE, call=True):
    if price <= 0 or T <= 0:
        return np.nan
    lo, hi = 0.01, 3.0
    if _bs(S, K, T, r, hi, call) < price:
        return np.nan
    for _ in range(60):
        mid = (lo + hi) / 2
        if _bs(S, K, T, r, mid, call) > price:
            hi = mid
        else:
            lo = mid
    return round((lo + hi) / 2 * 100, 2)


# ------------------------------------------------------------- per-index calc
def _max_pain(ch: pd.DataFrame) -> float:
    ks = sorted(ch["k"].unique())
    coi = ch[ch["opt"] == "CE"].set_index("k")["oi"]
    poi = ch[ch["opt"] == "PE"].set_index("k")["oi"]
    best, best_pain = np.nan, None
    for K in ks:
        pain = sum(coi.get(kc, 0) * max(0, K - kc) for kc in coi.index) + \
               sum(poi.get(kp, 0) * max(0, kp - K) for kp in poi.index)
        if best_pain is None or pain < best_pain:
            best, best_pain = K, pain
    return best


def analyze_index(fo: pd.DataFrame, sym: str, session_date: dt.date) -> list:
    """Two nearest expiries for one index -> list of metric dicts."""
    opts = fo[(fo["sym"] == sym) & (fo["typ"] == "IDO")
              & fo["k"].notna() & (fo["oi"] > 0)]
    futs = fo[(fo["sym"] == sym) & (fo["typ"] == "IDF")].sort_values("exp")
    if opts.empty:
        return []
    expiries = sorted(e for e in opts["exp"].unique()
                      if e.date() >= session_date)[:2]
    fut_near = futs.iloc[0] if len(futs) else None
    fut_oi = float(futs["oi"].sum()) if len(futs) else np.nan
    fut_oic = float(futs["oic"].sum()) if len(futs) else np.nan
    rows = []
    for exp in expiries:
        ch = opts[opts["exp"] == exp]
        dte = max((exp.date() - session_date).days, 0)
        T = max(dte, 0.5) / 365.0
        # spot: underlying price col if present, else near-future close,
        # else put-call parity at the strike with min |C-P|
        spot = ch["und"].dropna().iloc[0] if ch["und"].notna().any() else \
            (float(fut_near["close"]) if fut_near is not None else np.nan)
        piv = ch.pivot_table(index="k", columns="opt", values="close",
                             aggfunc="last")
        if (np.isnan(spot) or spot <= 0) and {"CE", "PE"} <= set(piv.columns):
            both = piv.dropna()
            if len(both):
                k0 = (both["CE"] - both["PE"]).abs().idxmin()
                spot = k0 + both.loc[k0, "CE"] - both.loc[k0, "PE"]
        if np.isnan(spot) or spot <= 0:
            continue
        atm = min(piv.index, key=lambda k: abs(k - spot))
        cpx = piv.loc[atm].get("CE", np.nan)
        ppx = piv.loc[atm].get("PE", np.nan)
        straddle = (cpx + ppx) if pd.notna(cpx) and pd.notna(ppx) else np.nan
        em_pct = round(straddle / spot * 100, 2) if pd.notna(straddle) else np.nan
        ivc = implied_vol(cpx, spot, atm, T, call=True) if pd.notna(cpx) else np.nan
        ivp = implied_vol(ppx, spot, atm, T, call=False) if pd.notna(ppx) else np.nan
        iv = round(np.nanmean([ivc, ivp]), 2)
        coi = ch[ch["opt"] == "CE"].groupby("k")["oi"].sum().sort_values(ascending=False)
        poi = ch[ch["opt"] == "PE"].groupby("k")["oi"].sum().sort_values(ascending=False)
        pcr = round(poi.sum() / coi.sum(), 2) if coi.sum() > 0 else np.nan
        rows.append(dict(
            date=session_date.isoformat(), sym=sym,
            exp=exp.date().isoformat(), dte=dte, spot=round(float(spot), 2),
            atm=float(atm), straddle=round(float(straddle), 2)
            if pd.notna(straddle) else np.nan,
            em_pct=em_pct, iv=iv, iv_skew=round((ivp or np.nan) - (ivc or np.nan), 2)
            if pd.notna(ivp) and pd.notna(ivc) else np.nan,
            pcr=pcr, max_pain=_max_pain(ch),
            cw=";".join(str(int(k)) for k in coi.head(3).index),
            pw=";".join(str(int(k)) for k in poi.head(3).index),
            fut_oi=fut_oi, fut_oi_chg=fut_oic,
            opt_val=float(ch["val"].sum())))
    return rows


# ------------------------------------------------------------- persistence
def append_metrics(rows: list) -> pd.DataFrame:
    new = pd.DataFrame(rows)
    if os.path.exists(METRICS_PATH):
        log = pd.read_csv(METRICS_PATH)
        if len(new):
            log = log[log["date"] != new["date"].iloc[0]]
        log = pd.concat([log, new], ignore_index=True)
    else:
        log = new
    os.makedirs(os.path.dirname(METRICS_PATH), exist_ok=True)
    log.to_csv(METRICS_PATH, index=False)
    return log


def _front_series(log: pd.DataFrame, sym: str) -> pd.DataFrame:
    """Per-date front-expiry row for one index (for IV rank / trend / RV)."""
    s = log[log["sym"] == sym].copy()
    if s.empty:
        return s
    s = s.sort_values(["date", "dte"]).groupby("date").first().reset_index()
    return s.sort_values("date")


# ------------------------------------------------------------- verdicts
def verdict(row: dict, hist: pd.DataFrame) -> dict:
    """Rule-based strategy-environment call with an explicit checklist.
    Default is STAND ASIDE - the system must EARN a green light."""
    checks, n = {}, len(hist)
    ivr = trend = rv = np.nan
    if n >= MIN_METRIC_ROWS_FOR_RANK and pd.notna(row["iv"]):
        lo, hi = hist["iv"].min(), hist["iv"].max()
        ivr = round((row["iv"] - lo) / (hi - lo) * 100, 0) if hi > lo else 50.0
    if n >= 6:
        px = hist["spot"].astype(float)
        rets = np.log(px / px.shift(1)).dropna().tail(5)
        rv = round(float(rets.std() * math.sqrt(252) * 100), 2) if len(rets) >= 4 else np.nan
    if n >= 21:
        sma21 = hist["spot"].astype(float).rolling(21).mean().iloc[-1]
        trend = 1 if row["spot"] > sma21 else -1
    walls_ok = False
    try:
        pw1 = float(row["pw"].split(";")[0]); cw1 = float(row["cw"].split(";")[0])
        walls_ok = pw1 < row["spot"] < cw1
    except (ValueError, IndexError, AttributeError):
        pass

    checks["IV rank available"] = pd.notna(ivr)
    checks[f"IV rank >= {IVR_SELL_MIN} (rich premium)"] = \
        pd.notna(ivr) and ivr >= IVR_SELL_MIN
    checks["IV > realized vol x1.1 (seller edge)"] = \
        pd.notna(rv) and pd.notna(row["iv"]) and rv > 0 \
        and row["iv"] >= rv * IV_OVER_RV_MIN
    checks["Spot inside OI walls (range-bound)"] = walls_ok
    checks["DTE >= 2 (not pin-risk day)"] = row["dte"] >= 2
    sell_green = all([checks[f"IV rank >= {IVR_SELL_MIN} (rich premium)"],
                      checks["IV > realized vol x1.1 (seller edge)"],
                      checks["Spot inside OI walls (range-bound)"],
                      checks["DTE >= 2 (not pin-risk day)"]])

    oi_dir = np.sign(row.get("fut_oi_chg") or 0)
    debit_checks = {
        f"IV rank <= {IVR_DEBIT_MAX} (cheap premium)":
            pd.notna(ivr) and ivr <= IVR_DEBIT_MAX,
        "Trend established (vs 21D)": trend in (1, -1),
        "Futures OI supports trend":
            trend in (1, -1) and oi_dir != 0 and
            ((trend == 1 and oi_dir > 0) or (trend == -1 and oi_dir < 0)),
    }
    debit_green = all(debit_checks.values())
    checks.update(debit_checks)

    if sell_green:
        call, family = "GREEN - SELL PREMIUM", \
            "Iron condor / iron fly; short strikes beyond +/- expected move"
    elif debit_green:
        d = "CALL" if trend == 1 else "PUT"
        call, family = "GREEN - DEBIT DIRECTIONAL", \
            f"{d} spread with the trend; or calendar to own cheap vol"
    elif pd.notna(ivr) and ivr <= 25 and walls_ok:
        call, family = "AMBER - CALENDAR ZONE", \
            "Cheap vol + range: calendars only, small size"
    elif sum(checks.values()) >= len(checks) - 2 and n >= MIN_METRIC_ROWS_FOR_RANK:
        call, family = "AMBER - PARTIAL SETUP", \
            "Most conditions met; wait for full confluence or size at half"
    else:
        call, family = "STAND ASIDE", \
            "Edge not present. No trade IS the trade today."
    if n < MIN_METRIC_ROWS_FOR_RANK:
        call, family = "STAND ASIDE (calibrating)", \
            f"IV history building: {n}/{MIN_METRIC_ROWS_FOR_RANK} sessions"
    return dict(call=call, family=family, checks=checks, ivr=ivr, rv=rv,
                trend=trend)


# ------------------------------------------------------------- expiry radar
def expiry_radar(row: dict, prev: pd.Series | None) -> dict | None:
    if row["dte"] > 2:
        return None
    out = dict(dist_to_pain_pct=round(
        (row["spot"] / row["max_pain"] - 1) * 100, 2)
        if row.get("max_pain") else np.nan)
    if prev is not None and pd.notna(prev.get("straddle")) \
            and pd.notna(row.get("straddle")):
        out["straddle_chg_pct"] = round(
            (row["straddle"] / prev["straddle"] - 1) * 100, 1)
    return out


# ------------------------------------------------------------- stock bridge
def stock_bridge(fo: pd.DataFrame, act_watch: list) -> pd.DataFrame:
    if fo.empty or not act_watch:
        return pd.DataFrame()
    sto = fo[(fo["typ"] == "STO") & fo["sym"].isin(act_watch)]
    stf = fo[(fo["typ"] == "STF") & fo["sym"].isin(act_watch)]
    if sto.empty and stf.empty:
        return pd.DataFrame()
    rows = []
    for sym in sorted(set(sto["sym"]) | set(stf["sym"])):
        f = stf[stf["sym"] == sym]
        o = sto[sto["sym"] == sym]
        oi, oic = f["oi"].sum(), f["oic"].sum()
        val_cr = o["val"].sum() / 1e7
        liq = "A" if val_cr >= 50 else ("B" if val_cr >= 10 else "C")
        rows.append(dict(SYMBOL=sym,
                         fut_oi_chg_pct=round(oic / max(oi - oic, 1) * 100, 1),
                         opt_val_cr=round(val_cr, 1), liq=liq))
    return pd.DataFrame(rows).set_index("SYMBOL")


# ------------------------------------------------------------- orchestrator
def run_fo(session_date: dt.date, act_watch: list) -> dict | None:
    # F&O bhavcopy for the exact session may not be published yet (or the
    # date is a holiday). Walk back up to 6 calendar days to the most recent
    # file that actually exists - mirrors how the equity leg uses the last
    # available session rather than insisting on "today".
    fo_raw, used_date = pd.DataFrame(), None
    for back in range(0, 7):
        d = session_date - dt.timedelta(days=back)
        if d.weekday() >= 5:
            continue
        cand = fetch_fo_bhav(d)
        if not cand.empty:
            fo_raw, used_date = cand, d
            if back:
                print(f"[info] FO bhavcopy for {session_date} not available; "
                      f"using most recent {used_date}")
            break
    if fo_raw.empty:
        print("[warn] FO bhavcopy unavailable (last 6 sessions all 404); "
              "FO page skipped this run")
        return None
    session_date = used_date
    fo = _std(fo_raw)
    if fo.empty:
        print("[warn] FO bhavcopy format unrecognized; FO page skipped")
        return None
    rows = []
    for sym in INDICES:
        rows += analyze_index(fo, sym, session_date)
    if not rows:
        return None
    log = append_metrics(rows)
    boards = []
    for r in rows:
        hist = _front_series(log[log["date"] < r["date"]], r["sym"])
        v = verdict(r, hist)
        prev = hist.iloc[-1] if len(hist) else None
        radar = expiry_radar(r, prev)
        boards.append(dict(metrics=r, verdict=v, radar=radar))
    return dict(boards=boards, session=session_date,
                bridge=stock_bridge(fo, act_watch),
                hist_days={s: len(_front_series(log, s)) for s in INDICES})

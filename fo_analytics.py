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

# per-run cache of option chains, keyed (sym, expiry-iso) -> dict with
# ce/pe frames (close, oi indexed by strike), strike step, spot.
LAST_CHAINS: dict = {}

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
        # collapse any duplicate strike/side rows within this expiry FIRST,
        # so straddle and walls are computed on clean per-strike figures.
        ce = ch[ch["opt"] == "CE"].groupby("k").agg(
            close=("close", "max"), oi=("oi", "sum")).sort_index()
        pe = ch[ch["opt"] == "PE"].groupby("k").agg(
            close=("close", "max"), oi=("oi", "sum")).sort_index()
        if ce.empty or pe.empty:
            continue
        # spot: underlying col if present, else near-future close, else
        # put-call parity at the strike with the smallest |C - P|
        spot = ch["und"].dropna().iloc[0] if ch["und"].notna().any() else \
            (float(fut_near["close"]) if fut_near is not None else np.nan)
        if np.isnan(spot) or spot <= 0:
            common = ce.index.intersection(pe.index)
            if len(common):
                diff = (ce.loc[common, "close"] - pe.loc[common, "close"]).abs()
                k0 = diff.idxmin()
                spot = k0 + ce.loc[k0, "close"] - pe.loc[k0, "close"]
        if np.isnan(spot) or spot <= 0:
            continue
        # ATM = strike closest to spot that has BOTH legs priced > 0
        both_k = [k for k in ce.index.intersection(pe.index)
                  if ce.loc[k, "close"] > 0 and pe.loc[k, "close"] > 0]
        if not both_k:
            continue
        atm = min(both_k, key=lambda k: abs(k - spot))
        cpx = float(ce.loc[atm, "close"])
        ppx = float(pe.loc[atm, "close"])
        straddle = cpx + ppx
        steps = np.diff(sorted(both_k))
        step = float(np.median(steps)) if len(steps) else 50.0
        LAST_CHAINS[(sym, exp.date().isoformat())] = dict(
            ce=ce, pe=pe, step=step, spot=float(spot), atm=float(atm))
        em_pct = round(straddle / spot * 100, 2)
        ivc = implied_vol(cpx, spot, atm, T, call=True)
        ivp = implied_vol(ppx, spot, atm, T, call=False)
        iv = round(np.nanmean([ivc, ivp]), 2)
        coi = ce["oi"].sort_values(ascending=False)
        poi = pe["oi"].sort_values(ascending=False)
        pcr = round(poi.sum() / coi.sum(), 2) if coi.sum() > 0 else np.nan
        rows.append(dict(
            date=session_date.isoformat(), sym=sym,
            exp=exp.date().isoformat(), dte=dte, spot=round(float(spot), 2),
            atm=float(atm), straddle=round(float(straddle), 2),
            em_pct=em_pct, iv=iv,
            iv_skew=round((ivp or np.nan) - (ivc or np.nan), 2)
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
        bp = build_blueprint(r, v["call"], v["trend"])
        boards.append(dict(metrics=r, verdict=v, radar=radar, blueprint=bp))
    bridge = stock_blueprints(fo, stock_bridge(fo, act_watch))
    return dict(boards=boards, session=session_date, bridge=bridge,
                hist_days={s: len(_front_series(log, s)) for s in INDICES})


# ------------------------------------------------------------- blueprints
MGMT = {
    "condor": ("Enter next session (re-quote live; EOD closes shift). "
               "Take profit at 50% of credit received. Hard exit if loss "
               "reaches 1x credit, or spot CLOSES beyond a short strike. "
               "Exit by 1 DTE regardless - never hold condors into expiry."),
    "fly": ("Expiry-day pin structure: HALF normal size. Take profit at "
            "25-40% of credit. Exit immediately if spot moves beyond a wing "
            "or 1.5x the remaining straddle from max pain."),
    "debit": ("Enter next session on live quotes. Risk = net debit, "
              "pre-defined. Take profit at 60-100% gain on debit. Exit if "
              "spot closes back below the 21 DMA (bull) / above it (bear), "
              "or at 50% loss of debit. Exit by 2 DTE."),
    "calendar": ("Enter next session. Risk = net debit. Profit zone is spot "
                 "pinned near the strike at front expiry. Close the whole "
                 "structure BEFORE the front leg's expiry day. Exit early "
                 "if spot moves > expected move from the strike."),
    "stock_spread": ("Defined-risk expression of the equity accumulation "
                     "signal. Risk = net debit only. Take profit at "
                     "60-100% of debit. Invalidation: stock closes below "
                     "its 21 DMA - exit, the accumulation thesis is "
                     "suspended. Exit by 5 DTE."),
}


def _pick(strikes, target, side):
    """Nearest listed strike >= target (side=+1) or <= target (side=-1)."""
    ks = sorted(strikes)
    cands = [k for k in ks if k >= target] if side > 0 else \
            [k for k in ks if k <= target]
    if not cands:
        return None
    return cands[0] if side > 0 else cands[-1]


def _px(frame, k):
    try:
        v = float(frame.loc[k, "close"])
        return v if v > 0 else None
    except KeyError:
        return None


def build_blueprint(m: dict, call: str, trend) -> dict | None:
    """Concrete structure with strikes/costs from EOD closes, for GREEN and
    AMBER verdicts plus the expiry pin-fly window. None = no blueprint."""
    ch = LAST_CHAINS.get((m["sym"], m["exp"]))
    if not ch:
        return None
    ce, pe, step, spot = ch["ce"], ch["pe"], ch["step"], ch["spot"]
    atm = ch["atm"]
    em = m.get("straddle") or 0
    wing = 3 * step

    if call.startswith("GREEN - SELL"):
        sc = _pick(ce.index, spot + em, +1)
        sp = _pick(pe.index, spot - em, -1)
        if not sc or not sp:
            return None
        lc = _pick(ce.index, sc + wing, +1) or max(
            (k for k in ce.index if k > sc), default=None)
        lp = _pick(pe.index, sp - wing, -1) or min(
            (k for k in pe.index if k < sp), default=None)
        legs_px = [_px(ce, sc), _px(ce, lc), _px(pe, sp), _px(pe, lp)]
        if None in legs_px or not lc or not lp:
            return None
        credit = legs_px[0] - legs_px[1] + legs_px[2] - legs_px[3]
        width = min(lc - sc, sp - lp)
        if credit <= 0:
            return None
        return dict(name="Iron condor (short strikes beyond expected move)",
                    legs=f"Sell {sp:.0f}P / Buy {lp:.0f}P · "
                         f"Sell {sc:.0f}C / Buy {lc:.0f}C",
                    cost=f"Est. net credit ≈ ₹{credit:,.0f} per lot-unit "
                         "(EOD closes)",
                    risk=f"Max risk ≈ ₹{width - credit:,.0f} per lot-unit",
                    zone=f"Profit zone {sp - credit:,.0f} – {sc + credit:,.0f}"
                         f" (walls: {m['pw'].split(';')[0]} / "
                         f"{m['cw'].split(';')[0]})",
                    mgmt=MGMT["condor"])

    if call.startswith("GREEN - DEBIT"):
        if trend == 1:
            k2 = _pick(ce.index, spot + em, +1)
            d = (_px(ce, atm) or 0) - (_px(ce, k2) or 0) if k2 else 0
            if not k2 or d <= 0:
                return None
            return dict(name="Bull call spread (with trend + OI)",
                        legs=f"Buy {atm:.0f}C / Sell {k2:.0f}C",
                        cost=f"Est. net debit ≈ ₹{d:,.0f} per lot-unit",
                        risk=f"Max risk = debit; max reward ≈ "
                             f"₹{(k2 - atm) - d:,.0f}",
                        zone=f"Breakeven ≈ {atm + d:,.0f}",
                        mgmt=MGMT["debit"])
        else:
            k2 = _pick(pe.index, spot - em, -1)
            d = (_px(pe, atm) or 0) - (_px(pe, k2) or 0) if k2 else 0
            if not k2 or d <= 0:
                return None
            return dict(name="Bear put spread (with trend + OI)",
                        legs=f"Buy {atm:.0f}P / Sell {k2:.0f}P",
                        cost=f"Est. net debit ≈ ₹{d:,.0f} per lot-unit",
                        risk=f"Max risk = debit; max reward ≈ "
                             f"₹{(atm - k2) - d:,.0f}",
                        zone=f"Breakeven ≈ {atm - d:,.0f}",
                        mgmt=MGMT["debit"])

    if call.startswith("AMBER - CALENDAR"):
        # same-strike calendar: sell this (front) expiry ATM call, buy the
        # next expiry's same strike.
        back = None
        for (s2, e2), c2 in LAST_CHAINS.items():
            if s2 == m["sym"] and e2 > m["exp"]:
                back = (e2, c2)
                break
        if not back:
            return None
        e2, c2 = back
        f_px, b_px = _px(ce, atm), _px(c2["ce"], atm)
        if f_px is None or b_px is None or b_px <= f_px:
            return None
        return dict(name="ATM call calendar (own cheap vol)",
                    legs=f"Sell {atm:.0f}C ({m['exp']}) / "
                         f"Buy {atm:.0f}C ({e2})",
                    cost=f"Est. net debit ≈ ₹{b_px - f_px:,.0f} per lot-unit",
                    risk="Max risk = net debit (defined)",
                    zone=f"Profit peaks with spot pinned near {atm:,.0f} "
                         "at front expiry",
                    mgmt=MGMT["calendar"])

    # expiry pin-fly: only when pinned tight to max pain with <=1 DTE
    if m["dte"] <= 1 and m.get("max_pain") \
            and abs(spot / m["max_pain"] - 1) < 0.003:
        mp = _pick(ce.index, m["max_pain"], +1) or atm
        lc, lp = _pick(ce.index, mp + wing, +1), _pick(pe.index, mp - wing, -1)
        px = [_px(ce, mp), _px(pe, mp), _px(ce, lc), _px(pe, lp)]
        if None in px or not lc or not lp:
            return None
        credit = px[0] + px[1] - px[2] - px[3]
        if credit <= 0:
            return None
        return dict(name="Expiry pin iron fly (HALF SIZE - optional)",
                    legs=f"Sell {mp:.0f} straddle / Buy {lp:.0f}P + {lc:.0f}C",
                    cost=f"Est. net credit ≈ ₹{credit:,.0f} per lot-unit",
                    risk=f"Max risk ≈ ₹{wing - credit:,.0f} per lot-unit",
                    zone=f"Pin thesis: spot stays near max pain "
                         f"{m['max_pain']:,.0f} into expiry",
                    mgmt=MGMT["fly"])
    return None


def stock_blueprints(fo: pd.DataFrame, bridge: pd.DataFrame) -> pd.DataFrame:
    """For liquid bridged stocks, derive a concrete bull-call-spread
    expression of the equity accumulation signal."""
    if bridge is None or bridge.empty:
        return bridge
    out = bridge.copy()
    for col in ("structure", "cost", "invalidation"):
        out[col] = ""
    sto = fo[fo["typ"] == "STO"]
    for sym in out.index:
        if out.loc[sym, "liq"] == "C":
            out.loc[sym, "structure"] = "— (illiquid: trade the cash instead)"
            continue
        o = sto[sto["sym"] == sym]
        if o.empty:
            continue
        exp = sorted(o["exp"].unique())[0]
        chn = o[o["exp"] == exp]
        ce = chn[chn["opt"] == "CE"].groupby("k").agg(
            close=("close", "max")).sort_index()
        spot = chn["und"].dropna().iloc[0] if chn["und"].notna().any() \
            else np.nan
        if np.isnan(spot):
            f = fo[(fo["typ"] == "STF") & (fo["sym"] == sym)]
            spot = float(f["close"].iloc[0]) if len(f) else np.nan
        if np.isnan(spot) or ce.empty:
            continue
        atm = min(ce.index, key=lambda k: abs(k - spot))
        k2 = _pick(ce.index, spot * 1.07, +1)
        d = (_px(ce, atm) or 0) - (_px(ce, k2) or 0) if k2 else 0
        if not k2 or d <= 0 or k2 <= atm:
            continue
        out.loc[sym, "structure"] = (f"Bull call spread {atm:.0f}C/{k2:.0f}C "
                                     f"({pd.Timestamp(exp).date()})")
        out.loc[sym, "cost"] = (f"debit ≈ ₹{d:,.1f}/share · "
                                f"max reward ≈ ₹{(k2 - atm) - d:,.1f}")
        out.loc[sym, "invalidation"] = "close below 21 DMA = thesis off"
    return out

"""Validate the screener on engineered synthetic data.
Expected outcomes are asserted so logic regressions are caught."""

import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import screener  # noqa: E402
import report_gen  # noqa: E402

rng = np.random.default_rng(7)
DATES = pd.bdate_range("2026-04-01", periods=60)


def make_stock(sym, base_price, base_vol, base_dp, tweak):
    """tweak(i, row) mutates the last few days to plant a pattern."""
    rows = []
    price = base_price
    for i, d in enumerate(DATES):
        prev = price
        drift = rng.normal(0, 0.008)
        price = max(1, price * (1 + drift))
        vol = max(1000, base_vol * rng.lognormal(0, 0.25))
        dp = np.clip(base_dp + rng.normal(0, 4), 5, 95)
        row = dict(SYMBOL=sym, SERIES="EQ", DATE=d.date().isoformat(),
                   CLOSE_PRICE=price, TTL_TRD_QNTY=vol, DELIV_PER=dp,
                   _prev=prev)
        row = tweak(i, row) or row
        row.pop("_prev", None)
        row["PREV_CLOSE"] = price
        row["OPEN_PRICE"] = row["CLOSE_PRICE"] * 0.995
        row["HIGH_PRICE"] = row["CLOSE_PRICE"] * 1.01
        row["LOW_PRICE"] = row["CLOSE_PRICE"] * 0.985
        row["DELIV_QTY"] = row["TTL_TRD_QNTY"] * row["DELIV_PER"] / 100
        row["TURNOVER_LACS"] = row["CLOSE_PRICE"] * row["TTL_TRD_QNTY"] / 1e5
        row["NO_OF_TRADES"] = int(row["TTL_TRD_QNTY"] / 50)
        rows.append(row)
        price = row["CLOSE_PRICE"]
    return rows


def last3(i):
    return i >= len(DATES) - 3


# --- ACCUM_A: textbook Grade A. Price/vol/del% all up 3 straight days.
def tweak_a(i, r):
    if last3(i):
        k = i - (len(DATES) - 3)  # 0,1,2
        r["CLOSE_PRICE"] = r["_prev"] * 1.015
        r["TTL_TRD_QNTY"] = 500_000 * (2.0 + k)          # 2x,3x,4x avg-ish
        r["DELIV_PER"] = 45 + 8 * (k + 1)                # 53,61,69
    return r


# --- ACCUM_B: soft price (up,down-tick,up net positive), vol expanded 2/3.
def tweak_b(i, r):
    if last3(i):
        k = i - (len(DATES) - 3)
        r["CLOSE_PRICE"] = r["_prev"] * [1.006, 0.998, 1.016][k]
        r["TTL_TRD_QNTY"] = [1_600_000, 700_000, 2_100_000][k]  # avg ~800k
        r["DELIV_PER"] = [50, 56, 63][k]
    return r


# --- EXTENDED: passes Grade A mechanics but 3-day move ~+18% -> Extended tag.
def tweak_ext(i, r):
    if last3(i):
        k = i - (len(DATES) - 3)
        r["CLOSE_PRICE"] = r["_prev"] * 1.058
        r["TTL_TRD_QNTY"] = 900_000 * (2 + k)
        r["DELIV_PER"] = 40 + 10 * (k + 1)
    return r


# --- ILLIQUID: perfect 9 Ys but ~₹3 lakh turnover -> must be excluded.
def tweak_illq(i, r):
    r["TTL_TRD_QNTY"] = 3000
    if last3(i):
        r["CLOSE_PRICE"] = r["_prev"] * 1.01
        k = i - (len(DATES) - 3)
        r["TTL_TRD_QNTY"] = 4000 + 1000 * k
        r["DELIV_PER"] = 60 + 5 * k
    return r


# --- DELIVTRAP: del% rising but delivery QTY collapsing (traders left).
def tweak_trap(i, r):
    if last3(i):
        k = i - (len(DATES) - 3)
        r["CLOSE_PRICE"] = r["_prev"] * 1.01
        r["TTL_TRD_QNTY"] = 400_000 / (2 + k)     # volume shrinking
        r["DELIV_PER"] = 55 + 10 * k              # % rising, qty falling
    return r


# --- NOISE stocks: random walks, should not appear.
def tweak_none(i, r):
    return r


def build():
    rows = []
    rows += make_stock("ACCUMA", 100, 500_000, 45, tweak_a)
    rows += make_stock("ACCUMB", 200, 800_000, 48, tweak_b)
    rows += make_stock("EXTENDED", 50, 900_000, 40, tweak_ext)
    rows += make_stock("ILLIQUID", 90, 3000, 55, tweak_illq)
    rows += make_stock("DELIVTRAP", 150, 400_000, 50, tweak_trap)
    for n in range(25):
        rows += make_stock(f"NOISE{n:02d}", 80 + n * 13, 300_000 + n * 40_000,
                           35 + n, tweak_none)
    # non-EQ junk that must be filtered
    junk = make_stock("618GS2024", 100, 500, 0, tweak_none)
    for r in junk:
        r["SERIES"] = "GS"
    rows += junk
    return pd.DataFrame(rows)


def build_events():
    bulk = pd.DataFrame([
        {"BD_SYMBOL": "ACCUMA", "BD_CLIENT_NAME": "MARQUEE FUND LLP",
         "BD_BUY_SELL": "BUY", "BD_QTY_TRD": 500000},
        {"BD_SYMBOL": "NOISE01", "BD_CLIENT_NAME": "SHADY TRADER",
         "BD_BUY_SELL": "BUY", "BD_QTY_TRD": 100000},
        {"BD_SYMBOL": "NOISE01", "BD_CLIENT_NAME": "SHADY TRADER",
         "BD_BUY_SELL": "SELL", "BD_QTY_TRD": 100000},
    ])
    insider = pd.DataFrame([
        {"symbol": "ACCUMB", "personCategory": "Promoters",
         "acqMode": "Market Purchase - Acquisition", "acquirerName": "MD"},
        {"symbol": "EXTENDED", "personCategory": "Promoters",
         "acqMode": "Market Sale - Disposal", "acquirerName": "Promoter Grp"},
    ])
    pledge = pd.DataFrame([
        {"symbol": "ACCUMA", "eventType": "Release of pledge"},
        {"symbol": "DELIVTRAP", "eventType": "Creation of pledge"},
    ])
    block = pd.DataFrame([
        {"symbol": "ACCUMB", "clientName": "BIG INSURANCE CO"},
    ])
    return {"bulk": bulk, "block": block, "insider": insider, "pledge": pledge}


hist = build()
events = build_events()
res = screener.run_screen(hist, events)
ga, gb = res["grade_a"], res["grade_b"]

print("GRADE A:", list(ga.index))
print("GRADE B:", list(gb.index))
print()
cols = ["score", "chg3d_pct", "vol_x_20d", "dq_x_20d", "structure",
        "promoter_buy", "promoter_sell", "pledge_release", "red_flag"]
print(pd.concat([ga, gb])[cols].round(1).to_string())

# ---- assertions
assert "ACCUMA" in ga.index, "textbook accumulation must be Grade A"
assert "ACCUMB" in gb.index, "soft-price pattern must be Grade B"
assert "ILLIQUID" not in ga.index and "ILLIQUID" not in gb.index, \
    "illiquid stock must be excluded by turnover floor"
assert "DELIVTRAP" not in ga.index and "DELIVTRAP" not in gb.index, \
    "rising del%% with collapsing del qty must be rejected"
assert "618GS2024" not in ga.index and "618GS2024" not in gb.index
if "EXTENDED" in ga.index:
    assert ga.loc["EXTENDED", "structure"] == "Extended", \
        "18%% 3-day mover must carry Extended tag"
assert ga.loc["ACCUMA", "pledge_release"]
assert bool(pd.concat([ga, gb]).loc["ACCUMB", "promoter_buy"])
noise = [s for s in list(ga.index) + list(gb.index) if s.startswith("NOISE")]
print(f"\nNoise leakage: {noise} (a couple by chance is acceptable)")
assert len(noise) <= 2, "too many random stocks passing = screen too loose"

html = report_gen.render(res)
out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "sample_report.html")
with open(out, "w") as f:
    f.write(html)
print(f"\nAll assertions passed. Sample report -> {out}")

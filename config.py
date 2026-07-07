"""
Smart Money Screener - Configuration
All thresholds we agreed on live here so iteration = editing one file.
"""

# ---------- Layer 1: Accumulation screen ----------
SERIES_ALLOWED = {"EQ"}          # EQ only; no BE/GS/SGB/MF junk
MIN_AVG_TURNOVER_LACS = 50.0     # ₹50 lakh avg daily turnover floor (TURNOVER_LACS)
LOOKBACK_AVG_DAYS = 20           # baseline window for volume / delivery averages
VOL_3DAY_MULTIPLE = 1.5          # 3-day total volume must exceed 1.5x of 3x 20d-avg
VOL_DAYS_ABOVE_AVG_MIN = 2       # at least 2 of 3 days individually above 20d avg

# ---------- Layer 2: Price structure ----------
EXTENDED_3DAY_PCT = 12.0         # 3-day move beyond this = "Extended" (red flag)
EXTENDED_ABOVE_SMA50_PCT = 25.0  # close >25% above 50DMA = Extended
BASE_NEAR_52WLOW_PCT = 10.0      # within 10% of 52w low = Base zone
BASE_NEAR_SMA200_PCT = 5.0       # within +/-5% of 200DMA reclaim = Base zone
SMA_SHORT = 50
SMA_LONG = 200

# ---------- Layer 3: Event overlay scoring ----------
SCORE_WEIGHTS = {
    "delivery_max": 30,          # delivery-quantity surge vs 20d avg
    "volume_max": 20,            # volume expansion
    "structure": {               # where in the price structure
        "Base accumulation": 20,
        "Continuation": 12,
        "Neutral": 8,
        "Extended": 2,
    },
    "promoter_buy": 15,
    "block_deal": 10,
    "bulk_buy": 8,
    "pledge_release": 5,
    # negative overrides
    "promoter_sell": -20,
    "pledge_creation": -15,
    "bulk_circular": -10,        # same entity on both sides within window
}

EVENT_LOOKBACK_DAYS = 7          # how far back events count toward today's score

# ---------- Delivery scaling ----------
# deliv_qty at 3x its 20d avg earns full 30 pts; linear in between
DELIV_FULL_SCORE_MULTIPLE = 3.0
VOL_FULL_SCORE_MULTIPLE = 2.0

# ---------- Report ----------
TOP_N_DISPLAY = 40               # max rows per grade table
MIN_HISTORY_DAYS = 25            # minimum days of data before screener runs

# ---------- Tier 1/2 additions ----------
MIN_PRICE = 30.0                 # sub-Rs30 counters are operator playgrounds
RS_LOOKBACK = 60                 # relative-strength window (sessions)
PERSIST_WINDOW = 15              # sessions to count repeat appearances
STEALTH_DELIV_MULT = 1.6         # 5d avg delivery qty vs 20d avg
STEALTH_MAX_ABS_5D_PCT = 3.0     # price must be quiet: |5d change| under this
STEALTH_TOP_N = 15
SCORECARD_HORIZONS = (5, 10, 20) # forward-return horizons (sessions)
SCORECARD_LOOKBACK_DAYS = 120    # how far back to evaluate past signals
FO_OI_TAG_PCT = 5.0              # futures OI change worth tagging
DISTRIB_DELIV_MULT = 1.5         # holdings: heavy delivery on a down day

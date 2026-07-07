"""
NSE data fetchers.
- Daily full bhavcopy (sec_bhavdata_full) -> compacted per-day CSV in data/bhav/
- Event data: bulk deals, block deals, insider (PIT), pledge (SAST) via NSE APIs.

NSE changes endpoints and anti-bot behaviour from time to time. Every fetcher
fails GRACEFULLY: it logs a warning and returns an empty frame so the report
still generates with whatever data is available.
"""

import io
import os
import time
import datetime as dt
import pandas as pd
import requests

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
BHAV_DIR = os.path.join(DATA_DIR, "bhav")
EVENTS_DIR = os.path.join(DATA_DIR, "events")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

KEEP_COLS = ["SYMBOL", "SERIES", "DATE1", "PREV_CLOSE", "OPEN_PRICE",
             "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE", "TTL_TRD_QNTY",
             "TURNOVER_LACS", "NO_OF_TRADES", "DELIV_QTY", "DELIV_PER"]


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    try:  # cookie warm-up needed for www.nseindia.com/api endpoints
        s.get("https://www.nseindia.com", timeout=15)
        time.sleep(1)
    except requests.RequestException:
        pass
    return s


# ------------------------------------------------------------------ bhavcopy
def bhavcopy_url(d: dt.date) -> str:
    return ("https://nsearchives.nseindia.com/products/content/"
            f"sec_bhavdata_full_{d.strftime('%d%m%Y')}.csv")


def fetch_bhavcopy(d: dt.date, session=None) -> pd.DataFrame | None:
    """Download and compact one day's bhavcopy. None if unavailable (holiday /
    not yet published)."""
    s = session or _session()
    try:
        r = s.get(bhavcopy_url(d), timeout=30)
    except requests.RequestException as e:
        print(f"[warn] bhavcopy {d}: network error {e}")
        return None
    if r.status_code != 200 or len(r.content) < 1000:
        return None
    df = pd.read_csv(io.BytesIO(r.content))
    df.columns = [c.strip() for c in df.columns]
    for c in df.select_dtypes(include="object").columns:
        df[c] = df[c].str.strip()
    df = df[[c for c in KEEP_COLS if c in df.columns]].copy()
    # numeric coercion ('-' appears in DELIV_* for non-EQ series)
    for c in ["PREV_CLOSE", "OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE",
              "CLOSE_PRICE", "TTL_TRD_QNTY", "TURNOVER_LACS",
              "NO_OF_TRADES", "DELIV_QTY", "DELIV_PER"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["DATE"] = d.isoformat()
    return df


def save_bhav(df: pd.DataFrame, d: dt.date):
    os.makedirs(BHAV_DIR, exist_ok=True)
    df.to_csv(os.path.join(BHAV_DIR, f"{d.isoformat()}.csv"), index=False)


def ensure_bhavcopy(d: dt.date, session=None) -> bool:
    path = os.path.join(BHAV_DIR, f"{d.isoformat()}.csv")
    if os.path.exists(path):
        return True
    df = fetch_bhavcopy(d, session)
    if df is None:
        return False
    save_bhav(df, d)
    print(f"[ok] bhavcopy saved {d}")
    return True


def load_history(max_days: int = 300) -> pd.DataFrame:
    """Load all stored compact bhav files (most recent max_days)."""
    if not os.path.isdir(BHAV_DIR):
        return pd.DataFrame()
    files = sorted(f for f in os.listdir(BHAV_DIR) if f.endswith(".csv"))[-max_days:]
    frames = [pd.read_csv(os.path.join(BHAV_DIR, f)) for f in files]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ------------------------------------------------------------------ events
def _api_get(session, url, params) -> list | dict | None:
    """NSE's www APIs are behind anti-bot protection that sometimes serves an
    HTML block page (=> JSON parse error) especially to cloud IPs. Retry once
    with a fresh cookie warm-up before giving up gracefully."""
    for attempt in (1, 2):
        try:
            r = session.get(url, params=params, timeout=30,
                            headers={"Accept": "application/json, text/plain, */*",
                                     "X-Requested-With": "XMLHttpRequest"})
            if r.status_code == 200:
                return r.json()
            print(f"[warn] api {url}: HTTP {r.status_code} (attempt {attempt})")
        except (requests.RequestException, ValueError) as e:
            print(f"[warn] api {url}: {e} (attempt {attempt})")
        if attempt == 1:
            try:  # fresh warm-up, then retry
                time.sleep(3)
                session.get("https://www.nseindia.com/market-data/large-deals",
                            timeout=15)
                time.sleep(2)
            except requests.RequestException:
                pass
    return None


def _dmy(d: dt.date) -> str:
    return d.strftime("%d-%m-%Y")


def fetch_bulk_block(session, frm: dt.date, to: dt.date):
    """Bulk and block deals. Plan A: the nsearchives CSVs (same server that
    already serves us the bhavcopy, so it works from cloud IPs). Plan B:
    the www API (often blocked from data centers)."""
    import re as _re
    out = {}
    archive = {"bulk-deals": "bulk.csv", "block-deals": "block.csv"}
    for kind, fname in archive.items():
        df = pd.DataFrame()
        # --- Plan A: archive CSV
        try:
            r = session.get(
                f"https://nsearchives.nseindia.com/content/equities/{fname}",
                timeout=30)
            if r.status_code == 200 and len(r.content) > 50:
                df = pd.read_csv(io.BytesIO(r.content))
                df.columns = [str(c).strip() for c in df.columns]
                for c in df.select_dtypes(include="object").columns:
                    df[c] = df[c].astype(str).str.strip()
                datec = next((c for c in df.columns
                              if _re.search("date", c, _re.I)), None)
                if datec:
                    d = pd.to_datetime(df[datec], format="%d-%b-%Y",
                                       errors="coerce")
                    d = d.fillna(pd.to_datetime(df[datec], dayfirst=True,
                                                errors="coerce"))
                    df = df[(d >= pd.Timestamp(frm)) & (d <= pd.Timestamp(to))]
        except requests.RequestException as e:
            print(f"[warn] archive {fname}: {e}")
        # --- Plan B: www API
        if df.empty:
            js = _api_get(session,
                          f"https://www.nseindia.com/api/historical/{kind}",
                          {"from": _dmy(frm), "to": _dmy(to)})
            rows = (js or {}).get("data", []) if isinstance(js, dict) \
                else (js or [])
            df = pd.DataFrame(rows)
        out[kind] = df
        print(f"[info] {kind}: {len(df)} rows")
    return out["bulk-deals"], out["block-deals"]


def fetch_insider(session, frm: dt.date, to: dt.date) -> pd.DataFrame:
    """Insider trading (PIT) disclosures."""
    js = _api_get(session, "https://www.nseindia.com/api/corporates-pit",
                  {"index": "equities", "from_date": _dmy(frm), "to_date": _dmy(to)})
    rows = (js or {}).get("data", []) if isinstance(js, dict) else (js or [])
    df = pd.DataFrame(rows)
    print(f"[info] insider PIT: {len(df)} rows")
    return df


def fetch_pledge(session, frm: dt.date, to: dt.date) -> pd.DataFrame:
    """SAST pledge disclosures. Endpoint most subject to NSE changes."""
    js = _api_get(session, "https://www.nseindia.com/api/corporate-sast-pledged",
                  {"index": "equities", "from_date": _dmy(frm), "to_date": _dmy(to)})
    rows = (js or {}).get("data", []) if isinstance(js, dict) else (js or [])
    df = pd.DataFrame(rows)
    print(f"[info] pledge SAST: {len(df)} rows")
    return df


def fetch_all_events(days_back: int = 7) -> dict:
    s = _session()
    to = dt.date.today()
    frm = to - dt.timedelta(days=days_back)
    bulk, block = fetch_bulk_block(s, frm, to)
    insider = fetch_insider(s, frm, to)
    pledge = fetch_pledge(s, frm, to)
    os.makedirs(EVENTS_DIR, exist_ok=True)
    for name, df in [("bulk", bulk), ("block", block),
                     ("insider", insider), ("pledge", pledge)]:
        df.to_csv(os.path.join(EVENTS_DIR, f"{name}.csv"), index=False)
    return {"bulk": bulk, "block": block, "insider": insider, "pledge": pledge}


def load_events() -> dict:
    out = {}
    for name in ("bulk", "block", "insider", "pledge"):
        p = os.path.join(EVENTS_DIR, f"{name}.csv")
        out[name] = pd.read_csv(p) if os.path.exists(p) and os.path.getsize(p) > 2 \
            else pd.DataFrame()
    return out

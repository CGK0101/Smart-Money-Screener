"""One-time F&O metrics backfill so IV rank / trend work from day one.
Downloads each day's FO bhavcopy, computes index metrics, appends to
data/fo_metrics.csv, discards the raw file.  python scripts/backfill_fo.py --days 90
"""
import argparse, datetime as dt, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fo_analytics as fa
import pandas as pd

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    a = ap.parse_args()
    have = set()
    if os.path.exists(fa.METRICS_PATH):
        have = set(pd.read_csv(fa.METRICS_PATH)["date"].unique())
    got, d, tried = 0, dt.date.today(), 0
    while got < a.days and tried < a.days * 2 + 60:
        if d.weekday() < 5:
            tried += 1
            if d.isoformat() in have:
                got += 1
            else:
                raw = fa.fetch_fo_bhav(d)
                if len(raw):
                    fo = fa._std(raw)
                    rows = []
                    for sym in fa.INDICES:
                        rows += fa.analyze_index(fo, sym, d)
                    if rows:
                        fa.append_metrics(rows)
                        got += 1
                        print(f"[ok] fo metrics {d}")
                    time.sleep(1.2)
        d -= dt.timedelta(days=1)
    # keep chronological
    if os.path.exists(fa.METRICS_PATH):
        log = pd.read_csv(fa.METRICS_PATH).sort_values(["date", "sym", "dte"])
        log.to_csv(fa.METRICS_PATH, index=False)
    print(f"[done] {got} sessions of FO metrics")

if __name__ == "__main__":
    main()

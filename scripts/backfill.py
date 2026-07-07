"""One-time backfill of historical bhavcopies.

    python scripts/backfill.py --days 250

Downloads compacted daily files into data/bhav/. ~150 KB per day stored,
so 250 trading days ~= 40 MB. Polite 1.2 s delay between requests.
Re-runnable: skips days already stored.
"""

import argparse
import datetime as dt
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nse_fetch as nf  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=250,
                    help="trading days of history to fetch (default 250)")
    args = ap.parse_args()

    got, d = 0, dt.date.today()
    tried = 0
    while got < args.days and tried < args.days * 2 + 60:
        if d.weekday() < 5:
            path = os.path.join(nf.BHAV_DIR, f"{d.isoformat()}.csv")
            if os.path.exists(path):
                got += 1
            else:
                if nf.ensure_bhavcopy(d):
                    got += 1
                    time.sleep(1.2)
            tried += 1
        d -= dt.timedelta(days=1)
    print(f"[done] {got} trading days stored in data/bhav/")


if __name__ == "__main__":
    main()

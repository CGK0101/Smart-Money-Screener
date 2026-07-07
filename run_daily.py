"""Daily pipeline: fetch today's bhavcopy + events -> screen -> HTML report.
Run by GitHub Actions every trading evening, or manually: python run_daily.py
"""

import os
import sys
import datetime as dt

import nse_fetch as nf
import screener
import report_gen
import config as C

BASE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(BASE, "docs")


def main():
    today = dt.date.today()

    # 1. today's bhavcopy (skip silently on holidays / weekends)
    if today.weekday() < 5:
        if not nf.ensure_bhavcopy(today):
            print(f"[info] bhavcopy for {today} not available yet "
                  "(holiday or not published). Report will use last session.")
    # opportunistic patch of last few days in case a run was missed
    for back in range(1, 6):
        d = today - dt.timedelta(days=back)
        if d.weekday() < 5:
            nf.ensure_bhavcopy(d)

    # 2. events (graceful failure -> empty frames)
    try:
        events = nf.fetch_all_events(days_back=C.EVENT_LOOKBACK_DAYS)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] event fetch failed entirely: {e}")
        events = nf.load_events()

    # 3. screen
    history = nf.load_history()
    if history.empty:
        sys.exit("No bhavcopy history stored. Run scripts/backfill.py first.")
    res = screener.run_screen(history, events)

    # 4. report (latest + dated archive)
    os.makedirs(os.path.join(DOCS, "archive"), exist_ok=True)
    html = report_gen.render(res)
    with open(os.path.join(DOCS, "index.html"), "w") as f:
        f.write(html)
    stamp = res["latest"].strftime("%Y-%m-%d")
    with open(os.path.join(DOCS, "archive", f"{stamp}.html"), "w") as f:
        f.write(html)
    print(f"[done] report for {stamp}: Grade A={len(res['grade_a'])}, "
          f"Grade B={len(res['grade_b'])}")


if __name__ == "__main__":
    main()

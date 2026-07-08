"""Daily pipeline: fetch -> screen -> analytics -> HTML report -> Telegram.
Run by GitHub Actions every trading evening, or manually: python run_daily.py
"""

import os
import sys
import datetime as dt

import requests

import nse_fetch as nf
import screener
import report_gen
import analytics
import config as C

BASE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(BASE, "docs")


def send_telegram(res):
    """Optional nightly alert. Activates when TELEGRAM_BOT_TOKEN and
    TELEGRAM_CHAT_ID are set as GitHub Actions secrets."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return
    ga, gb = res["grade_a"], res["grade_b"]
    acts = [(s, int(r["score"])) for s, r in
            __import__("pandas").concat([ga, gb]).iterrows()
            if r["action"] == "ACT"]
    lines = [f"Smart Money Screener - {res['latest'].strftime('%d %b %Y')}",
             f"ACT: {len(acts)} | Grade A: {len(ga)} | Grade B: {len(gb)}"]
    lines += [f"  {s} ({sc})" for s, sc in acts[:10]]
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        lines.append(f"https://{owner.lower()}.github.io/{name}/")
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": "\n".join(lines)},
                      timeout=20)
        print("[info] telegram alert sent")
    except requests.RequestException as e:
        print(f"[warn] telegram: {e}")


def main():
    today = dt.date.today()

    # 1. bhavcopies (today + patch any recently missed days)
    if today.weekday() < 5:
        if not nf.ensure_bhavcopy(today):
            print(f"[info] bhavcopy for {today} not available yet "
                  "(holiday or not published). Report will use last session.")
    for back in range(1, 6):
        d = today - dt.timedelta(days=back)
        if d.weekday() < 5:
            nf.ensure_bhavcopy(d)

    # 2. events + F&O OI + sector map (all fail gracefully)
    try:
        events = nf.fetch_all_events(days_back=C.EVENT_LOOKBACK_DAYS)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] event fetch failed entirely: {e}")
        events = nf.load_events()

    history = nf.load_history()
    if history.empty:
        sys.exit("No bhavcopy history stored. Run scripts/backfill.py first.")
    last_date = dt.date.fromisoformat(str(history["DATE"].max())[:10])

    fo_oi = nf.fetch_fo_oi(last_date)
    sector_map = nf.fetch_sector_map()
    equity_syms = nf.fetch_equity_symbols()

    # 3. screen
    res = screener.run_screen(history, events, fo_oi=fo_oi,
                              equity_syms=equity_syms)

    # 4. analytics: signals log, persistence, scorecard, sectors, holdings
    log = analytics.append_signals(res)
    counts = analytics.persistence_counts(log)
    for frame in (res["grade_a"], res["grade_b"]):
        if len(frame):
            frame["seen"] = [max(1, counts.get(s, 1)) for s in frame.index]
    res["scorecard"] = analytics.scorecard(log, history)
    res["sectors"] = analytics.sector_clusters(res, sector_map)
    res["holdings"] = analytics.load_holdings()
    res["distribution"] = analytics.distribution_watch(history,
                                                       res["holdings"])

    # 5. report (latest + dated archive)
    os.makedirs(os.path.join(DOCS, "archive"), exist_ok=True)
    html = report_gen.render(res)
    with open(os.path.join(DOCS, "index.html"), "w") as f:
        f.write(html)
    stamp = res["latest"].strftime("%Y-%m-%d")
    with open(os.path.join(DOCS, "archive", f"{stamp}.html"), "w") as f:
        f.write(html)
    print(f"[done] report for {stamp}: Grade A={len(res['grade_a'])}, "
          f"Grade B={len(res['grade_b'])}, "
          f"stealth={len(res.get('stealth', []))}")

    # 6. optional Telegram alert
    send_telegram(res)


if __name__ == "__main__":
    main()

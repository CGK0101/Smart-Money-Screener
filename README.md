# Smart Money Screener (NSE)

Automated daily report that detects institutional/insider accumulation
footprints in NSE stocks using price + volume + delivery data, price-structure
context, and corporate disclosures (bulk/block deals, promoter buying, pledges).
Replaces the manual Excel bhavcopy tracking workflow.

## The logic (3 layers)

**Layer 1 — Accumulation gate** (EQ series only, ≥₹50L avg daily turnover)
- **Grade A**: price, volume AND delivery % each rising 3 consecutive sessions,
  with delivery *quantity* also rising and above its 20-day average.
- **Grade B**: price up 2-of-3 (net positive, no close below prior low),
  3-day volume > 1.5x normal with ≥2 days above average, delivery % rising
  3 days, delivery quantity above average.

**Layer 2 — Structure tag**: Base accumulation (near 52w low / 200DMA reclaim)
· Continuation (uptrend) · Extended (3-day move >12% or >25% above 50DMA —
you're likely late) · Neutral.

**Layer 3 — Event overlay** (last 7 days): promoter buys/sells (PIT),
block deals, bulk deals + circular-trading detection, pledge creation/release.

**Composite Smart Money Score (0–100)**: delivery surge 30 + volume 20 +
structure 20 + events 30. Promoter selling / pledge creation / circular bulk
deals set a red flag — skip those regardless of score.

All thresholds live in `config.py`.

## Setup (one time, ~15 minutes, browser only — no coding tools needed)

1. Create a free account at github.com. Create a **new repository**, name
   it `smart-money-screener`, set it to **Public** (required for the free
   website hosting; it contains only public NSE data and generic code).
   Tick "Add a README" so the repo isn't empty.
2. Unzip the package on your computer. In the repo click
   **Add file → Upload files** and drag EVERYTHING from inside the unzipped
   folder (including the `.github` folder — enable "show hidden files" if
   you don't see it). Commit.
3. **Settings → Actions → General → Workflow permissions → select
   "Read and write permissions" → Save.**
4. **Actions tab → "Daily Smart Money Report" → Run workflow.** The first
   run downloads 250 days of NSE history automatically (~10 minutes, green
   tick when done). Every run after this takes under a minute.
5. **Settings → Pages → Source: Deploy from a branch → Branch: `main`,
   folder: `/docs` → Save.** After a minute your report is live at
   `https://<your-username>.github.io/smart-money-screener/`
   — bookmark it on your phone.

From then on it refreshes itself every trading day at ~7:15 PM IST
(with an 8:30 PM retry). Dated archives at `/archive/YYYY-MM-DD.html`.
To force a refresh anytime: Actions tab → Run workflow.

## Daily routine (5 minutes)

1. Open the report. Look at Grade A sorted by score.
2. Ignore anything red-flagged or tagged Extended.
3. Pick the 2–3 highest-score names in **Base accumulation** or
   **Continuation** → run your usual chart + fundamentals check.
4. Skim the market-wide events section for promoter buying in stocks the
   screen hasn't caught yet — occasionally the disclosure leads the
   delivery data by a week.

## Known limitations / maintenance

- **NSE changes its APIs.** The bhavcopy archive URL is very stable; the
  event APIs (bulk/block/PIT/pledge) occasionally change shape. The code
  fails gracefully — the report still generates, with events marked
  unavailable. If events stay empty for several days, the endpoints in
  `nse_fetch.py` need a refresh.
- FII/DII stock-level buying is only public quarterly; delivery + block
  deals is the best daily proxy that legally exists.
- Delivery data reflects T-day settlement obligations, not intent —
  expect ~1 real signal per 5 shortlisted even in good markets. The edge
  is systematic early detection, not certainty.
- Not investment advice. The manual conviction gate stays yours.

## Files

```
config.py              all tunable thresholds
nse_fetch.py           NSE downloaders (bhavcopy + events)
screener.py            3-layer logic + scoring
report_gen.py          HTML report
run_daily.py           daily orchestrator
scripts/backfill.py    one-time history builder
scripts/test_synthetic.py  logic validation (run after any config change)
.github/workflows/daily.yml  the 7:15 PM IST automation
docs/                  published reports (GitHub Pages)
```

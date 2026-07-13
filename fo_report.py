"""Render the F&O strategy report to docs/fo.html."""

import html
import pandas as pd
from report_gen import CSS, JS  # shared styling


def _vclass(call: str) -> str:
    if call.startswith("GREEN"):
        return "a-act"
    if call.startswith("AMBER"):
        return "a-watch"
    return "a-ign"


def _checklist(checks: dict) -> str:
    items = []
    for name, ok in checks.items():
        mark = "✓" if ok else "✗"
        cls = "pos" if ok else "neg"
        items.append(f'<div><span class="{cls}">{mark}</span> '
                     f'<span class="sub">{html.escape(name)}</span></div>')
    return "".join(items)


def _blueprint_box(bp) -> str:
    if not bp:
        return ""
    return f"""<div style="background:#0d2818;border:1px solid #1e5c38;
border-radius:10px;padding:12px 14px;margin:10px 0">
<div style="font-weight:800;color:#86efac;margin-bottom:4px">
🎯 Strategy blueprint: {html.escape(bp['name'])}</div>
<div><b>{html.escape(bp['legs'])}</b></div>
<div class="sub" style="margin:4px 0">{html.escape(bp['cost'])} ·
{html.escape(bp['risk'])}</div>
<div class="sub">{html.escape(bp['zone'])}</div>
<div class="sub" style="margin-top:6px;border-top:1px solid #1e5c38;
padding-top:6px"><b>Management:</b> {html.escape(bp['mgmt'])}</div>
<div class="sub" style="margin-top:4px;font-style:italic">Prices are EOD
closes - re-quote live before entry. Strikes and thesis are the signal;
your fill and size are yours.</div></div>"""


def _board(b: dict) -> str:
    m, v = b["metrics"], b["verdict"]
    ivr = "—" if pd.isna(v["ivr"]) else f"{v['ivr']:.0f}"
    rv = "—" if pd.isna(v["rv"]) else f"{v['rv']:.1f}%"
    trend = {1: "Up (vs 21D)", -1: "Down (vs 21D)"}.get(v["trend"], "—")
    em = "—" if pd.isna(m["em_pct"]) else \
        f"±{m['straddle']:,.0f} pts (±{m['em_pct']:.1f}%)"
    radar = ""
    if b.get("radar"):
        r = b["radar"]
        sc = r.get("straddle_chg_pct")
        radar = f"""<div class="note"><b>⏱ Expiry radar (DTE {m['dte']}):</b>
spot is {r['dist_to_pain_pct']:+.2f}% from max pain {m['max_pain']:,.0f}
{f" · straddle {sc:+.1f}% vs prev session" if sc is not None else ""}.
Pin-risk territory - expiry trades only, defined risk, half size.</div>"""
    return f"""<div style="background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:14px 16px;margin:14px 0">
<div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px">
<div><span class="sym" style="font-size:18px">{html.escape(m['sym'])}</span>
<span class="sub"> · expiry {m['exp']} (DTE {m['dte']})</span></div>
<span class="act {_vclass(v['call'])}" style="font-size:13px">
{html.escape(v['call'])}</span></div>
<div class="sub" style="margin:6px 0 10px">{html.escape(v['family'])}</div>
<div class="kpis">
<div class="kpi"><b>{m['spot']:,.0f}</b><span>Spot (derived)</span></div>
<div class="kpi"><b>{em}</b><span>Expected move to expiry</span></div>
<div class="kpi"><b>{'—' if pd.isna(m['iv']) else f"{m['iv']:.1f}%"}</b>
<span>ATM IV</span></div>
<div class="kpi"><b>{ivr}</b><span>IV rank</span></div>
<div class="kpi"><b>{rv}</b><span>Realized vol 5D</span></div>
<div class="kpi"><b>{'—' if pd.isna(m['pcr']) else m['pcr']}</b>
<span>PCR (OI)</span></div>
<div class="kpi"><b>{trend}</b><span>Trend</span></div>
<div class="kpi"><b>{m['max_pain']:,.0f}</b><span>Max pain</span></div>
</div>
<div style="display:flex;gap:24px;flex-wrap:wrap;margin:8px 0">
<div><span class="sub">Put walls (support):</span>
<b class="pos">{html.escape(m['pw'].replace(';', ' · '))}</b></div>
<div><span class="sub">Call walls (resistance):</span>
<b class="neg">{html.escape(m['cw'].replace(';', ' · '))}</b></div>
<div><span class="sub">Fut OI chg:</span>
<b>{'—' if pd.isna(m['fut_oi_chg']) else f"{m['fut_oi_chg']:,.0f}"}</b></div>
</div>
{radar}
{_blueprint_box(b.get('blueprint'))}
<details><summary class="sub" style="cursor:pointer">Verdict checklist
</summary><div style="columns:2;margin-top:6px">{_checklist(v['checks'])}
</div></details>
</div>"""


def _bridge_table(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return ('<div class="empty">None of today\'s equity ACT/WATCH names '
                'are in the F&amp;O segment, or FO data was unavailable.</div>')
    rows = []
    for sym, r in df.iterrows():
        liq_cls = {"A": "pos", "B": "", "C": "neg"}[r["liq"]]
        rows.append(f"""<tr><td class="sym">{html.escape(str(sym))}</td>
<td data-v="{r['fut_oi_chg_pct']}"
class="{'pos' if r['fut_oi_chg_pct']>=0 else 'neg'}">
{r['fut_oi_chg_pct']:+.1f}%</td>
<td data-v="{r['opt_val_cr']}">₹{r['opt_val_cr']:,.0f} cr</td>
<td class="{liq_cls}"><b>{r['liq']}</b></td>
<td>{html.escape(str(r.get('structure','')))}</td>
<td class="sub">{html.escape(str(r.get('cost','')))}</td>
<td class="sub">{html.escape(str(r.get('invalidation','')))}</td>
</tr>""")
    return f"""<div class="scrollx"><table><thead><tr><th>Equity signal</th>
<th>Fut OI chg</th><th>Opt value</th><th>Liq</th><th>Suggested structure</th>
<th>Cost / reward (EOD)</th><th>Invalidation</th></tr>
</thead><tbody>{''.join(rows)}</tbody></table></div>"""


def render_fo(fo: dict) -> str:
    d = fo["session"].strftime("%d %b %Y (%A)")
    boards = "".join(_board(b) for b in fo["boards"])
    hist = " · ".join(f"{k}: {v} sessions" for k, v in fo["hist_days"].items())
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>F&O Strategy Board — {d}</title><style>{CSS}</style></head><body>
<div class="wrap">
<div class="sub"><a class="lnk" href="index.html">← Equity Smart Money
Screener</a></div>
<h1>F&amp;O Strategy Board</h1>
<div class="sub">Session: <b>{d}</b> · IV history: {hist}</div>
<div class="note"><b>Default state is STAND ASIDE.</b> A green light means
every condition on the checklist passed together — high conviction through
confluence, never certainty. No signal survives being oversized: risk a
fixed small % per structure, always defined-risk, and skip any day that
isn't green. The trades you don't take are where this page earns its keep.
</div>
{boards}
<h2>Stock options bridge — today's equity signals with F&amp;O</h2>
<div class="sub" style="margin-bottom:8px">Delivery accumulation (leg 1) +
futures OI buildup + liquid options = candidate for a defined-risk spread
instead of cash. Grade C liquidity = slippage eats the edge.</div>
{_bridge_table(fo.get('bridge'))}
<div class="sub" style="margin-top:22px">EOD analysis from the NSE F&amp;O
bhavcopy. Educational tool — not investment advice. Strikes, sizing and
final judgment remain yours.</div>
</div><script>{JS}</script></body></html>"""

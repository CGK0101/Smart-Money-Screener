"""Generate the daily HTML report (mobile-friendly, zero dependencies)."""

import html
import pandas as pd
import config as C

CSS = """
:root{--bg:#0f1420;--card:#182032;--txt:#e8ecf4;--mut:#8b96ad;--line:#26304a;
--grn:#22c55e;--red:#ef4444;--amb:#f59e0b;--blu:#3b82f6}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;padding:16px}
.wrap{max-width:1100px;margin:0 auto}h1{font-size:22px;margin:4px 0}
h2{font-size:17px;margin:26px 0 8px;border-bottom:1px solid var(--line);
padding-bottom:6px}.sub{color:var(--mut);font-size:13px}
.kpis{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:10px 14px;min-width:120px}.kpi b{font-size:20px;display:block}
.kpi span{color:var(--mut);font-size:12px}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13px}
th{background:#111827;color:var(--mut);text-align:left;padding:8px 9px;
font-weight:600;white-space:nowrap;cursor:pointer}
td{padding:7px 9px;border-top:1px solid var(--line);white-space:nowrap}
tr:hover td{background:#1d2740}.sym{font-weight:700;color:#fff}
.pos{color:var(--grn)}.neg{color:var(--red)}
.badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:12px;
font-weight:700}.s-hi{background:#14532d;color:#86efac}
.s-md{background:#3f3413;color:#fde68a}.s-lo{background:#3b1d1d;color:#fca5a5}
.tag{display:inline-block;padding:1px 7px;border-radius:6px;font-size:11px;
margin-right:4px}.t-base{background:#0c4a6e;color:#7dd3fc}
.t-cont{background:#1e3a5f;color:#93c5fd}.t-ext{background:#450a0a;color:#fca5a5}
.t-neu{background:#27303f;color:#9ca3af}
.ev{font-size:11px;padding:1px 6px;border-radius:6px;margin-right:3px}
.ev-g{background:#14532d;color:#86efac}.ev-r{background:#450a0a;color:#fca5a5}
.ev-b{background:#1e3a8a;color:#bfdbfe}
.act{display:inline-block;padding:2px 9px;border-radius:6px;font-size:11px;
font-weight:800;letter-spacing:.4px}
.a-act{background:#166534;color:#bbf7d0}.a-watch{background:#78350f;color:#fde68a}
.a-late{background:#7c2d12;color:#fdba74}.a-ign{background:#27303f;color:#8b96ad}
a.lnk{color:#7db3f5;text-decoration:none;font-size:12px}
a.lnk:hover{text-decoration:underline}
.note{background:#1c2436;border-left:3px solid var(--amb);padding:10px 12px;
border-radius:6px;font-size:13px;color:var(--mut);margin:10px 0}
.empty{color:var(--mut);padding:14px;background:var(--card);
border:1px dashed var(--line);border-radius:10px}
.scrollx{overflow-x:auto}
"""

JS = """
document.querySelectorAll('th').forEach(th=>th.addEventListener('click',()=>{
const t=th.closest('table'),i=[...th.parentNode.children].indexOf(th),
r=[...t.querySelectorAll('tbody tr')],asc=th.dataset.a!=='1';
r.sort((a,b)=>{const x=a.children[i].dataset.v??a.children[i].innerText,
y=b.children[i].dataset.v??b.children[i].innerText,
nx=parseFloat(x),ny=parseFloat(y);
return(!isNaN(nx)&&!isNaN(ny)?nx-ny:x.localeCompare(y))*(asc?1:-1)});
r.forEach(x=>t.querySelector('tbody').appendChild(x));th.dataset.a=asc?'1':'0';}));
"""


def _score_badge(s):
    cls = "s-hi" if s >= 65 else ("s-md" if s >= 40 else "s-lo")
    return f'<span class="badge {cls}">{s}</span>'


def _struct_tag(s):
    m = {"Base accumulation": "t-base", "Continuation": "t-cont",
         "Extended": "t-ext", "Neutral": "t-neu"}
    return f'<span class="tag {m.get(s,"t-neu")}">{html.escape(s)}</span>'


def _events_cell(r):
    bits = []
    if r["promoter_buy"]:
        bits.append('<span class="ev ev-g">Promoter BUY</span>')
    if r["block_deal"]:
        bits.append('<span class="ev ev-b">Block</span>')
    if r["bulk_buy"]:
        bits.append('<span class="ev ev-b">Bulk buy</span>')
    if r["pledge_release"]:
        bits.append('<span class="ev ev-g">Pledge ↓</span>')
    if r["promoter_sell"]:
        bits.append('<span class="ev ev-r">Promoter SELL</span>')
    if r["pledge_creation"]:
        bits.append('<span class="ev ev-r">Pledge ↑</span>')
    if r["bulk_circular"]:
        bits.append('<span class="ev ev-r">Circular</span>')
    return "".join(bits) or '<span class="sub">—</span>'


def _action_badge(a):
    m = {"ACT": "a-act", "WATCH": "a-watch", "LATE": "a-late",
         "IGNORE": "a-ign"}
    return f'<span class="act {m.get(a,"a-ign")}">{html.escape(str(a))}</span>'


def _links(sym):
    s = html.escape(str(sym))
    return (f'<a class="lnk" target="_blank" '
            f'href="https://www.screener.in/company/{s}/">Fund</a> · '
            f'<a class="lnk" target="_blank" '
            f'href="https://in.tradingview.com/chart/?symbol=NSE%3A{s}">Chart</a>')


def _table(df: pd.DataFrame) -> str:
    if df.empty:
        return '<div class="empty">No stocks passed this screen today.</div>'
    rows = []
    for sym, r in df.head(C.TOP_N_DISPLAY).iterrows():
        chg = r["chg3d_pct"]
        d21 = r["vs_21dma_pct"]
        d21s = "—" if pd.isna(d21) else f"{d21:+.1f}%"
        rows.append(f"""<tr>
<td class="sym">{html.escape(str(sym))}</td>
<td data-v="{'3' if r['action']=='ACT' else '2' if r['action']=='WATCH'
    else '1' if r['action']=='LATE' else '0'}">{_action_badge(r['action'])}</td>
<td data-v="{r['score']}">{_score_badge(r['score'])}</td>
<td data-v="{r['close']}">₹{r['close']:,.2f}</td>
<td data-v="{chg:.2f}" class="{'pos' if chg>=0 else 'neg'}">{chg:+.1f}%</td>
<td data-v="{r['vol_x_20d']}">{r['vol_x_20d']}x</td>
<td data-v="{r['dq_x_20d']}">{r['dq_x_20d']}x</td>
<td data-v="{r['deliv_pct']}">{r['deliv_pct_prev']:.0f}→{r['deliv_pct']:.0f}%</td>
<td data-v="{-999 if pd.isna(d21) else d21}"
 class="{'pos' if not pd.isna(d21) and d21>=0 else 'neg'}">{d21s}</td>
<td>{_struct_tag(r['structure'])}</td>
<td class="sub">{html.escape(str(r.get('tech_posture','')))}</td>
<td data-v="{r['from_52w_high_pct']}">{r['from_52w_high_pct']:.0f}%</td>
<td>{_events_cell(r)}</td>
<td>{_links(sym)}</td></tr>""")
    return f"""<div class="scrollx"><table><thead><tr>
<th>Symbol</th><th>Action</th><th>Score</th><th>Close</th><th>3D chg</th>
<th>Vol vs 20D</th><th>DelQty vs 20D</th><th>Del% 3D</th><th>vs 21DMA</th>
<th>Structure</th><th>Trend</th><th>vs 52wH</th><th>Events (7d)</th>
<th>Check</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"""


def _events_section(flags: pd.DataFrame) -> str:
    if flags.empty:
        return ('<div class="empty">Event data unavailable today (NSE API '
                'unreachable or no disclosures). Screen results above are '
                'unaffected.</div>')
    keep = flags[flags[["promoter_buy", "promoter_sell", "pledge_creation",
                        "pledge_release", "block_deal"]].any(axis=1)]
    if keep.empty:
        return '<div class="empty">No notable disclosures in the window.</div>'
    rows = []
    for _, r in keep.iterrows():
        rows.append(f"""<tr><td class="sym">{html.escape(str(r['SYMBOL']))}</td>
<td>{_events_cell(r)}</td>
<td class="sub">{html.escape(str(r.get('notes','')))}</td></tr>""")
    return f"""<div class="scrollx"><table><thead><tr><th>Symbol</th>
<th>Flags</th><th>Detail</th></tr></thead><tbody>{''.join(rows)}</tbody>
</table></div>"""


def render(res: dict) -> str:
    d = res["latest"].strftime("%d %b %Y (%A)")
    ga, gb = res["grade_a"], res["grade_b"]
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smart Money Screener — {d}</title><style>{CSS}</style></head><body>
<div class="wrap">
<h1>Smart Money Screener</h1>
<div class="sub">Session: <b>{d}</b> · History: {res['n_days']} trading days
· Liquid EQ universe: {res['universe']} stocks</div>
<div class="kpis">
<div class="kpi"><b>{int((ga['action']=='ACT').sum()
    + (gb['action']=='ACT').sum())}</b><span>ACT tonight</span></div>
<div class="kpi"><b>{len(ga)}</b><span>Grade A signals</span></div>
<div class="kpi"><b>{len(gb)}</b><span>Grade B signals</span></div>
<div class="kpi"><b>{int((ga['structure']=='Base accumulation').sum()
    + (gb['structure']=='Base accumulation').sum())}</b>
<span>In base zone</span></div>
<div class="kpi"><b>{int(ga['red_flag'].sum()+gb['red_flag'].sum())}</b>
<span>Red-flagged</span></div></div>

<h2>Grade A — price, volume &amp; delivery all rising 3 days</h2>
{_table(ga)}
<h2>Grade B — accumulation pattern, volume 2-of-3 but expanded</h2>
{_table(gb)}
<div class="note"><b>How to read:</b>
<span class="act a-act">ACT</span> = strong footprint (score ≥55) trading
above its 21 DMA — run your chart + fundamentals check tonight (use the
Fund/Chart links). <span class="act a-watch">WATCH</span> = real signal,
not compelling yet or below 21 DMA. <span class="act a-late">LATE</span> =
Extended, the move likely already happened. <span class="act a-ign">IGNORE
</span> = red-flagged or weak. ACT is a triage label, not a buy call —
your conviction gate stays manual. Red events (promoter sell, pledge ↑,
circular bulk) override everything.</div>

<h2>Market-wide smart money events (last {C.EVENT_LOOKBACK_DAYS} days)</h2>
{_events_section(res.get('event_flags', pd.DataFrame()))}

<div class="sub" style="margin-top:22px">Data: NSE bhavcopy &amp; corporate
disclosures. Educational tool — not investment advice. Click any column
header to sort.</div>
</div><script>{JS}</script></body></html>"""

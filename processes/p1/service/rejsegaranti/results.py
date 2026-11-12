"""Read-only results page (GET /results on the bridge): claim outcomes so far, as HTML, and the data behind it
as downloads (GET /results/<file>, see DOWNLOADS).

This is the page shared publicly through Tailscale Funnel, so it only reads the registry and Camunda's history,
and neither the page nor the downloads carry passenger IDs or coordinates.
"""
from __future__ import annotations

import csv
import io
import os
import tempfile
import re
from collections import Counter
from functools import lru_cache
from html import escape
from typing import Optional, Tuple

from . import perline, xes
from .stations import LINES
from .store import Store

STYLE = """
:root { --bg: #fafaf9; --fg: #1c1917; --muted: #78716c; --line: #e7e5e4; --card: #fff;
        --paid: #15803d; --rejected: #b91c1c; --warn-bg: #fef3c7; --warn-fg: #78350f; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #1c1917; --fg: #f5f5f4; --muted: #a8a29e; --line: #44403c; --card: #292524;
          --paid: #4ade80; --rejected: #f87171; --warn-bg: #422006; --warn-fg: #fde68a; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.5 system-ui, sans-serif; }
main { max-width: 1000px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 1.5rem; margin: 0 0 4px; }
header { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: flex-start; gap: 8px 16px; }
.login { border: 1px solid var(--line); background: var(--card); border-radius: 8px; padding: 6px 12px;
         text-decoration: none; font-weight: 600; white-space: nowrap; }
.login:hover { border-color: var(--muted); }
h2 { font-size: 1.1rem; margin: 32px 0 8px; }
p { margin: 4px 0; }
.muted { color: var(--muted); }
.warn { background: var(--warn-bg); color: var(--warn-fg); padding: 10px 14px; border-radius: 8px; margin: 16px 0; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin: 16px 0; }
.tile { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; }
.tile b { display: block; font-size: 1.6rem; font-variant-numeric: tabular-nums; }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; background: var(--card); font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--line); white-space: nowrap; }
th { color: var(--muted); font-weight: 600; }
td.num, th.num { text-align: right; }
.paid { color: var(--paid); font-weight: 600; }
.rejected { color: var(--rejected); font-weight: 600; }
a { color: inherit; }
.signup { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 16px 0; }
.signup input { flex: 1 1 220px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px;
  background: var(--card); color: var(--fg); font: inherit; }
.signup button { padding: 8px 14px; border: 1px solid var(--fg); border-radius: 8px; background: var(--fg);
  color: var(--bg); font: inherit; font-weight: 600; cursor: pointer; }
.signup label { width: 100%; font-weight: 600; }
.downloads { display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0; }
.downloads a { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 8px 12px;
  text-decoration: none; font-weight: 600; }
.downloads a:hover, .downloads a:focus-visible { border-color: var(--fg); }
"""


DOWNLOADS = {  # file -> label on the page
    "claims.csv": "Claims and outcomes (CSV)",
    "per-line.csv": "Delays per line (CSV)",
    "claims.xes": "Claim event log (XES)",
    "delay-cases.xes": "Delay-case event log (XES)",
}
CLAIM_FIELDS = ["claimId", "delayId", "line", "tripStart", "transportType", "amountDkk", "outcome", "payoutDkk",
                "reason"]


def download(name: str, store: Store, camunda=None) -> Optional[Tuple[bytes, str]]:
    """(content, media type) of one of DOWNLOADS, or None if there is no such file (or no Camunda for a log).
    The event logs are exported from Camunda's history on each request, as `rejsegaranti export` does."""
    if name == "claims.csv":
        delays = {d["delayId"]: d for d in store.all("delays")}
        outcomes = {o["claimId"]: o for o in store.all("outcomes")}
        buf = io.StringIO()
        w = csv.DictWriter(buf, CLAIM_FIELDS, lineterminator="\n")
        w.writeheader()
        for c in sorted(store.all("claims"), key=lambda c: c.get("tripStart", "")):
            o = outcomes.get(c["claimId"], {})
            r = c.get("receipt") or {}
            w.writerow({"claimId": c["claimId"], "delayId": o.get("delayId"),
                        "line": delays.get(o.get("delayId"), {}).get("line"), "tripStart": c.get("tripStart"),
                        "transportType": r.get("transportType"), "amountDkk": r.get("amountDkk"),
                        "outcome": o.get("outcome", "in progress"), "payoutDkk": o.get("payoutDkk"),
                        "reason": o.get("reason")})
        return buf.getvalue().encode(), "text/csv; charset=utf-8"
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, name)
        if name == "per-line.csv":
            state = {k: store.all(k) for k in ("delays", "weather", "outcomes")}
            perline.write_csv(path, perline.per_line(state, _journeys()))
            media = "text/csv; charset=utf-8"
        elif name in ("claims.xes", "delay-cases.xes") and camunda is not None:
            from .dcr import ROLES
            from .export import CLAIM_KEY, DELAY_KEY, traces_from_camunda
            if name == "claims.xes":
                xes.write_xes(path, traces_from_camunda(camunda, CLAIM_KEY), f"{CLAIM_KEY} (Camunda history)",
                              roles=ROLES)
            else:
                xes.write_xes(path, traces_from_camunda(camunda, DELAY_KEY), f"{DELAY_KEY} (Camunda history)")
            media = "application/xml"
        else:
            return None
        with open(path, "rb") as fh:
            return fh.read(), media


@lru_cache(maxsize=1)
def _journeys():
    return perline.load_journeys()


# Reload every minute, but not while someone is typing an address into the sign-up form
RELOAD = """<script>setInterval(() => { const e = document.getElementById("email");
  if (!e || (!e.value && document.activeElement !== e)) location.reload(); }, 60000);</script>
<noscript><meta http-equiv="refresh" content="60"></noscript>
"""


def _page(title: str, body: str, refresh: bool = False) -> str:
    meta = RELOAD if refresh else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{meta}<title>{escape(title)}</title><style>{STYLE}</style></head>
<body><main>
{body}
</main></body></html>"""


def message(title: str, text: str, form: Optional[Tuple[str, str, str]] = None) -> str:
    """A small page for the mailing list. form: (action, token, button label) of a POST button."""
    button = ""
    if form:
        action, token, label = form
        button = (f'<form class="signup" method="post" action="{escape(action)}">'
                  f'<input type="hidden" name="token" value="{escape(token)}">'
                  f'<button type="submit">{escape(label)}</button></form>')
    # relative link back: the page is "/" publicly and "/results" locally
    return _page(title, f"<h1>{escape(title)}</h1><p>{escape(text)}</p>{button}"
                        '<p><a href="to-be">Back to the results</a></p>')


def _scope(store: Store) -> Tuple[str, str]:
    """(the lines the process runs on, the banner saying which data is real)"""
    delays = store.all("delays")
    real = sum(d.get("source") == "Dataudveksleren" for d in delays)
    if LINES == {"L"}:
        line = "Line L"
    elif len(LINES) == 1:
        line = f"line {next(iter(LINES))}"
    else:
        line = f"Copenhagen's buses and S-trains ({len(LINES)} lines)"
    if not delays:
        banner = (f"<b>No delays yet.</b> A case starts when a delay of 30+ minutes on {line} is reported. "
                  "The claims on this page are always generated by the project's simulator.")
    elif real == len(delays):
        banner = (f"<b>Real delays, simulated claims.</b> The delays are real: Movia's and DSB's real-time data for {line} "
                  "(via Dataudveksleren), standing in for Line L, for which no real-time data is published. "
                  "The claims are generated by the project's simulator; none comes from a real passenger.")
    else:
        banner = (f"<b>Simulated data.</b> {len(delays) - real} of the {len(delays)} delays and every claim here "
                  "are generated by the project's simulator. They do not describe real delays or real passengers.")
    return line, banner


def _readable(reason: str) -> str:
    """A reason as shown on the page: distances in km from 1,000 m ("8296 m" -> "8.3 km"); the data keeps metres."""
    return re.sub(r"\b(\d+) m\b", lambda m: f"{int(m[1]) / 1000:.1f} km" if int(m[1]) >= 1000 else m[0], reason)


def _claim_rows(rows: list, claims: dict, limit: int, amount_key: str) -> str:
    out = []
    for o in rows[:limit]:
        c = claims.get(o["claimId"], {})
        r = c.get("receipt") or {}
        out.append(
            f"<tr><td>{escape(o['claimId'])}</td><td>{escape(c.get('tripStart', '')[:16].replace('T', ' '))}</td>"
            f"<td>{escape(r.get('transportType', ''))}</td>"
            f"<td class=num>{float(r.get('amountDkk') or 0):.2f}</td>"
            f"<td class={escape(o['outcome'])}>{escape(o['outcome'])}</td>"
            f"<td class=num>{float(o.get(amount_key) or 0):.2f}</td>"
            f"<td>{escape(_readable(o.get('reason') or ''))}</td></tr>")
    return "".join(out)


def render(store: Store, limit: int = 100, signup: bool = False) -> str:
    """The TO-BE page: the outcomes the running process decided."""
    claims = {c["claimId"]: c for c in store.all("claims")}
    outcomes = store.all("outcomes")
    by_outcome = Counter(o["outcome"] for o in outcomes)
    reasons = Counter(o.get("reason") or "-" for o in outcomes if o["outcome"] != "paid")
    payout = sum(float(o.get("payoutDkk") or 0) for o in outcomes)
    delays = store.all("delays")
    line, banner = _scope(store)
    open_claims = len(claims) - len(outcomes)

    rows = sorted(outcomes, key=lambda o: claims.get(o["claimId"], {}).get("tripStart", ""), reverse=True)
    trs = _claim_rows(rows, claims, limit, "payoutDkk")
    per_line = ""
    if LINES != {"L"}:
        state = {k: store.all(k) for k in ("delays", "weather", "outcomes")}
        lrows = [r for r in perline.per_line(state, _journeys()) if r["cases"] or r["line"] == "total"]
        cells = "".join(
            f"<tr><td>{escape(r['line'])}</td><td class=num>{r['journeys']:,}</td><td class=num>{r['cases']}</td>"
            f"<td class=num>{r['casesPer1000']:.2f}</td><td class=num>{r['low']:.2f} - {r['high']:.2f}</td>"
            f"<td class=num>{r['claims']}</td><td class=num>{r['paid']}</td></tr>" for r in lrows)
        per_line = f"""<h2>Delays per line</h2>
<p class="muted">Delay cases per 1,000 scheduled journeys while the bridge was receiving data, so busy and quiet lines
can be compared. Ranked by the lower end of the 95% interval. Lines without a case are left out but count in the total.</p>
<div class="scroll"><table>
<tr><th>Line</th><th class=num>Journeys</th><th class=num>Cases</th><th class=num>Per 1,000</th>
<th class=num>95% interval</th><th class=num>Claims</th><th class=num>Paid</th></tr>{cells}</table></div>"""
    # relative links: the page is "/" publicly and "/results" locally, and the bridge serves both paths
    links = "".join(f'<a href="{f}" download>{escape(label)}</a>' for f, label in DOWNLOADS.items())
    signup_form = "" if not signup else """<h2>Get an email for every claim</h2>
<p class="muted">One email per decided claim, roughly 50 a day once delays come in. You confirm by email first,
and every email has an unsubscribe link. Your address is not shown anywhere.</p>
<form class="signup" method="post" action="subscribe">
<label for="email">Email address</label>
<input id="email" name="email" type="email" required maxlength="254" autocomplete="email" placeholder="you@example.com">
<button type="submit">Sign up</button>
</form>"""
    reason_rows = "".join(f"<tr><td>{escape(_readable(k))}</td><td class=num>{n}</td></tr>" for k, n in reasons.most_common())

    return _page("Rejsegaranti results, TO-BE", f"""<header><h1>Rejsegaranti claims, {escape(line)}: TO-BE</h1>
<a class="login" href="/camunda/app/cockpit/" title="Inspect the process instances (login required)">Camunda Cockpit login</a></header>
<p class="muted">Process 1 of the Process-Oriented and Event-Driven Software project (DTU), running on Camunda 7.
Refreshes every minute.</p>
<nav class="downloads" aria-label="Rules"><a href="./">AS-IS rules</a><a href="to-be" aria-current="page">TO-BE rules</a></nav>
<p class="muted">The outcomes the running TO-BE process decided. Its rules are still the first, Line L version
(30+ minutes, 250 m, receipt checks), until the TO-BE is rebuilt on the travel guarantee tables.</p>
<div class="warn">{banner}</div>
<div class="tiles">
  <div class="tile"><b>{len(delays)}</b>delays of 30+ min</div>
  <div class="tile"><b>{len(claims)}</b>claims received</div>
  <div class="tile"><b class="paid">{by_outcome.get('paid', 0)}</b>paid</div>
  <div class="tile"><b class="rejected">{by_outcome.get('rejected', 0)}</b>rejected</div>
  <div class="tile"><b>{open_claims}</b>in progress</div>
  <div class="tile"><b>{payout:,.0f}</b>DKK paid out</div>
</div>
{per_line}
{signup_form}
<h2>Download the data</h2>
<p class="muted">The event logs are exported from Camunda when you click, so they can take a few seconds.</p>
<nav class="downloads" aria-label="Downloads">{links}</nav>
<h2>Why claims were rejected</h2>
<div class="scroll"><table><tr><th>Reason</th><th class=num>Claims</th></tr>{reason_rows}</table></div>
<h2>Latest decided claims</h2>
<p class="muted">The newest {min(limit, len(rows))} of {len(rows)}, by trip start (Copenhagen time).
Claims with an incomplete receipt are refused at the form and never reach the process, so they are not listed.</p>
<div class="scroll"><table>
<tr><th>Claim</th><th>Trip start</th><th>Transport</th><th class=num>Receipt DKK</th><th>Outcome</th>
<th class=num>Paid DKK</th><th>Reason</th></tr>
{trs}
</table></div>
""", refresh=True)


def render_as_is(store: Store, camunda, limit: int = 100) -> str:
    """The main page: the same claims judged by the AS-IS rules, the travel guarantee's four DMN tables."""
    from . import asis
    claims = {c["claimId"]: c for c in store.all("claims")}
    line, banner = _scope(store)
    nav = ('<nav class="downloads" aria-label="Rules"><a href="./" aria-current="page">AS-IS rules</a>'
           '<a href="to-be">TO-BE rules</a></nav>')
    head = f"""<header><h1>Rejsegaranti claims, {escape(line)}: AS-IS</h1>
<a class="login" href="/camunda/app/cockpit/" title="Inspect the decisions (login required)">Camunda Cockpit login</a></header>
<p class="muted">Process 1 of the Process-Oriented and Event-Driven Software project (DTU). Refreshes every minute.</p>
{nav}"""
    try:
        judged = asis.judge(store, camunda) if camunda is not None else None
    except Exception as e:  # Camunda down or restarting
        judged, error = None, e
    else:
        error = None if judged is not None else "no Camunda connection"
    if judged is None:
        return _page("Rejsegaranti results, AS-IS", f"""{head}
<div class="warn"><b>The AS-IS decisions are not available right now</b> ({escape(str(error))}). They are made by
Camunda, which may be restarting. Try again in a minute.</div>""", refresh=True)

    by_outcome = Counter(o["outcome"] for o in judged)
    reasons = Counter(o.get("reason") or "-" for o in judged if o["outcome"] != "paid")
    refunded = sum(o["refundDkk"] for o in judged)
    rows = sorted(judged, key=lambda o: o.get("tripStart", ""), reverse=True)
    reason_rows = "".join(f"<tr><td>{escape(_readable(k))}</td><td class=num>{n}</td></tr>" for k, n in reasons.most_common())
    return _page("Rejsegaranti results, AS-IS", f"""{head}
<p>Every claim judged by the AS-IS rules: Movia's travel guarantee as the four decision tables of the AS-IS model.
Table 1 checks the claim (receipt attached, received within 14 days); Tables 2 to 4 decide whether the guarantee
applies (more than 20 minutes late; no strike, weather, force majeure, Flextrafik or planned change announced
24 hours before); the refund is the taxi fare, at most 300 kr. The tables run on Camunda.</p>
<div class="warn">{banner}</div>
<div class="tiles">
  <div class="tile"><b>{len(claims)}</b>claims received</div>
  <div class="tile"><b class="paid">{by_outcome.get('paid', 0)}</b>covered</div>
  <div class="tile"><b class="rejected">{by_outcome.get('rejected', 0)}</b>not covered</div>
  <div class="tile"><b>{refunded:,.0f}</b>DKK refunded</div>
</div>
<h2>Why claims were not covered</h2>
<div class="scroll"><table><tr><th>Reason</th><th class=num>Claims</th></tr>{reason_rows}</table></div>
<h2>Latest claims</h2>
<p class="muted">The newest {min(limit, len(rows))} of {len(rows)}, by trip start (Copenhagen time). Each claim is
one leg. The delay a claim refers to is the one the running process matched it to. The simulator's taxis, shared
bikes and cars are all treated as the taxi the AS-IS rules expect.</p>
<div class="scroll"><table>
<tr><th>Claim</th><th>Trip start</th><th>Transport</th><th class=num>Receipt DKK</th><th>Outcome</th>
<th class=num>Refund DKK</th><th>Reason</th></tr>
{_claim_rows(rows, claims, limit, "refundDkk")}
</table></div>
""", refresh=True)

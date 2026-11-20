#!/usr/bin/env python3
"""
P3 - run test cases on dcrgraphs.net and save what happened as an event log.

Every case is started as a simulation of your graph on dcrgraphs.net, and every
step is executed through the API (with data where the model needs it). Only steps
that dcrgraphs.net accepted are written to the log. The result is an execution
log: P3_execution_log.xes (for Process DoCtoR) and P3_execution_log.csv.

Times: the steps run seconds apart, so each event also gets a "pretend" date
(days between steps, board meetings 6 times a year). The log uses the pretend
dates, so the 30-day and 90-day rules can be checked. Say this in the report.

Usage (Command Prompt, in the folder with this file):
    python p3_run_cases.py --graph 2016797 --model P3_updated.xml
    python p3_run_cases.py --graph <TO-BE graph id> --model P3_TOBE_dcrgraphs.xml
It asks for your dcrgraphs.net username and password (typing the password shows nothing).
--model must be the dcrgraphs.net XML of the SAME graph (labels and roles are read from it).

Test without the internet (uses a small local DCR engine instead of dcrgraphs.net):
    python p3_run_cases.py --model P3_updated.xml --dry-run

Only the Python standard library is needed.
"""
import argparse, base64, csv, datetime as dt, getpass, json, re, sys, urllib.error, urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import quoteattr

API = "https://repository.dcrgraphs.net/api"

# Data names used by the Process DoCtoR model (the log must use these names).
PD_NAMES = {"Complain": ("onBehalfOfAdult", "boolean"), "SubmitForm": ("amount", "int"),
            "NewDeadline": ("newAmount", "int")}

# ---------------------------------------------------------------- the test cases
# A step is (activity id, days after the previous step, data value or None).
# BOARD = the board decides at the first meeting at least 14 days later.
BOARD = "BOARD"
MEETINGS = [dt.date(2025, m, d) for m, d in ((2, 19), (4, 23), (6, 18), (8, 27), (10, 22), (12, 10))] + \
           [dt.date(2026, m, d) for m, d in ((2, 18), (4, 22), (6, 17), (8, 26), (10, 21), (12, 9))]

S = [("IssueFine", 0, None)]
def complain(gap=5, behalf=False): return [("Complain", gap, behalf)]
def decision(gap=1, amount=1000): return [("NewDeadline", gap, amount)]
REJECT = [("RejectCompl", 16, None)] + decision()
def REDUCE(to, gap=15): return [("ReduceFine", gap, None)] + decision(1, to)
COLLECT = [("SendReminder", 15, None), ("SendReminder", 10, None), ("Transfer", 10, None)]
def appeal(amount=1000, hearings=(10,), behalf=False, gap=14):
    fee = "PayFee160" if amount >= 160 else "PayFee80"
    s = [("SubmitForm", gap, amount), (fee, 2, None)]
    if behalf: s += [("OnBehalf", 0, None), ("PassengerDetails", 0, None)]
    return s + [("CreateCase", 6, None)] + [("Hearing", g, None) for g in hearings]
UPHELD = [("SendToBoard", 20, None), ("Uphold", BOARD, None), ("Refund", 9, None), ("CaseFee", 1, None)]
REJECTED = [("SendToBoard", 22, None), ("RejectAppeal", BOARD, None)] + decision(3)
def act(*names, gap=10): return [(n, gap, None) for n in names]

CASES = [
    ("E01", "Accepted by Movia", S + complain(5) + act("AcceptCompl", gap=18)),
    ("E02", "Accepted by Movia", S + complain(3) + act("AcceptCompl", gap=12)),
    ("E03", "Accepted, complaint for another adult", S + complain(7, True) + act("AttachAuth", gap=6) + act("AcceptCompl", gap=15)),
    ("E04", "Identity misuse, accepted", S + act("ComplainMisuse", gap=10) + act("HandleMisuse", gap=21) + act("AcceptCompl", gap=2)),
    ("E05", "Reduced to 125 kr (commuter card), no appeal", S + complain(4) + REDUCE(125)),
    ("E06", "Reduced to 500 kr, no appeal", S + complain(9) + REDUCE(500, 20)),
    ("E07", "Rejected, no appeal", S + complain(2) + REJECT),
    ("E08", "Rejected, no appeal, sent to Gaeldsstyrelsen", S + complain(6) + REJECT + COLLECT),
    ("E09", "Reminder before the complaint, rejected, no appeal", S + act("SendReminder", gap=16) + complain(3) + REJECT),
    ("E10", "Appeal upheld, Movia complies", S + complain(5) + REJECT + appeal(hearings=(10, 28)) + UPHELD + act("Comply", gap=14)),
    ("E11", "Appeal rejected", S + complain(4) + REJECT + appeal() + REJECTED),
    ("E12", "Reduced, appeal upheld, Movia opts out, forwarded", S + complain(8) + REDUCE(500, 22) + appeal(500) + UPHELD +
        act("OptOut", gap=15) + act("RequestForward", gap=6) + act("Forward", gap=4)),
    ("E13", "Transferred, then appealed; extra information; appeal rejected", S + complain(4) + REJECT + COLLECT +
        appeal(gap=30, hearings=(12,)) + act("RequestInfo", gap=25) + act("Event_0zujvdo", gap=9) + act("Hearing", gap=14) + REJECTED),
    ("E14", "Appeal withdrawn", S + complain(5) + REJECT + appeal(hearings=(14,)) + act("Withdraw", gap=20) + act("Refund", gap=8)),
    ("E15", "Appeal settled", S + complain(6) + REJECT + appeal(hearings=(15,)) + act("Settle", gap=30) + act("Refund", gap=7) + act("CaseFee", gap=1)),
    ("E16", "Prior judgment, case rejected, review requested", S + complain(5) + REJECT + appeal(hearings=()) +
        act("PriorJudgment", gap=9) + act("RejectCase", gap=3) + act("Refund", gap=10) + act("RequestReview", gap=12)),
    ("E17", "Reduced to 125 kr, appeal for a child (80 kr fee), rejected", S + complain(6) + REDUCE(125, 14) +
        appeal(125, hearings=(12,), behalf=True) + REJECTED[:2] + decision(3, 125)),
    ("E18", "Complaint for another adult, appeal upheld", S + complain(5, True) + act("AttachAuth", gap=9) + REJECT +
        appeal(hearings=(13, 24), behalf=True) + UPHELD + act("Comply", gap=12)),
    ("E19", "Identity misuse rejected, extra information, appeal rejected", S + act("ComplainMisuse", gap=12) + act("HandleMisuse", gap=25) +
        REJECT + appeal() + act("RequestInfo", gap=14) + act("Event_0zujvdo", gap=12) + REJECTED),
    ("E20", "Appeal for another adult, withdrawn", S + complain(4, True) + act("AttachAuth", gap=5) + REJECT +
        appeal(behalf=True, hearings=(11,)) + act("Withdraw", gap=25) + act("Refund", gap=6)),
]
# Only for the TO-BE (models with "Notify parties of delay"): the board is slow.
TOBE_CASES = [
    ("E21", "TO-BE: board slow, parties notified at day 89, appeal rejected", S + complain(5) + REJECT + appeal() +
        [("SendToBoard", 20, None), ("NotifyDelay", 89, None), ("RejectAppeal", BOARD, None)] + decision(3)),
    ("E22", "TO-BE: board slow, parties notified at day 89, appeal upheld", S + complain(6) + REJECT + appeal(hearings=(12,)) +
        [("SendToBoard", 18, None), ("NotifyDelay", 89, None), ("Uphold", BOARD, None), ("Refund", 9, None), ("CaseFee", 1, None)] +
        act("Comply", gap=10)),
]

def pretend_dates(steps, start):
    t = dt.datetime.combine(start, dt.time(9, 0)); out = []
    for i, (e, gap, v) in enumerate(steps):
        if gap == BOARD:
            t = dt.datetime.combine(next(m for m in MEETINGS if m >= t.date() + dt.timedelta(days=14)), dt.time(10, 0))
        elif gap == 0:
            t += dt.timedelta(minutes=20)
        else:
            t = dt.datetime.combine(t.date() + dt.timedelta(days=gap), dt.time(9 + (i * 3) % 7, (i * 17) % 60))
        out.append((e, t, v))
    return out

# ---------------------------------------------------------------- reading the model
def read_model(path):
    """Labels, roles, data types, start marking and relations from a dcrgraphs.net XML file."""
    r = ET.parse(path).getroot(); spec = r.find("specification")
    lab = {m.get("eventId"): m.get("labelId") for m in spec.find("resources/labelMappings")}
    expr = {e.get("id"): e.get("value") for e in spec.find("resources/expressions")}
    inc = {e.get("id") for e in r.find("runtime/marking/included")}
    ev, groups = {}, {}
    def walk(node):
        kids = []
        for e in node.findall("event"):
            i = e.get("id")
            if e.get("type") == "nesting":
                groups[i] = walk(e); kids += groups[i]
            else:
                role = e.find("custom/roles/role"); dtp = e.find("custom/eventData/dataType")
                typ = None
                if dtp is not None:
                    typ = "bool" if dtp.get("format") == "bool" or (dtp.text or "").strip() == "bool" else "int"
                ev[i] = dict(label=lab[i], role=(role.text if role is not None else ""), included=i in inc, type=typ)
                kids.append(i)
        return kids
    walk(spec.find("resources/events"))
    atoms = lambda i: [i] if i in ev else groups[i]
    rels = []
    for tag, k in (("conditions", "condition"), ("responses", "response"), ("includes", "include"),
                   ("excludes", "exclude"), ("milestones", "milestone")):
        for c in spec.find("constraints/" + tag):
            g = expr.get(c.get("expressionId")) if c.get("expressionId") else None
            for a in atoms(c.get("sourceId")):
                for b in atoms(c.get("targetId")):
                    rels.append((k, a, b, g))
    return ev, rels

# ---------------------------------------------------------------- dry run: local engine
class LocalEngine:
    """Stands in for dcrgraphs.net in --dry-run (same DCR rules, guards like 'Complain = true')."""
    def __init__(self, ev, rels): self.ev, self.rels = ev, rels
    def start(self): return dict(inc={e for e, d in self.ev.items() if d["included"]}, ex=set(), pen=set())
    def guard(self, g, e, v):
        if not g: return True
        if v is None: return False
        expr = g.replace(" = ", " == ").replace("true", "True").replace("false", "False")
        return bool(eval(expr, {}, {e: v}))
    def execute(self, m, e, v):
        if e not in m["inc"]: return "excluded"
        for k, a, b, g in self.rels:
            if b == e and a in m["inc"]:
                if k == "condition" and a not in m["ex"]: return f"condition from {self.ev[a]['label']} not met"
                if k == "milestone" and a in m["pen"]: return f"blocked by pending {self.ev[a]['label']}"
        m["ex"].add(e); m["pen"].discard(e); inc, exc = set(), set()
        for k, a, b, g in self.rels:
            if a == e and self.guard(g, e, v):
                if k == "response": m["pen"].add(b)
                elif k == "include": inc.add(b)
                elif k == "exclude": exc.add(b)
        m["inc"] = (m["inc"] - exc) | inc
        return None
    def unfinished(self, m): return sorted(self.ev[b]["label"] for b in m["pen"] if b in m["inc"])

# ---------------------------------------------------------------- dcrgraphs.net API
class Api:
    def __init__(self, graph, user, pw):
        self.graph = graph
        self.auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
    def call(self, method, path, body=None):
        req = urllib.request.Request(API + path, method=method, data=json.dumps(body or {}).encode() if method == "POST" else None,
                                     headers={"Authorization": self.auth, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r: return r.status, dict(r.headers), r.read().decode()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read().decode(errors="replace")
    def new_case(self):
        st, h, body = self.call("POST", f"/graphs/{self.graph}/sims")
        sim = {k.lower(): v for k, v in h.items()}.get("x-dcr-simulation-id")
        if st not in (200, 201) or not sim:
            sys.exit(f"Could not start a simulation (HTTP {st}). Check the graph id and your login.\n{body[:300]}")
        return sim
    def execute(self, sim, e, v, typ):
        body = {}
        if v is not None and typ:
            val = str(v).lower() if typ == "bool" else str(int(v))
            body = {"DataXML": f'<globalStore><variable id="{e}" value="{val}" isNull="false" type="{typ}" /></globalStore>'}
        st, _, txt = self.call("POST", f"/graphs/{self.graph}/sims/{sim}/events/{e}", body)
        return None if st in (200, 204) else f"HTTP {st}: {txt.strip()[:200]}"
    def unfinished(self, sim):
        st, _, txt = self.call("GET", f"/graphs/{self.graph}/sims/{sim}/events?filter=enabled-or-pending")
        try: txt = json.loads(txt)               # the API returns the XML as a quoted JSON string
        except ValueError: pass
        out = []
        for tag in re.findall(r"<event\b[^>]*>", txt):
            a = dict(re.findall(r'(\w+)="([^"]*)"', tag))
            if a.get("pending") == "true" and a.get("included", "true") == "true":
                out.append(a.get("label") or a.get("id"))
        return sorted(out)

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="dcrgraphs.net XML of the graph (labels, roles)")
    ap.add_argument("--graph", help="graph id on dcrgraphs.net")
    ap.add_argument("--dry-run", action="store_true", help="use the local engine, no internet")
    ap.add_argument("--out", default="P3_execution_log", help="output file name without extension")
    a = ap.parse_args()

    ev, rels = read_model(a.model)
    cases = CASES + (TOBE_CASES if "NotifyDelay" in ev else [])
    missing = sorted({e for _, _, steps in cases for e, _, _ in steps if e not in ev})
    if missing: sys.exit(f"These activity ids are not in {a.model}: {missing}")

    if a.dry_run:
        eng = LocalEngine(ev, rels); out_name = a.out + "_DRYRUN"
    else:
        if not a.graph: sys.exit("Give --graph <id> (or use --dry-run).")
        user = input("dcrgraphs.net username: ").strip()
        pw = getpass.getpass("dcrgraphs.net password (nothing shows while typing): ")
        eng = Api(a.graph, user, pw); out_name = a.out

    rows, problems = [], []
    for n, (cid, variant, steps) in enumerate(cases):
        dated = pretend_dates(steps, dt.date(2025, 1, 6) + dt.timedelta(days=11 * n))
        m = eng.start() if a.dry_run else eng.new_case()
        done = []
        for e, t, v in dated:
            err = eng.execute(m, e, v) if a.dry_run else eng.execute(m, e, v, ev[e]["type"])
            if err:
                problems.append(f"{cid}: '{ev[e]['label']}' refused ({err}); rest of the case skipped")
                break
            done.append((e, t, v))
        left = eng.unfinished(m)
        if left: problems.append(f"{cid}: finished with pending steps: {', '.join(left)}")
        rows.append((cid, variant, done, "" if a.dry_run else m))
        print(f"{cid}  {len(done)}/{len(dated)} steps  {'OK' if len(done) == len(dated) and not left else 'CHECK'}"
              + ("" if a.dry_run else f"  (simulation {m})"))

    def data(e, v):
        if v is None or e not in PD_NAMES: return None
        name, typ = PD_NAMES[e]
        return name, typ, (str(v).lower() if typ == "boolean" else str(v))

    src = "dry run with a local engine (TEST ONLY)" if a.dry_run else f"executed on dcrgraphs.net, graph {a.graph}"
    x = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<log xes.version="1.0" xes.features="nested-attributes" xmlns="http://www.xes-standard.org/">',
         '  <extension name="Concept" prefix="concept" uri="http://www.xes-standard.org/concept.xesext"/>',
         '  <extension name="Time" prefix="time" uri="http://www.xes-standard.org/time.xesext"/>',
         '  <classifier name="Activity" keys="concept:name"/>',
         f'  <string key="concept:name" value={quoteattr("P3 execution log - test cases " + src + "; dates are pretend dates")}/>']
    for cid, variant, done, sim in rows:
        x += ["  <trace>", f'    <string key="concept:name" value="{cid}"/>', f"    <string key=\"variant\" value={quoteattr(variant)}/>"]
        if sim: x.append(f'    <string key="simulation" value="{sim}"/>')
        for e, t, v in done:
            x += ["    <event>", f"      <string key=\"concept:name\" value={quoteattr(ev[e]['label'])}/>",
                  f"      <string key=\"role\" value={quoteattr(ev[e]['role'])}/>",
                  f'      <date key="time:timestamp" value="{t.isoformat()}.000+01:00"/>']
            d = data(e, v)
            if d: x.append(f'      <{d[1]} key="{d[0]}" value="{d[2]}"/>')
            x.append("    </event>")
        x.append("  </trace>")
    x.append("</log>")
    open(out_name + ".xes", "w", encoding="utf-8").write("\n".join(x) + "\n")
    with open(out_name + ".csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["case_id", "variant", "activity", "role", "pretend_time", "data_name", "data_value"])
        for cid, variant, done, _ in rows:
            for e, t, v in done:
                d = data(e, v); w.writerow([cid, variant, ev[e]["label"], ev[e]["role"], t.isoformat(), d[0] if d else "", d[2] if d else ""])

    print(f"\n{len(rows)} cases, {sum(len(r[2]) for r in rows)} events -> {out_name}.xes and {out_name}.csv")
    print("No problems." if not problems else "Problems:\n  " + "\n  ".join(problems))

if __name__ == "__main__":
    main()

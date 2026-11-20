#!/usr/bin/env python3
"""
P3 Flask app: connects the CEP engine (Siddhi) with the P3 DCR graph on dcrgraphs.net.

  CEP -> P3   POST /event  {"fineId": "F123", "activity": "Complain", "onBehalfOfAdult": false}
              One P3 case per fine number. The first event for a fine starts a new case (simulation)
              on dcrgraphs.net. The activity is executed only if the graph allows it right now.
  P3 -> CEP   After every executed activity, the app POSTs {"event": {...}} to CEP_URL (if set).
  People      http://localhost:5000 shows every case with buttons for what can happen next,
              e.g. Movia's decision or the board's decision in the demo.
  Log         Every executed activity is appended to p3_events.jsonl.
              "python p3_app.py export" turns it into p3_live_log.xes for Process DoCtoR.

TO-BE: "Create case" (role System) is executed automatically as soon as the graph allows it.

Run (Command Prompt, in the folder with this file, p3_run_cases.py and the model XML):
    pip install flask
    set CEP_URL=http://localhost:8006/p3          (optional: where Siddhi listens for P3's events)
    python p3_app.py --graph <graph id> --model P3_TOBE_dcrgraphs.xml
It asks for your dcrgraphs.net username and password once, at start.
Without dcrgraphs.net (local DCR engine, for testing):
    python p3_app.py --local --model P3_TOBE_dcrgraphs.xml
"""
import argparse, datetime as dt, getpass, html, json, os, re, sys, threading, urllib.request
from flask import Flask, jsonify, redirect, request

from p3_run_cases import Api, LocalEngine, read_model, PD_NAMES   # same folder

CASES_FILE, EVENTS_FILE = "p3_cases.json", "p3_events.jsonl"
AUTO = {"CreateCase"}                       # System steps run as soon as they are enabled (TO-BE change 2)

# Event types other processes may send instead of an activity id (change to the names the group agrees on).
ALIASES = {
    "FineIssued": "IssueFine",              # from P2
    "ComplaintReceived": "Complain",        # from P2 / the passenger
    "IdentityMisuseReported": "ComplainMisuse",
    "ReminderSent": "SendReminder",         # from P2
    "TransferredToGaeldsstyrelsen": "Transfer",   # from P2
    "AppealSubmitted": "SubmitForm",        # from the passenger
    "BoardDelayDeadline": "NotifyDelay",    # from Siddhi's 90-day watchdog (TO-BE change 1)
}
# Data fields other systems may use, per activity (the value goes to dcrgraphs.net).
VALUE_FIELDS = {e: n for e, (n, _) in PD_NAMES.items()}   # Complain: onBehalfOfAdult, SubmitForm: amount, NewDeadline: newAmount


class LocalBackend:
    """Same interface as dcrgraphs.net, but a local DCR engine (for testing)."""
    def __init__(self, ev, rels): self.eng, self.ev, self.m = LocalEngine(ev, rels), ev, {}
    def new_case(self):
        sim = f"local-{len(self.m) + 1}"; self.m[sim] = self.eng.start(); return sim
    def state(self, sim):
        m, out = self.m[sim], []
        for e in self.ev:
            if e not in m["inc"]: continue
            blocked = any(k == "condition" and b == e and a in m["inc"] and a not in m["ex"] or
                          k == "milestone" and b == e and a in m["inc"] and a in m["pen"] for k, a, b, g in self.eng.rels)
            if not blocked or e in m["pen"]:
                out.append({"id": e, "enabled": not blocked, "pending": e in m["pen"]})
        return out
    def execute(self, sim, e, v, typ): return self.eng.execute(self.m[sim], e, v)


class DcrBackend(Api):
    def state(self, sim):
        st, _, txt = self.call("GET", f"/graphs/{self.graph}/sims/{sim}/events?filter=enabled-or-pending")
        try: txt = json.loads(txt)
        except ValueError: pass
        out = []
        for tag in re.findall(r"<event\b[^>]*>", txt):
            a = dict(re.findall(r'(\w+)="([^"]*)"', tag))
            if a.get("id"):
                out.append({"id": a["id"], "enabled": a.get("enabled") == "true", "pending": a.get("pending") == "true"})
        return out


def create_app(backend, ev, cep_url=None, workdir="."):
    app = Flask(__name__)
    lock = threading.Lock()
    cases_path, events_path = os.path.join(workdir, CASES_FILE), os.path.join(workdir, EVENTS_FILE)
    cases = json.load(open(cases_path)) if os.path.exists(cases_path) else {}   # fineId -> simulation id
    by_label = {d["label"].lower(): e for e, d in ev.items()}

    def to_cep(event):
        if not cep_url: return
        try:
            req = urllib.request.Request(cep_url, data=json.dumps({"event": event}).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).close()
        except Exception as err:
            print(f"  (could not reach the CEP at {cep_url}: {err})")

    def resolve(name):
        name = (name or "").strip()
        return name if name in ev else ALIASES.get(name) or by_label.get(name.lower())

    def case_for(fine, create):
        if fine not in cases and create:
            cases[fine] = backend.new_case()
            json.dump(cases, open(cases_path, "w"), indent=1)
            print(f"[{fine}] new P3 case: simulation {cases[fine]}")
        return cases.get(fine)

    def run(fine, act, value, when, source):
        """Execute one activity in the case of this fine. Returns (http status, body)."""
        sim = case_for(fine, create=True)
        allowed = {s["id"] for s in backend.state(sim) if s["enabled"]}
        if act not in allowed:
            return 409, {"fineId": fine, "activity": act, "executed": False,
                         "reason": f"'{ev[act]['label']}' is not possible in this case right now"}
        err = backend.execute(sim, act, value, ev[act]["type"])
        if err:
            return 409, {"fineId": fine, "activity": act, "executed": False, "reason": err}
        record = {"fineId": fine, "activity": act, "label": ev[act]["label"], "role": ev[act]["role"],
                  "value": value, "time": when or dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                  "source": source, "simulation": sim}
        with open(events_path, "a", encoding="utf-8") as f: f.write(json.dumps(record) + "\n")
        print(f"[{fine}] {ev[act]['role']}: {ev[act]['label']}" + (f" ({value})" if value is not None else ""))
        state = backend.state(sim)
        named = {VALUE_FIELDS[act]: value} if act in VALUE_FIELDS and value is not None else {}   # e.g. "amount": 1000
        to_cep({"eventType": act, "process": "P3", **{k: record[k] for k in ("fineId", "label", "role")}, **named,
                "pending": ", ".join(ev[s["id"]]["label"] for s in state if s["pending"])})
        return 200, {**record, "executed": True}

    def run_with_auto(fine, act, value, when, source):
        code, body = run(fine, act, value, when, source)
        if code == 200:
            for _ in range(5):                                    # TO-BE: System steps that run by themselves
                nxt = [s["id"] for s in backend.state(cases[fine]) if s["enabled"] and s["id"] in AUTO]
                if not nxt: break
                run(fine, nxt[0], None, when, "system")
        return code, body

    def parse_value(act, data):
        field = VALUE_FIELDS.get(act)
        v = data.get("value", data.get(field)) if field else None
        if v is None or ev[act]["type"] is None: return None
        if ev[act]["type"] == "bool":
            return v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes")
        return int(float(v))

    @app.post("/event")
    def event():
        data = request.get_json(force=True, silent=True) or {}
        data = data.get("event", data)                          # Siddhi's {"event": {...}} wrapper
        fine = str(data.get("fineId") or "").strip()
        act = resolve(data.get("activity") or data.get("eventType"))
        if not fine or not act:
            return jsonify(error="need fineId and a known activity (or eventType)"), 400
        if ev[act]["type"] and parse_value(act, data) is None:
            return jsonify(error=f"'{ev[act]['label']}' needs a value ({VALUE_FIELDS.get(act, 'value')})"), 400
        with lock:
            code, body = run_with_auto(fine, act, parse_value(act, data), data.get("time"), data.get("source", "cep"))
        return jsonify(body), code

    @app.get("/cases")
    def list_cases():
        with lock:
            return jsonify({fine: [{**s, "label": ev[s["id"]]["label"]} for s in backend.state(sim)]
                            for fine, sim in cases.items()})

    @app.post("/ui/run")
    def ui_run():
        f = request.form; act = f["activity"]
        with lock:
            code, body = run_with_auto(f["fineId"], act, parse_value(act, f), None, "user")
        msg = "" if code == 200 else body["reason"]
        return redirect("/?msg=" + urllib.request.quote(msg) if msg else "/")

    @app.get("/")
    def page():
        rows = []
        with lock:
            for fine, sim in cases.items():
                st = backend.state(sim)
                btns = []
                for s in st:
                    e, d = s["id"], ev[s["id"]]
                    if not s["enabled"]: continue
                    extra = ""
                    if d["type"] == "bool":
                        extra = f'<select name="value"><option>false</option><option>true</option></select>'
                    elif d["type"] == "int":
                        extra = f'<input name="value" size="5" value="1000">'
                    btns.append(f'<form method="post" action="/ui/run"><input type="hidden" name="fineId" value="{html.escape(fine)}">'
                                f'<input type="hidden" name="activity" value="{e}">{extra}'
                                f'<button>{html.escape(d["role"])}: {html.escape(d["label"])}{" *" if s["pending"] else ""}</button></form>')
                pend = [ev[s["id"]]["label"] for s in st if s["pending"]]
                rows.append(f"<section><h2>Fine {html.escape(fine)}</h2><p class=sim>simulation {sim}</p>"
                            f"<div class=btns>{''.join(btns) or '<i>nothing can happen now</i>'}</div>"
                            f"<p>Pending: {html.escape(', '.join(pend)) or 'none (the case can be finished)'}</p></section>")
        msg = request.args.get("msg")
        return f"""<!doctype html><meta charset=utf-8><title>P3 cases</title>
<style>body{{font:15px system-ui;margin:24px;max-width:900px}} section{{border:1px solid #ccc;border-radius:8px;padding:8px 16px;margin:12px 0}}
.btns form{{display:inline-block;margin:4px}} button{{padding:6px 10px}} .sim{{color:#777;font-size:12px}} .msg{{background:#fde;padding:8px}}</style>
<h1>P3: kontrolafgift disputes</h1>{f'<p class=msg>{html.escape(msg)}</p>' if msg else ''}
<form method="post" action="/ui/run"><input name="fineId" placeholder="fine number, e.g. F123">
<input type="hidden" name="activity" value="IssueFine"><button>Start a case (Issue fine)</button></form>
{''.join(rows) or '<p>No cases yet.</p>'}<p class=sim>* = pending (has to happen before the case is finished)</p>"""
    return app


def export(events_path=EVENTS_FILE, out="p3_live_log.xes"):
    from xml.sax.saxutils import quoteattr
    traces = {}
    for line in open(events_path, encoding="utf-8"):
        r = json.loads(line); traces.setdefault(r["fineId"], []).append(r)
    x = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<log xes.version="1.0" xes.features="nested-attributes" xmlns="http://www.xes-standard.org/">',
         '  <extension name="Concept" prefix="concept" uri="http://www.xes-standard.org/concept.xesext"/>',
         '  <extension name="Time" prefix="time" uri="http://www.xes-standard.org/time.xesext"/>',
         '  <classifier name="Activity" keys="concept:name"/>',
         '  <string key="concept:name" value="P3 live log from the Flask app"/>']
    for fine, evs in traces.items():
        x += ["  <trace>", f"    <string key=\"concept:name\" value={quoteattr(fine)}/>"]
        for r in evs:
            x += ["    <event>", f"      <string key=\"concept:name\" value={quoteattr(r['label'])}/>",
                  f"      <string key=\"role\" value={quoteattr(r['role'])}/>",
                  f"      <date key=\"time:timestamp\" value=\"{dt.datetime.fromisoformat(r['time']).isoformat(timespec='milliseconds')}\"/>"]
            if r["value"] is not None and r["activity"] in PD_NAMES:
                name, typ = PD_NAMES[r["activity"]]
                x.append(f'      <{typ} key="{name}" value="{str(r["value"]).lower() if typ == "boolean" else r["value"]}"/>')
            x.append("    </event>")
        x.append("  </trace>")
    x.append("</log>")
    open(out, "w", encoding="utf-8").write("\n".join(x) + "\n")
    print(f"{len(traces)} cases -> {out}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["export"]:
        export(); sys.exit()
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True); ap.add_argument("--graph"); ap.add_argument("--local", action="store_true")
    ap.add_argument("--port", type=int, default=5000)
    a = ap.parse_args()
    ev, rels = read_model(a.model)
    if a.local:
        backend = LocalBackend(ev, rels)
    else:
        if not a.graph: sys.exit("Give --graph <id> (or use --local).")
        backend = DcrBackend(a.graph, input("dcrgraphs.net username: ").strip(),
                             getpass.getpass("dcrgraphs.net password (nothing shows while typing): "))
    cep = os.environ.get("CEP_URL")
    print(f"P3 app on http://localhost:{a.port}  |  events from the CEP: POST /event  |  "
          f"events to the CEP: {cep or 'off (set CEP_URL)'}")
    create_app(backend, ev, cep).run(host="0.0.0.0", port=a.port)   # 0.0.0.0: reachable from Siddhi in Docker

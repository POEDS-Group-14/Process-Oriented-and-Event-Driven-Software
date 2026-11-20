#!/usr/bin/env python3
"""
Sends a demo scenario to the P3 app, the way Siddhi would. Use it to test the app before Siddhi runs.

    python p3_send_events.py            (fine number F100)
    python p3_send_events.py F101       (another fine)
Then open http://localhost:5000 and click Movia's and the board's decisions yourself.
"""
import json, sys, urllib.error, urllib.request

APP = "http://localhost:5000/event"
fine = sys.argv[1] if len(sys.argv) > 1 else "F100"

STEPS = [  # (what, extra data) - outside-world events only; decisions are clicked in the browser
    ("FineIssued", {}),
    ("ComplaintReceived", {"onBehalfOfAdult": False}),
    ("RejectCompl", {}),                      # remove these two lines to click Movia's decision yourself
    ("NewDeadline", {"newAmount": 1000}),
    ("AppealSubmitted", {"amount": 1000}),
    ("PayFee80", {}),                         # wrong fee: P3 refuses it
    ("PayFee160", {}),                        # right fee: "Create case" follows automatically
    ("Hearing", {}),
    ("SendToBoard", {}),
]

for what, data in STEPS:
    body = {"event": {"fineId": fine, "eventType": what, **data}}       # same shape Siddhi sends
    req = urllib.request.Request(APP, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r: out = json.loads(r.read())
        print(f"OK       {out['role']}: {out['label']}")
    except urllib.error.HTTPError as err:
        out = json.loads(err.read() or b"{}")
        print(f"REFUSED  {what}: {out.get('reason') or out.get('error')}")
    except urllib.error.URLError:
        sys.exit("The P3 app is not running (start it with python p3_app.py ...).")
print(f"\nNow open http://localhost:5000 to see fine {fine} and click the board's decision.")

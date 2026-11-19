"""Command line for process 1 (rejsegaranti).

  uv run rejsegaranti deploy               deploy the TO-BE models (models/to-be/) and the AS-IS decision tables
  uv run rejsegaranti run                  CEP bridge (:8090) + external-task workers
  uv run rejsegaranti replay               decide every stored delay and claim again with the TO-BE models
  uv run rejsegaranti simulate --post      send simulated situations and claims to the bridge
  uv run rejsegaranti feed                 post simulated delays and claims every hour, forever
  uv run rejsegaranti feed --real          simulate claims for the real delays the bridge received
  uv run rejsegaranti export               Camunda history -> out/*.xes and .csv
  uv run rejsegaranti per-line             delay cases and claims per 1,000 journeys, per line -> out/per-line.csv

Settings (environment): CAMUNDA_PORT (default 8085) or CAMUNDA_URL (default http://localhost:8085/engine-rest),
CEP_URL (Siddhi HTTP source; optional), BRIDGE_PORT (8090), DATA_DIR (./out),
SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM, PUBLIC_URL (claim emails; see mailing.py).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
MODELS = os.path.join(ROOT, "models")
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(ROOT, "out"))
# Deployment name -> files. Camunda skips a deployment whose files did not change. The AS-IS tables (they
# decide the claims on the main results page, asis.py) go in a deployment of their own: both DMN files use
# the definitions id Definitions_travel_guarantee, and Camunda refuses two of them in one deployment.
DEPLOY = {
    "rejsegaranti": [
        os.path.join(MODELS, "to-be", "assess-disruption.bpmn"),
        os.path.join(MODELS, "to-be", "decide-claim.bpmn"),
        os.path.join(MODELS, "to-be", "handle-appeal.bpmn"),
        os.path.join(MODELS, "to-be", "travel-guarantee-to-be.dmn"),
    ],
    "rejsegaranti-as-is": [os.path.join(MODELS, "travel-guarantee", "travel-guarantee.dmn")],
}


def cmd_deploy(a):
    from .camunda import Camunda
    cam = Camunda()
    for name, files in DEPLOY.items():
        r = cam.deploy(name, files)
        defs = [*(r.get("deployedProcessDefinitions") or {}).values(),
                *(r.get("deployedDecisionDefinitions") or {}).values()]
        print(name, r["id"], "->", ", ".join(f"{d['key']} v{d['version']}" for d in defs) or "no change")


def cmd_run(a):
    from .bridge import Bridge, make_server
    from .camunda import Camunda
    from .cep import CepSink
    from .mailing import MailingList, Notifier
    from .store import Store
    from .workers import run_workers

    os.makedirs(DATA_DIR, exist_ok=True)
    store = Store(os.path.join(DATA_DIR, "registry.json"))
    notifier = Notifier(MailingList(os.path.join(DATA_DIR, "subscribers.json")), store)
    cep = CepSink(journal=os.path.join(DATA_DIR, "cep-events.jsonl"),
                  listeners=[notifier.on_event] if notifier.enabled else [])
    cam = Camunda()
    port = int(os.environ.get("BRIDGE_PORT", a.port))
    srv = make_server(Bridge(store, cam, notifier if notifier.enabled else None), port=port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    logging.info("bridge listening on :%d, Camunda at %s, CEP %s", port, cam.base, cep.url or "(journal only)")
    try:
        run_workers(cam, store, cep)
    except KeyboardInterrupt:
        srv.shutdown()


def cmd_simulate(a):
    from . import simulate as S

    delays = S.read_delays_csv(a.delays) if a.delays else None
    sc = S.generate(seed=a.seed, n_delays=a.delays_n, delays=delays)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "scenario.json"), "w", encoding="utf-8") as fh:
        json.dump(sc, fh, ensure_ascii=False, indent=1)
    print(f"{len(sc['delays'])} delays, {len(sc['notices'])} notices, "
          f"{len(sc['weather'])} weather situations, {len(sc['claims'])} claims")
    if a.post:
        S.run_live(a.bridge, sc)


def cmd_feed(a):
    from . import simulate as S
    if a.real:
        S.feed_real(a.bridge, interval=a.interval)
    else:
        S.feed(a.bridge, batch=a.batch, interval=a.interval)


def cmd_export(a):
    from .activities import ROLES
    from .camunda import Camunda
    from .export import KEYS, traces_from_camunda
    from .xes import write_csv, write_xes

    cam = Camunda()
    os.makedirs(DATA_DIR, exist_ok=True)
    for key, name in KEYS.items():
        tr = traces_from_camunda(cam, key)
        write_xes(os.path.join(DATA_DIR, name + ".xes"), tr, f"{key} (Camunda history)", roles=ROLES)
        write_csv(os.path.join(DATA_DIR, name + ".csv"), tr)
        print(f"{key}: {len(tr)} traces -> {name}.xes")


def cmd_per_line(a):
    import urllib.request
    from . import perline

    with urllib.request.urlopen(a.bridge.rstrip("/") + "/state", timeout=60) as r:
        state = json.loads(r.read())
    rows = perline.per_line(state, perline.load_journeys())
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "per-line.csv")
    perline.write_csv(path, rows)
    print(f"{'line':>6} {'journeys':>9} {'cases':>6} {'per 1000':>9}  95% interval   {'claims':>6} {'paid':>5}")
    for r in rows:
        print(f"{r['line']:>6} {r['journeys']:>9} {r['cases']:>6} {r['casesPer1000']:>9.2f}  "
              f"{r['low']:5.2f} - {r['high']:5.2f}   {r['claims']:>6} {r['paid']:>5}")
    print("wrote", path)


def cmd_replay(a):
    """Decide every stored delay and claim again with the TO-BE models.

    The registry file belongs to the running `rejsegaranti run` process: its Store reads the file once at
    start and rewrites it whole on every change, so a second process writing it would lose the other's
    changes (or have its own overwritten). This command therefore never touches the file. It talks to the
    running bridge over HTTP: POST /replay/reset makes the bridge copy the file to
    registry.before-replay-<time>.json and clear the assessments, outcomes, appeals and paid receipts in its
    own Store; POST /replay/delay and /replay/claim make it start the instances (reprocessed = true, so no
    emails), with the start variables worked out by intake.py as for live intake; GET /state shows the
    workers' progress. The workers run in that same process, so every write goes through one Store.

    Order: one assess-disruption instance per delay, then wait until every delay has an assessment, then
    one decide-claim instance per claim by trip start (intake.replay_order), so "receipt used on an earlier
    claim" means an earlier trip. A claim counts as received when its first AS-IS-era instance started
    (asis.received_dates), else when the bridge stamped it, else at its trip start.

    Running it twice is safe: it first deletes any TO-BE instances still running (an earlier replay that
    was stopped, or an appeal waiting for review), then clears and decides everything again.
    """
    from collections import Counter

    from . import asis
    from .camunda import Camunda
    from .intake import replay_order
    from .simulate import post

    base = a.bridge.rstrip("/")
    cam = Camunda()

    def state():
        import urllib.request
        with urllib.request.urlopen(base + "/state", timeout=120) as r:
            return json.loads(r.read())

    def wait(kind, ids, what):
        deadline = time.monotonic() + a.timeout
        while True:
            done = {x.get("claimId") or x.get("delayId") for x in state()[kind]} & ids
            incidents = cam.get("/incident/count")["count"]
            print(f"  {len(done)}/{len(ids)} {what}, {incidents} incidents", flush=True)
            if len(done) == len(ids):
                return
            if incidents:
                sys.exit("stopped: Camunda has incidents (see Cockpit)")
            if time.monotonic() > deadline:
                sys.exit(f"stopped: not every {what[:-1]} done after {a.timeout:.0f} s")
            time.sleep(3)

    from .activities import APPEAL_KEY, ASSESS_KEY, CLAIM_KEY
    running = [p for k in (ASSESS_KEY, CLAIM_KEY, APPEAL_KEY)
               for p in cam.get("/process-instance", processDefinitionKey=k) or []]
    for p in running:
        cam.delete_instance(p["id"], "replay")
    print(f"deleted {len(running)} running TO-BE instances")

    r = post(base, "/replay/reset", {})
    if "status" in r:
        sys.exit(f"the bridge refused the reset: {r}")
    print("registry backed up to", r["backup"], "; cleared", r["cleared"])

    s = state()
    delays = {d["delayId"] for d in s["delays"]}
    print(f"assessing {len(delays)} delays")
    for d in s["delays"]:
        post(base, "/replay/delay", {"delayId": d["delayId"]})
    wait("assessments", delays, "delays assessed")

    try:
        received = asis.received_dates(cam)
    except Exception as e:  # noqa: BLE001 - the old process may never have been deployed here
        print("no AS-IS claim history:", e)
        received = {}
    claims = sorted(s["claims"], key=replay_order)
    print(f"deciding {len(claims)} claims ({sum(c['claimId'] in received for c in claims)} with an AS-IS "
          "received time)")
    for c in claims:
        res = post(base, "/replay/claim", {"claimId": c["claimId"], "receivedAt": asis.received_at(c, received)})
        if "status" in res:
            print("  claim", c["claimId"], res)
    wait("outcomes", {c["claimId"] for c in claims}, "claims decided")

    outcomes = state()["outcomes"]
    print("\noutcomes:", dict(Counter(o["outcome"] for o in outcomes)))
    print(f"paid out: {sum(float(o.get('payoutDkk') or 0) for o in outcomes):,.2f} DKK")
    for reason, n in Counter(o.get("reason") for o in outcomes if o["outcome"] != "paid").most_common():
        print(f"  {n:5d}  {reason}")
    try:
        from .results import compare
        from .store import Store
        store = Store()
        for kind, records in state().items():
            store.data[kind] = {i: r for i, r in enumerate(records)}
        cmp = compare(store, cam)
    except Exception as e:  # noqa: BLE001 - the comparison is extra; the replay itself is done
        print("no AS-IS comparison:", e)
        return
    print("\nAS-IS (rows) against TO-BE (columns):")
    for x in ("paid", "rejected"):
        print(f"  {x:9}" + "".join(f"  TO-BE {y}: {cmp['cross'].get((x, y), 0):4d}" for y in ("paid", "rejected")))
    print(f"{len(cmp['differ'])} claims decided differently")


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="rejsegaranti", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("deploy").set_defaults(fn=cmd_deploy)
    s = sub.add_parser("run")
    s.add_argument("--port", type=int, default=8090)
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("replay", help="decide every stored delay and claim again with the TO-BE models")
    s.add_argument("--bridge", default=f"http://localhost:{os.environ.get('BRIDGE_PORT', '8090')}")
    s.add_argument("--timeout", type=float, default=1800, help="seconds to wait for each stage")
    s.set_defaults(fn=cmd_replay)
    s = sub.add_parser("simulate")
    s.add_argument("--seed", type=int, default=14)
    s.add_argument("--delays-n", type=int, default=40, help="number of simulated delays")
    s.add_argument("--delays", help="CSV of recorded delays (data/delays-format.csv)")
    s.add_argument("--post", action="store_true", help="send to the bridge")
    s.add_argument("--bridge", default="http://localhost:8090")
    s.set_defaults(fn=cmd_simulate)
    s = sub.add_parser("feed", help="post a batch of simulated delays and claims every interval, forever")
    s.add_argument("--bridge", default="http://localhost:8090")
    s.add_argument("--batch", type=int, default=5, help="delays per batch")
    s.add_argument("--interval", type=float, default=3600, help="seconds between batches")
    s.add_argument("--real", action="store_true",
                   help="don't make up delays: simulate claims for the real delays the bridge received")
    s.set_defaults(fn=cmd_feed)
    sub.add_parser("export").set_defaults(fn=cmd_export)
    s = sub.add_parser("per-line", help="delay cases and claims per 1,000 scheduled journeys, per line")
    s.add_argument("--bridge", default="http://pinklady:8090")
    s.set_defaults(fn=cmd_per_line)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()

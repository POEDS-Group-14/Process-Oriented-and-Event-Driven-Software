"""Command line for process 1 (rejsegaranti).

  uv run rejsegaranti deploy               deploy the TO-BE BPMN to Camunda
  uv run rejsegaranti run                  CEP bridge (:8090) + external-task workers
  uv run rejsegaranti simulate --post      send simulated situations and claims to the bridge
  uv run rejsegaranti simulate --offline   dry run without Camunda -> out/*.xes
  uv run rejsegaranti feed                 post simulated delays and claims every hour, forever
  uv run rejsegaranti feed --real          simulate claims for the real delays the bridge received
  uv run rejsegaranti export               Camunda history -> out/*.xes and .csv
  uv run rejsegaranti per-line             delay cases and claims per 1,000 journeys, per line -> out/per-line.csv
  uv run rejsegaranti check LOG.xes        replay a log against the DCR rules
  uv run rejsegaranti build-dcr            regenerate models/rejsegaranti-rules.*

Settings (environment): CAMUNDA_PORT (default 8085) or CAMUNDA_URL (default http://localhost:8085/engine-rest),
CEP_URL (Siddhi HTTP source; optional), BRIDGE_PORT (8090), DATA_DIR (./out),
SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM, PUBLIC_URL (claim emails; see mailing.py).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
MODELS = os.path.join(ROOT, "models")
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(ROOT, "out"))
RULES_XML = os.path.join(MODELS, "rejsegaranti-rules.dcr.xml")


def cmd_deploy(a):
    from .camunda import Camunda
    files = [os.path.join(MODELS, "rejsegaranti-to-be.bpmn")]
    r = Camunda().deploy("rejsegaranti", files)
    defs = list((r.get("deployedProcessDefinitions") or {}).values())
    print("deployment", r["id"], "->", ", ".join(f"{d['key']} v{d['version']}" for d in defs) or "no change")


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
    from .dcr import ROLES
    from .xes import write_csv, write_xes

    delays = S.read_delays_csv(a.delays) if a.delays else None
    sc = S.generate(seed=a.seed, n_delays=a.delays_n, delays=delays)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "scenario.json"), "w", encoding="utf-8") as fh:
        json.dump(sc, fh, ensure_ascii=False, indent=1)
    print(f"{len(sc['delays'])} delays, {len(sc['notices'])} notices, "
          f"{len(sc['weather'])} weather situations, {len(sc['claims'])} claims")
    if a.post:
        S.run_live(a.bridge, sc)
    if a.offline:
        d, c, st = S.run_offline(sc, seed=a.seed, noise=a.noise)
        print(f"{len(st.refused_at_intake)} claims refused at the form (incomplete receipt)")
        tag = "offline" + (f"-noise{int(a.noise * 100)}" if a.noise else "")
        write_xes(os.path.join(DATA_DIR, f"claims-{tag}.xes"), c, "Rejsegaranti claims (offline, simulated)",
                  roles=ROLES)
        write_csv(os.path.join(DATA_DIR, f"claims-{tag}.csv"), c)
        write_xes(os.path.join(DATA_DIR, f"delay-cases-{tag}.xes"), d, "Rejsegaranti delay cases (offline, simulated)")
        print(f"wrote {len(c)} claim traces and {len(d)} delay-case traces to {DATA_DIR}")


def cmd_feed(a):
    from . import simulate as S
    if a.real:
        S.feed_real(a.bridge, interval=a.interval)
    else:
        S.feed(a.bridge, batch=a.batch, interval=a.interval)


def cmd_export(a):
    from .camunda import Camunda
    from .dcr import ROLES
    from .export import CLAIM_KEY, DELAY_KEY, traces_from_camunda
    from .xes import write_csv, write_xes

    cam = Camunda()
    os.makedirs(DATA_DIR, exist_ok=True)
    for key, name in ((CLAIM_KEY, "claims-camunda"), (DELAY_KEY, "delay-cases-camunda")):
        tr = traces_from_camunda(cam, key)
        write_xes(os.path.join(DATA_DIR, name + ".xes"), tr, f"{key} (Camunda history)",
                  roles=ROLES if key == CLAIM_KEY else None)
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


def cmd_check(a):
    from .dcr import Graph, check_log
    from .xes import read_xes

    g = Graph.from_xml(a.rules)
    r = check_log(g, read_xes(a.log))
    print(f"traces {r['traces']}, variants {r['variants']}, violating {r['violating']}, "
          f"fitness {r['fitness']:.3f}")
    for k, n in r["by_kind"].most_common():
        print(f"  {n:4d}  {k}")
    if a.verbose:
        for tid, p in r["details"].items():
            print(tid, "|", "; ".join(p))
    sys.exit(1 if r["violating"] and a.strict else 0)


def cmd_build_dcr(a):
    from .dcr import to_dot, to_xml

    to_xml(RULES_XML)
    dot = os.path.join(MODELS, "rejsegaranti-rules.dot")
    to_dot(dot)
    for fmt in ("pdf", "svg"):
        try:
            subprocess.run(["dot", f"-T{fmt}", dot, "-o", dot[:-4] + "." + fmt], check=True)
        except (OSError, subprocess.CalledProcessError) as e:
            print("graphviz not available, skipped", fmt, e)
    print("wrote", RULES_XML)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="rejsegaranti", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("deploy").set_defaults(fn=cmd_deploy)
    s = sub.add_parser("run")
    s.add_argument("--port", type=int, default=8090)
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("simulate")
    s.add_argument("--seed", type=int, default=14)
    s.add_argument("--delays-n", type=int, default=40, help="number of simulated delays")
    s.add_argument("--delays", help="CSV of recorded Line L delays (data/delays-format.csv)")
    s.add_argument("--post", action="store_true", help="send to the bridge")
    s.add_argument("--bridge", default="http://localhost:8090")
    s.add_argument("--offline", action="store_true", help="dry run without Camunda")
    s.add_argument("--noise", type=float, default=0.0, help="share of offline traces with injected violations")
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
    s = sub.add_parser("check")
    s.add_argument("log")
    s.add_argument("--rules", default=RULES_XML)
    s.add_argument("-v", "--verbose", action="store_true")
    s.add_argument("--strict", action="store_true", help="exit 1 on violations")
    s.set_defaults(fn=cmd_check)
    sub.add_parser("build-dcr").set_defaults(fn=cmd_build_dcr)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()

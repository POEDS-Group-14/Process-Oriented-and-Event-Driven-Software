# Process 1 - Travel guarantee claim (Movia)

Owner and author of every model in this folder: **Liam Palmqvist**.
Issues: #1, with sub-issues #7 AS-IS, #8 TO-BE, #9 Camunda, #10 DCR and #11 event log.

| Issue | What | Where |
|---|---|---|
| #7 | AS-IS: claim-handling BPMN and the DRD with its four DMN tables (the first, Line L version is in `models/archive/`) | `models/travel-guarantee/`, `docs/as-is.md` |
| #8 | TO-BE: three BPMN processes (assess disruption, decide claim, handle appeal), their processdoctor variants and the TO-BE DRD (the first, Line L version is in `models/archive/`) | `models/to-be/`, `docs/to-be-redesign.md` |
| #9 | Runs in Camunda: started by CEP situations, outcomes sent back to the CEP, simulated claims | `service/`, `docker-compose.yml`, `docs/cep-interface.md` |
| #10 | DCR compliance rules of the Line L version (archived; the service no longer checks them) | `models/archive/rejsegaranti-rules.dcr.xml` / `.pdf`, `docs/dcr-rules.md` |
| #11 | Event log export and conformance pre-check | `service/rejsegaranti/export.py`, `docs/conformance.md` |

## Requirements

- [uv](https://docs.astral.sh/uv/) (`brew install uv`). There are no third-party dependencies; uv takes care of the Python version and the virtual environment. The project file is `service/pyproject.toml`.
- Docker, for Camunda 7 Run. Alternatively, download Camunda 7 Run 7.24 (needs Java 17+) and start it with `server.port: 8085` in its `configuration/default.yml`.
- macOS with the repo in an iCloud-synced folder (Desktop & Documents): iCloud marks everything inside `.venv` as hidden, and Python 3.13+ then skips the `.pth` file of the editable install, so `uv run rejsegaranti` fails with `No module named 'rejsegaranti'`. Keep the environment in a folder iCloud does not sync, and point `.venv` at it:
  ```bash
  cd service && rm -rf .venv && UV_PROJECT_ENVIRONMENT=venv.nosync uv sync && ln -s venv.nosync .venv
  ```

## Run it

```bash
docker compose up -d                  # Camunda at http://localhost:8085/camunda (demo/demo)
cd service
uv run rejsegaranti deploy        # the TO-BE models in models/to-be/ and the AS-IS decision tables
uv run rejsegaranti run           # bridge on :8090 + external-task workers (leave running)

# in a second terminal
cd service
uv run rejsegaranti simulate --post     # simulated delays, notices, weather and claims
uv run rejsegaranti export              # Camunda history -> ../out/claims-camunda.xes and the other logs
uv run rejsegaranti replay              # decide every stored delay and claim again with the TO-BE models
```

`replay` is for after a model change. It asks the running bridge to back up `registry.json` to
`registry.before-replay-<time>.json` and clear the assessments, outcomes, appeals and paid receipts, then
starts one assess-disruption instance per stored delay and, once every delay is assessed, one decide-claim
instance per stored claim in order of trip start. Replayed events carry `reprocessed`, so the mailing list
gets no email for them. It prints the outcomes, the rejection reasons and how they compare with the AS-IS
tables. Running it twice is safe: it deletes any TO-BE instances still running first.

An appeal against a decided claim goes to the bridge: `curl -X POST localhost:8090/appeals -d
'{"claimId": "C0001", "reason": "..."}'`. A case worker then reviews it in Camunda Tasklist (group
`caseworker`).

Settings, all optional:

- `CAMUNDA_PORT` (default 8085). Set it to the same value for `docker compose` and `uv run` if 8085 is taken too. `CAMUNDA_URL` overrides the whole address
- `CEP_URL`, the Siddhi HTTP source for outcome events
- `BRIDGE_PORT` (default 8090)
- `DATA_DIR` (default `out/`)

## Always on: the pinklady server

Liam's Mac mini (`pinklady`) runs Camunda, the bridge, the recorder and a feed of simulated claims as systemd services that start on boot. Cases start from **real** delays on the 79 Movia bus and DSB S-tog lines serving Copenhagen, which stand in for Line L because no real-time data for Line L is published (bus 150S alone gave no case in 2.6 days, see `docs/process-log.md` section 11); the claims are simulated. Public results page: https://pinklady.tail3b2c97.ts.net (Cockpit at `/camunda`, read-only `guest` login). Setup, access, updating and troubleshooting: [`deploy/README.md`](deploy/README.md).

## Tests

```bash
cd service && uv run python -m unittest -v
```

The tests cover:

- the facts the tables read: the notice covering a delay and how long before it was announced (including the example from #6), the weather at the delay, the delay a claim matches
- the start variables (one function for live intake and for `replay`): receipt, days to claim in Copenhagen days, receipt used on an earlier claim
- the workers of all three processes, including the retry while a delay is not yet assessed and the duplicate-payout guard
- the bridge over HTTP: intake, `/appeals`, the `/replay` endpoints, the TO-BE page with its AS-IS comparison
- the models: activity names and topics match `activities.py`, every table input is a variable the processes set, and the processdoctor rules hold for the `*.processdoctor.bpmn` files and `passenger-journey.bpmn`
- no email for replayed claims

## Regenerating the models

- `uv run --project service python tools/build_to_be_bpmn.py` regenerates the BPMN files in `models/to-be/` (the executable processes, their processdoctor variants, the collaboration overview and the passenger journey). The activity names come from `service/rejsegaranti/activities.py`. If you edit a model in Camunda Modeler, stop regenerating it, or port the change to the generator.
- `python3 tools/build_to_be_dmn.py` regenerates `models/to-be/travel-guarantee-to-be.dmn`, the TO-BE decision tables.
- After a change, run `uv run rejsegaranti deploy` and `uv run rejsegaranti replay` to decide the stored claims again.
- The PDFs and SVGs of the BPMN models were rendered with bpmn-js. To redo them, open the `.bpmn` in Camunda Modeler and use Export as PNG/SVG.

## Known gaps

- **Real delay data for Line L:** none is published. We looked on Dataudveksleren (7 November 2026) and found no Letbane dataset, so the server uses real delays of Copenhagen's buses and S-trains instead (see `deploy/README.md`, "Why Copenhagen's buses and S-trains"). Locally, delays are simulated unless you pass `--delays file.csv`.
- **Line L station coordinates** in `service/rejsegaranti/stations.py` are approximate (about ±100 m); the Copenhagen lines' come from GTFS (`data/stations.csv`). They only decide which delay a claim's trip matches.
- **processdoctor.dtu.dk** check: still to do by hand (upload the `*.processdoctor.bpmn` files and the exported XES).
- **Fresh start:** the bridge keeps its state in `out/registry.json`. To decide everything again, use `replay`. To start from nothing, delete the file and run `docker compose down` for Camunda before re-sending the same simulated scenario. Otherwise delays are deduplicated and repeated claims get HTTP 409.

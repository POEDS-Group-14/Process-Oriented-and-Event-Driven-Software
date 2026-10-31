# Process 1 – Rejsegaranti claim (Line L)

Owner and author of every model in this folder: **Liam Palmqvist**.
Issues: #1, with sub-issues #7 AS-IS, #8 TO-BE, #9 Camunda, #10 DCR and #11 event log.

| Issue | What | Where |
|---|---|---|
| #7 | AS-IS BPMN | `models/rejsegaranti-as-is.bpmn` / `.pdf`, `docs/as-is.md` |
| #8 | TO-BE BPMN and redesign rationale | `models/rejsegaranti-to-be.bpmn` / `.pdf`, `docs/to-be-redesign.md` |
| #9 | Runs in Camunda: started by CEP situations, outcomes sent back to the CEP, simulated claims | `service/`, `docker-compose.yml`, `docs/cep-interface.md` |
| #10 | DCR compliance rules | `models/rejsegaranti-rules.dcr.xml` / `.pdf`, `docs/dcr-rules.md` |
| #11 | Event log export and conformance pre-check | `service/rejsegaranti/export.py`, `docs/conformance.md` |

## Requirements

- Python 3.9 or newer. Only the standard library is needed.
- Docker, for Camunda 7 Run. Alternatively, download Camunda 7 Run 7.24 (needs Java 17+) and start it on port 8080.

## Run it

```bash
docker compose up -d                  # Camunda at http://localhost:8080/camunda (demo/demo)
cd service
python3 -m rejsegaranti deploy        # deploys models/rejsegaranti-to-be.bpmn (both processes)
python3 -m rejsegaranti run           # bridge on :8090 + external-task workers (leave running)

# in a second terminal
cd service
python3 -m rejsegaranti simulate --post     # simulated delays, notices, weather and claims
python3 -m rejsegaranti export              # Camunda history -> ../out/claims-camunda.xes
python3 -m rejsegaranti check ../out/claims-camunda.xes -v
```

Settings, all optional:

- `CAMUNDA_URL` (default `http://localhost:8080/engine-rest`)
- `CEP_URL`, the Siddhi HTTP source for outcome events
- `BRIDGE_PORT` (default 8090)
- `DATA_DIR` (default `out/`)

Without Camunda, run `python3 -m rejsegaranti simulate --offline [--noise 0.1]`. It runs the same rules and writes an XES log with the same activity names.

## Tests

```bash
cd service && python3 -m unittest -v
```

The tests cover:

- the rules: 30 min, 300 kr, 250 m, receipt, notice by the day before (including the example from #6), DMI/OWM force majeure
- the workers, including the retry while a delay case is not yet assessed and the duplicate-receipt guard
- the bridge over HTTP
- the BPMN, checking that names, topics and messages match the code
- that offline traces only use paths that exist in the BPMN
- the DCR rules: a clean log conforms, and each kind of injected violation is caught

## Regenerating the models

- `python3 tools/build_bpmn.py` regenerates both BPMN files from code. If you edit a model in Camunda Modeler, stop regenerating it, or port the change to `tools/build_bpmn.py`.
- `python3 -m rejsegaranti build-dcr` regenerates the DCR XML and drawing (needs Graphviz `dot` for the PDF).
- The PDFs and SVGs of the BPMN models were rendered with bpmn-js. To redo them, open the `.bpmn` in Camunda Modeler and use Export as PNG/SVG.

## Known gaps

- **Real delay data:** Rejseplanen API 2.0 access is on request only. Until it is granted, delays are simulated. Once the recorder (#29/#35) writes `data/delays-format.csv`-style files, use `simulate --post --delays file.csv`.
- **Station coordinates** in `service/rejsegaranti/stations.py` are approximate (about ±100 m). Check them before using the 250 m rule on anything real.
- **Untested against a live Camunda engine:** the BPMN validates with bpmn-moddle and the Camunda moddle, and the REST calls follow the Camunda 7 REST API. Expect small fixes on the first live run.
- **processdoctor.dtu.dk** check: still to do by hand (upload the DCR XML and the exported XES).
- **Fresh start:** the bridge keeps its state in `out/registry.json`. Delete it, and run `docker compose down -v` for Camunda, before re-sending the same simulated scenario. Otherwise delays are deduplicated and repeated claims get HTTP 409.

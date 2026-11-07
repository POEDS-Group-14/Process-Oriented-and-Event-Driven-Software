# Process 1 - Rejsegaranti claim (Line L)

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
uv run rejsegaranti deploy        # deploys models/rejsegaranti-to-be.bpmn (both processes)
uv run rejsegaranti run           # bridge on :8090 + external-task workers (leave running)

# in a second terminal
cd service
uv run rejsegaranti simulate --post     # simulated delays, notices, weather and claims
uv run rejsegaranti export              # Camunda history -> ../out/claims-camunda.xes
uv run rejsegaranti check ../out/claims-camunda.xes -v
```

Settings, all optional:

- `CAMUNDA_PORT` (default 8085). Set it to the same value for `docker compose` and `uv run` if 8085 is taken too. `CAMUNDA_URL` overrides the whole address
- `CEP_URL`, the Siddhi HTTP source for outcome events
- `BRIDGE_PORT` (default 8090)
- `DATA_DIR` (default `out/`)

Without Camunda, run `uv run rejsegaranti simulate --offline [--noise 0.1]`. It runs the same rules and writes an XES log with the same activity names.

## Always on: the pinklady server

Liam's Mac mini (`pinklady`) runs Camunda, the bridge, an hourly feed of **simulated** delays and claims, and the weather recorder as systemd services that start on boot. Cockpit: http://pinklady:8085/camunda (home network or Tailscale). Setup, access, updating and troubleshooting: [`deploy/README.md`](deploy/README.md).

## Tests

```bash
cd service && uv run python -m unittest -v
```

The tests cover:

- the rules: 30 min, 300 kr, 250 m, receipt, notice by the day before (including the example from #6), DMI/OWM force majeure
- the workers, including the retry while a delay case is not yet assessed and the duplicate-receipt guard
- the bridge over HTTP
- the BPMN, checking that names, topics and messages match the code
- that offline traces only use paths that exist in the BPMN
- the DCR rules: a clean log conforms, and each kind of injected violation is caught

## Regenerating the models

- `uv run --project service python tools/build_bpmn.py` regenerates both BPMN files from code. If you edit a model in Camunda Modeler, stop regenerating it, or port the change to `tools/build_bpmn.py`.
- `uv run rejsegaranti build-dcr` regenerates the DCR XML and drawing (needs Graphviz `dot` for the PDF).
- The PDFs and SVGs of the BPMN models were rendered with bpmn-js. To redo them, open the `.bpmn` in Camunda Modeler and use Export as PNG/SVG.

## Known gaps

- **Real delay data:** Rejseplanen API 2.0 access is on request only. Until it is granted, delays are simulated. Once the recorder (#29/#35) writes `data/delays-format.csv`-style files, use `simulate --post --delays file.csv`.
- **Station coordinates** in `service/rejsegaranti/stations.py` are approximate (about ±100 m). Check them before using the 250 m rule on anything real.
- **processdoctor.dtu.dk** check: still to do by hand (upload the DCR XML and the exported XES).
- **Fresh start:** the bridge keeps its state in `out/registry.json`. Delete it, and run `docker compose down` for Camunda, before re-sending the same simulated scenario. Otherwise delays are deduplicated and repeated claims get HTTP 409.

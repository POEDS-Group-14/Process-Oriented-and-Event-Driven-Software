# Rejsegaranti recorder (Movia)

Author: Liam Palmqvist

Logs three data sources to disk, around the clock, for process 1 (Movia travel guarantee):

| Source | What is logged | How |
|---|---|---|
| Dataudveksleren real-time [1][6]: dataset 788 (Movia buses) and 801 (DSB S-tog) | SIRI-ET: delays, early departures, cancellations. SIRI-SX (disruption messages incl. planned changes) is parsed too, if a subscribed dataset sends it | Pushed over Azure Service Bus (AMQP 1.0) |
| OpenWeatherMap [3] | Wind speed, wind gust, rain and snow in the last hour | Polled every 10 min |
| DMI Open Data, metObs [4] | Observations from one DMI weather station | Polled every 10 min |

Everything received is stored **raw first**, then turned into CSV files. If a parser turns out to be wrong for the real data, the CSVs can be rebuilt from the raw files.

## Output

In `RECORDER_DATA_DIR` (`/var/lib/rejsegaranti-recorder` with the systemd service):

| File | Contents |
|---|---|
| `raw/<source>/YYYY-MM-DD.jsonl.gz` | Every API response or feed message, unchanged, with the time it was received. Feed messages go to `raw/dataudveksleren/<topic>/`, one folder per dataset |
| `movia-incidents.csv` | `delay` (≥ 20 min), `early_departure` (actual departure > 2 min early), `cancelled` |
| `movia-situations.csv` | Disruption messages with `planned`, validity period, lines, stops and **`firstSeen`** |
| `weather-owm.csv` | `wind_speed`, `wind_gust`, `rain_1h`, `snow_1h` (same names as the force-majeure thresholds in `service/rejsegaranti/rules.py`) |
| `weather-dmi.csv` | `stationId`, `parameterId`, `value`, `observed` |
| `stop-names.json` | Movia stop number (`StopPointRef`) to stop name. Movia sends names only when it sends a journey in full, so the recorder learns them from those messages and fills in the rest; the stops in `../data/stations.csv` are known from the start |
| `recorder.sqlite` | The same rows as the four CSV files, in tables `movia_incidents`, `movia_situations`, `weather_owm` and `weather_dmi` |

The SQLite database uses Python's built-in `sqlite3`, so nothing extra is installed. Each table has a unique key, so the same observation is never stored twice, even after a restart. You can query it while the recorder is running:

```bash
cd /opt/rejsegaranti/recorder
sudo -u rejsegaranti env RECORDER_DATA_DIR=/var/lib/rejsegaranti-recorder python3 -m recorder sql \
  "SELECT line, stopName, minutes, timeKind, aimedTime FROM movia_incidents WHERE type='delay' ORDER BY aimedTime DESC LIMIT 20"

sudo -u rejsegaranti env RECORDER_DATA_DIR=/var/lib/rejsegaranti-recorder python3 -m recorder sql \
  "SELECT observedAt, wind_speed, wind_gust, rain_1h FROM weather_owm WHERE wind_gust >= 32.7 OR rain_1h >= 30"
```

Or copy `recorder.sqlite` home and open it in any SQLite tool, such as DB Browser for SQLite.

**Sending to the process.** With `BRIDGE_URL` and `BRIDGE_LINES` set (the server sets them in the systemd unit), the recorder also acts as the CEP for process 1: every journey on those lines (`*`: every line) that runs 20+ minutes late becomes one delay situation on the bridge (`POST /cep/delay`), and every OpenWeatherMap observation a weather situation (`POST /cep/weather`). Stops are named by station (the stop name without the street in brackets). The bridge applies the rules; see `cep.py`.

How this feeds the models:

- **Delays and early departures** → the DMN input *Incident* ("delay over 20 min", "left over 2 min early").
- **`timeKind`** says whether a delay is `expected` (a prediction) or `actual`. The guarantee says a predicted +20 min on Rejseplan is not enough on its own [5], so the claim check should look at the actual time.
- **`firstSeen` of a planned change** → the DMN input *Planned change announced 24 h before*. It is the time *we* first saw the message, not when Movia published it.
- **Weather** → the DMN input *Cause of disruption* (force majeure), using the thresholds and their justification in `docs/to-be-redesign.md`.
- **Not in these feeds:** whether the passenger was visible to the driver, whether the bus was full, and whether a stop was requested. Those come from the claim.

## Install on a Debian or Ubuntu server

The recorder is installed together with the rest of process 1, as the systemd service `rejsegaranti-recorder` (see "Always on: the pinklady server" in `../README.md`). From the laptop:

```bash
../deploy/install.sh liam@pinklady.local
```

On the first install, `deploy/recorder.env` is copied to `/etc/rejsegaranti-recorder.env` (mode 600), which the service reads. Edit that file on the server to change settings, then `sudo systemctl restart rejsegaranti-recorder`. It uses the system `python3`; the Dataudveksleren feed needs the Azure SDK, which `setup-server.sh` installs from Debian (`python3-azure`). Elsewhere, install the package's `siri` extra.

To test by hand before or after installing:

```bash
cd /opt/rejsegaranti/recorder
sudo -u rejsegaranti env RECORDER_ENV_FILE=/etc/rejsegaranti-recorder.env RECORDER_DATA_DIR=/tmp/recorder-test python3 -m recorder check
sudo -u rejsegaranti env RECORDER_ENV_FILE=/etc/rejsegaranti-recorder.env RECORDER_DATA_DIR=/tmp/recorder-test python3 -m recorder once
sudo journalctl -u rejsegaranti-recorder -f      # live log of the service
```

`deploy/recorder.env` is listed in `.gitignore`, so the key is not committed to the group's repository. Settings already set in the environment override the file.

**Connecting Dataudveksleren.** Log in to the portal, create a service account (profile, Service Accounts) and note its client ID and password; the password is shown only once. Then, for each dataset, associate the service account and subscribe ("Subscribe to events"). The popup gives the tenant ID and the dataset's AMQP url. Put them in the settings file:

```
SB_AMQP_URLS=amqps://sb-duvproddistribution.servicebus.windows.net/t-distribution-788/<subscription>,amqps://.../t-distribution-801/<subscription>
AZURE_TENANT_ID=<tenant ID>
AZURE_CLIENT_ID=<service account client ID>
AZURE_CLIENT_SECRET=<service account password>
```

The recorder runs one receiver per url. Messages wait on the subscription while the recorder is down, and a message is only removed from it after it has been stored. Vejdirektoratet runs Dataudveksleren; its listing gives vd@vd.dk, subject "Spørgsmål angående Dataudveksleren", for questions [1].

**Keep the Mac awake.** A laptop that suspends stops recording. On Ubuntu:

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
# and in /etc/systemd/logind.conf: HandleLidSwitch=ignore, then: sudo systemctl restart systemd-logind
```

**Getting the data home.** Copy the folder (including `recorder.sqlite`) to your own machine with, for example, `rsync -av liam@pinklady.local:/var/lib/rejsegaranti-recorder/ ./recorder-data/`.

## Tests

```bash
python3 -m unittest -v tests.test_recorder
```

The tests use **hand-written format examples, not real Movia data**. Add a few real messages from `raw/dataudveksleren/` as test cases.

## What is checked and what is not

Checked:

- The OpenWeatherMap URL, parameters and field names against its API documentation [3].
- The DMI base URL and rate limit against DMI's documentation [4]. DMI's documentation says a key is needed, but metObs answers without one, so the key is optional.
- The Dataudveksleren connection, live, on 7 November 2026: both datasets connect, and their messages are SIRI-ET (`EstimatedTimetableDelivery`, subscription refs `movia-et` and `dsb-s-tog-et`), sent as `text/xml`. The parser turned them into delay and cancellation rows.
- The SIRI parsing against SIRI's standard element names, with the format examples in the tests.
- OpenWeatherMap and DMI, live, on the same day.

**Not** checked, because it needs your accounts:

- **SIRI-SX.** In the first messages, neither dataset sent SIRI-SX (disruption messages). If none arrives, planned changes need another dataset or source.
- **Whether every parsed field is right for real data.** For example, whether `timeKind` (expected vs actual) is filled the way the claim check assumes. Check real rows against the raw messages before relying on them.
- **DMI weather warnings.** I could not confirm that DMI Open Data offers warnings through an API, so only station observations are logged. The force-majeure rule prefers DMI warnings, so this gap needs a decision: find a warnings source, or justify using observations and OpenWeatherMap.

## References

[1] Vejdirektoratet. *Dataudveksleren* (Denmark's National Access Point for traffic data). Listing: https://www.opendata.dk/andres-data/dataudveksleren. Portal: https://du-portal-ui.dataudveksler.app.vd.dk/ (accessed 3 November 2026).

[2] marcin-sieradzki/mylder, GitHub issue #5, *How SIRI-ET and NeTEx are delivered and joined*. https://github.com/marcin-sieradzki/mylder/issues/5 (accessed 3 November 2026). Third-party source.

[3] OpenWeather. *Current weather data.* https://openweathermap.org/current (accessed 3 November 2026).

[4] DMI. *Open Data documentation: Basics.* https://opendatadocs.dmi.govcloud.dk/Basics, and *FAQ*, https://opendatadocs.dmi.govcloud.dk/FAQ (accessed 3 November 2026).

[5] Movia travel guarantee terms, as quoted in `Project/1. Rejsegaranti claim.md`.

[6] Vejdirektoratet. *AMQP - Receive messages* and *Use of services via the web portal*, Dataudveksler Confluence. https://vejdirektoratet.atlassian.net/wiki/spaces/DP/pages/3236430385/AMQP+-+Receive+messages (accessed 7 November 2026).

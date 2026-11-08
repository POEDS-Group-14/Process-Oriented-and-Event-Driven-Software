# Process 1 on the pinklady server

Author: Liam Palmqvist

Process 1 (rejsegaranti) runs around the clock on Liam's Mac mini, `pinklady` (Debian 13). Camunda, the bridge, a feed of simulated claims and the recorder of real-time data run as four systemd services. They start on boot without anyone logging in, run in the background, and restart after an error.

**Real delays, simulated claims, and bus 150S instead of Line L.** The cases in Camunda start from real delays: the recorder reads Movia's real-time feed and reports every 150S journey that runs 20+ minutes late to the bridge, which starts a case at 30+ minutes. Force majeure is decided on the real OpenWeatherMap weather. The claims are simulated, because there is no public claims data, so don't present the outcomes as findings about real passengers.

### Why bus 150S

The process is designed for Line L (Hovedstadens Letbane), but no real-time data for Line L is published. On 7 November 2026 we searched for it: the two Dataudveksleren datasets we subscribed to carry Movia's buses (788, operator `Movia`, 169 lines and no `L`) and DSB S-tog (801, lines A, B, C and F). A third-party read of the Dataudveksleren catalogue (31 October 2026) found no Letbane or Metro dataset either [1], and we found none. So the server runs the same process and rules on Movia bus **150S** (Kokkedal - Lundtofte - DTU - Nørreport). It is a high-frequency S-bus, it shares the Letbane's DTU and Lundtofte stops, and it had the most journeys of the candidate lines in the first data. The rules (30 minutes, 300 kr, 250 m, notices, force majeure) are still Line L's [2]; Movia's own bus guarantee differs. The BPMN, DCR and activity names still say Line L.

The line is set in three units: `REJSEGARANTI_LINE=150S` in the bridge and feed units, and `BRIDGE_LINES=150S` in the recorder unit. Its stops and coordinates come from Rejseplanen's GTFS feed, in `data/stations-150S.csv` (made by `tools/build_stations.py`). The two sides of the road share a station name, and the 250 m rule measures to the closer one.

[1] marcin-sieradzki/mylder, GitHub issues #146 and #158, https://github.com/marcin-sieradzki/mylder/issues/146 (accessed 7 November 2026). Third-party source.
[2] https://dinletbane.dk/da/kontakt-og-kundeservice/rejsegaranti/, as summarised in `service/rejsegaranti/rules.py`.

## What runs

| Service | What it does | Data |
|---|---|---|
| `rejsegaranti-camunda` | Camunda 7 Run 7.24 on port 8085 (8080 is taken on pinklady), with webapps and REST API | H2 database in `/opt/camunda/camunda-h2-default` (includes the history, which is the event log) |
| `rejsegaranti-bridge` | Waits for Camunda, deploys `models/rejsegaranti-to-be.bpmn` (nothing happens if it is unchanged), then runs the CEP bridge on port 8090, the results page at `/results` and the external-task workers | `/var/lib/rejsegaranti` (`registry.json`, `cep-events.jsonl`) |
| `rejsegaranti-feed` | `rejsegaranti feed --real --interval 300`: every 5 minutes, simulates claims for the real delays the bridge has received, once each delay has played out (40 minutes after the delayed departure) | None of its own. Everything goes through the bridge |
| `rejsegaranti-recorder` | The recorder (`../recorder`): **real** Movia bus and DSB S-tog delays and cancellations from Dataudveksleren (datasets 788 and 801), plus OpenWeatherMap and DMI every 10 minutes. Sends 150S delays and the OpenWeatherMap weather to the bridge | `/var/lib/rejsegaranti-recorder` |

All four run as the system user `rejsegaranti` and use the system `python3`. The code has no third-party dependencies, so nothing is installed with pip or uv. The code lives in `/opt/rejsegaranti`, owned by root and read-only to the services.

A real delay's ID is the line and Movia's journey reference, such as `150S|2026-11-07:15000062-8601650400000`: one case per delayed journey. Its simulated claims' IDs start with a hash of that, and the claims depend only on the delay, so after a restart the feed sends the same claims again and the bridge refuses them as duplicates (HTTP 409). Cases only start when a 150S bus actually runs 30+ minutes late, so there may be none for hours, especially at night.

Without `--real`, the feed makes up 5 delays an hour instead, with simulated notices and weather; the IDs then start with the time of the batch.

The services start in order: Camunda, then the bridge, then the feed. On this machine Camunda takes a minute or two to start, and the bridge and the feed wait for it.

## Opening it

From the home network, or from anywhere through Tailscale. pinklady is on Liam's tailnet as `pinklady` (100.84.116.80).

| What | Address |
|---|---|
| Camunda Cockpit and Tasklist | http://pinklady:8085/camunda |
| Camunda REST API | http://pinklady:8085/engine-rest |
| Bridge state (delays, claims, outcomes) | http://pinklady:8090/state |
| Results page | http://pinklady:8090/results |

The Camunda login is `admin`. The password was generated at install time and is stored on the server:

```bash
ssh pinklady 'sudo cat /etc/rejsegaranti/camunda-admin'
```

On the home network, `pinklady.local` works too. Away from home, your device must have Tailscale switched on.

**The REST API and the bridge have no login.** Anyone who can reach ports 8085 and 8090 can start and complete process instances. That's acceptable on the home network and the tailnet, with simulated data. Never put them on Tailscale Funnel or a port forward.

## Sharing with people outside the tailnet

Tailscale Funnel publishes exactly two things on the public internet, over HTTPS:

| Public address | What | Login |
|---|---|---|
| https://pinklady.tail3b2c97.ts.net/ | Results page: claim outcomes, rejection reasons, latest claims. Read-only, no passenger IDs, refreshes every minute | None |
| https://pinklady.tail3b2c97.ts.net/camunda | Camunda Cockpit | `guest` (read-only) or `admin` |

Everything else on that address (the REST API, `/state`, the POST endpoints) answers 404.

Give people the `guest` login. Its password is on the server:

```bash
ssh pinklady 'sudo cat /etc/rejsegaranti/camunda-guest'
```

`guest` can open Cockpit and read the process definitions, running instances and history. It cannot deploy, suspend, cancel, change variables or open Tasklist and Admin. That holds because Camunda's authorization checks are switched on (`camunda.bpm.authorization.enabled` in `/opt/camunda/configuration/default.yml`). The bridge and workers are not affected, because they call the REST API without logging in, which skips the checks. Keep the `admin` password to yourself.

To take it offline: `sudo tailscale funnel reset`. Running `install.sh` again turns it back on. `tailscale funnel status` shows what is published.

## Getting the event log

Export straight from the laptop. The export only reads Camunda's REST API:

```bash
cd processes/p1-rejsegaranti/service
CAMUNDA_URL=http://pinklady:8085/engine-rest uv run rejsegaranti export
uv run rejsegaranti check ../out/claims-camunda.xes -v
```

The weather data:

```bash
rsync -av pinklady:/var/lib/rejsegaranti-recorder/ ./recorder-data/
```

## Installing and updating

Run from the laptop, in `processes/p1-rejsegaranti`:

```bash
deploy/install.sh liam@pinklady.local
```

It copies this folder to `~/rejsegaranti-src` on the server, then runs `deploy/setup-server.sh` there with sudo, which asks for the password once. The setup can run again safely. It updates the code and restarts the services, but keeps Camunda's database, the bridge's state and the recorder's data.

On the first run it also:

- installs `openjdk-21-jre-headless`
- creates the `rejsegaranti` user
- downloads Camunda Run 7.24 to `/opt/camunda` and sets port 8085, the admin password and no demo invoice process
- turns on Camunda's authorization checks and creates the read-only `guest` user
- publishes the results page and Cockpit through Tailscale Funnel
- copies `recorder/deploy/recorder.env`, which holds the OpenWeatherMap key, to `/etc/rejsegaranti-recorder.env` (mode 600). It is not copied again later, so change recorder settings in that file on the server

Run the tests (`cd service && uv run python -m unittest`) before deploying.

| File | What |
|---|---|
| `install.sh` | Run on the laptop: copies the code and starts the setup |
| `setup-server.sh` | Run on the server as root: installs, updates, restarts |
| `rejsegaranti-camunda.service`, `rejsegaranti-bridge.service`, `rejsegaranti-feed.service` | systemd units, installed to `/etc/systemd/system/` |
| `../recorder/deploy/rejsegaranti-recorder.service` | The recorder's unit, installed the same way |

## Day to day

On the server (`ssh pinklady`):

```bash
systemctl status 'rejsegaranti-*'                 # are they running?
sudo journalctl -u rejsegaranti-bridge -f         # live log; also -feed, -camunda, -recorder
sudo systemctl restart rejsegaranti-bridge        # restart one service
sudo systemctl stop rejsegaranti-feed             # pause the simulated claims (starts again on reboot)
sudo systemctl disable --now rejsegaranti-feed    # stop it for good
```

Quick health check (incidents should be 0):

```bash
curl -s localhost:8085/engine-rest/incident/count
curl -s 'localhost:8085/engine-rest/history/process-instance/count?finished=true'
```

To change the line, set it in the three units, make its station file with `tools/build_stations.py`, and run `install.sh` again. To go back to fully simulated data, remove `--real` from `rejsegaranti-feed.service`.

## Starting over with empty data

```bash
sudo systemctl stop rejsegaranti-feed rejsegaranti-bridge rejsegaranti-camunda
sudo rm -rf /opt/camunda/camunda-h2-default /var/lib/rejsegaranti/registry.json
sudo systemctl start rejsegaranti-camunda rejsegaranti-bridge rejsegaranti-feed
```

Always delete both. Camunda's history and the bridge's registry belong together. The `guest` user is stored in Camunda's database too, so run `install.sh` afterwards to recreate it (with the same password). The recorder's data in `/var/lib/rejsegaranti-recorder` is separate. Leave it alone unless you want to lose the weather history.

## Removing it

```bash
sudo tailscale funnel reset
sudo systemctl disable --now rejsegaranti-feed rejsegaranti-bridge rejsegaranti-camunda rejsegaranti-recorder
sudo rm /etc/systemd/system/rejsegaranti-*.service && sudo systemctl daemon-reload
sudo rm -rf /opt/rejsegaranti /opt/camunda /etc/rejsegaranti /etc/rejsegaranti-recorder.env
sudo rm -rf /var/lib/rejsegaranti /var/lib/rejsegaranti-recorder    # the data
sudo userdel rejsegaranti
```

## Troubleshooting

| Symptom | Where to look |
|---|---|
| Bridge stuck in `activating` | It is waiting for Camunda. Check `journalctl -u rejsegaranti-camunda` and `/opt/camunda/logs/` |
| `status=200/CHDIR` in the log | `/opt/rejsegaranti` lost its read permission. Run `install.sh` again |
| Incidents in Cockpit | See "Where bugs are most likely" in `../RUN_CAMUNDA.md` |
| `dmi: URLError` in the recorder log | DMI's API host. The old `dmigw.govcloud.dk` no longer resolves, so the recorder uses `opendataapi.dmi.dk` (`../recorder/recorder/dmi.py`). If that moves too, update `BASE` there |
| Can't reach `pinklady` away from home | Switch Tailscale on, on your device. `tailscale status` should list pinklady |

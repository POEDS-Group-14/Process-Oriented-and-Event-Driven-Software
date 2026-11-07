# Process 1 on the pinklady server

Author: Liam Palmqvist

Process 1 (rejsegaranti) runs around the clock on Liam's Mac mini, `pinklady` (Debian 13). Camunda, the bridge, a feed of simulated data and the weather recorder run as four systemd services. They start on boot without anyone logging in, run in the background, and restart after an error.

**All delays and claims on this server are simulated.** We have no Rejseplanen API key, so `rejsegaranti feed` makes them up. Only the weather (OpenWeatherMap and DMI) is real. Don't present anything from this server as a finding about real Line L delays.

## What runs

| Service | What it does | Data |
|---|---|---|
| `rejsegaranti-camunda` | Camunda 7 Run 7.24 on port 8085 (8080 is taken on pinklady), with webapps and REST API | H2 database in `/opt/camunda/camunda-h2-default` (includes the history, which is the event log) |
| `rejsegaranti-bridge` | Waits for Camunda, deploys `models/rejsegaranti-to-be.bpmn` (nothing happens if it is unchanged), then runs the CEP bridge on port 8090 and the external-task workers | `/var/lib/rejsegaranti` (`registry.json`, `cep-events.jsonl`) |
| `rejsegaranti-feed` | `rejsegaranti feed --batch 5 --interval 3600`: every hour, posts 5 simulated delays from the past hour to the bridge, with their notices, weather situations and claims | None of its own. Everything goes through the bridge |
| `rejsegaranti-recorder` | The weather recorder (`../recorder`): OpenWeatherMap and DMI every 10 minutes. Dataudveksleren (Movia) is not connected | `/var/lib/rejsegaranti-recorder` |

All four run as the system user `rejsegaranti` and use the system `python3`. The code has no third-party dependencies, so nothing is installed with pip or uv. The code lives in `/opt/rejsegaranti`, owned by root and read-only to the services.

The feed's IDs start with the time of the batch, such as `20261107223335-C0004`. Batches therefore never clash with each other, and nothing clashes when the feed restarts.

The services start in order: Camunda, then the bridge, then the feed. On this machine Camunda takes a minute or two to start, and the bridge and the feed wait for it.

## Opening it

From the home network, or from anywhere through Tailscale. pinklady is on Liam's tailnet as `pinklady` (100.84.116.80).

| What | Address |
|---|---|
| Camunda Cockpit and Tasklist | http://pinklady:8085/camunda |
| Camunda REST API | http://pinklady:8085/engine-rest |
| Bridge state (delays, claims, outcomes) | http://pinklady:8090/state |

The Camunda login is `admin`. The password was generated at install time and is stored on the server:

```bash
ssh pinklady 'sudo cat /etc/rejsegaranti/camunda-admin'
```

On the home network, `pinklady.local` works too. Away from home, your device must have Tailscale switched on.

**The REST API and the bridge have no login.** Anyone who can reach ports 8085 and 8090 can start and complete process instances. That's acceptable on the home network and the tailnet, with simulated data. Don't expose them to the internet with Tailscale Funnel or port forwarding.

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
sudo systemctl stop rejsegaranti-feed             # pause the simulated data (starts again on reboot)
sudo systemctl disable --now rejsegaranti-feed    # stop it for good
```

Quick health check (incidents should be 0):

```bash
curl -s localhost:8085/engine-rest/incident/count
curl -s 'localhost:8085/engine-rest/history/process-instance/count?finished=true'
```

To change how much data the feed sends, edit `--batch` and `--interval` in `rejsegaranti-feed.service` and run `install.sh` again.

## Starting over with empty data

```bash
sudo systemctl stop rejsegaranti-feed rejsegaranti-bridge rejsegaranti-camunda
sudo rm -rf /opt/camunda/camunda-h2-default /var/lib/rejsegaranti/registry.json
sudo systemctl start rejsegaranti-camunda rejsegaranti-bridge rejsegaranti-feed
```

Always delete both. Camunda's history and the bridge's registry belong together. The recorder's data in `/var/lib/rejsegaranti-recorder` is separate. Leave it alone unless you want to lose the weather history.

## Removing it

```bash
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

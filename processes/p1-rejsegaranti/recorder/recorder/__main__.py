"""Movia travel guarantee recorder.

  python -m recorder check          show which sources are configured
  python -m recorder once           fetch OpenWeatherMap and DMI once (tests the API keys)
  python -m recorder dmi-stations   list DMI stations near RECORDER_LAT/LON (to pick DMI_STATION_ID)
  python -m recorder run            log all configured sources until stopped (used by systemd)
  python -m recorder sql "SELECT ..."   query recorder.sqlite (read-only)

Output (RECORDER_DATA_DIR):
  raw/<source>/YYYY-MM-DD.jsonl.gz   every API response / feed message, unchanged
  weather-owm.csv                    OpenWeatherMap metrics
  weather-dmi.csv                    DMI station observations
  movia-incidents.csv                delays >= 20 min, early departures > 2 min, cancellations (from SIRI-ET)
  movia-situations.csv               disruption messages incl. planned changes (from SIRI-SX), with firstSeen
  recorder.sqlite                    the same rows in tables weather_owm, weather_dmi, movia_incidents, movia_situations
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading

from . import dmi, owm, siri
from .config import Config, load_env_file
from .http import HttpError
from .store import Store, now_utc

log = logging.getLogger("recorder")


class Recorder:
    def __init__(self, cfg: Config):
        self.cfg, self.store = cfg, Store(cfg.data_dir)
        self.stop = threading.Event()
        self._dmi_seen: set = set()
        self._incidents: dict = {}  # incidentId -> (minutes, timeKind) last written
        self._first_seen_path = os.path.join(cfg.data_dir, "situations-first-seen.json")
        try:
            with open(self._first_seen_path, encoding="utf-8") as fh:
                self._first_seen = json.load(fh)
        except (OSError, ValueError):
            self._first_seen = {}

    # ---- weather -----------------------------------------------------------
    def owm_once(self) -> None:
        data = owm.fetch(self.cfg.owm_api_key, self.cfg.lat, self.cfg.lon)
        t = now_utc()
        self.store.raw("owm", data, t)
        self.store.rows("weather-owm.csv", owm.FIELDS, [owm.to_row(data, t)])

    def dmi_once(self) -> int:
        data = dmi.fetch_observations(self.cfg.dmi_api_key, self.cfg.dmi_station_id,
                                      now_utc() - dmi.lookback(self.cfg.dmi_interval_s))
        t = now_utc()
        self.store.raw("dmi", data, t, {"stationId": self.cfg.dmi_station_id})
        rows = []
        for r in dmi.to_rows(data, t):
            key = (r["stationId"], r["parameterId"], r["observed"])
            if key not in self._dmi_seen:
                self._dmi_seen.add(key)
                rows.append(r)
        return self.store.rows("weather-dmi.csv", dmi.FIELDS, rows)

    def _poll(self, name, fn, interval):
        while not self.stop.is_set():
            try:
                fn()
                log.info("%s: ok", name)
            except HttpError as e:
                log.warning("%s: %s", name, e)
            except Exception as e:  # keep the recorder alive on network errors
                log.warning("%s: %s: %s", name, type(e).__name__, e)
            self.stop.wait(interval)

    # ---- Movia real-time (Dataudveksleren) ---------------------------------
    def handle_siri(self, text: str, meta: dict) -> None:
        t = now_utc()
        self.store.raw("dataudveksleren", text, t, meta)  # always keep the original first
        try:
            kind = siri.message_kind(text)
        except Exception as e:
            log.warning("message %s is not XML: %s", meta.get("messageId"), e)
            return
        if kind == "ET":
            rows = [r for r in siri.parse_et(text, t, self.cfg.delay_threshold_min, self.cfg.early_threshold_min)
                    if self._incident_changed(r)]
            self.store.rows("movia-incidents.csv", siri.INCIDENT_FIELDS, rows)
        elif kind == "SX":
            rows = siri.parse_sx(text, t)
            changed = False
            for r in rows:
                key = f"{r['situationNumber']}|{r['version']}"
                if key not in self._first_seen:
                    self._first_seen[key] = r["receivedAt"]
                    changed = True
                r["firstSeen"] = self._first_seen[key]
            if changed:
                tmp = self._first_seen_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self._first_seen, fh)
                os.replace(tmp, self._first_seen_path)
            self.store.rows("movia-situations.csv", siri.SITUATION_FIELDS, rows)

    def _incident_changed(self, r: dict) -> bool:
        """Write an incident the first time, when a prediction becomes an actual time, or when it moves 5+ min."""
        prev = self._incidents.get(r["incidentId"])
        cur = (r["minutes"], r["timeKind"])
        if prev is not None and prev[1] == cur[1] and (
                cur[0] == "" or abs(float(cur[0]) - float(prev[0] or 0)) < 5):
            return False
        self._incidents[r["incidentId"]] = cur
        return True

    # ---- run ---------------------------------------------------------------
    def run(self) -> None:
        threads = []
        c = self.cfg
        if c.owm_api_key:
            threads.append(threading.Thread(target=self._poll, args=("owm", self.owm_once, c.owm_interval_s), daemon=True))
        if c.dmi_station_id:
            threads.append(threading.Thread(target=self._poll, args=("dmi", self.dmi_once, c.dmi_interval_s), daemon=True))
        if (c.sb_connection_string or c.sb_namespace) and (c.sb_queue or (c.sb_topic and c.sb_subscription)):
            from .servicebus import receive_forever
            threads.append(threading.Thread(target=receive_forever, args=(c, self.handle_siri, self.stop), daemon=True))
        for p in c.problems():
            log.warning("not running: %s", p)
        if not threads:
            log.error("nothing to run: no source is configured")
            sys.exit(2)
        for th in threads:
            th.start()
        log.info("recording to %s", c.data_dir)
        self.stop.wait()
        for th in threads:
            th.join(timeout=60)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="recorder", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["check", "once", "dmi-stations", "run", "sql"])
    ap.add_argument("query", nargs="?", help="SQL for the sql command")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    env_file = load_env_file()
    cfg = Config()
    if a.command == "check":
        problems = cfg.problems()
        print("settings file:", env_file or "(none found, using environment only)")
        print("data dir:", cfg.data_dir)
        print("\n".join("- " + p for p in problems) if problems else "all sources configured")
        return
    if a.command == "sql":
        import sqlite3
        from .store import DB_NAME
        path = os.path.join(cfg.data_dir, DB_NAME)
        if not os.path.exists(path):
            sys.exit(f"no database at {path} yet")
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        cur = db.execute(a.query or "SELECT name FROM sqlite_master WHERE type='table'")
        print(" | ".join(d[0] for d in cur.description or []))
        for row in cur:
            print(" | ".join("" if v is None else str(v) for v in row))
        return
    if a.command == "dmi-stations":
        data = dmi.fetch_stations_near(cfg.dmi_api_key, cfg.lat, cfg.lon)
        for f in data.get("features") or []:
            p = f.get("properties") or {}
            print(p.get("stationId"), "|", p.get("name"), "|", p.get("status", ""), "|", ", ".join(p.get("parameterId") or [])[:120])
        return
    rec = Recorder(cfg)
    if a.command == "once":
        for name, ready, fn in (("OpenWeatherMap", cfg.owm_api_key, rec.owm_once),
                                ("DMI", cfg.dmi_station_id, rec.dmi_once)):
            if not ready:
                print(f"{name}: not configured")
                continue
            try:
                fn()
                print(f"{name}: ok")
            except Exception as e:
                print(f"{name}: FAILED: {e}")
        return
    signal.signal(signal.SIGTERM, lambda *_: rec.stop.set())
    try:
        rec.run()
    except KeyboardInterrupt:
        rec.stop.set()


if __name__ == "__main__":
    main()

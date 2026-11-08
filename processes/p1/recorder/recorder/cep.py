"""Send what the recorder sees to the process-1 bridge (service/rejsegaranti/bridge.py), in the CEP's place:

- a delay situation (POST /cep/delay) per delayed journey on the configured lines, sent again whenever it
  reaches more stops or its delay moves 5+ minutes (the bridge starts one case per journey and keeps the
  latest stops and minutes);
- every OpenWeatherMap observation as a weather situation (POST /cep/weather), valid until the next one.

Stops are named by station, as in data/stations-<line>.csv: the stop name without the street in brackets.
The bridge applies the rules (30+ minutes, force-majeure thresholds); this only reports.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request
from datetime import datetime, timedelta

log = logging.getLogger("recorder.cep")


def station_name(stop_name: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", stop_name).strip()


def post(base: str, path: str, body: dict) -> None:
    req = urllib.request.Request(base.rstrip("/") + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10):
        pass


class DelayTracker:
    """Folds incident rows (siri.parse_et) into one delay situation per journey."""

    def __init__(self, lines):
        self.lines = {str(x).upper() for x in lines}
        self.journeys: dict = {}  # delayId -> situation
        self.sent: dict = {}      # delayId -> (minutes, number of stops) last sent

    def update(self, rows: list) -> list:
        """Situations that are new or changed enough to send."""
        touched = []
        for r in rows:
            if (r["type"] != "delay" or str(r["line"]).upper() not in self.lines or r["minutes"] == ""
                    or not r["aimedTime"]):
                continue
            key = f"{r['line']}|{r['journeyRef']}"
            s = self.journeys.setdefault(key, {
                "delayId": key, "line": r["line"], "stops": [], "plannedDeparture": r["aimedTime"],
                "delayMinutes": 0.0, "detectedAt": r["receivedAt"], "timeKind": "expected",
                "source": "Dataudveksleren", "cause": None})
            stop = station_name(r["stopName"])
            if stop and stop not in s["stops"]:
                s["stops"].append(stop)
            s["plannedDeparture"] = min(s["plannedDeparture"], r["aimedTime"])
            s["delayMinutes"] = max(s["delayMinutes"], float(r["minutes"]))
            if r["timeKind"] == "actual":
                s["timeKind"] = "actual"
            if key not in touched:
                touched.append(key)
        out = []
        for key in touched:
            s = self.journeys[key]
            prev = self.sent.get(key)
            if prev is None or len(s["stops"]) > prev[1] or abs(s["delayMinutes"] - prev[0]) >= 5:
                out.append(dict(s, stops=list(s["stops"])))
        return out

    def mark_sent(self, s: dict) -> None:
        self.sent[s["delayId"]] = (s["delayMinutes"], len(s["stops"]))

    def forget_before(self, cutoff: datetime) -> None:
        """Drop journeys whose delayed departure is before cutoff, to bound memory."""
        for key in [k for k, s in self.journeys.items() if datetime.fromisoformat(s["plannedDeparture"]) < cutoff]:
            del self.journeys[key]
            self.sent.pop(key, None)


def weather_situation(row: dict, valid_s: float) -> dict:
    t = datetime.fromisoformat(row["observedAt"])
    return {"situationId": f"OWM-{row['observedAt']}", "source": "OWM",
            "metrics": {k: float(row[k] or 0) for k in ("wind_speed", "wind_gust", "rain_1h", "snow_1h")},
            "validFrom": row["observedAt"], "validTo": (t + timedelta(seconds=valid_s)).isoformat()}

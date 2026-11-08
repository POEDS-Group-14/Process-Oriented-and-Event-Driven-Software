"""Write data/stations-<line>.csv from Rejseplanen's GTFS feed (the stops of one bus line, with coordinates).

  curl -o GTFS.zip https://www.rejseplanen.info/labs/GTFS.zip
  python3 tools/build_stations.py GTFS.zip 150S

The file lists every stop point any trip of the line uses: those of the longest trip in direction 0, in
route order, then those that only direction 1 uses, then those only on shorter trips (variants). A stop point is
named by its station: the GTFS stop name without the street in brackets, "Nørre Campus (Tagensvej)"
-> "Nørre Campus". The two sides of the road then share one station name, and the 250 m rule measures
to the closer side. The recorder names stops the same way (station_name below), because Movia's
real-time feed uses the same stop names as GTFS. Its StopPointRef is the GTFS stop_id as a number
(stopRef column); the recorder uses that to name stops in updates that carry no StopPointName.
"""
from __future__ import annotations

import csv
import io
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def station_name(stop_name: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", stop_name).strip()


def main(gtfs: str, line: str, agency_name: str = "Movia") -> None:
    z = zipfile.ZipFile(gtfs)

    def rows(name):
        return csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig"))

    agencies = {a["agency_id"] for a in rows("agency.txt") if a["agency_name"] == agency_name}
    routes = {r["route_id"] for r in rows("routes.txt") if r["route_short_name"] == line and r["agency_id"] in agencies}
    if not routes:
        sys.exit(f"no {agency_name} route {line} in {gtfs}")
    trips = {t["trip_id"]: t.get("direction_id") or "0" for t in rows("trips.txt") if t["route_id"] in routes}
    calls = {}
    for st in rows("stop_times.txt"):
        if st["trip_id"] in trips:
            calls.setdefault(st["trip_id"], []).append((int(st["stop_sequence"]), st["stop_id"]))
    longest = {}
    for trip, c in calls.items():
        d = trips[trip]
        if len(c) > len(longest.get(d, [])):
            longest[d] = sorted(c)
    stops = {s["stop_id"]: s for s in rows("stops.txt")}

    out, seen = [], set()
    ordered = [sid for d in sorted(longest) for _, sid in longest[d]]
    ordered += [sid for c in calls.values() for _, sid in sorted(c)]
    for sid in ordered:
        if sid not in seen:
            seen.add(sid)
            s = stops[sid]
            ref = str(int(sid)) if sid.isdigit() else sid
            out.append((station_name(s["stop_name"]), float(s["stop_lat"]), float(s["stop_lon"]), s["stop_name"], ref))
    path = os.path.join(ROOT, "data", f"stations-{line}.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["station", "lat", "lon", "stopName", "stopRef"])
        for name, lat, lon, full, ref in out:
            w.writerow([name, f"{lat:.6f}", f"{lon:.6f}", full, ref])
    print(f"{path}: {len(out)} stop points, {len({o[0] for o in out})} stations")


if __name__ == "__main__":
    main(*sys.argv[1:3])

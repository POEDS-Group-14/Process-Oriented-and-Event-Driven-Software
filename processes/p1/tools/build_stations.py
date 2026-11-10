"""Write data/stations.csv from Rejseplanen's GTFS feed: the stops, with coordinates, of every Movia bus and
DSB S-tog line that serves Copenhagen.

  curl -o GTFS.zip https://www.rejseplanen.info/labs/GTFS.zip
  python3 tools/build_stations.py GTFS.zip            # every line with a stop in the Copenhagen box
  python3 tools/build_stations.py GTFS.zip 150S 5C    # only these lines

A line serves Copenhagen when at least one of its stops lies in COPENHAGEN (Copenhagen and Frederiksberg
municipalities and the suburbs inside the ring roads); all of its stops are then listed, also those outside.

Per line, the file lists every stop point any trip of the line uses: those of the longest trip in direction 0,
in route order, then those that only direction 1 uses, then those only on shorter trips (variants). A stop point
is named by its station: the GTFS stop name without the street in brackets, "Nørre Campus (Tagensvej)"
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

AGENCIES = ("Movia", "DSB S-tog")  # the operators in the Dataudveksleren datasets the recorder reads
COPENHAGEN = (55.60, 55.76, 12.40, 12.65)  # lat min, lat max, lon min, lon max


def station_name(stop_name: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", stop_name).strip()


def in_copenhagen(s: dict) -> bool:
    lat0, lat1, lon0, lon1 = COPENHAGEN
    return lat0 <= float(s["stop_lat"]) <= lat1 and lon0 <= float(s["stop_lon"]) <= lon1


def main(gtfs: str, only: list) -> None:
    z = zipfile.ZipFile(gtfs)

    def rows(name):
        return csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig"))

    agencies = {a["agency_id"] for a in rows("agency.txt") if a["agency_name"] in AGENCIES}
    line_of = {r["route_id"]: r["route_short_name"] for r in rows("routes.txt")
               if r["agency_id"] in agencies and r["route_short_name"] and (not only or r["route_short_name"] in only)}
    trips = {t["trip_id"]: (line_of[t["route_id"]], t.get("direction_id") or "0")
             for t in rows("trips.txt") if t["route_id"] in line_of}
    calls = {}
    for st in rows("stop_times.txt"):
        if st["trip_id"] in trips:
            calls.setdefault(st["trip_id"], []).append((int(st["stop_sequence"]), st["stop_id"]))
    stops = {s["stop_id"]: s for s in rows("stops.txt")}

    by_line = {}  # line -> {"longest": {direction: calls}, "all": [calls]}
    for trip, c in calls.items():
        line, d = trips[trip]
        c = sorted(c)
        entry = by_line.setdefault(line, {"longest": {}, "all": []})
        entry["all"].append(c)
        if len(c) > len(entry["longest"].get(d, [])):
            entry["longest"][d] = c

    out = []
    for line in sorted(by_line):
        entry = by_line[line]
        ordered = [sid for d in sorted(entry["longest"]) for _, sid in entry["longest"][d]]
        ordered += [sid for c in entry["all"] for _, sid in c]
        if not only and not any(in_copenhagen(stops[sid]) for sid in ordered):
            continue
        seen = set()
        for sid in ordered:
            if sid not in seen:
                seen.add(sid)
                s = stops[sid]
                ref = str(int(sid)) if sid.isdigit() else sid
                out.append((line, station_name(s["stop_name"]), float(s["stop_lat"]), float(s["stop_lon"]),
                            s["stop_name"], ref))
    if not out:
        sys.exit(f"no {' or '.join(AGENCIES)} lines found in {gtfs}")
    path = os.path.join(ROOT, "data", "stations.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["line", "station", "lat", "lon", "stopName", "stopRef"])
        for line, name, lat, lon, full, ref in out:
            w.writerow([line, name, f"{lat:.6f}", f"{lon:.6f}", full, ref])
    print(f"{path}: {len({o[0] for o in out})} lines, {len(out)} stop points, "
          f"{len({(o[0], o[1]) for o in out})} stations")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])

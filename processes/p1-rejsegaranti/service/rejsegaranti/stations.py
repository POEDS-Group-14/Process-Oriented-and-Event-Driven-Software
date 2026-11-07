"""Stations of the line the process runs on, chosen with REJSEGARANTI_LINE (default L).

Line L (Hovedstadens Letbane) is the line the process is designed for, but no real-time data for it
is published (see deploy/README.md), so the server runs it on Movia bus 150S. Its stops come from
Rejseplanen's GTFS feed (data/stations-150S.csv, made by tools/build_stations.py). A station may
have several stop points (one per side of the road); distances are measured to the closest one.

LINE L COORDINATES ARE APPROXIMATE (entered by hand, roughly +/- 100 m).
Verify them against OpenStreetMap or Rejseplanen stop data before the
250 m rule is used on real claims. The simulator generates trips relative
to these same coordinates, so simulated runs stay internally consistent.
"""
from __future__ import annotations

import csv
import math
import os
from typing import List, Optional, Tuple

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data")
LINE = os.environ.get("REJSEGARANTI_LINE", "L").upper()

LINE_L = [
    ("Lundtofte", 55.7930, 12.5120),
    ("Rævehøjvej (DTU)", 55.7870, 12.5170),
    ("Anker Engelunds Vej DTU", 55.7845, 12.5225),
    ("Akademivej DTU", 55.7810, 12.5150),
    ("Fortunbyen", 55.7740, 12.5110),
    ("Lyngby Centrum", 55.7705, 12.5045),
    ("Lyngby", 55.7680, 12.5030),
    ("Gammelmosevej", 55.7590, 12.4930),
    ("Buddinge", 55.7470, 12.4940),
    ("Gladsaxe Rådhus", 55.7400, 12.4920),
    ("Gladsaxevej", 55.7350, 12.4850),
    ("Gladsaxe Trafikplads", 55.7310, 12.4700),
    ("Dynamovej", 55.7290, 12.4520),
    ("Herlev Hospital", 55.7300, 12.4410),
    ("Herlev Bymidte", 55.7270, 12.4340),
    ("Herlev", 55.7190, 12.4440),
    ("Herlev Syd", 55.7070, 12.4480),
    ("Rødovre Nord", 55.6940, 12.4430),
    ("Glostrup Ejby", 55.6870, 12.4130),
    ("Glostrup Nord", 55.6820, 12.4040),
    ("Glostrup Hospital", 55.6730, 12.4010),
    ("Glostrup", 55.6650, 12.3980),
    ("Kirkebjerg", 55.6550, 12.4050),
    ("Brøndbyvester", 55.6460, 12.4130),
    ("Delta Park", 55.6340, 12.3900),
    ("Vallensbæk", 55.6240, 12.3830),
    ("Strandhaven", 55.6170, 12.3700),
    ("Ishøj Strand", 55.6130, 12.3600),
    ("Ishøj", 55.6150, 12.3510),
]



def load(line: str) -> List[Tuple[str, float, float]]:
    """(station, lat, lon) per stop point, in route order."""
    if line == "L":
        return LINE_L
    with open(os.path.join(DATA, f"stations-{line}.csv"), encoding="utf-8") as fh:
        return [(r["station"], float(r["lat"]), float(r["lon"])) for r in csv.DictReader(fh)]


STATIONS = load(LINE)
STATION_BY_NAME = {}  # station -> its first stop point
for _name, _lat, _lon in STATIONS:
    STATION_BY_NAME.setdefault(_name, (_lat, _lon))

EARTH_RADIUS_M = 6_371_000.0


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle (haversine) distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def station_distance_m(lat: float, lon: float, name: str) -> float:
    """Distance in metres to the closest stop point of a station (inf if the line has no such station)."""
    return min((distance_m(lat, lon, slat, slon) for n, slat, slon in STATIONS if n == name), default=math.inf)


def nearest_station(lat: float, lon: float) -> Tuple[str, float]:
    """Return (station name, distance in metres) of the closest station of the line."""
    best: Optional[Tuple[str, float]] = None
    for name, slat, slon in STATIONS:
        d = distance_m(lat, lon, slat, slon)
        if best is None or d < best[1]:
            best = (name, d)
    assert best is not None
    return best


def offset(lat: float, lon: float, north_m: float, east_m: float) -> Tuple[float, float]:
    """Move a point by metres north/east (good enough at city scale)."""
    dlat = north_m / 111_320.0
    dlon = east_m / (111_320.0 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon

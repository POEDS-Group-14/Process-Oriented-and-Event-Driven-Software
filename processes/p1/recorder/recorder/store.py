"""Storage. Everything is append-only.

  raw/<source>/YYYY-MM-DD.jsonl.gz   every response or message, unchanged, with the time received
  <name>.csv                         derived rows (weather metrics, incidents, situations)
  recorder.sqlite                    the same derived rows, one table per CSV file

gzip files are opened in append mode per write, so a crash loses at most one record and
the file stays readable (each append is its own gzip member).

SQLite (Python's built-in sqlite3) uses WAL mode, so you can query the database while the
recorder is writing to it. Each table has a UNIQUE key, so the same observation is never
stored twice, even after a restart.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

_lock = threading.Lock()

DB_NAME = "recorder.sqlite"

# table -> (columns with SQLite types, UNIQUE key). Column names match the CSV headers.
TABLES = {
    "weather_owm": ({"observedAt": "TEXT", "receivedAt": "TEXT", "lat": "REAL", "lon": "REAL",
                     "wind_speed": "REAL", "wind_gust": "REAL", "rain_1h": "REAL", "snow_1h": "REAL",
                     "temp": "REAL", "weather_main": "TEXT", "weather_description": "TEXT"},
                    ("observedAt", "lat", "lon")),
    "weather_dmi": ({"observed": "TEXT", "receivedAt": "TEXT", "stationId": "TEXT", "parameterId": "TEXT",
                     "value": "REAL"},
                    ("stationId", "parameterId", "observed")),
    "movia_incidents": ({"incidentId": "TEXT", "type": "TEXT", "minutes": "REAL", "timeKind": "TEXT",
                         "operator": "TEXT", "line": "TEXT", "journeyRef": "TEXT", "stopRef": "TEXT",
                         "stopName": "TEXT", "aimedTime": "TEXT", "observedTime": "TEXT", "detectedAt": "TEXT",
                         "receivedAt": "TEXT"},
                        ("incidentId", "timeKind", "observedTime")),
    "movia_situations": ({"situationNumber": "TEXT", "version": "TEXT", "creationTime": "TEXT",
                          "firstSeen": "TEXT", "planned": "TEXT", "validFrom": "TEXT", "validTo": "TEXT",
                          "lines": "TEXT", "stops": "TEXT", "reason": "TEXT", "summary": "TEXT",
                          "receivedAt": "TEXT"},
                         ("situationNumber", "version", "validFrom")),
}
INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_incidents_line ON movia_incidents(line, type, aimedTime)",
    "CREATE INDEX IF NOT EXISTS ix_situations_line ON movia_situations(lines, validFrom)",
    "CREATE INDEX IF NOT EXISTS ix_owm_time ON weather_owm(observedAt)",
    "CREATE INDEX IF NOT EXISTS ix_dmi_time ON weather_dmi(observed, parameterId)",
]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def table_for(csv_name: str) -> str:
    """'weather-owm.csv' -> 'weather_owm'"""
    return csv_name.rsplit(".", 1)[0].replace("-", "_")


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    for table, (cols, key) in TABLES.items():
        db.execute(f"CREATE TABLE IF NOT EXISTS {table} ("
                   + ", ".join(f'"{c}" {t}' for c, t in cols.items())
                   + ", UNIQUE (" + ", ".join(f'"{k}"' for k in key) + "))")
    for sql in INDEXES:
        db.execute(sql)
    db.commit()
    return db


def _value(v, typ: str):
    # Text stays "" rather than NULL: SQLite treats NULLs as all different, so a NULL in a
    # UNIQUE key column would let duplicates through (e.g. cancellations have no observed time).
    if typ != "REAL":
        return "" if v is None else str(v)
    if v is None or v == "":
        return None
    if typ == "REAL":
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return str(v)


class Store:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.db_path = os.path.join(root, DB_NAME)
        self.db = connect(self.db_path)

    def raw(self, source: str, payload, received: datetime | None = None, meta: dict | None = None) -> None:
        received = received or now_utc()
        folder = os.path.join(self.root, "raw", source)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, received.strftime("%Y-%m-%d") + ".jsonl.gz")
        rec = {"receivedAt": received.isoformat(), "source": source, "meta": meta or {}, "payload": payload}
        line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
        with _lock, gzip.open(path, "ab") as fh:
            fh.write(line)

    def rows(self, name: str, fieldnames: list, rows: list) -> int:
        """Append rows to <name>.csv and insert them into the matching SQLite table."""
        if not rows:
            return 0
        path = os.path.join(self.root, name)
        with _lock:
            new = not os.path.exists(path) or os.path.getsize(path) == 0
            with open(path, "a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerows(rows)
            table = table_for(name)
            if table in TABLES:
                cols = TABLES[table][0]
                names = list(cols)
                sql = (f"INSERT OR IGNORE INTO {table} (" + ", ".join(f'"{c}"' for c in names) + ") VALUES ("
                       + ", ".join("?" for _ in names) + ")")
                self.db.executemany(sql, [[_value(r.get(c), cols[c]) for c in names] for r in rows])
                self.db.commit()
        return len(rows)

    def close(self) -> None:
        with _lock:
            self.db.close()

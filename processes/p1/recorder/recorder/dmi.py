"""DMI Open Data, metObs (observations from DMI weather stations).

Base URL: https://opendataapi.dmi.dk/v2/ (the old dmigw.govcloud.dk host no longer resolves)   Docs: https://opendatadocs.dmi.govcloud.dk/
- DMI's documentation says an API key is needed (header X-Gravitee-Api-Key), but in practice
  metObs answers without one. The key is therefore optional and only sent if DMI_API_KEY is set.
  If DMI starts answering 401/403, get a key and set DMI_API_KEY.
- Fair use limit: 500 requests per 5 seconds (HTTP 429 above that).
- Responses are GeoJSON (OGC API - Features).

DMI's weather WARNINGS (varsler) are not part of this: I could not confirm that DMI Open Data
offers warnings through an API. Only observations are logged here.

Every response is stored raw, so nothing is lost if a field name below is different from
what DMI sends; the derived CSV can be rebuilt from the raw files.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .http import get_json

BASE = "https://opendataapi.dmi.dk/v2/metObs/collections"
FIELDS = ["observed", "receivedAt", "stationId", "parameterId", "value"]


def _h(api_key: str) -> dict:
    return {"X-Gravitee-Api-Key": api_key} if api_key else {}


def fetch_observations(api_key: str, station_id: str, since: datetime, limit: int = 1000) -> dict:
    since_s = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    return get_json(f"{BASE}/observation/items", {"stationId": station_id, "datetime": f"{since_s}/..",
                                                  "limit": limit}, headers=_h(api_key))


def fetch_stations_near(api_key: str, lat: float, lon: float, km: float = 15) -> dict:
    d = km / 111.0
    bbox = f"{lon - d * 1.8:.4f},{lat - d:.4f},{lon + d * 1.8:.4f},{lat + d:.4f}"
    return get_json(f"{BASE}/station/items", {"bbox": bbox, "limit": 500}, headers=_h(api_key))


def to_rows(data: dict, received: datetime) -> list:
    rows = []
    for f in data.get("features") or []:
        p = f.get("properties") or {}
        rows.append({"observed": p.get("observed", ""), "receivedAt": received.isoformat(),
                     "stationId": p.get("stationId", ""), "parameterId": p.get("parameterId", ""),
                     "value": p.get("value", "")})
    return rows


def lookback(interval_s: float) -> timedelta:
    # ask for a little more than one interval, duplicates are dropped by (stationId, parameterId, observed)
    return timedelta(seconds=interval_s * 2 + 600)

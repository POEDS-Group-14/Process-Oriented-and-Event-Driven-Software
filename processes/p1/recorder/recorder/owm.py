"""OpenWeatherMap current weather.

API: https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={API key}
Docs: https://openweathermap.org/current
- units=metric gives wind in m/s.
- rain.1h and snow.1h are in mm/h and only present "where available"; missing means none reported.
- dt is the time of data calculation (unix, UTC).
The field names match the metrics used for force majeure in service/rejsegaranti/rules.py
(wind_speed, wind_gust, rain_1h, snow_1h).
"""
from __future__ import annotations

from datetime import datetime, timezone

from .http import get_json

URL = "https://api.openweathermap.org/data/2.5/weather"
FIELDS = ["observedAt", "receivedAt", "lat", "lon", "wind_speed", "wind_gust", "rain_1h", "snow_1h",
          "temp", "weather_main", "weather_description"]


def fetch(api_key: str, lat: float, lon: float) -> dict:
    return get_json(URL, {"lat": lat, "lon": lon, "appid": api_key, "units": "metric"})


def to_row(data: dict, received: datetime) -> dict:
    wind, rain, snow = data.get("wind") or {}, data.get("rain") or {}, data.get("snow") or {}
    weather = (data.get("weather") or [{}])[0]
    coord = data.get("coord") or {}
    dt = data.get("dt")
    return {
        "observedAt": datetime.fromtimestamp(dt, timezone.utc).isoformat() if dt is not None else "",
        "receivedAt": received.isoformat(),
        "lat": coord.get("lat", ""),
        "lon": coord.get("lon", ""),
        "wind_speed": wind.get("speed", ""),
        "wind_gust": wind.get("gust", ""),
        "rain_1h": rain.get("1h", 0),
        "snow_1h": snow.get("1h", 0),
        "temp": (data.get("main") or {}).get("temp", ""),
        "weather_main": weather.get("main", ""),
        "weather_description": weather.get("description", ""),
    }

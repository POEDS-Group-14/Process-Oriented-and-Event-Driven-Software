"""Settings, read from environment variables (see deploy/recorder.env.example).

Nothing secret is stored in the code or the repo.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ENV_FILES = [os.path.join(HERE, "..", "deploy", "recorder.env"), "/etc/rejsegaranti-recorder.env"]


def load_env_file() -> str | None:
    """Load KEY=value lines from RECORDER_ENV_FILE, deploy/recorder.env or /etc/rejsegaranti-recorder.env
    (the first that exists). Variables already set in the environment win. Returns the file used."""
    candidates = [os.environ["RECORDER_ENV_FILE"]] if os.environ.get("RECORDER_ENV_FILE") else DEFAULT_ENV_FILES
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if value:
                os.environ.setdefault(key.strip(), value)
        return os.path.abspath(path)
    return None


def _f(name: str, default: str) -> float:
    return float(os.environ.get(name, default))


@dataclass
class Config:
    data_dir: str = field(default_factory=lambda: os.environ.get("RECORDER_DATA_DIR", os.path.abspath("recorder-data")))

    # Where to observe the weather. Default: central Copenhagen.
    lat: float = field(default_factory=lambda: _f("RECORDER_LAT", "55.6761"))
    lon: float = field(default_factory=lambda: _f("RECORDER_LON", "12.5683"))

    # OpenWeatherMap, current weather API (https://openweathermap.org/current)
    owm_api_key: str = field(default_factory=lambda: os.environ.get("OWM_API_KEY", ""))
    owm_interval_s: float = field(default_factory=lambda: _f("OWM_INTERVAL_S", "600"))

    # DMI Open Data, metObs API (https://opendatadocs.dmi.govcloud.dk/). The key is optional:
    # it is only sent if set.
    dmi_api_key: str = field(default_factory=lambda: os.environ.get("DMI_API_KEY", ""))
    dmi_station_id: str = field(default_factory=lambda: os.environ.get("DMI_STATION_ID", ""))
    dmi_interval_s: float = field(default_factory=lambda: _f("DMI_INTERVAL_S", "600"))

    # Dataudveksleren real-time feeds (Azure Service Bus, AMQP 1.0). One AMQP url per subscribed dataset,
    # copied from the dataset's "Subscribe to events" popup, separated by commas.
    sb_amqp_urls: list = field(default_factory=lambda: [
        u for u in re.split(r"[,\s]+", os.environ.get("SB_AMQP_URLS", "")) if u])
    azure_tenant_id: str = field(default_factory=lambda: os.environ.get("AZURE_TENANT_ID", ""))
    azure_client_id: str = field(default_factory=lambda: os.environ.get("AZURE_CLIENT_ID", ""))
    azure_client_secret: str = field(default_factory=lambda: os.environ.get("AZURE_CLIENT_SECRET", ""))

    # Process-1 bridge (service/rejsegaranti): where to send delay and weather situations, and for which
    # lines. Empty BRIDGE_URL: send nothing.
    bridge_url: str = field(default_factory=lambda: os.environ.get("BRIDGE_URL", ""))
    bridge_lines: list = field(default_factory=lambda: [
        x for x in re.split(r"[,\s]+", os.environ.get("BRIDGE_LINES", "")) if x])

    # Incident thresholds (Movia travel guarantee, see '1. Rejsegaranti claim.md')
    delay_threshold_min: float = field(default_factory=lambda: _f("DELAY_THRESHOLD_MIN", "20"))
    early_threshold_min: float = field(default_factory=lambda: _f("EARLY_THRESHOLD_MIN", "2"))

    def problems(self) -> list:
        """Human-readable list of sources that cannot run with the current settings."""
        out = []
        if not self.owm_api_key:
            out.append("OpenWeatherMap: OWM_API_KEY is not set")
        if not self.dmi_station_id:
            out.append("DMI: DMI_STATION_ID is not set (find one with: python -m recorder dmi-stations)")
        if not self.sb_amqp_urls:
            out.append("Dataudveksleren: SB_AMQP_URLS is not set")
        for u in self.sb_amqp_urls:
            if sb_subscription(u) is None:
                out.append(f"Dataudveksleren: {u} is not amqps://<namespace>/<topic>/<subscription>")
        if self.sb_amqp_urls and not (
                self.azure_tenant_id and self.azure_client_id and self.azure_client_secret):
            out.append("Dataudveksleren: set AZURE_TENANT_ID, AZURE_CLIENT_ID and AZURE_CLIENT_SECRET (Entra ID)")
        return out


def sb_subscription(url: str):
    """Split a Dataudveksleren AMQP url into (namespace, topic, subscription), or None if it doesn't fit."""
    u = urlsplit(url)
    parts = [p for p in u.path.split("/") if p]
    if u.scheme not in ("amqp", "amqps") or not u.hostname or len(parts) != 2:
        return None
    return u.hostname, parts[0], parts[1]

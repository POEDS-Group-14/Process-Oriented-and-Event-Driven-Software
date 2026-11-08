"""Write and read XES event logs (IEEE 1849) without extra libraries."""
from __future__ import annotations

import csv
import xml.etree.ElementTree as ET
from typing import Dict, Iterable, List, Optional

from .timeutil import parse_ts


def _attr(parent: ET.Element, key: str, value) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        ET.SubElement(parent, "boolean", key=key, value=str(value).lower())
    elif isinstance(value, int):
        ET.SubElement(parent, "int", key=key, value=str(value))
    elif isinstance(value, float):
        ET.SubElement(parent, "float", key=key, value=repr(value))
    else:
        ET.SubElement(parent, "string", key=key, value=str(value))


def write_xes(path: str, traces: Iterable[dict], log_name: str, roles: Optional[Dict[str, str]] = None) -> int:
    """traces: [{"id": str, "attrs": {...}, "events": [{"name", "time", "attrs"?}]}]
    roles: activity -> role, written as the event attribute "role" (the key processdoctor reads)"""
    root = ET.Element("log", {"xes.version": "1.0", "xes.features": "nested-attributes",
                              "xmlns": "http://www.xes-standard.org/"})
    for name, prefix, uri in (
        ("Concept", "concept", "http://www.xes-standard.org/concept.xesext"),
        ("Time", "time", "http://www.xes-standard.org/time.xesext"),
        ("Lifecycle", "lifecycle", "http://www.xes-standard.org/lifecycle.xesext"),
    ):
        ET.SubElement(root, "extension", name=name, prefix=prefix, uri=uri)
    g = ET.SubElement(root, "global", scope="trace")
    ET.SubElement(g, "string", key="concept:name", value="__INVALID__")
    g = ET.SubElement(root, "global", scope="event")
    ET.SubElement(g, "string", key="concept:name", value="__INVALID__")
    ET.SubElement(g, "date", key="time:timestamp", value="1970-01-01T00:00:00.000+00:00")
    ET.SubElement(root, "classifier", name="Activity", keys="concept:name")
    ET.SubElement(root, "string", key="concept:name", value=log_name)
    n = 0
    for t in traces:
        tr = ET.SubElement(root, "trace")
        ET.SubElement(tr, "string", key="concept:name", value=str(t["id"]))
        for k, v in (t.get("attrs") or {}).items():
            _attr(tr, k, v)
        for e in t["events"]:
            ev = ET.SubElement(tr, "event")
            ET.SubElement(ev, "string", key="concept:name", value=e["name"])
            ET.SubElement(ev, "string", key="lifecycle:transition", value="complete")
            if roles and e["name"] in roles:
                ET.SubElement(ev, "string", key="role", value=roles[e["name"]])
            ts = parse_ts(e["time"]).isoformat(timespec="milliseconds")
            ET.SubElement(ev, "date", key="time:timestamp", value=ts)
            for k, v in (e.get("attrs") or {}).items():
                _attr(ev, k, v)
        n += 1
    ET.indent(root) if hasattr(ET, "indent") else None
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return n


def write_csv(path: str, traces: Iterable[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "activity", "timestamp"])
        for t in traces:
            for e in t["events"]:
                w.writerow([t["id"], e["name"], parse_ts(e["time"]).isoformat(timespec="milliseconds")])


def read_xes(path: str) -> List[dict]:
    """Return [{"id", "events": [{"name", "time"}]}] (namespace-agnostic)."""
    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    traces = []
    for tr in ET.parse(path).getroot():
        if local(tr.tag) != "trace":
            continue
        tid, events = None, []
        for child in tr:
            if local(child.tag) == "string" and child.get("key") == "concept:name":
                tid = child.get("value")
            elif local(child.tag) == "event":
                ev = {}
                for a in child:
                    if a.get("key") == "concept:name":
                        ev["name"] = a.get("value")
                    elif a.get("key") == "time:timestamp":
                        ev["time"] = a.get("value")
                events.append(ev)
        events.sort(key=lambda e: parse_ts(e["time"]))  # stable: ties keep file order
        traces.append({"id": tid, "events": events})
    return traces

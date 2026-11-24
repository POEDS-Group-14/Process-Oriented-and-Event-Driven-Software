"""What the recorded SIRI-ET feeds say about disruptions, for the report (python -m recorder feed-stats).

Reads raw/dataudveksleren/<topic>/YYYY-MM-DD.jsonl.gz (dates are UTC receive dates) and counts, per feed:

  updates        EstimatedVehicleJourney elements. The feed resends a journey whenever its prediction
                 changes, so this is far more than the number of journeys
  journeys       unique journeys (DataFrameRef + DatedVehicleJourneyRef)
  cancelled      journeys with Cancellation true before their call lists: the whole journey is cancelled
  skipped stops  journeys with Cancellation true inside a call only: one or more stops are skipped
  flags          every Cancellation element, true or false, over all updates
  inaccurate     PredictionInaccurate true
  causes         any *Reason* or *Situation* element (SituationRef, reasons): what the feed says about why

Only the raw files are read; nothing is written.
"""
from __future__ import annotations

import glob
import gzip
import json
import os
import re
from collections import Counter
from multiprocessing import Pool
from typing import Dict, Optional

JOURNEY = re.compile(r"<EstimatedVehicleJourney>(.*?)</EstimatedVehicleJourney>", re.S)
KEY = re.compile(r"<DataFrameRef>([^<]*)</DataFrameRef>\s*<DatedVehicleJourneyRef>([^<]*)<")
CALLS = re.compile(r"<(EstimatedCalls|RecordedCalls)>")
CAUSE = re.compile(r"<[A-Za-z:]*(Reason|Situation)[A-Za-z]*[\s>/]")
TRUE, FALSE = "<Cancellation>true<", "<Cancellation>false<"


def scan(path: str) -> dict:
    """Counts and journey sets for one raw file."""
    n = Counter()
    journeys, cancelled, skipped = set(), set(), set()
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            xml = json.loads(line).get("payload")
            if not isinstance(xml, str):
                continue
            n["causes"] += len(CAUSE.findall(xml))
            n["flags true"] += xml.count(TRUE)
            n["flags false"] += xml.count(FALSE)
            n["inaccurate"] += xml.count("<PredictionInaccurate>true<")
            for block in JOURNEY.findall(xml):
                n["updates"] += 1
                k = KEY.search(block)
                if not k:
                    continue
                key = k.group(1) + "|" + k.group(2)
                journeys.add(key)
                m = CALLS.search(block)
                head, calls = (block[:m.start()], block[m.start():]) if m else (block, "")
                if TRUE in head:
                    cancelled.add(key)
                if TRUE in calls:
                    skipped.add(key)
    return {"counts": n, "journeys": journeys, "cancelled": cancelled, "skipped": skipped}


def files(data_dir: str, start: Optional[str] = None, end: Optional[str] = None) -> Dict[str, list]:
    """Raw SIRI files per feed folder, limited to file dates start..end (YYYY-MM-DD, inclusive)."""
    out: Dict[str, list] = {}
    for path in sorted(glob.glob(os.path.join(data_dir, "raw", "dataudveksleren", "*", "*.jsonl.gz"))):
        day = os.path.basename(path)[:10]
        if (start and day < start) or (end and day > end):
            continue
        out.setdefault(os.path.basename(os.path.dirname(path)), []).append(path)
    return out


def stats(data_dir: str, start: Optional[str] = None, end: Optional[str] = None) -> Dict[str, dict]:
    """Per feed: files, first and last file date, compressed bytes and the merged result of scan()."""
    feeds = files(data_dir, start, end)
    paths = [p for ps in feeds.values() for p in ps]
    with Pool() as pool:
        scanned = dict(zip(paths, pool.map(scan, paths)))
    out = {}
    for feed, ps in feeds.items():
        total = {"counts": Counter(), "journeys": set(), "cancelled": set(), "skipped": set()}
        for p in ps:
            total["counts"].update(scanned[p]["counts"])
            for k in ("journeys", "cancelled", "skipped"):
                total[k] |= scanned[p][k]
        out[feed] = {"files": len(ps), "from": os.path.basename(ps[0])[:10], "to": os.path.basename(ps[-1])[:10],
                     "bytes": sum(os.path.getsize(p) for p in ps), **total}
    return out


def report(result: Dict[str, dict]) -> str:
    lines = []
    for feed, r in sorted(result.items()):
        n, c, s = r["counts"], r["cancelled"], r["skipped"]
        lines += [
            f"{feed}: {r['files']} files, {r['from']} to {r['to']} (UTC), {r['bytes'] / 1e6:.0f} MB compressed",
            f"  journey updates             {n['updates']:>12,}",
            f"  unique journeys             {len(r['journeys']):>12,}",
            f"  cancelled outright          {len(c):>12,}",
            f"  skipped stops only          {len(s - c):>12,}",
            f"  Cancellation true / false   {n['flags true']:>12,} / {n['flags false']:,}",
            f"  PredictionInaccurate true   {n['inaccurate']:>12,}",
            f"  cause elements              {n['causes']:>12,}",
        ]
    return "\n".join(lines) if lines else "no raw SIRI files in that range"

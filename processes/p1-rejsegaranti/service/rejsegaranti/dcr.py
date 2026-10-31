"""DCR compliance rules for rejsegaranti claims (issue #10).

The rules are defined once in RULES below. From them we generate
  * models/rejsegaranti-rules.dcr.xml  (dcrgraphs.net XML, for the DCR
    portal and processdoctor.dtu.dk)
  * models/rejsegaranti-rules.dot/.pdf (a drawing for the report)
and the checker below replays an event log against the XML file, so the
rules can be tested locally before uploading to processdoctor.dtu.dk.
"""
from __future__ import annotations

import re
import textwrap
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from typing import Dict, List, Set, Tuple
from xml.sax.saxutils import escape

from . import activities as A

EVENTS = [
    # (label, role)
    (A.CLAIM_SUBMITTED, "Passenger"),
    (A.MATCH_DELAY, "Claim engine"),
    (A.NO_VERIFIED_DELAY, "Claim engine"),
    (A.NOTICE_BY_DAY_BEFORE, "Claim engine"),
    (A.FORCE_MAJEURE, "Claim engine"),
    (A.DELAY_VERIFIED, "Claim engine"),
    (A.CHECK_RECEIPT, "Claim engine"),
    (A.CHECK_DISTANCE, "Claim engine"),
    (A.RECEIPT_REJECTED, "Claim engine"),
    (A.RECEIPT_VERIFIED, "Claim engine"),
    (A.TRIP_TOO_FAR, "Claim engine"),
    (A.TRIP_WITHIN, "Claim engine"),
    (A.CALC_PAYOUT, "Claim engine"),
    (A.PAY_OUT, "Finance"),
    (A.REJECT, "Claim engine"),
    (A.REPORT_OUTCOME, "Claim engine"),
    (A.CLAIM_CLOSED, "Claim engine"),
]
# The claim logs carry these roles too: processdoctor only replays a log event
# on a graph event with the same role, so a log without roles never conforms.
ROLES = dict(EVENTS)

# (kind, source, target, why) - kind in condition/response/exclude/include/milestone
RULES: List[Tuple[str, str, str, str]] = [
    # flow of the checks
    ("condition", A.CLAIM_SUBMITTED, A.MATCH_DELAY, "a claim is matched only after it is submitted"),
    ("condition", A.MATCH_DELAY, A.DELAY_VERIFIED, "verification comes from the CEP match"),
    ("condition", A.MATCH_DELAY, A.NO_VERIFIED_DELAY, ""),
    ("condition", A.DELAY_VERIFIED, A.CHECK_RECEIPT, "receipt checked only for a verified delay"),
    ("condition", A.DELAY_VERIFIED, A.CHECK_DISTANCE, ""),
    ("condition", A.CHECK_RECEIPT, A.RECEIPT_VERIFIED, ""),
    ("condition", A.CHECK_RECEIPT, A.RECEIPT_REJECTED, ""),
    ("condition", A.CHECK_DISTANCE, A.TRIP_WITHIN, ""),
    ("condition", A.CHECK_DISTANCE, A.TRIP_TOO_FAR, ""),
    # R1: no payout without a receipt
    ("condition", A.RECEIPT_VERIFIED, A.PAY_OUT, "R1 no payout without a verified receipt"),
    ("exclude", A.RECEIPT_REJECTED, A.PAY_OUT, "R1 a rejected receipt rules out payout"),
    # R2: no payout when the planned change was announced by the day before
    ("exclude", A.NOTICE_BY_DAY_BEFORE, A.PAY_OUT, "R2 no payout for changes announced by the day before"),
    # R3: no payout under force majeure, or without a verified 30+ min delay
    ("exclude", A.FORCE_MAJEURE, A.PAY_OUT, "R3 no payout under force majeure"),
    ("exclude", A.NO_VERIFIED_DELAY, A.PAY_OUT, "R3 no payout without a verified delay"),
    ("condition", A.DELAY_VERIFIED, A.PAY_OUT, "R3 payout needs a verified delay"),
    # R4: trip within 250 m of stations
    ("condition", A.TRIP_WITHIN, A.PAY_OUT, "R4 payout needs start and end within 250 m"),
    ("exclude", A.TRIP_TOO_FAR, A.PAY_OUT, "R4"),
    # R5: payout is calculated (300 kr cap) before it is paid
    ("condition", A.CALC_PAYOUT, A.PAY_OUT, "R5 the capped amount is calculated first"),
    # R6: at most one payout per claim; payout and rejection exclude each other
    ("exclude", A.PAY_OUT, A.PAY_OUT, "R6 at most one payout per claim"),
    ("exclude", A.PAY_OUT, A.REJECT, "R6 a paid claim cannot also be rejected"),
    ("exclude", A.REJECT, A.PAY_OUT, "R6 a rejected claim cannot be paid"),
    # R7: every claim gets a decision that is reported back to the CEP
    ("response", A.CLAIM_SUBMITTED, A.REPORT_OUTCOME, "R7 every claim gets an outcome"),
    ("condition", A.PAY_OUT, A.REPORT_OUTCOME, "R7 the outcome follows the decision"),
    ("condition", A.REJECT, A.REPORT_OUTCOME, "R7"),
    ("exclude", A.REPORT_OUTCOME, A.REPORT_OUTCOME, "R7 reported once"),
    # R8: an eligible claim must be paid
    ("response", A.TRIP_WITHIN, A.PAY_OUT, "R8 an eligible claim must be paid"),
    ("exclude", A.TRIP_WITHIN, A.REJECT, "R8 an eligible claim cannot be rejected"),
    ("condition", A.REPORT_OUTCOME, A.CLAIM_CLOSED, ""),
]

KIND_TAG = {"condition": ("conditions", "condition"), "response": ("responses", "response"),
            "exclude": ("excludes", "exclude"), "include": ("includes", "include"),
            "milestone": ("milestones", "milestone")}


def _eid(i: int) -> str:
    return f"Activity{i}"


def to_xml(path: str, title: str = "Rejsegaranti claim - compliance rules (author: Liam Palmqvist)") -> None:
    ids = {label: _eid(i) for i, (label, _) in enumerate(EVENTS)}
    root = ET.Element("dcrgraph", title=title, dataTypesStatus="hide", filterLevel="-1",
                      insightFilter="false", zoomLevel="-1", formGroupStyle="Normal",
                      formLayoutStyle="Horizontal", graphBG="#f1f6fe", graphType="0",
                      exercise="false", version="1.1")
    spec = ET.SubElement(root, "specification")
    res = ET.SubElement(spec, "resources")
    evs = ET.SubElement(res, "events")
    for i, (label, role) in enumerate(EVENTS):
        e = ET.SubElement(evs, "event", id=ids[label])
        ET.SubElement(e, "precondition", message="")
        c = ET.SubElement(e, "custom")
        vis = ET.SubElement(c, "visualization")
        ET.SubElement(vis, "location", xLoc=str(50 + (i % 6) * 200), yLoc=str(50 + (i // 6) * 220))
        ET.SubElement(vis, "size", width="130", height="150")
        roles = ET.SubElement(c, "roles")
        ET.SubElement(roles, "role").text = role
        for tag in ("groups", "phases"):
            ET.SubElement(ET.SubElement(c, tag), tag[:-1])
        ET.SubElement(c, "eventType")
        ET.SubElement(c, "eventScope").text = "private"
        ET.SubElement(c, "eventTypeData")
        ET.SubElement(c, "eventDescription")
        ET.SubElement(c, "purpose")
        ET.SubElement(c, "guide")
        ET.SubElement(c, "insight", use="false")
        ET.SubElement(c, "level").text = "1"
        ET.SubElement(c, "sequence").text = str(i + 1)
        ET.SubElement(c, "costs").text = "0"
        ET.SubElement(c, "eventData")
        ET.SubElement(c, "interfaces")
    ET.SubElement(res, "subProcesses")
    ET.SubElement(ET.SubElement(res, "distribution"), "externalEvents")
    labels = ET.SubElement(res, "labels")
    for label, _ in EVENTS:
        ET.SubElement(labels, "label", id=label)
    lm = ET.SubElement(res, "labelMappings")
    for label, _ in EVENTS:
        ET.SubElement(lm, "labelMapping", eventId=ids[label], labelId=label)
    ET.SubElement(res, "expressions")
    ET.SubElement(res, "variables")
    ET.SubElement(ET.SubElement(res, "variableAccesses"), "writeAccesses")
    custom = ET.SubElement(res, "custom")
    ET.SubElement(custom, "keywords")
    roles = ET.SubElement(custom, "roles")
    for r in sorted({r for _, r in EVENTS}):
        ET.SubElement(roles, "role", description="").text = r
    for tag in ("groups", "phases", "eventTypes", "eventParameters"):
        ET.SubElement(custom, tag)
    ET.SubElement(custom, "graphDetails").text = (
        "Compliance rules for Line L rejsegaranti claims (process 1). "
        "Source: dinletbane.dk/da/kontakt-og-kundeservice/rejsegaranti/")
    ET.SubElement(custom, "graphDocumentation")
    ET.SubElement(custom, "graphLanguage").text = "en-US"
    ET.SubElement(custom, "domain").text = "process"

    cons = ET.SubElement(spec, "constraints")
    groups = {}
    for kind in ("condition", "response", "coresponce", "exclude", "include", "milestone", "update", "spawn"):
        tag = KIND_TAG.get(kind, (kind + "s", kind))[0]
        groups[kind] = ET.SubElement(cons, tag)
    for kind, src, tgt, why in RULES:
        ET.SubElement(groups[kind], KIND_TAG[kind][1], sourceId=ids[src], targetId=ids[tgt],
                      filterLevel="1", description=why, time="", groups="")

    marking = ET.SubElement(ET.SubElement(root, "runtime"), "marking")
    ET.SubElement(marking, "globalStore")
    ET.SubElement(marking, "executed")
    inc = ET.SubElement(marking, "included")
    for label, _ in EVENTS:
        ET.SubElement(inc, "event", id=ids[label])
    ET.SubElement(marking, "pendingResponses")
    ET.indent(root) if hasattr(ET, "indent") else None
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def to_dot(path: str, side: int = 100) -> None:
    """Draw the graph in DCR notation: square events with the role in a header band."""
    style = {"condition": 'color="#e8a33d" arrowhead=dotnormal',
             "response": 'color="#2a7de1" arrowtail=dot dir=both arrowhead=normal',
             # headlabel, not label: orthogonal edges cannot place mid-edge labels
             "exclude": 'color="#d64545" arrowhead=normal headlabel="%" fontcolor="#d64545"',
             "include": 'color="#2e9e5b" arrowhead=normal headlabel="+" fontcolor="#2e9e5b"',
             "milestone": 'color="#7a4dd8" arrowhead=diamond'}
    lines = ["digraph DCR {", '  graph [rankdir=LR, splines=ortho, fontname="Helvetica", labelloc=t, fontsize=18,',
             '         label="Rejsegaranti claim: DCR compliance rules  (author: Liam Palmqvist)\\n'
             'orange = condition, blue = response, red % = exclude"];',
             '  node [shape=plain, fontname="Helvetica", fontsize=11];',
             '  edge [fontname="Helvetica", fontsize=10];']
    ids = {label: f"e{i}" for i, (label, _) in enumerate(EVENTS)}
    band = 22
    for label, role in EVENTS:
        keep_units = re.sub(r"(\d) (\w)", "\\1\u00a0\\2", label)  # no break inside "250 m"
        text = "<BR/>".join(escape(s) for s in textwrap.wrap(keep_units, 15))
        lines.append(
            f'  {ids[label]} [label=<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" STYLE="rounded" BGCOLOR="white">'
            f'<TR><TD WIDTH="{side}" HEIGHT="{band}" FIXEDSIZE="TRUE">'
            f'<FONT POINT-SIZE="9" COLOR="#555555">{escape(role)}</FONT></TD></TR><HR/>'
            f'<TR><TD WIDTH="{side}" HEIGHT="{side - band}" FIXEDSIZE="TRUE">{text}</TD></TR></TABLE>>];')
    for kind, s, t, _ in RULES:
        lines.append(f"  {ids[s]} -> {ids[t]} [{style[kind]}];")
    lines.append("}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# ---- reading and checking -------------------------------------------------

class Graph:
    def __init__(self):
        self.labels: Dict[str, str] = {}  # event id -> label
        self.rel: Dict[str, Dict[str, Set[str]]] = {k: defaultdict(set) for k in KIND_TAG}
        self.included0: Set[str] = set()
        self.executed0: Set[str] = set()
        self.pending0: Set[str] = set()

    @classmethod
    def from_xml(cls, path: str) -> "Graph":
        g = cls()
        root = ET.parse(path).getroot()
        for m in root.iter("labelMapping"):
            g.labels[m.get("eventId")] = m.get("labelId")
        for e in root.iter("event"):  # events without a mapping keep their id
            if e.get("id") and e.find("custom") is not None:
                g.labels.setdefault(e.get("id"), e.get("id"))
        for kind, (group, tag) in KIND_TAG.items():
            for grp in root.iter(group):
                for r in grp.findall(tag):
                    g.rel[kind][r.get("sourceId")].add(r.get("targetId"))
        mk = root.find("runtime/marking")
        if mk is not None:
            g.included0 = {e.get("id") for e in mk.findall("included/event")}
            g.executed0 = {e.get("id") for e in mk.findall("executed/event")}
            g.pending0 = {e.get("id") for e in mk.findall("pendingResponses/event")}
        else:
            g.included0 = set(g.labels)
        return g

    def by_label(self) -> Dict[str, str]:
        return {v: k for k, v in self.labels.items()}

    def enabled(self, e: str, ex: Set[str], inc: Set[str], pend: Set[str]) -> Tuple[bool, str]:
        if e not in inc:
            return False, "excluded"
        for c, targets in self.rel["condition"].items():
            if e in targets and c in inc and c not in ex:
                return False, f"condition '{self.labels[c]}' not executed"
        for m, targets in self.rel["milestone"].items():
            if e in targets and m in inc and m in pend:
                return False, f"milestone '{self.labels[m]}' pending"
        return True, ""

    def replay(self, trace: List[str]) -> List[str]:
        """Return a list of violations (empty = the trace is accepted)."""
        ids = self.by_label()
        ex, inc, pend = set(self.executed0), set(self.included0), set(self.pending0)
        problems = []
        for i, label in enumerate(trace):
            e = ids.get(label)
            if e is None:
                problems.append(f"#{i + 1} '{label}': not in the graph")
                continue
            ok, why = self.enabled(e, ex, inc, pend)
            if not ok:
                problems.append(f"#{i + 1} '{label}': not enabled ({why})")
            ex.add(e)
            pend.discard(e)
            pend |= self.rel["response"][e]
            inc -= self.rel["exclude"][e]
            inc |= self.rel["include"][e]
        for p in sorted(pend & inc):
            problems.append(f"end: '{self.labels[p]}' still pending")
        return problems


def check_log(graph: Graph, traces: List[dict]) -> dict:
    bad, kinds = {}, Counter()
    variants = Counter(tuple(e["name"] for e in t["events"]) for t in traces)
    for t in traces:
        p = graph.replay([e["name"] for e in t["events"]])
        if p:
            bad[t["id"]] = p
            for x in p:
                kinds[x.split(": ", 1)[-1] if x.startswith("end") else x.split("': ", 1)[-1]] += 1
    return {"traces": len(traces), "variants": len(variants), "violating": len(bad),
            "fitness": 1 - len(bad) / max(1, len(traces)), "by_kind": kinds, "details": bad}

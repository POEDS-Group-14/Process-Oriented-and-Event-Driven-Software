"""Generate the TO-BE travel guarantee DRD for process 1 (Movia bus and local train).

  python3 tools/build_to_be_dmn.py      -> models/to-be/travel-guarantee-to-be.dmn

The tables decide every claim in Camunda (business rule tasks in models/to-be/*.bpmn). The service
only gathers the raw inputs, so every limit (20 min, 24 h, 14 days, 300 kr, the weather limits) is in
a rule here and nowhere in Python.

Changes from the AS-IS DRD (models/travel-guarantee/travel-guarantee.dmn), which stays deployed for the
AS-IS results page, so every key here starts with ToBe_:
  * the limits are rules on raw numbers instead of booleans worked out by hand
  * new "Cause of disruption" table (DMI warning, OpenWeatherMap readings, operator cause codes)
  * new "Refund amount" table (the 300 kr cap was a task in the AS-IS)
  * "Excluded by cause" is decided once per delay (Assess disruption) and reaches "Travel guarantee
    applies" as input data, instead of being decided again for every claim

Opens in Camunda Modeler; if you edit the model there, stop regenerating it (or port the change
here). Standard library only.
"""
from __future__ import annotations

import os
from xml.sax.saxutils import escape

OUT = os.path.join(os.path.dirname(__file__), "..", "models", "to-be", "travel-guarantee-to-be.dmn")

# (id, name, variable, typeRef)
INPUTS = [
    ("exclusion", "Exclusion (from Assess disruption)", "string"),
    ("operatorCause", "Operator's cause code", "string"),
    ("dmiLevel", "DMI warning level", "number"),
    ("windSpeed", "Wind speed (m/s)", "number"),
    ("windGust", "Wind gust (m/s)", "number"),
    ("rain1h", "Rain, last hour (mm)", "number"),
    ("snow1h", "Snow, last hour (mm)", "number"),
    ("service", "Service", "string"),
    ("hoursAnnouncedBefore", "Hours the change was announced before", "number"),
    ("incident", "Incident", "string"),
    ("delayMinutes", "Delay (min)", "number"),
    ("visibleToDriver", "Visible to driver", "boolean"),
    ("busFull", "Bus full", "boolean"),
    ("stopRequested", "Stop requested in time", "boolean"),
    ("minutesToNextDeparture", "Minutes to the next departure", "number"),
    ("receiptAttached", "Taxi receipt attached", "boolean"),
    ("daysToClaim", "Days from incident to claim received", "number"),
    ("receiptUsedBefore", "Receipt used on an earlier claim", "boolean"),
    ("fareDkk", "Taxi fare (kr)", "number"),
]
TYPES = {v: t for v, _, t in INPUTS}
LABELS = {v: n for v, n, _ in INPUTS}

EXCLUSIONS = '"none","Flextrafik","Strike, weather or force majeure","Planned change"'
ASSESSMENTS = ('"Qualifies","No verified delay","Delay 20 min or less","Not visible to driver","Bus full",'
               '"Next bus within 20 min","Stop not requested in time","Next departure within 20 min",'
               '"Missed connection"')

# A decision: id, name, hit policy, knowledge source, required decisions,
# table inputs as (label, expression, typeRef, allowed values),
# outputs as (name, label, typeRef, allowed values), rules as (input entries, output entries, annotation).
DECISIONS = [
    {
        "id": "ToBe_TravelGuarantee", "name": "Travel guarantee applies", "hit": "UNIQUE", "ks": "KS_Terms",
        "requires": ["ToBe_Incident"],
        "in": [("Exclusion", "exclusion", "string", EXCLUSIONS),
               ("Incident assessment", "incidentAssessment", "string", ASSESSMENTS)],
        "out": [("covered", "Covered", "boolean", None), ("reason", "Reason", "string", None)],
        "rules": [
            (['not("none")', "-"], ["false", "exclusion"], "An exclusion always wins"),
            (['"none"', '"Qualifies"'], ["true", '"Covered"'],
             "Only the part of the trip the bus or local train should have covered"),
            (['"none"', 'not("Qualifies")'], ["false", "incidentAssessment"], ""),
        ],
    },
    {
        "id": "ToBe_Exclusion", "name": "Excluded by cause", "hit": "UNIQUE", "ks": "KS_Terms",
        "requires": ["ToBe_Cause"],
        "in": [("Service", "service", "string", '"bus","local train","Flextrafik"'),
               ("Cause of disruption", "cause", "string", '"none","strike","weather","force majeure"'),
               ("Hours the change was announced before", "hoursAnnouncedBefore", "number", None)],
        "out": [("exclusion", "Exclusion", "string", EXCLUSIONS)],
        "rules": [
            (['"Flextrafik"', "-", "-"], ['"Flextrafik"'], "Flextrafik is not covered"),
            (['not("Flextrafik")', 'not("none")', "-"], ['"Strike, weather or force majeure"'],
             "Bus strikes, weather that makes the timetable impossible, force majeure"),
            (['not("Flextrafik")', '"none"', ">= 24"], ['"Planned change"'],
             "Announced on dinoffentligetransport.dk 24 h before departure"),
            (['not("Flextrafik")', '"none"', "< 24"], ['"none"'], "Announced too late to count"),
            (['not("Flextrafik")', '"none"', "null"], ['"none"'], "No notice of a planned change"),
        ],
    },
    {
        "id": "ToBe_Cause", "name": "Cause of disruption", "hit": "FIRST", "ks": "KS_Weather",
        "requires": [],
        "in": [("Operator's cause code", "operatorCause", "string",
                '"none","strike","fire","person_collision","natural_event"'),
               ("DMI warning level", "dmiLevel", "number", None),
               ("Wind speed (m/s)", "windSpeed", "number", None),
               ("Wind gust (m/s)", "windGust", "number", None),
               ("Rain, last hour (mm)", "rain1h", "number", None),
               ("Snow, last hour (mm)", "snow1h", "number", None)],
        "out": [("cause", "Cause of disruption", "string", '"none","strike","weather","force majeure"')],
        "rules": [
            (['"strike"', "-", "-", "-", "-", "-"], ['"strike"'], "Strike reported by the operator"),
            (['"fire","person_collision","natural_event"', "-", "-", "-", "-", "-"], ['"force majeure"'],
             "Fire, person collision, unusual natural event"),
            (["-", ">= 2", "-", "-", "-", "-"], ['"weather"'], 'DMI level 2 "Farligt vejr" or 3'),
            (["-", "< 2", "-", "-", "-", "-"], ['"none"'],
             'DMI level 1 "Voldsomt vejr": the official warning wins over the readings below'),
            (["-", "-", ">= 24.5", "-", "-", "-"], ['"weather"'], "No DMI warning: Beaufort 10, storm"),
            (["-", "-", "-", ">= 32.7", "-", "-"], ['"weather"'], "No DMI warning: hurricane-force gusts"),
            (["-", "-", "-", "-", ">= 30", "-"], ['"weather"'],
             "No DMI warning: cloudburst (15 mm in 30 min) kept up for an hour"),
            (["-", "-", "-", "-", "-", ">= 5"], ['"weather"'], "No DMI warning: heavy snowfall"),
            (["-", "-", "-", "-", "-", "-"], ['"none"'], "Nothing outside the operator's control"),
        ],
    },
    {
        "id": "ToBe_Incident", "name": "Incident qualifies", "hit": "UNIQUE", "ks": "KS_Terms",
        "requires": [],
        "in": [("Incident", "incident", "string",
                '"delay","bus passed stop","train did not stop","left over 2 min early","missed connection"'),
               ("Delay (min)", "delayMinutes", "number", None),
               ("Visible to driver", "visibleToDriver", "boolean", None),
               ("Bus full", "busFull", "boolean", None),
               ("Stop requested in time", "stopRequested", "boolean", None),
               ("Minutes to the next departure", "minutesToNextDeparture", "number", None)],
        "out": [("incidentAssessment", "Incident assessment", "string", ASSESSMENTS)],
        "rules": [
            (['"delay"', "> 20", "-", "-", "-", "-"], ['"Qualifies"'], "Actual delay, not a projected one"),
            (['"delay"', "<= 20", "-", "-", "-", "-"], ['"Delay 20 min or less"'], ""),
            (['"delay"', "null", "-", "-", "-", "-"], ['"No verified delay"'],
             "No delay in the real-time data matches the trip"),
            (['"bus passed stop"', "-", "false", "-", "-", "-"], ['"Not visible to driver"'], ""),
            (['"bus passed stop"', "-", "true", "true", "-", "-"], ['"Bus full"'], ""),
            (['"bus passed stop"', "-", "true", "false", "-", "<= 20"], ['"Next bus within 20 min"'], ""),
            (['"bus passed stop"', "-", "true", "false", "-", "> 20"], ['"Qualifies"'], ""),
            (['"train did not stop"', "-", "-", "-", "false", "-"], ['"Stop not requested in time"'], ""),
            (['"train did not stop"', "-", "-", "-", "true", "-"], ['"Qualifies"'], ""),
            (['"left over 2 min early"', "-", "-", "-", "-", "<= 20"], ['"Next departure within 20 min"'], ""),
            (['"left over 2 min early"', "-", "-", "-", "-", "> 20"], ['"Qualifies"'], ""),
            (['"missed connection"', "-", "-", "-", "-", "-"], ['"Missed connection"'],
             "Lost connections after a delay"),
        ],
    },
    {
        "id": "ToBe_ClaimValid", "name": "Claim valid", "hit": "UNIQUE", "ks": "KS_Claim",
        "requires": [],
        "in": [("Taxi receipt attached", "receiptAttached", "boolean", None),
               ("Days from incident to claim received", "daysToClaim", "number", None),
               ("Receipt used on an earlier claim", "receiptUsedBefore", "boolean", None)],
        "out": [("claimValid", "Claim valid", "boolean", None),
                ("claimReason", "Claim reason", "string",
                 '"Valid","No receipt","Claim too late","Receipt already claimed"')],
        "rules": [
            (["false", "-", "-"], ["false", '"No receipt"'], "Receipt must be attached [2][3]"),
            (["true", "> 14", "-"], ["false", '"Claim too late"'], "Claim must reach Movia within 14 days [3]"),
            (["true", "<= 14", "true"], ["false", '"Receipt already claimed"'],
             "TO-BE: one refund per receipt, checked at intake instead of at payout"),
            (["true", "<= 14", "false"], ["true", '"Valid"'], ""),
        ],
    },
    {
        "id": "ToBe_Refund", "name": "Refund amount", "hit": "UNIQUE", "ks": "KS_Claim",
        "requires": [],
        "in": [("Taxi fare (kr)", "fareDkk", "number", None)],
        "out": [("refundDkk", "Refund (kr)", "number", None)],
        "rules": [
            (["<= 300"], ["fareDkk"], "The fare in full"),
            (["> 300"], ["300"], "At most 300 kr per taxi, however many share it [2][3]"),
        ],
    },
]
# Raw inputs of each decision (the DRD's information requirements); the table columns use them
# directly, except the columns filled by a required decision.
DECISION_INPUTS = {
    "ToBe_TravelGuarantee": ["exclusion"],
    "ToBe_Exclusion": ["service", "hoursAnnouncedBefore"],
    "ToBe_Cause": ["operatorCause", "dmiLevel", "windSpeed", "windGust", "rain1h", "snow1h"],
    "ToBe_Incident": ["incident", "delayMinutes", "visibleToDriver", "busFull", "stopRequested",
                          "minutesToNextDeparture"],
    "ToBe_ClaimValid": ["receiptAttached", "daysToClaim", "receiptUsedBefore"],
    "ToBe_Refund": ["fareDkk"],
}
SOURCES = [
    ("KS_Terms", "Travel guarantee terms (Movia) [1]"),
    ("KS_Claim", "Movia claim rules [2][3]"),
    ("KS_Weather", "DMI and OpenWeatherMap limits (group proposal)"),
]
NOTES = [
    ("Annotation_Author", "Travel guarantee, Movia bus and local train. Author: Liam Palmqvist. Hit policy "
     "Unique, except First in Cause of disruption. Sources: references in '1. Rejsegaranti claim.md'.",
     None),
    ("Annotation_Scope", "Covers only the part of the trip the bus or local train should have covered. No other "
     "costs (e.g. flight or show tickets, lost earnings).", "ToBe_TravelGuarantee"),
    ("Annotation_Delay", "Actual delay: a +20 min estimate on Rejseplan or a stop screen is not enough, the bus "
     "or train may catch up.", "Input_delayMinutes"),
    ("Annotation_Deadline", "14 days: quoted from Movia in ABTM decision 2022-0042 [3]. Not confirmed on a "
     "current Movia page.", "Input_daysToClaim"),
    ("Annotation_PerDelay", "Once per delay, in Assess disruption. Its result reaches 'Travel guarantee applies' "
     "as the input Exclusion, so every claim on one delay gets the same exclusion.", "ToBe_Exclusion"),
    ("Annotation_Weather", "Weather limits are the group's proposal: Beaufort 10 wind, Beaufort 12 gusts, DMI's "
     "cloudburst rate, heavy snow. To be justified in the report.", "KS_Weather"),
]

# DRD layout: two clusters, each decision with its inputs underneath it. Left, the decisions made once
# per delay (Assess disruption); right, the ones made once per claim (Decide claim and pay out).
W, H, IW, IH = 180, 80, 130, 45
STEP = 145  # inputs sit in one row under their decision, so no requirement line crosses another input
POS = {
    "ToBe_Cause": (377, 420), "ToBe_Exclusion": (560, 220), "KS_Weather": (130, 428),
    "ToBe_TravelGuarantee": (1437, 40), "ToBe_Incident": (1437, 220), "Input_exclusion": (1180, 237),
    "KS_Terms": (900, 50),
    "ToBe_ClaimValid": (2130, 220), "ToBe_Refund": (2465, 220), "KS_Claim": (2300, 40),
}


def place_inputs(variables, x0, y0):
    for n, v in enumerate(variables):
        POS[f"Input_{v}"] = (x0 + n * STEP, y0)


place_inputs(["operatorCause", "dmiLevel", "windSpeed", "windGust", "rain1h", "snow1h"], 40, 640)
place_inputs(["service", "hoursAnnouncedBefore"], 620, 420)
place_inputs(["incident", "delayMinutes", "visibleToDriver", "busFull", "stopRequested",
              "minutesToNextDeparture"], 1100, 420)
place_inputs(["receiptAttached", "daysToClaim", "receiptUsedBefore"], 2010, 420)
place_inputs(["fareDkk"], 2490, 420)
NOTE_POS = {"Annotation_Author": (40, 20, 420, 70), "Annotation_PerDelay": (40, 120, 380, 70),
            "Annotation_Scope": (1660, 40, 300, 60), "Annotation_Delay": (1100, 500, 300, 50),
            "Annotation_Deadline": (2010, 500, 300, 50), "Annotation_Weather": (40, 505, 180, 80)}


def box(el):
    if el in NOTE_POS:
        return NOTE_POS[el]
    (x, y), (w, h) = POS[el], size(el)
    return x, y, w, h


def nearest_sides(a, b):
    """The closest pair of side midpoints of two boxes: a short line that does not run across the drawing."""
    def sides(el):
        x, y, w, h = box(el)
        return [(x + w // 2, y), (x + w // 2, y + h), (x, y + h // 2), (x + w, y + h // 2)]
    return min(((p, q) for p in sides(a) for q in sides(b)),
               key=lambda pq: (pq[0][0] - pq[1][0]) ** 2 + (pq[0][1] - pq[1][1]) ** 2)


def size(el):
    return (IW, IH) if el.startswith("Input_") else (170, 63) if el.startswith("KS_") else (W, H)


def top(el):
    (x, y), (w, _) = POS[el], size(el)
    return x + w // 2, y


def bottom(el):
    (x, y), (w, h) = POS[el], size(el)
    return x + w // 2, y + h


def t(s: str) -> str:
    return f"<text>{escape(s)}</text>"


def decision_xml(d: dict) -> str:
    i = d["id"]
    out = [f'  <decision id="{i}" name="{escape(d["name"])}" camunda:historyTimeToLive="180">']
    for r in d["requires"]:
        out.append(f'    <informationRequirement id="IR_{i}_{r}"><requiredDecision href="#{r}" /></informationRequirement>')
    for v in DECISION_INPUTS.get(i, []):
        out.append(f'    <informationRequirement id="IR_{i}_Input_{v}"><requiredInput href="#Input_{v}" />'
                   f'</informationRequirement>')
    out.append(f'    <authorityRequirement id="AR_{i}"><requiredAuthority href="#{d["ks"]}" /></authorityRequirement>')
    out.append(f'    <decisionTable id="DT_{i}" hitPolicy="{d["hit"]}">')
    for n, (label, expr, typ, values) in enumerate(d["in"]):
        iv = f'<inputValues id="IV_{i}_{n}">{t(values)}</inputValues>' if values else ""
        out.append(f'      <input id="In_{i}_{n}" label="{escape(label)}"><inputExpression id="IE_{i}_{n}" '
                   f'typeRef="{typ}">{t(expr)}</inputExpression>{iv}</input>')
    for n, (name, label, typ, values) in enumerate(d["out"]):
        ov = f'<outputValues id="OV_{i}_{n}">{t(values)}</outputValues>' if values else ""
        out.append(f'      <output id="Out_{i}_{n}" label="{escape(label)}" name="{name}" typeRef="{typ}">{ov}</output>')
    for r, (ins, outs, note) in enumerate(d["rules"], 1):
        assert len(ins) == len(d["in"]) and len(outs) == len(d["out"]), (i, r)
        out.append(f'      <rule id="R_{i}_{r}">')
        if note:
            out.append(f"        <description>{escape(note)}</description>")
        out += [f'        <inputEntry id="IN_{i}_{r}_{n}">{t(e)}</inputEntry>' for n, e in enumerate(ins)]
        out += [f'        <outputEntry id="OUT_{i}_{r}_{n}">{t(e)}</outputEntry>' for n, e in enumerate(outs)]
        out.append("      </rule>")
    out += ["    </decisionTable>", "  </decision>"]
    return "\n".join(out)


def edge(eid: str, src: str, dst: str) -> str:
    if src.startswith("KS_"):  # authority: between the nearest sides
        (x1, y1), (x2, y2) = nearest_sides(src, dst)
    else:  # information requirement: from the input's top up to the decision's bottom
        (x1, y1), (x2, y2) = top(src), bottom(dst)
    return (f'    <dmndi:DMNEdge id="{eid}_di" dmnElementRef="{eid}"><di:waypoint x="{x1}" y="{y1}" />'
            f'<di:waypoint x="{x2}" y="{y2}" /></dmndi:DMNEdge>')


def build() -> str:
    head = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<definitions xmlns="https://www.omg.org/spec/DMN/20191111/MODEL/" '
            'xmlns:dmndi="https://www.omg.org/spec/DMN/20191111/DMNDI/" '
            'xmlns:dc="http://www.omg.org/spec/DMN/20180521/DC/" xmlns:di="http://www.omg.org/spec/DMN/20180521/DI/" '
            'xmlns:modeler="http://camunda.org/schema/modeler/1.0" xmlns:camunda="http://camunda.org/schema/1.0/dmn" '
            'id="Definitions_travel_guarantee_to_be" '
            'name="Travel guarantee (TO-BE)" namespace="http://poeds.dtu.dk/travel-guarantee" exporter="build_dmn.py" '
            'exporterVersion="2.0" modeler:executionPlatform="Camunda Platform" '
            'modeler:executionPlatformVersion="7.24.0">')
    body = [decision_xml(d) for d in DECISIONS]
    body += [f'  <inputData id="Input_{v}" name="{escape(n)}"><variable id="Var_{v}" name="{v}" typeRef="{ty}" />'
             f'</inputData>' for v, n, ty in INPUTS]
    body += [f'  <knowledgeSource id="{k}" name="{escape(n)}" />' for k, n in SOURCES]
    for nid, text, target in NOTES:
        body.append(f'  <textAnnotation id="{nid}">{t(text)}</textAnnotation>')
        if target:
            body.append(f'  <association id="Assoc_{nid}"><sourceRef href="#{nid}" /><targetRef href="#{target}" />'
                        f'</association>')
    di = ['  <dmndi:DMNDI><dmndi:DMNDiagram id="DMNDiagram_1">']
    for el in [d["id"] for d in DECISIONS] + [k for k, _ in SOURCES] + [f"Input_{v}" for v, _, _ in INPUTS]:
        (x, y), (w, h) = POS[el], size(el)
        di.append(f'    <dmndi:DMNShape id="{el}_di" dmnElementRef="{el}"><dc:Bounds x="{x}" y="{y}" width="{w}" '
                  f'height="{h}" /></dmndi:DMNShape>')
    for d in DECISIONS:
        i = d["id"]
        di += [edge(f"IR_{i}_{r}", r, i) for r in d["requires"]]
        di += [edge(f"IR_{i}_Input_{v}", f"Input_{v}", i) for v in DECISION_INPUTS.get(i, [])]
        di.append(edge(f"AR_{i}", d["ks"], i))
    for nid, _, target in NOTES:
        x, y, w, h = NOTE_POS[nid]
        di.append(f'    <dmndi:DMNShape id="{nid}_di" dmnElementRef="{nid}"><dc:Bounds x="{x}" y="{y}" width="{w}" '
                  f'height="{h}" /></dmndi:DMNShape>')
        if target:
            (x1, y1), (x2, y2) = nearest_sides(nid, target)
            di.append(f'    <dmndi:DMNEdge id="Assoc_{nid}_di" dmnElementRef="Assoc_{nid}"><di:waypoint x="{x1}" '
                      f'y="{y1}" /><di:waypoint x="{x2}" y="{y2}" /></dmndi:DMNEdge>')
    di.append("  </dmndi:DMNDiagram></dmndi:DMNDI>")
    return "\n".join([head, *body, *di, "</definitions>", ""])


if __name__ == "__main__":
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(build())
    print("wrote", os.path.relpath(OUT))
